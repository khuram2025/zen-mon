"""Application Monitoring (APM) control plane.

Ingest-key lifecycle (zpi_ for SDK, zpr_ for RUM), enrollment tokens, and the
shared ingest-key authentication used by the OTLP receiver (apm_ingest.py).

Mounted under /api/v1 (prefix "/apm"). High-volume telemetry never touches these
endpoints — they manage Postgres config/registry rows only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/apm", tags=["APM control plane"])

SDK_KEY_PREFIX = "zpi_"   # zenplus APM ingest key (SDK/Collector)
RUM_KEY_PREFIX = "zpr_"   # zenplus RUM ingest key (browser)
KindT = Literal["sdk", "rum"]


# ── crypto helpers (forked from agents.py) ───────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_ingest_key(kind: KindT) -> tuple[str, str, str]:
    """Return (plaintext, sha256_hash, display_prefix)."""
    prefix = RUM_KEY_PREFIX if kind == "rum" else SDK_KEY_PREFIX
    raw = prefix + secrets.token_urlsafe(32)
    return raw, _sha256(raw), raw[:12]


def _strip_bearer(value: Optional[str]) -> str:
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


# ── shared ingest-key authentication (read-through cache, 30s TTL) ───────────

_KEY_CACHE: dict[str, tuple[float, dict]] = {}
_KEY_CACHE_TTL = 30.0

# `last_used_at` is what tells an operator a key is live vs. abandoned, but the
# ingest path runs per batch — writing it on every call would put a Postgres
# UPDATE in front of the hot loop. Stamp it at most once per key per interval.
_LAST_USED_TTL = 60.0
_LAST_USED_SEEN: dict[str, float] = {}


def invalidate_ingest_key_cache() -> None:
    _KEY_CACHE.clear()
    _LAST_USED_SEEN.clear()


async def _touch_last_used(db: AsyncSession, key_id, key_hash: str) -> None:
    """Best-effort `last_used_at` stamp, throttled to one write per minute."""
    now = time.monotonic()
    if _LAST_USED_SEEN.get(key_hash, 0.0) > now:
        return
    _LAST_USED_SEEN[key_hash] = now + _LAST_USED_TTL
    try:
        await db.execute(
            text("UPDATE apm_ingest_keys SET last_used_at = NOW() WHERE id = :id"),
            {"id": key_id},
        )
        await db.commit()
    except Exception:  # never fail an ingest because bookkeeping failed
        await db.rollback()


async def authenticate_ingest_key(
    bearer: str, db: AsyncSession, *, kind: Optional[KindT] = None
) -> dict:
    """Validate an ingest key (constant-time hash compare) and return its row.

    Raises HTTPException(401) on any failure. Caches valid lookups for 30s so the
    hot ingest path does not hit Postgres on every batch.
    """
    token = _strip_bearer(bearer)
    if not token or not (token.startswith(SDK_KEY_PREFIX) or token.startswith(RUM_KEY_PREFIX)):
        raise HTTPException(401, "Missing or malformed APM ingest key")

    key_hash = _sha256(token)
    now = time.monotonic()
    cached = _KEY_CACHE.get(key_hash)
    if cached and cached[0] > now:
        row = cached[1]
    else:
        rec = (await db.execute(
            text(
                """
                SELECT k.id, k.kind, k.key_hash, k.enabled, k.revoked_at,
                       k.env_id, e.name AS env_name, k.origin_allowlist,
                       k.application_id
                FROM apm_ingest_keys k
                LEFT JOIN apm_environments e ON e.id = k.env_id
                WHERE k.key_hash = :h
                """
            ),
            {"h": key_hash},
        )).mappings().first()
        if not rec:
            raise HTTPException(401, "Invalid APM ingest key")
        # constant-time confirm (defence-in-depth; the SELECT already matched)
        if not hmac.compare_digest(key_hash, str(rec["key_hash"])):
            raise HTTPException(401, "Invalid APM ingest key")
        if rec["revoked_at"] is not None or not rec["enabled"]:
            raise HTTPException(401, "APM ingest key revoked or disabled")
        row = dict(rec)
        _KEY_CACHE[key_hash] = (now + _KEY_CACHE_TTL, row)

    if kind is not None and row["kind"] != kind:
        raise HTTPException(401, f"Ingest key is not of kind '{kind}'")
    await _touch_last_used(db, row["id"], key_hash)
    return row


# ── schemas ──────────────────────────────────────────────────────────────────

class IngestKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    kind: KindT = "sdk"
    env: Optional[str] = Field(default=None, max_length=64)
    origin_allowlist: list[str] = Field(default_factory=list)
    # Optional for backwards compatibility.  New browser keys should normally
    # bind to one application so a leaked public key cannot poison a different
    # application's data while presenting a forged Origin header.
    application_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )


class IngestKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: KindT
    key_prefix: str
    env: Optional[str] = None
    origin_allowlist: list[str] = []
    application_id: Optional[str] = None
    enabled: bool
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class IngestKeyCreated(IngestKeyResponse):
    key: str  # plaintext — shown ONCE


class EnrollmentTokenCreate(BaseModel):
    kind: KindT = "sdk"
    env: Optional[str] = None
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_in_hours: Optional[int] = Field(default=720, ge=1, le=8760)


class EnrollmentTokenResponse(BaseModel):
    id: uuid.UUID
    token_prefix: str
    kind: KindT
    env: Optional[str] = None
    max_uses: int
    uses: int
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class EnvironmentResponse(BaseModel):
    id: uuid.UUID
    name: str
    retention_days_raw: int
    sampling_target_tps: int


# ── helpers ──────────────────────────────────────────────────────────────────

async def _resolve_env_id(db: AsyncSession, env: Optional[str]) -> Optional[uuid.UUID]:
    if not env:
        return None
    row = (await db.execute(
        text("SELECT id FROM apm_environments WHERE name = :n"), {"n": env}
    )).first()
    if not row:
        raise HTTPException(400, f"Unknown APM environment '{env}'")
    return row[0]


def _key_row_to_response(r: dict) -> IngestKeyResponse:
    return IngestKeyResponse(
        id=r["id"], name=r["name"], kind=r["kind"], key_prefix=r["key_prefix"],
        env=r.get("env_name"), origin_allowlist=list(r.get("origin_allowlist") or []),
        application_id=r.get("application_id"),
        enabled=r["enabled"], last_used_at=r.get("last_used_at"),
        revoked_at=r.get("revoked_at"), created_at=r["created_at"],
    )


# ── environments ─────────────────────────────────────────────────────────────

@router.get("/environments", response_model=list[EnvironmentResponse])
async def list_environments(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    rows = (await db.execute(text(
        "SELECT id, name, retention_days_raw, sampling_target_tps FROM apm_environments ORDER BY name"
    ))).mappings().all()
    return [EnvironmentResponse(**dict(r)) for r in rows]


# ── ingest keys ──────────────────────────────────────────────────────────────

@router.get("/ingest-keys", response_model=list[IngestKeyResponse])
async def list_ingest_keys(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    rows = (await db.execute(text(
        """
        SELECT k.*, e.name AS env_name
        FROM apm_ingest_keys k LEFT JOIN apm_environments e ON e.id = k.env_id
        ORDER BY k.created_at DESC
        """
    ))).mappings().all()
    return [_key_row_to_response(dict(r)) for r in rows]


@router.post("/ingest-keys", response_model=IngestKeyCreated, status_code=201)
async def create_ingest_key(
    body: IngestKeyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if body.kind == "rum":
        if not body.application_id:
            raise HTTPException(400, "New browser RUM keys require an application_id binding")
        if not body.origin_allowlist:
            raise HTTPException(400, "Browser RUM keys require at least one allowed origin")
        from urllib.parse import urlsplit
        for origin in body.origin_allowlist:
            try:
                parsed = urlsplit(origin)
                invalid = (parsed.scheme not in {"http", "https"} or not parsed.hostname
                           or parsed.username is not None or parsed.password is not None
                           or parsed.path not in {"", "/"} or parsed.query
                           or parsed.fragment or "*" in origin)
            except (TypeError, ValueError):
                invalid = True
            if invalid:
                raise HTTPException(400, f"Invalid RUM origin '{origin}'; use an exact http(s) origin without a path or wildcard")
    elif body.origin_allowlist or body.application_id:
        raise HTTPException(400, "Origin allowlists and application binding are only valid for browser RUM keys")
    plaintext, key_hash, key_prefix = _new_ingest_key(body.kind)
    env_id = await _resolve_env_id(db, body.env)
    import json
    row = (await db.execute(
        text(
            """
            INSERT INTO apm_ingest_keys (name, kind, key_hash, key_prefix, env_id,
                                         origin_allowlist, application_id, created_by)
            VALUES (:name, :kind, :hash, :prefix, :env_id,
                    CAST(:origins AS jsonb), :application_id, :uid)
            RETURNING id, name, kind, key_prefix, enabled, last_used_at,
                      revoked_at, created_at, origin_allowlist
            """
        ),
        {
            "name": body.name, "kind": body.kind, "hash": key_hash,
            "prefix": key_prefix, "env_id": env_id,
            "origins": json.dumps(body.origin_allowlist),
            "application_id": body.application_id,
            "uid": getattr(user, "id", None),
        },
    )).mappings().first()
    await db.commit()
    invalidate_ingest_key_cache()
    resp = _key_row_to_response({**dict(row), "env_name": body.env,
                                 "application_id": body.application_id})
    await write_audit_log(
        db, actor=user, action="apm.ingest_key.create",
        resource_type="apm_ingest_key", resource_id=str(row["id"]),
        metadata={"name": body.name, "kind": body.kind, "env": body.env,
                  "key_prefix": key_prefix, "application_id": body.application_id},
    )
    await db.commit()
    return IngestKeyCreated(**resp.model_dump(), key=plaintext)


@router.delete("/ingest-keys/{key_id}", status_code=204)
async def revoke_ingest_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(
        text("UPDATE apm_ingest_keys SET revoked_at = NOW(), enabled = FALSE "
             "WHERE id = :id AND revoked_at IS NULL"),
        {"id": key_id},
    )
    await db.commit()
    invalidate_ingest_key_cache()
    if res.rowcount == 0:
        raise HTTPException(404, "Ingest key not found or already revoked")
    await write_audit_log(
        db, actor=user, action="apm.ingest_key.revoke",
        resource_type="apm_ingest_key", resource_id=str(key_id),
    )
    await db.commit()
    return None


# ── enrollment tokens (mirror agent_enrollment_tokens) ───────────────────────

@router.get("/enrollment-tokens", response_model=list[EnrollmentTokenResponse])
async def list_enrollment_tokens(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    rows = (await db.execute(text(
        """
        SELECT t.*, e.name AS env_name
        FROM apm_enrollment_tokens t LEFT JOIN apm_environments e ON e.id = t.env_id
        ORDER BY t.created_at DESC
        """
    ))).mappings().all()
    return [
        EnrollmentTokenResponse(
            id=r["id"], token_prefix=r["token_prefix"], kind=r["kind"],
            env=r.get("env_name"), max_uses=r["max_uses"], uses=r["uses"],
            expires_at=r["expires_at"], revoked_at=r["revoked_at"], created_at=r["created_at"],
        )
        for r in rows
    ]


class EnrollmentTokenCreated(EnrollmentTokenResponse):
    token: str  # plaintext — shown ONCE


@router.post("/enrollment-tokens", response_model=EnrollmentTokenCreated, status_code=201)
async def create_enrollment_token(
    body: EnrollmentTokenCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    prefix = RUM_KEY_PREFIX if body.kind == "rum" else SDK_KEY_PREFIX
    raw = prefix + "enroll_" + secrets.token_urlsafe(24)
    token_hash, token_prefix = _sha256(raw), raw[:12]
    env_id = await _resolve_env_id(db, body.env)
    expires = (
        text("NOW() + (:h || ' hours')::interval") if body.expires_in_hours else None
    )
    row = (await db.execute(
        text(
            f"""
            INSERT INTO apm_enrollment_tokens (token_hash, token_prefix, kind, env_id,
                                               max_uses, expires_at)
            VALUES (:hash, :prefix, :kind, :env_id, :max_uses,
                    {"NOW() + (:eh || ' hours')::interval" if body.expires_in_hours else "NULL"})
            RETURNING id, token_prefix, kind, max_uses, uses, expires_at, revoked_at, created_at
            """
        ),
        {
            "hash": token_hash, "prefix": token_prefix, "kind": body.kind,
            "env_id": env_id, "max_uses": body.max_uses,
            **({"eh": str(body.expires_in_hours)} if body.expires_in_hours else {}),
        },
    )).mappings().first()
    await db.commit()
    await write_audit_log(
        db, actor=user, action="apm.enrollment_token.create",
        resource_type="apm_enrollment_token", resource_id=str(row["id"]),
        metadata={"kind": body.kind, "env": body.env,
                  "max_uses": body.max_uses, "expires_in_hours": body.expires_in_hours},
    )
    await db.commit()
    return EnrollmentTokenCreated(
        id=row["id"], token_prefix=row["token_prefix"], kind=row["kind"], env=body.env,
        max_uses=row["max_uses"], uses=row["uses"], expires_at=row["expires_at"],
        revoked_at=row["revoked_at"], created_at=row["created_at"], token=raw,
    )


@router.delete("/enrollment-tokens/{token_id}", status_code=204)
async def revoke_enrollment_token(
    token_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(
        text("UPDATE apm_enrollment_tokens SET revoked_at = NOW() "
             "WHERE id = :id AND revoked_at IS NULL"),
        {"id": token_id},
    )
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Enrollment token not found or already revoked")
    await write_audit_log(
        db, actor=user, action="apm.enrollment_token.revoke",
        resource_type="apm_enrollment_token", resource_id=str(token_id),
    )
    await db.commit()
    return None


# ── Data quality (ingest health, service freshness, agent forwarders) ────────

@router.get("/data-quality")
async def data_quality(
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Monitor the *quality* of ingested APM data: cluster-wide ingest
    counters (accepted / rejected / clock-skewed / dropped, persisted across
    restarts), per-service reporting freshness, and the health of agent-side
    APM forwarders. This is how operators catch unacceptable data — skewed
    clocks, silently dying producers, flush failures — before it corrupts
    dashboards."""
    import asyncio as _asyncio
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    hours = max(1, min(hours, 720))

    def _ingest() -> dict:
        from app.core.database import get_ch_client
        since = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        client = get_ch_client()
        totals = client.query(
            "SELECT sum(accepted), sum(rejected), sum(dropped), sum(skewed), sum(flushes) "
            "FROM zenplus.apm_ingest_stats WHERE timestamp >= %(s)s",
            parameters={"s": since},
        ).result_rows[0]
        series = client.query(
            "SELECT toStartOfInterval(timestamp, INTERVAL 300 SECOND) AS t, "
            "       sum(accepted), sum(rejected) + sum(skewed), sum(dropped) "
            "FROM zenplus.apm_ingest_stats WHERE timestamp >= %(s)s "
            "GROUP BY t ORDER BY t",
            parameters={"s": since},
        ).result_rows
        accepted = int(totals[0] or 0)
        rejected = int(totals[1] or 0)
        return {
            "accepted": accepted, "rejected": rejected,
            "dropped": int(totals[2] or 0), "skewed": int(totals[3] or 0),
            "flushes": int(totals[4] or 0),
            "reject_rate": round(rejected / (accepted + rejected), 5)
                           if (accepted + rejected) else 0.0,
            "series": [{"t": r[0].isoformat(), "accepted": int(r[1]),
                        "rejected": int(r[2]), "dropped": int(r[3])} for r in series],
        }

    ingest = await _asyncio.to_thread(_ingest)

    # Live queue depth for THIS worker (indicative; counters above are global).
    from app.api.v1 import apm_ingest as _ingest_mod
    ingest["queue_depth"] = _ingest_mod._queue.qsize() if _ingest_mod._queue else None

    services = [
        {
            "name": r["name"], "health": r["health"],
            "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            "silent_for_s": int(r["silent_s"]) if r["silent_s"] is not None else None,
            "reporting": (r["silent_s"] or 0) < 600 if r["silent_s"] is not None else False,
        }
        for r in (await db.execute(text("""
            SELECT name, health, last_seen_at,
                   EXTRACT(EPOCH FROM (NOW() - last_seen_at)) AS silent_s
            FROM apm_services ORDER BY last_seen_at DESC NULLS LAST
        """))).mappings().all()
    ]

    forwarders = [
        {
            "agent_id": str(r["id"]), "hostname": r["hostname"],
            "agent_status": r["status"], "clock_skew_s": r["clock_skew_s"],
            **{k: (r["apm_status"] or {}).get(k) for k in
               ("enabled", "failed", "spans_forwarded_1m", "export_errors_1m",
                "spool_depth_spans", "dropped_spans_total", "last_error")},
        }
        for r in (await db.execute(text("""
            SELECT id, hostname, status, clock_skew_s, apm_status
            FROM agents WHERE apm_status IS NOT NULL ORDER BY hostname
        """))).mappings().all()
    ]

    silent = sum(1 for s in services if not s["reporting"])
    issues = []
    if ingest["dropped"]:
        issues.append(f"{ingest['dropped']} span(s) dropped on ClickHouse flush failures")
    if ingest["skewed"]:
        issues.append(f"{ingest['skewed']} span(s) rejected for clock skew")
    if silent:
        issues.append(f"{silent} registered service(s) not reporting")
    failing_fwd = [f["hostname"] for f in forwarders
                   if f.get("failed") or (f.get("export_errors_1m") or 0) > 0]
    if failing_fwd:
        issues.append(f"agent forwarder issues on: {', '.join(failing_fwd[:5])}")

    return {
        "ingest": ingest,
        "services": services,
        "agent_forwarders": forwarders,
        "health": "issues" if issues else "ok",
        "issues": issues,
    }
