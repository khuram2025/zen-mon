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
from app.core.security import get_current_user
from app.models.user import User

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


def invalidate_ingest_key_cache() -> None:
    _KEY_CACHE.clear()


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
                       k.env_id, e.name AS env_name, k.origin_allowlist
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
    return row


# ── schemas ──────────────────────────────────────────────────────────────────

class IngestKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    kind: KindT = "sdk"
    env: Optional[str] = Field(default=None, max_length=64)
    origin_allowlist: list[str] = Field(default_factory=list)


class IngestKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: KindT
    key_prefix: str
    env: Optional[str] = None
    origin_allowlist: list[str] = []
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
    user: User = Depends(get_current_user),
):
    plaintext, key_hash, key_prefix = _new_ingest_key(body.kind)
    env_id = await _resolve_env_id(db, body.env)
    import json
    row = (await db.execute(
        text(
            """
            INSERT INTO apm_ingest_keys (name, kind, key_hash, key_prefix, env_id,
                                         origin_allowlist, created_by)
            VALUES (:name, :kind, :hash, :prefix, :env_id,
                    CAST(:origins AS jsonb), :uid)
            RETURNING id, name, kind, key_prefix, enabled, last_used_at,
                      revoked_at, created_at, origin_allowlist
            """
        ),
        {
            "name": body.name, "kind": body.kind, "hash": key_hash,
            "prefix": key_prefix, "env_id": env_id,
            "origins": json.dumps(body.origin_allowlist),
            "uid": getattr(user, "id", None),
        },
    )).mappings().first()
    await db.commit()
    invalidate_ingest_key_cache()
    resp = _key_row_to_response({**dict(row), "env_name": body.env})
    return IngestKeyCreated(**resp.model_dump(), key=plaintext)


@router.delete("/ingest-keys/{key_id}", status_code=204)
async def revoke_ingest_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
    return EnrollmentTokenCreated(
        id=row["id"], token_prefix=row["token_prefix"], kind=row["kind"], env=body.env,
        max_uses=row["max_uses"], uses=row["uses"], expires_at=row["expires_at"],
        revoked_at=row["revoked_at"], created_at=row["created_at"], token=raw,
    )


@router.delete("/enrollment-tokens/{token_id}", status_code=204)
async def revoke_enrollment_token(
    token_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    res = await db.execute(
        text("UPDATE apm_enrollment_tokens SET revoked_at = NOW() "
             "WHERE id = :id AND revoked_at IS NULL"),
        {"id": token_id},
    )
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Enrollment token not found or already revoked")
    return None
