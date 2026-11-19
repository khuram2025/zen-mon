"""Event alert expiry: close event alerts that stopped happening.

Event alerts (SNMP traps, syslog matches, Device Tracker endpoint events, NCM
configuration changes) record that something happened; no evaluator ever sees
them recover, so they used to stay open until someone closed them by hand —
rogue-endpoint alerts open for 106 days, 180 acknowledged NCM alerts on one
appliance (alerting audit G-7).

This sweep resolves an OPEN event alert whose last occurrence
(``metadata.last_seen``, else ``triggered_at``) is older than the window:

* trap rows (``metadata.trap``),
* syslog rows (``metadata.syslog_source``),
* Device Tracker event rows — new, rogue, watched and moved endpoints
  (``metadata.udt_dedupe`` of those kinds; port capacity is a level with its
  own recovery and is never expired),
* NCM configuration-change rows (``metadata.ncm_event = 'config.changed'``).

Nothing else: baseline anomalies, metric and status incidents, NCM backup
failures (which recover on the next good backup) and notices are untouched.

An expired row gets ``metadata.resolved_by = 'expired'`` (closure reason
``expired``, "Expired — no new event within the window"), an ``expired``
lifecycle event, and no notification: it is not a recovery, and the
escalation sweeper sends no all-clear for it.

Settings: ``system_settings['alert_event_expiry']`` = ``{"enabled": true,
"hours": 168}`` (on by default). The sweep runs from the escalation sweeper
loop at most once a minute, in one API worker at a time, and closes at most
:data:`BATCH` rows per pass.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_workflow as wf

logger = logging.getLogger("zenplus.alert_expiry")

SETTINGS_KEY = "alert_event_expiry"
DEFAULT_SETTINGS: dict[str, Any] = {"enabled": True, "hours": 168}
HOURS_LIMITS = (1, 8760)
MIN_INTERVAL_S = 60
BATCH = 500
RESOLVED_BY = "expired"
UDT_EVENT_KINDS = ("new_endpoint", "rogue_detected", "watch_seen", "endpoint_moved")

_last_run = 0.0


# ── Settings ─────────────────────────────────────────────────────────────────

def clean_settings(raw: Any) -> dict[str, Any]:
    """Normalize stored or posted settings: unknown keys dropped, hours
    clamped to 1..8760, anything unreadable falls back to the default."""
    out = dict(DEFAULT_SETTINGS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    hours = raw.get("hours")
    if not isinstance(hours, bool) and isinstance(hours, (int, float)):
        lo, hi = HOURS_LIMITS
        out["hours"] = max(lo, min(hi, int(hours)))
    return out


async def load_settings(db: AsyncSession) -> dict[str, Any]:
    try:
        async with db.begin_nested():
            row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                    {"k": SETTINGS_KEY})).first()
    except Exception as exc:
        logger.warning("event expiry settings unreadable, using the default: %s", exc)
        return clean_settings(None)
    return clean_settings(row[0] if row else None)


async def save_settings(db: AsyncSession, body: Any, actor_id=None) -> dict[str, Any]:
    clean = clean_settings(body)
    await db.execute(text(
        "INSERT INTO system_settings (key, value, updated_at, updated_by)"
        " VALUES (:k, CAST(:v AS jsonb), now(), CAST(:by AS uuid))"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),"
        " updated_by = EXCLUDED.updated_by"
    ), {"k": SETTINGS_KEY, "v": json.dumps(clean), "by": str(actor_id) if actor_id else None})
    return clean


def window_label(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def policy_sentence(s: dict) -> str:
    if not s.get("enabled"):
        return ("Off: trap, syslog, Device Tracker and configuration-change alerts stay open "
                "until someone closes them.")
    return (f"Trap, syslog, Device Tracker and configuration-change alerts close by themselves "
            f"after {window_label(int(s['hours']))} without a new event. Closing sends no notification.")


# ── Selection ────────────────────────────────────────────────────────────────

def event_kind_sql(a: str = "a") -> str:
    """The event-alert kind of row ``a`` ('trap', 'syslog', 'udt', 'ncm_change'),
    NULL for everything that is not an expirable event alert."""
    kinds = ", ".join(f"'{k}'" for k in UDT_EVENT_KINDS)
    return (
        f"CASE WHEN COALESCE({a}.metadata->>'trap', 'false') = 'true' THEN 'trap'"
        f" WHEN {a}.metadata->>'syslog_source' IS NOT NULL THEN 'syslog'"
        f" WHEN split_part(COALESCE({a}.metadata->>'udt_dedupe', ''), chr(58), 1) IN ({kinds}) THEN 'udt'"
        f" WHEN COALESCE({a}.metadata->>'ncm', 'false') = 'true'"
        f" AND {a}.metadata->>'ncm_event' = 'config.changed' THEN 'ncm_change'"
        f" END"
    )


def last_seen_sql(a: str = "a") -> str:
    """The row's last occurrence: metadata.last_seen when it is a timestamp,
    else triggered_at."""
    return (f"COALESCE(CASE WHEN {a}.metadata->>'last_seen' ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'"
            f" THEN ({a}.metadata->>'last_seen')::timestamptz END, {a}.triggered_at)")


def expirable_sql(a: str = "a") -> str:
    """Open event alerts whose last occurrence is older than ``:hours``."""
    return (f"{a}.status IN ('active', 'acknowledged')"
            f" AND {event_kind_sql(a)} IS NOT NULL"
            f" AND NOT {wf.notice_sql(a)}"
            f" AND {last_seen_sql(a)} < now() - make_interval(hours => CAST(:hours AS int))")


# ── Sweep ────────────────────────────────────────────────────────────────────

async def sweep(db: AsyncSession, *, settings: dict | None = None) -> dict[str, int]:
    """Resolve expired event alerts (at most :data:`BATCH`); commits."""
    s = settings if settings is not None else await load_settings(db)
    if not s.get("enabled"):
        return {"expired": 0}
    hours = int(s["hours"])
    rows = (await db.execute(text(
        f"SELECT a.id, a.status, {event_kind_sql('a')} AS kind, {last_seen_sql('a')} AS last_seen"
        f" FROM alerts a WHERE {expirable_sql('a')}"
        f" ORDER BY a.triggered_at LIMIT {BATCH} FOR UPDATE OF a SKIP LOCKED"
    ), {"hours": hours})).fetchall()
    if not rows:
        return {"expired": 0}
    now = datetime.now(timezone.utc)
    meta = {"resolved_by": RESOLVED_BY, "expired_at": now.isoformat(), "expiry_hours": hours}
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = now(),"
        " metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb)"
        " WHERE id = ANY(CAST(:ids AS uuid[])) AND status IN ('active', 'acknowledged')"
    ), {"ids": [str(r.id) for r in rows], "m": json.dumps(meta)})
    note = f"No new event within {window_label(hours)}"
    for r in rows:
        await wf.record_event(db, r.id, "expired", source="system", reason=note,
                              old_status=r.status, new_status="resolved",
                              detail={"kind": r.kind, "hours": hours,
                                      "last_seen": r.last_seen.isoformat() if r.last_seen else None})
    await db.commit()
    logger.info("expired %d event alerts (window %d h)", len(rows), hours)
    return {"expired": len(rows)}


async def run_pass(session_factory=None, *, force: bool = False):
    """One expiry pass in one worker at a time, at most once a minute per
    worker (None when skipped)."""
    global _last_run
    mono = time.monotonic()
    if not force and _last_run and mono - _last_run < MIN_INTERVAL_S:
        return None
    _last_run = mono
    from app.services.alert_worker_lock import ALERT_EXPIRY_PASS, run_pass as locked
    return await locked(ALERT_EXPIRY_PASS, sweep, session_factory)


def reset_for_tests() -> None:
    global _last_run
    _last_run = 0.0
