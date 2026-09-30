"""Alert Center workflow: lifecycle transitions, suppression and ownership.

Operational condition and human workflow are separate facts (Alerts review
A06/A07/A23):

* ``status`` is the incident lifecycle: pending -> active -> acknowledged ->
  resolved. Transitions here are guarded (``SELECT ... FOR UPDATE`` then a
  status check), idempotent on repeat, and write their lifecycle event and
  audit record in the caller's transaction — the caller commits once, so a
  state change can never land without its history.
* A **suppression** (snooze) is a silence on the *condition*. It no longer
  resolves anything: the open incidents stay open, are stamped
  ``metadata.suppression`` and simply stop notifying/escalating. Resolving a
  still-breaching condition used to make every report treat the snooze as a
  recovery and let the escalation sweeper announce an all-clear.
* ``closure`` explains how a resolved row was closed. Only ``recovered`` is a
  genuine recovery; manual/suppressed/superseded/administrative closures are
  kept out of time-to-recover metrics.

Notice rows (recovery notices, dependency-suppressed downstream records, hold
cancellations) are stored as resolved alerts by the evaluators; they are not
incidents and are excluded from incident populations by ``notice_sql``.

Everything that needs migrate-135 (alert_events, assigned_to, delivery log)
feature-detects it via ``supported()`` so a rolling upgrade keeps working
before the migration has run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit_service import write_audit_log

logger = logging.getLogger("zenplus.alert_workflow")

OPEN_STATUSES = ("pending", "active", "acknowledged")
MAX_SNOOZE_MINUTES = 43200  # 30 days
MIN_SNOOZE_MINUTES = 5

_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

CLOSURE_LABELS = {
    "recovered": "Recovered automatically",
    "manual": "Closed by operator",
    "suppressed": "Closed by snooze (legacy)",
    "superseded": "Superseded by a newer status",
    "administrative": "Closed administratively",
    "hold_cancelled": "Hold cancelled — condition cleared before confirmation",
    "dependency": "Suppressed by upstream dependency",
    "notice": "Recovery notice",
}


# ── SQL builders ─────────────────────────────────────────────────────────────

def effective_rule_sql(a: str = "a") -> str:
    """The alert's rule id. Server (host) alerts historically stored the rule
    only in metadata.rule_id with the FK column empty (A10)."""
    return (f"COALESCE({a}.rule_id, CASE WHEN ({a}.metadata->>'rule_id') ~* '{_UUID_RE}' "
            f"THEN ({a}.metadata->>'rule_id')::uuid END)")


def silence_match_sql(a: str = "a", s: str = "s") -> str:
    """Silence ``s`` covers alert ``a``'s condition — the same identity the
    evaluators check before raising (server dedupe, or device rule[+ifIndex])."""
    return (
        f"(({a}.server_id IS NOT NULL AND {s}.server_id = {a}.server_id"
        f" AND {s}.dedupe = {a}.metadata->>'dedupe')"
        f" OR ({a}.device_id IS NOT NULL AND {a}.rule_id IS NOT NULL"
        f" AND {s}.device_id = {a}.device_id"
        f" AND {s}.dedupe = 'rule' || chr(58) || {a}.rule_id::text"
        f" || COALESCE(chr(58) || 'if' || chr(58) || NULLIF({a}.metadata->>'if_index', ''), '')))"
    )


def suppressed_sql(a: str = "a") -> str:
    """Alert ``a``'s condition has an active (unexpired) silence."""
    return (f"EXISTS (SELECT 1 FROM alert_silences _sil"
            f" WHERE (_sil.until IS NULL OR _sil.until > NOW())"
            f" AND {silence_match_sql(a, '_sil')})")


def notice_sql(a: str = "a") -> str:
    """Rows the evaluators record for information, not incidents."""
    return (f"(COALESCE({a}.metadata->>'is_recovery', 'false') = 'true'"
            f" OR COALESCE({a}.metadata->>'suppressed_by_dependency', 'false') = 'true'"
            f" OR COALESCE({a}.metadata->>'hold_cancelled', 'false') = 'true')")


def closure_reason_sql(a: str = "a") -> str:
    return (
        f"CASE WHEN {a}.status <> 'resolved' THEN NULL"
        f" WHEN COALESCE({a}.metadata->>'is_recovery', 'false') = 'true' THEN 'notice'"
        f" WHEN COALESCE({a}.metadata->>'suppressed_by_dependency', 'false') = 'true' THEN 'dependency'"
        f" WHEN COALESCE({a}.metadata->>'hold_cancelled', 'false') = 'true' THEN 'hold_cancelled'"
        f" WHEN {a}.metadata->>'resolved_by' = 'manual' THEN 'manual'"
        f" WHEN {a}.metadata->>'resolved_by' = 'snooze' THEN 'suppressed'"
        f" WHEN {a}.metadata->>'resolved_by' = 'status_transition' THEN 'superseded'"
        f" WHEN {a}.metadata->>'resolved_by' = 'migration' THEN 'administrative'"
        f" ELSE 'recovered' END"
    )


# ── Python classification (rows already in hand) ────────────────────────────

def notice_kind(meta: dict | None) -> str | None:
    m = meta or {}
    if str(m.get("is_recovery", "")).lower() == "true":
        return "recovery"
    if str(m.get("suppressed_by_dependency", "")).lower() == "true":
        return "dependency"
    if str(m.get("hold_cancelled", "")).lower() == "true":
        return "hold_cancelled"
    return None


def closure_reason(status: str | None, meta: dict | None) -> str | None:
    if status != "resolved":
        return None
    m = meta or {}
    nk = notice_kind(m)
    if nk == "recovery":
        return "notice"
    if nk:
        return nk
    by = m.get("resolved_by")
    if by == "manual":
        return "manual"
    if by == "snooze":
        return "suppressed"
    if by == "status_transition":
        return "superseded"
    if by == "migration":
        return "administrative"
    return "recovered"


# ── Feature detection ────────────────────────────────────────────────────────

_SUPPORTED: bool | None = None


async def supported(db: AsyncSession) -> bool:
    """Whether migrate-135 has run. Cached per process once positive; a
    negative answer is re-checked so the API picks the migration up."""
    global _SUPPORTED
    if _SUPPORTED:
        return True
    try:
        row = (await db.execute(text(
            "SELECT (SELECT 1 FROM information_schema.columns WHERE table_name = 'alerts'"
            " AND column_name = 'assigned_to') IS NOT NULL"
            " AND to_regclass('public.alert_events') IS NOT NULL"
        ))).scalar()
        _SUPPORTED = bool(row)
    except Exception:
        await db.rollback()
        _SUPPORTED = False
    return bool(_SUPPORTED)


# ── Errors ───────────────────────────────────────────────────────────────────

@dataclass
class WorkflowError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.status_code}: {self.detail}"


def actor_name(user) -> str | None:
    if user is None:
        return None
    return (getattr(user, "full_name", None) or getattr(user, "username", None) or None)


# ── Events ───────────────────────────────────────────────────────────────────

async def record_event(db: AsyncSession, alert_id, kind: str, *, actor=None,
                       source: str = "user", reason: str | None = None,
                       old_status: str | None = None, new_status: str | None = None,
                       detail: dict | None = None) -> None:
    """Append one lifecycle event (same transaction as the change)."""
    if not await supported(db):
        return
    await db.execute(text(
        "INSERT INTO alert_events (alert_id, kind, actor_id, actor_name, source,"
        " reason, old_status, new_status, detail)"
        " VALUES (:aid, :kind, :uid, :uname, :src, :reason, :old, :new, CAST(:detail AS jsonb))"
    ), {
        "aid": alert_id, "kind": kind,
        "uid": getattr(actor, "id", None), "uname": actor_name(actor),
        "src": source, "reason": (reason or None),
        "old": old_status, "new": new_status,
        "detail": json.dumps(detail or {}, default=str),
    })


async def _lock(db: AsyncSession, alert_id):
    return (await db.execute(text(
        "SELECT id, status, severity, device_id, server_id, rule_id, triggered_at,"
        " acknowledged_at, resolved_at, metadata FROM alerts WHERE id = :id FOR UPDATE"
    ), {"id": alert_id})).first()


def _fmt(ts) -> str:
    if not ts:
        return "unknown time"
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(ts)


# ── Transitions ──────────────────────────────────────────────────────────────

async def acknowledge(db: AsyncSession, alert_id, user, *, note: str | None = None,
                      source: str = "user") -> bool:
    """active -> acknowledged. Returns True when the state changed.

    Repeat acknowledgement is a no-op (the first actor and time are kept);
    pending (not yet confirmed) and resolved alerts are 409."""
    row = await _lock(db, alert_id)
    if not row:
        raise WorkflowError(404, "Alert not found")
    if row.status == "acknowledged":
        return False
    if row.status == "resolved":
        raise WorkflowError(409, f"Alert was already resolved at {_fmt(row.resolved_at)}.")
    if row.status == "pending":
        raise WorkflowError(409, "Alert is still in its hold period and has not fired yet.")
    has_owner = await supported(db)
    await db.execute(text(
        "UPDATE alerts SET status = 'acknowledged', acknowledged_at = NOW(),"
        " acknowledged_by = :uid"
        + (", assigned_to = COALESCE(assigned_to, :uid),"
           " assigned_at = COALESCE(assigned_at, NOW())" if has_owner else "")
        + " WHERE id = :id"
    ), {"id": alert_id, "uid": user.id})
    await record_event(db, alert_id, "acknowledged", actor=user, source=source,
                       reason=note, old_status=row.status, new_status="acknowledged")
    await write_audit_log(db, actor=user, action="alert.acknowledge", resource_type="alert",
                          resource_id=str(alert_id),
                          metadata={"severity": row.severity, "source": source,
                                    "device_id": str(row.device_id) if row.device_id else None,
                                    "server_id": str(row.server_id) if row.server_id else None})
    return True


async def resolve(db: AsyncSession, alert_id, user, *, reason: str | None = None,
                  source: str = "user") -> bool:
    """pending/active/acknowledged -> resolved (manual closure)."""
    row = await _lock(db, alert_id)
    if not row:
        raise WorkflowError(404, "Alert not found")
    if row.status == "resolved":
        return False
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW(),"
        " metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb)"
        " WHERE id = :id"
    ), {"id": alert_id, "m": json.dumps({
        "resolved_by": "manual", "resolved_by_user": str(user.id),
        "resolved_by_name": actor_name(user),
        **({"resolution_note": reason} if reason else {}),
    })})
    await record_event(db, alert_id, "resolved", actor=user, source=source, reason=reason,
                       old_status=row.status, new_status="resolved")
    await write_audit_log(db, actor=user, action="alert.resolve", resource_type="alert",
                          resource_id=str(alert_id),
                          metadata={"severity": row.severity, "reason": reason, "source": source,
                                    "previous_status": row.status,
                                    "device_id": str(row.device_id) if row.device_id else None,
                                    "server_id": str(row.server_id) if row.server_id else None})
    return True


async def assign(db: AsyncSession, alert_id, user, assignee_id, *, note: str | None = None,
                 source: str = "user") -> bool:
    if not await supported(db):
        raise WorkflowError(503, "Assignment needs database migration 135.")
    row = await _lock(db, alert_id)
    if not row:
        raise WorkflowError(404, "Alert not found")
    if row.status == "resolved":
        raise WorkflowError(409, "Resolved alerts cannot be reassigned.")
    target = None
    if assignee_id is not None:
        target = (await db.execute(text(
            "SELECT id, username, full_name FROM users WHERE id = :id AND is_active = true"
        ), {"id": assignee_id})).first()
        if not target:
            raise WorkflowError(422, "Assignee must be an active user.")
    current = (await db.execute(text("SELECT assigned_to FROM alerts WHERE id = :id"),
                                {"id": alert_id})).scalar()
    new_id = target.id if target else None
    if (str(current) if current else None) == (str(new_id) if new_id else None):
        return False
    await db.execute(text(
        "UPDATE alerts SET assigned_to = CAST(:aid AS uuid),"
        " assigned_at = CASE WHEN CAST(:aid AS uuid) IS NULL THEN NULL ELSE NOW() END"
        " WHERE id = :id"
    ), {"id": alert_id, "aid": new_id})
    name = (target.full_name or target.username) if target else None
    await record_event(db, alert_id, "assigned" if target else "unassigned", actor=user,
                       source=source, reason=note,
                       detail={"assignee_id": str(new_id) if new_id else None,
                               "assignee_name": name,
                               "previous_assignee_id": str(current) if current else None})
    await write_audit_log(db, actor=user, action="alert.assign", resource_type="alert",
                          resource_id=str(alert_id),
                          metadata={"assignee_id": str(new_id) if new_id else None,
                                    "previous_assignee_id": str(current) if current else None,
                                    "note": note})
    return True


async def comment(db: AsyncSession, alert_id, user, body: str) -> None:
    if not await supported(db):
        raise WorkflowError(503, "Comments need database migration 135.")
    exists = (await db.execute(text("SELECT 1 FROM alerts WHERE id = :id"),
                               {"id": alert_id})).first()
    if not exists:
        raise WorkflowError(404, "Alert not found")
    await record_event(db, alert_id, "comment", actor=user, reason=body)
    await write_audit_log(db, actor=user, action="alert.comment", resource_type="alert",
                          resource_id=str(alert_id), metadata={"length": len(body)})


# ── Suppression (snooze) ─────────────────────────────────────────────────────

@dataclass
class ConditionKey:
    scope_col: str          # server_id | device_id
    scope_id: str
    dedupe: str
    rule_id: str | None
    if_index: str


def device_dedupe(rule_id, if_index) -> str:
    d = f"rule:{rule_id}"
    if if_index not in (None, ""):
        d += f":if:{if_index}"
    return d


async def condition_key(db: AsyncSession, alert_id) -> ConditionKey | None:
    row = (await db.execute(text(
        "SELECT server_id, device_id, rule_id, metadata->>'dedupe' AS dedupe,"
        " metadata->>'if_index' AS if_index FROM alerts WHERE id = :id"
    ), {"id": alert_id})).first()
    if not row:
        return None
    if row.server_id and row.dedupe:
        return ConditionKey("server_id", str(row.server_id), row.dedupe, None, "")
    if row.device_id and row.rule_id:
        return ConditionKey("device_id", str(row.device_id),
                            device_dedupe(row.rule_id, row.if_index),
                            str(row.rule_id), row.if_index or "")
    return None


def _open_condition_sql(key: ConditionKey) -> tuple[str, dict]:
    if key.scope_col == "server_id":
        return ("server_id = :sid AND metadata->>'dedupe' = :d",
                {"sid": key.scope_id, "d": key.dedupe})
    return ("device_id = :sid AND rule_id = :rid AND COALESCE(metadata->>'if_index', '') = :ifx",
            {"sid": key.scope_id, "rid": key.rule_id, "ifx": key.if_index})


async def open_condition_alerts(db: AsyncSession, key: ConditionKey) -> list:
    where, params = _open_condition_sql(key)
    return (await db.execute(text(
        f"SELECT id, status FROM alerts WHERE {where}"
        f" AND status IN ('pending', 'active', 'acknowledged') ORDER BY triggered_at"
    ), params)).fetchall()


async def condition_label(db: AsyncSession, alert_id) -> str:
    row = (await db.execute(text(
        f"SELECT a.message, r.name AS rule_name, d.hostname, s.hostname AS server_name,"
        f" a.metadata->>'if_index' AS if_index"
        f" FROM alerts a LEFT JOIN alert_rules r ON r.id = {effective_rule_sql('a')}"
        f" LEFT JOIN devices d ON d.id = a.device_id LEFT JOIN servers s ON s.id = a.server_id"
        f" WHERE a.id = :id"
    ), {"id": alert_id})).first()
    if not row:
        return "condition"
    subject = row.hostname or row.server_name or "entity"
    what = row.rule_name or (row.message or "alert")[:80]
    iface = f" (interface {row.if_index})" if row.if_index else ""
    return f"{what} on {subject}{iface}"


def validate_snooze(minutes: int | None, reason: str | None, *, can_forever: bool) -> None:
    if minutes is None:
        if not can_forever:
            raise WorkflowError(403, "Indefinite suppression needs the alerts.manage permission.")
        if not reason or len(reason.strip()) < 3:
            raise WorkflowError(422, "Indefinite suppression needs a reason.")
        return
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise WorkflowError(422, "minutes must be a whole number.")
    if minutes < MIN_SNOOZE_MINUTES or minutes > MAX_SNOOZE_MINUTES:
        raise WorkflowError(
            422, f"Snooze must be between {MIN_SNOOZE_MINUTES} minutes and "
                 f"{MAX_SNOOZE_MINUTES // 1440} days; use indefinite suppression explicitly.")


async def snooze(db: AsyncSession, alert_id, user, *, minutes: int | None,
                 reason: str | None, can_forever: bool, source: str = "user") -> dict:
    """Silence the alert's condition. Open incidents stay open (suppressed)."""
    validate_snooze(minutes, reason, can_forever=can_forever)
    key = await condition_key(db, alert_id)
    if not key:
        raise WorkflowError(400, "This alert has no condition identity that can be snoozed.")
    label = await condition_label(db, alert_id)
    has_cols = await supported(db)
    await db.execute(text(
        f"DELETE FROM alert_silences WHERE {key.scope_col} = :sid AND dedupe = :d"
    ), {"sid": key.scope_id, "d": key.dedupe})
    until_expr = "NOW() + make_interval(mins => :m)" if minutes else "NULL"
    extra_cols = ", alert_id, condition_label" if has_cols else ""
    extra_vals = ", :aid, :label" if has_cols else ""
    silence = (await db.execute(text(
        f"INSERT INTO alert_silences ({key.scope_col}, dedupe, until, reason, created_by{extra_cols})"
        f" VALUES (:sid, :d, {until_expr}, :reason, :uid{extra_vals}) RETURNING id, until"
    ), {"sid": key.scope_id, "d": key.dedupe, "reason": reason or None, "uid": user.id,
        "aid": alert_id, "label": label, **({"m": minutes} if minutes else {})})).first()
    stamp = {"silence_id": str(silence.id), "at": datetime.now(timezone.utc).isoformat(),
             "by": str(user.id), "by_name": actor_name(user),
             "until": silence.until.isoformat() if silence.until else None,
             "reason": reason or None}
    rows = await open_condition_alerts(db, key)
    for r in rows:
        await db.execute(text(
            "UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb)"
            " || jsonb_build_object('suppression', CAST(:s AS jsonb)) WHERE id = :id"
        ), {"id": r.id, "s": json.dumps(stamp)})
        await record_event(db, r.id, "snoozed", actor=user, source=source, reason=reason,
                           old_status=r.status, new_status=r.status,
                           detail={"until": stamp["until"], "forever": minutes is None,
                                   "minutes": minutes, "silence_id": stamp["silence_id"]})
    await write_audit_log(db, actor=user, action="alert.snooze", resource_type="alert",
                          resource_id=str(alert_id),
                          metadata={"minutes": minutes, "forever": minutes is None,
                                    "reason": reason, "dedupe": key.dedupe,
                                    "condition": label, "open_alerts": len(rows),
                                    "silence_id": stamp["silence_id"]})
    return {"snoozed": True, "forever": minutes is None, "minutes": minutes,
            "until": stamp["until"], "silence_id": stamp["silence_id"],
            "open_alerts": len(rows), "condition_label": label}


async def _clear_suppression(db: AsyncSession, alert_ids: list, user, kind: str,
                             *, source: str, detail: dict) -> None:
    for aid, status in alert_ids:
        await db.execute(text(
            "UPDATE alerts SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'suppression')"
            " || jsonb_build_object('suppression_ended', CAST(:e AS jsonb)) WHERE id = :id"
        ), {"id": aid, "e": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                         "kind": kind})})
        await record_event(db, aid, kind, actor=user, source=source,
                           old_status=status, new_status=status, detail=detail)


async def unsnooze(db: AsyncSession, alert_id, user, source: str = "user") -> int:
    key = await condition_key(db, alert_id)
    if not key:
        raise WorkflowError(400, "This alert has no condition identity to unsnooze.")
    res = await db.execute(text(
        f"DELETE FROM alert_silences WHERE {key.scope_col} = :sid AND dedupe = :d RETURNING id"
    ), {"sid": key.scope_id, "d": key.dedupe})
    removed = [str(r.id) for r in res.fetchall()]
    rows = await open_condition_alerts(db, key)
    await _clear_suppression(db, [(r.id, r.status) for r in rows], user, "unsnoozed",
                             source=source, detail={"silence_ids": removed})
    await write_audit_log(db, actor=user, action="alert.unsnooze", resource_type="alert",
                          resource_id=str(alert_id),
                          metadata={"dedupe": key.dedupe, "removed": len(removed),
                                    "silence_ids": removed})
    return len(removed)


async def remove_silence(db: AsyncSession, silence_id, user) -> int:
    """Delete one silence (suppression workspace) and un-stamp its alerts."""
    matched = (await db.execute(text(
        f"SELECT a.id, a.status FROM alerts a JOIN alert_silences s ON s.id = :sid"
        f" WHERE a.status IN ('pending', 'active', 'acknowledged') AND {silence_match_sql('a', 's')}"
    ), {"sid": silence_id})).fetchall()
    info = (await db.execute(text(
        "DELETE FROM alert_silences WHERE id = :id RETURNING id, server_id, device_id, dedupe, until"
    ), {"id": silence_id})).first()
    if not info:
        return 0
    await _clear_suppression(db, [(r.id, r.status) for r in matched], user, "unsnoozed",
                             source="user", detail={"silence_ids": [str(silence_id)]})
    await write_audit_log(db, actor=user, action="alert.silence.remove", resource_type="alert_silence",
                          resource_id=str(silence_id),
                          metadata={"dedupe": info.dedupe, "forever": info.until is None,
                                    "server_id": str(info.server_id) if info.server_id else None,
                                    "device_id": str(info.device_id) if info.device_id else None,
                                    "open_alerts": len(matched)})
    return 1


# ── Response targets ─────────────────────────────────────────────────────────

RESPONSE_TARGETS_KEY = "alert_response_targets"
DEFAULT_RESPONSE_TARGETS = {"critical": 15, "warning": 60, "info": None}


def _clean_targets(raw: Any) -> dict:
    out = dict(DEFAULT_RESPONSE_TARGETS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if isinstance(raw, dict):
        for sev in out:
            if sev in raw:
                v = raw[sev]
                out[sev] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    return out


async def response_targets(db: AsyncSession) -> dict:
    try:
        row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                {"k": RESPONSE_TARGETS_KEY})).first()
    except Exception:
        await db.rollback()
        return dict(DEFAULT_RESPONSE_TARGETS)
    return _clean_targets(row[0] if row else None)


async def save_response_targets(db: AsyncSession, targets: dict) -> dict:
    clean = _clean_targets(targets)
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    ), {"k": RESPONSE_TARGETS_KEY, "v": json.dumps(clean)})
    return clean


def ack_due(status: str, severity: str, triggered_at, targets: dict, suppressed: bool):
    """(due_at, overdue) for an open, unacknowledged, unsuppressed alert."""
    if status != "active" or suppressed or not triggered_at:
        return None, False
    minutes = targets.get(severity)
    if not minutes:
        return None, False
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    due = triggered_at + timedelta(minutes=minutes)
    return due, datetime.now(timezone.utc) > due


# ── Suppression expiry sweeper ───────────────────────────────────────────────

async def sweep_suppression_expiry(db: AsyncSession) -> dict[str, int]:
    """Open incidents whose silence expired while the condition is still open:
    clear the suppression stamp, record it, and remind the rule's channels —
    otherwise an expired snooze would leave a still-breaching condition silent
    forever (the evaluators only notify on the transition that opens a row)."""
    if not await supported(db):
        return {"expired": 0, "reminded": 0}
    rows = (await db.execute(text(
        f"SELECT a.id FROM alerts a WHERE a.status IN ('active', 'acknowledged')"
        f" AND jsonb_exists(COALESCE(a.metadata, '{{}}'::jsonb), 'suppression')"
        f" AND NOT {suppressed_sql('a')} LIMIT 200"
    ))).fetchall()
    expired = reminded = 0
    for r in rows:
        try:
            won = (await db.execute(text(
                "UPDATE alerts SET metadata = (metadata - 'suppression')"
                " || jsonb_build_object('suppression_ended', jsonb_build_object("
                "   'at', to_jsonb(NOW()), 'kind', 'suppression_expired'))"
                 " WHERE id = :id AND jsonb_exists(metadata, 'suppression')"
                " RETURNING id, status, severity, message, triggered_at, device_id, server_id,"
                f" {effective_rule_sql('alerts')} AS eff_rule"
            ), {"id": r.id})).first()
            if not won:
                await db.rollback()
                continue
            await record_event(db, won.id, "suppression_expired", source="system",
                               old_status=won.status, new_status=won.status)
            if won.eff_rule and won.status == "active":
                reminded += await _remind(db, won)
            await db.commit()
            expired += 1
        except Exception:
            logger.exception("suppression expiry for alert %s failed", r.id)
            await db.rollback()
    return {"expired": expired, "reminded": reminded}


async def _remind(db: AsyncSession, row) -> int:
    from app.services import alert_phrasing as ap
    from app.services.host_alert_service import dispatch_to_channels

    rule = (await db.execute(text(
        "SELECT id, name, enabled, severity, notify_channels FROM alert_rules WHERE id = :id"
    ), {"id": row.eff_rule})).first()
    if not rule or not rule.enabled or not rule.notify_channels:
        return 0
    subject = (await db.execute(text(
        "SELECT COALESCE(d.hostname, s.hostname) FROM alerts a"
        " LEFT JOIN devices d ON d.id = a.device_id LEFT JOIN servers s ON s.id = a.server_id"
        " WHERE a.id = :id"
    ), {"id": row.id})).scalar() or "monitored object"
    sev = (row.severity or rule.severity or "warning")
    duration = ap.duration_between(row.triggered_at, datetime.now(timezone.utc))
    return await dispatch_to_channels(db, rule.notify_channels, {
        "subject": f"[{sev.upper()}] Snooze ended, still active: {rule.name} — {subject}",
        "body": (f"{row.message} The snooze on this condition has expired and the alert is "
                 f"still open (active for {duration})."),
        "message": f"ZenPlus {sev.upper()} snooze ended — {row.message} Still active ({duration}).",
        "hostname": subject, "ip_address": "",
        "status": "STILL ACTIVE", "severity": sev, "resolved": False,
        "rule_name": rule.name, "rule_id": str(rule.id),
        "details": [("Alert rule", rule.name), ("Active for", duration)],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "is_recovery": False, "alert_id": str(row.id), "delivery_kind": "reminder",
    })
