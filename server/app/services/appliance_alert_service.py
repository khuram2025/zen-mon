"""ZenPlus appliance health alerts (alerting plan G-1, migrate-198).

The appliance watched every device, server and service except itself: its own
data disk, ClickHouse, scheduled backups, updates, licence, clock, failing
notification channels and remote sensors raised nothing (one customer
appliance failed 180 deliveries in 30 days and nobody knew). This evaluator
gives those signals the "appliance" rule family and the standard metric
incident lifecycle (docs/alert-notification-policy.md):

* one incident per rule + dimension (channel, sensor, database, or none);
* a breach hold (``min_duration``): the condition must persist that long;
* a clear level (an explicit ``reset_threshold``, or for the disk 2 points on
  the healthy side) that must hold for ``clear_duration`` (NULL = 300 s,
  0 = at once) before the incident resolves;
* a missing reading never resolves and never counts as clear time;
* a recovery notice when the rule asks for one and the trigger went out.

Security and operations events (alerting plan G-16, migrate-206) use the same
family: bursts of failed sign-ins per account or source address, security-
relevant administrative changes (event-like: one incident per burst, see
:func:`event_view`) and scheduled report runs that failed or were only partly
delivered (per schedule, clearing on its next good run).

The readings have no stored history, so the hold start times live in
``appliance_alert_state`` (rule + dimension). Passes run every
``EVAL_INTERVAL_S`` in one API worker at a time (``alert_worker_lock``);
every notification goes through ``host_alert_service.dispatch_to_channels``
with ``alert_id`` and ``delivery_event``.

The incidents have no device or server: they are rule alerts about "ZenPlus
appliance" (``metadata.source = 'appliance_health'``, entity "ZenPlus
appliance" in the alert list, as for the controller-monitoring alert). A
remote sensor incident also carries ``sensor_id``, so it links to the sensor.
Rule exclusions and snoozes are per device or server and do not apply; turn a
rule off, or give it a schedule, instead.

Every source is only read here. Storage changes stay with the root volume
helper and the storage watchdog; this module never writes outside its own
tables and the ``alerts`` row it owns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_funnel as funnel
from app.services import alert_notify_state as ns
from app.services import alert_phrasing as ap

logger = logging.getLogger("zenplus.appliance_alerts")

EVAL_INTERVAL_S = 60
# A pass gap longer than this is missing data: holds restart.
MAX_GAP_S = 3 * EVAL_INTERVAL_S
DEFAULT_CLEAR_S = 300
SOURCE = "appliance_health"
APPLIANCE = "ZenPlus appliance"

DISK = "appliance_disk_pct"
DB_DOWN = "appliance_db_down"
BACKUP = "appliance_backup_failed"
UPDATE = "appliance_update_failed"
LICENSE = "appliance_license_days"
TIME = "appliance_time_unsynced"
DELIVERY = "appliance_delivery_failures"
SENSOR = "appliance_sensor_offline"
# Security and operations events (alerting plan G-16, migrate-206).
LOGIN = "appliance_login_failures"
ADMIN = "appliance_admin_changes"
REPORT = "appliance_report_failed"
APPLIANCE_METRICS = (DISK, DB_DOWN, BACKUP, UPDATE, LICENSE, TIME, DELIVERY, SENSOR,
                     LOGIN, ADMIN, REPORT)
# Dimensioned metrics: a dimension that disappears (channel deleted, sensor
# removed or disabled, report schedule deleted or turned off) closes its
# incident without a recovery notice.
DIMENSIONED = frozenset({DELIVERY, SENSOR, LOGIN, REPORT})
# Event-like metrics: one incident per burst of events. The reading counts the
# events of the window not already reported by an earlier incident of the same
# rule + dimension, and an open incident clears once no event has happened for
# ``clear_duration`` (NULL = EVENT_DEFAULT_CLEAR_S).
EVENT_METRICS = frozenset({ADMIN})
EVENT_DEFAULT_CLEAR_S = 900
# Percent metrics that clear 2 points on the healthy side unless the rule
# sets its own reset level (as host/SNMP CPU, memory and filesystem rules do).
AUTO_CLEAR = frozenset({DISK})
AUTO_CLEAR_POINTS = 2.0

# A backup is overdue this long after its schedule's period (the hour window,
# the 3 h stale-run limit of storage_service and a margin).
BACKUP_GRACE_H = 4
DELIVERY_WINDOW_S = 3600
LICENSE_NO_EXPIRY_DAYS = 36500.0
CLICKHOUSE_TIMEOUT_S = 15
LOGIN_WINDOW_S = 900
ADMIN_WINDOW_S = 900
# The newest qualifying change is looked up this far back (for a clear hold
# longer than the counting window).
ADMIN_LOOKBACK_S = 30 * 86400
# Distinct accounts / source addresses evaluated per pass (most failures first;
# dimensions with an open incident are always evaluated).
LOGIN_MAX_DIMENSIONS = 200
# Event times kept on a reading (and on its incident) to tell a new burst
# from one an earlier incident already reported.
EVENT_TIMES_KEPT = 200

_HIGH_OPS = {">", ">="}
_LOW_OPS = {"<", "<="}
_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def is_appliance_metric(metric) -> bool:
    return str(metric or "") in APPLIANCE_METRICS


# ─── Readings ────────────────────────────────────────────────────────────────

@dataclass
class Reading:
    """One measurement of one metric. ``value`` None = unknown (missing data)."""
    value: Optional[float]
    dimension: str = ""
    label: str = ""                # "ClickHouse", "email channel NOC mail", "remote sensor edge-1"
    facts: dict = field(default_factory=dict)
    sensor_id: Optional[str] = None


# Error text is shown to people: no URLs, addresses, host names, numbers that
# could be phone numbers, or key=value secrets.
_SCRUB = [
    (re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "<address>"),
    (re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|auth)\b\s*[=:]\s*\S+"),
     r"\1=***"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"), "<address>"),
    (re.compile(r"\b[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}\b"), "<address>"),
    (re.compile(r"\b(?=[a-zA-Z0-9-]*[a-zA-Z])[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+\.[a-zA-Z]{2,}\b"), "<host>"),
    (re.compile(r"\+?\d[\d ()-]{6,}\d"), "<number>"),
]


def scrub(message, limit: int = 160) -> str:
    """Error text safe to print in an alert: no credentials, addresses or tokens."""
    if message is None:
        return ""
    out = " ".join(str(message).split())
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _epoch(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


# ─── Collectors (pure parts; each is tested with faked inputs) ───────────────

def disk_reading(watchdog_state: Optional[dict], samples: Optional[list], now: float,
                 stale_s: float = 180) -> Reading:
    """Data-volume usage: the storage watchdog's state when it is alive, else a
    direct statvfs sample. The data volume is whatever filesystem backs
    ``/data`` (often the root one); then the one holding ClickHouse; then /."""
    entries: list[dict] = []
    if watchdog_state and now - float(watchdog_state.get("heartbeat") or 0) < stale_s:
        entries = [e for e in watchdog_state.get("filesystems") or [] if isinstance(e, dict)]
    if not entries and samples:
        entries = [{"mount": s.mount, "usage_pct": s.usage_pct, "avail_bytes": s.avail_bytes,
                    "total_bytes": s.total_bytes} for s in samples]
    if not entries:
        return Reading(None, label="data disk")

    def mounts(e):
        return str(e.get("mount") or "").split()

    chosen = (next((e for e in entries if "/data" in mounts(e)), None)
              or next((e for e in entries if e.get("holds_clickhouse")), None)
              or next((e for e in entries if "/" in mounts(e)), None)
              or entries[0])
    pct = chosen.get("usage_pct")
    if pct is None:
        return Reading(None, label="data disk")
    return Reading(float(pct), label="data disk", facts={
        "mount": chosen.get("mount"), "avail_bytes": chosen.get("avail_bytes"),
        "total_bytes": chosen.get("total_bytes"),
        "watchdog_state": chosen.get("state")})


def clickhouse_reading(error: Optional[BaseException]) -> Reading:
    from app.services.alert_delivery_log import error_category
    if error is None:
        return Reading(0.0, dimension="clickhouse", label="ClickHouse")
    category = error_category(error) or "error"
    return Reading(1.0, dimension="clickhouse", label="ClickHouse",
                   facts={"error": category})


def backup_reading(schedule: dict, latest: Optional[dict], last_completed_at: Optional[float],
                   schedule_set_at: Optional[float], now: float) -> Reading:
    """1 when backups are scheduled and the last scheduled one failed, or none
    completed within the schedule period plus :data:`BACKUP_GRACE_H` hours
    (counted from the last completed one, or from when the schedule was saved)."""
    if not schedule or not schedule.get("enabled"):
        return Reading(0.0, facts={"scheduled": False})
    frequency = "weekly" if schedule.get("frequency") == "weekly" else "daily"
    allowance_h = (168 if frequency == "weekly" else 24) + BACKUP_GRACE_H
    facts: dict = {"scheduled": True, "frequency": frequency, "allowance_hours": allowance_h}
    if latest and latest.get("status") == "failed":
        facts.update(reason="failed", failed_at=_epoch(latest.get("finished_at") or latest.get("created_at")),
                     error=scrub(latest.get("error")))
        return Reading(1.0, facts=facts)
    reference = max([t for t in (last_completed_at, schedule_set_at) if t is not None], default=None)
    if reference is None:
        return Reading(None, facts=facts)
    facts["last_completed_at"] = last_completed_at
    hours = (now - reference) / 3600.0
    if hours > allowance_h:
        facts.update(reason="overdue", hours_since=round(hours, 1))
        return Reading(1.0, facts=facts)
    return Reading(0.0, facts=facts)


def update_reading(history: Optional[list]) -> Reading:
    """The last finished update attempt: failed / rolled back = 1, success = 0;
    unknown without one (no history file, nothing finished yet)."""
    if not isinstance(history, list):
        return Reading(None)
    for record in reversed(history):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status not in ("success", "failed", "rolled_back"):
            continue
        facts = {"version": str(record.get("version") or ""),
                 "from_version": str(record.get("from_version") or ""), "status": status,
                 "finished_at": record.get("completed_at") or record.get("updated_at") or ""}
        if status == "success":
            return Reading(0.0, facts=facts)
        facts["error"] = scrub(record.get("error"))
        return Reading(1.0, facts=facts)
    return Reading(None)


def license_reading(remote: Optional[dict], local_expires_at, now: float) -> Reading:
    """Days until the licence expires (negative once expired). The subscription
    the updater synchronises from zentryc.com wins over the local row, as on
    the Licenses page. A licence without an expiry date reads as far away."""
    expires = None
    source = None
    if isinstance(remote, dict) and remote.get("plan"):
        source = "zentryc"
        expires = _epoch(remote.get("expires_at"))
        if expires is None:
            if remote.get("is_expired"):
                return Reading(0.0, facts={"source": source, "expires_at": None, "expired": True})
            return Reading(LICENSE_NO_EXPIRY_DAYS, facts={"source": source, "expires_at": None})
    elif local_expires_at is not None:
        source = "local"
        expires = _epoch(local_expires_at)
    if expires is None:
        return Reading(None)
    days = (expires - now) / 86400.0
    return Reading(round(days, 2), facts={"source": source, "expires_at": expires})


def time_reading(info: Optional[dict]) -> Reading:
    """``timedatectl show``: NTPSynchronized=no -> 1; unknown without it."""
    info = info or {}
    synced = info.get("ntp_synchronized")
    if synced is None:
        return Reading(None)
    return Reading(0.0 if synced else 1.0, facts={"ntp_service": info.get("ntp_service")})


def channel_label(name: Optional[str], ctype: Optional[str]) -> str:
    kind = {"email": "email", "sms": "SMS", "webhook": "webhook", "slack": "Slack",
            "telegram": "Telegram"}.get(str(ctype or "").lower(), str(ctype or "notification"))
    return f"{kind} channel {name}" if name else f"{kind} channel"


# Failed sign-in reasons (the auth.login_failed audit ``reason``), as the
# Sign-in Activity report groups them.
LOGIN_REASONS = {
    "invalid": "wrong username or password",
    "locked": "account locked by the failure threshold",
    "blocked": "attempt while the account was locked",
    "mfa": "wrong MFA code",
    "disabled": "account disabled",
    "sso_only": "single sign-on account used the password form",
    "sso": "single sign-on error",
    "other": "other",
}


def login_reason(reason, method=None) -> str:
    r = str(reason or "").strip().lower()
    if "temporarily locked" in r or "account locked" in r:
        return "locked"
    if r.startswith("too many failed sign-in attempts. try again"):
        return "blocked"
    if r == "mfa" or r.startswith("invalid verification code"):
        return "mfa"
    if "disabled" in r:
        return "disabled"
    if "signs in with single sign-on" in r:
        return "sso_only"
    if str(method or "") == "oidc":
        return "sso"
    if r.startswith("invalid username or password"):
        return "invalid"
    return "other"


def login_label(dimension: str, account: Optional[str] = None) -> str:
    """"user jsmith" for an existing account; "unknown accounts from one
    address" otherwise. The typed text of a name that is not an account is
    never shown (it is often a mistyped password)."""
    if dimension.startswith("user:"):
        return f"user {scrub(account or dimension[5:], 80)}"
    return "unknown accounts from one address"


def login_reading(row: dict) -> Reading:
    """One account (``user:<name>``) or, for names that are no account and
    single sign-on failures without a name, one source address (``ip:<addr>``):
    failed sign-ins in the last LOGIN_WINDOW_S. The address stays in the
    dimension key; text never prints it."""
    dim = str(row["dim"])
    reason = login_reason(row.get("last_reason"), row.get("last_method"))
    facts = {"window_s": LOGIN_WINDOW_S, "kind": "account" if dim.startswith("user:") else "address",
             "last_reason": reason, "locked": bool(row.get("locked")),
             "sources": int(row.get("sources") or 0), "names": int(row.get("names") or 0),
             "last_failed_at": _epoch(row.get("last_at"))}
    return Reading(float(row.get("n") or 0), dimension=dim, label=login_label(dim, row.get("account")),
                   facts=facts)


# Security-relevant administrative changes (audit_logs.action). user.update
# counts only when it changes the account's access (role, role source,
# visibility scope, enabled); alert_rule.toggle only when it turns a rule off.
ADMIN_ACTIONS = (
    "user.create", "user.delete", "user.update", "user.password_reset", "user.mfa_reset",
    "role.create", "role.update", "role.delete",
    "settings.auth.update", "settings.auth.toggle", "settings.auth.policy",
    "security.access.update", "security.tls.config", "security.tls.cert_installed",
    "security.tls.cert_removed",
    "apm.ingest_key.create", "apm.enrollment_token.create", "sensor.enrollment_token.regenerate",
    "alert_rule.delete", "alert_rule.toggle", "alert_rule.exclude",
    "notification_channel.delete", "escalation_policy.delete",
)
USER_ACCESS_CHANGES = ("role", "role_source", "scope_tags", "is_active")
ADMIN_FILTER_SQL = (
    "action = ANY(:actions)"
    " AND NOT (action = 'alert_rule.toggle' AND COALESCE(metadata->>'enabled', '') <> 'false')"
    " AND NOT (action = 'user.update' AND NOT (COALESCE(metadata->'changes', '{}'::jsonb)"
    " ?| ARRAY['role', 'role_source', 'scope_tags', 'is_active']))"
)
_AUTH_KEYS = {"auth.ldap": "LDAP", "auth.radius": "RADIUS", "auth.oidc": "single sign-on"}


def _as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def admin_change_label(action: str, metadata, resource_id=None) -> str:
    """What one audited change did, in a few words ("user jsmith created")."""
    m = _as_dict(metadata)

    def name(key="name"):
        v = m.get(key)
        return f" {scrub(v, 60)}" if v else ""

    user = name("username")
    if action == "user.create":
        return f"user{user} created"
    if action == "user.delete":
        return f"user{user} deleted"
    if action == "user.update":
        changes = _as_dict(m.get("changes"))
        if "role" in changes:
            to = _as_dict(changes["role"]).get("to")
            return f"user{user} role set to {scrub(to, 60)}" if to else f"user{user} role changed"
        if "is_active" in changes:
            return f"user{user} {'enabled' if _as_dict(changes['is_active']).get('to') else 'disabled'}"
        if "scope_tags" in changes:
            return f"user{user} visibility scope changed"
        if "role_source" in changes:
            return f"user{user} role source changed"
        return f"user{user} changed"
    if action == "user.password_reset":
        return f"user{user} password reset"
    if action == "user.mfa_reset":
        return f"user{user} MFA reset"
    if action.startswith("role."):
        verb = {"role.create": "created", "role.update": "changed", "role.delete": "deleted"}[action]
        return f"role{name()} {verb}"
    if action in ("settings.auth.update", "settings.auth.toggle"):
        what = _AUTH_KEYS.get(str(resource_id or ""), "directory sign-in")
        if action == "settings.auth.toggle":
            return f"{what} sign-in turned {'on' if m.get('enabled') else 'off'}"
        return f"{what} settings changed"
    return {
        "settings.auth.policy": "sign-in and password policy changed",
        "security.access.update": "management access list changed",
        "security.tls.config": "HTTPS settings changed",
        "security.tls.cert_installed": "HTTPS certificate installed",
        "security.tls.cert_removed": "HTTPS certificate removed",
        "apm.ingest_key.create": f"APM ingest key{name()} created",
        "apm.enrollment_token.create": "APM enrollment token created",
        "sensor.enrollment_token.regenerate": "sensor enrollment token regenerated",
        "alert_rule.delete": f"alert rule{name()} deleted",
        "alert_rule.toggle": f"alert rule{name()} turned off",
        "alert_rule.exclude": f"alert rule{name()} turned off for one asset",
        "notification_channel.delete": f"notification channel{name()} deleted",
        "escalation_policy.delete": f"escalation policy{name()} deleted",
    }.get(action, action)


def admin_reading(rows: list, last_event_at, now: float) -> Reading:
    """Qualifying changes in the last ADMIN_WINDOW_S, newest first. ``rows``:
    dicts with at (epoch), action, actor, metadata, resource_id."""
    events = sorted(({"at": float(r["at"]), "action": r["action"],
                      "what": admin_change_label(r["action"], r.get("metadata"), r.get("resource_id")),
                      "by": scrub(r.get("actor"), 60) or "the system"} for r in rows),
                    key=lambda e: -e["at"])
    facts = {"window_s": ADMIN_WINDOW_S,
             "event_times": [e["at"] for e in events[:EVENT_TIMES_KEPT]],
             "events": events[:10],
             "last_event_at": _epoch(last_event_at) if last_event_at is not None
             else (events[0]["at"] if events else None)}
    return Reading(float(len(events)), facts=facts)


def report_reading(schedule: dict, run: Optional[dict]) -> Reading:
    """A report schedule's latest finished run: failed / partial = 1,
    success / skipped = 0, none yet = unknown. Runs still running or waiting
    for a delivery retry are not finished."""
    sid = str(schedule["id"])
    label = f'report schedule "{scrub(schedule.get("name"), 80)}"'
    facts: dict = {"schedule_name": scrub(schedule.get("name"), 80)}
    if not run or not run.get("status"):
        return Reading(None, dimension=sid, label=label, facts=facts)
    status = str(run["status"])
    notes = run.get("notes")
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except ValueError:
            notes = []
    detail = run.get("error") or next((n for n in (notes or []) if isinstance(n, str)
                                       and not n.startswith("Retry ")), None)
    facts.update(status=status, run_id=str(run.get("id") or ""),
                 generated_at=_epoch(run.get("generated_at")))
    if status in ("failed", "partial"):
        facts["error"] = scrub(detail) if detail else ""
        return Reading(1.0, dimension=sid, label=label, facts=facts)
    return Reading(0.0, dimension=sid, label=label, facts=facts)


# ─── Sources (I/O; replaced in tests) ────────────────────────────────────────

class Sources:
    """Where each reading comes from. Every method returns a list of readings,
    or raises: a failed source is missing data for that metric this pass."""

    async def disk(self, db: AsyncSession, now: float) -> list[Reading]:
        from app.services import storage_service, storage_watchdog
        state = await asyncio.to_thread(storage_watchdog._read_json, "state.json")
        samples = None
        if not state or now - float((state or {}).get("heartbeat") or 0) >= storage_service.WATCHDOG_STALE_S:
            samples = await asyncio.to_thread(storage_watchdog.sample_filesystems)
        return [disk_reading(state, samples, now, storage_service.WATCHDOG_STALE_S)]

    async def databases(self, db: AsyncSession, now: float) -> list[Reading]:
        # PostgreSQL answers if this pass runs at all: it holds the rules, the
        # incidents and the channels. Watching it needs an external monitor
        # (GET /api/v1/system/health); see docs/appliance-health-alerts.md.
        def probe():
            from app.core.database import get_clickhouse_client
            get_clickhouse_client().query("SELECT 1")
        try:
            await asyncio.wait_for(asyncio.to_thread(probe), CLICKHOUSE_TIMEOUT_S)
            return [clickhouse_reading(None)]
        except asyncio.TimeoutError as exc:
            return [clickhouse_reading(TimeoutError(f"timed out after {CLICKHOUSE_TIMEOUT_S} s: {exc}"))]
        except Exception as exc:  # noqa: BLE001 - any failure means "not answering"
            return [clickhouse_reading(exc)]

    async def backup(self, db: AsyncSession, now: float) -> list[Reading]:
        from app.services import storage_service
        schedule = await storage_service.get_backup_schedule(db)
        latest = (await db.execute(text(
            "SELECT status, error, created_at, finished_at FROM storage_backups"
            " WHERE created_by = 'schedule' AND created_at <= to_timestamp(:now)"
            " ORDER BY created_at DESC LIMIT 1"), {"now": now})).mappings().first()
        completed = (await db.execute(text(
            "SELECT max(COALESCE(finished_at, created_at)) FROM storage_backups"
            " WHERE created_by = 'schedule' AND status = 'completed'"))).scalar()
        set_at = (await db.execute(text("SELECT updated_at FROM system_settings WHERE key = :k"),
                                   {"k": storage_service.BACKUP_SCHEDULE_KEY})).scalar()
        return [backup_reading(schedule, dict(latest) if latest else None,
                               _epoch(completed), _epoch(set_at), now)]

    async def update(self, db: AsyncSession, now: float) -> list[Reading]:
        from app.api.v1.system_updates import HISTORY_FILE

        def read():
            path = Path(HISTORY_FILE)
            if not path.exists():
                return None
            return json.loads(path.read_text())
        try:
            history = await asyncio.to_thread(read)
        except (OSError, ValueError):
            history = None
        return [update_reading(history)]

    async def license(self, db: AsyncSession, now: float) -> list[Reading]:
        from app.api.v1.subscription import _load_remote_subscription
        remote = await asyncio.to_thread(_load_remote_subscription)
        local = (await db.execute(text(
            "SELECT expires_at FROM subscriptions ORDER BY created_at DESC LIMIT 1"))).scalar()
        return [license_reading(remote, local, now)]

    async def time_sync(self, db: AsyncSession, now: float) -> list[Reading]:
        from app.api.v1.system_time import read_timedatectl
        return [time_reading(await asyncio.to_thread(read_timedatectl))]

    async def deliveries(self, db: AsyncSession, now: float) -> list[Reading]:
        """Failed deliveries per channel in the last hour (alert_deliveries).
        Every configured channel is a dimension (0 when nothing failed), and
        so are the direct recipients of escalation policies when they failed."""
        from app.services.host_alert_service import DIRECT_CHANNEL_ID
        window = {"since": now - DELIVERY_WINDOW_S, "now": now}
        failed = {str(r.channel_id): (int(r.n), r.last_error) for r in (await db.execute(text(
            "SELECT channel_id, count(*) AS n,"
            " (array_agg(error ORDER BY attempted_at DESC))[1] AS last_error"
            " FROM alert_deliveries WHERE outcome = 'failed' AND channel_id IS NOT NULL"
            " AND attempted_at > to_timestamp(:since) AND attempted_at <= to_timestamp(:now)"
            " GROUP BY channel_id"), window)).all()}
        out = []
        for ch in (await db.execute(text(
                "SELECT id::text AS id, name, type, enabled FROM notification_channels ORDER BY name"))).all():
            n, err = failed.get(ch.id, (0, None))
            out.append(Reading(float(n), dimension=ch.id, label=channel_label(ch.name, ch.type),
                               facts={"channel_type": ch.type, "channel_enabled": bool(ch.enabled),
                                      "last_error": scrub(err) if err else ""}))
        if DIRECT_CHANNEL_ID in failed:
            n, err = failed[DIRECT_CHANNEL_ID]
            out.append(Reading(float(n), dimension=DIRECT_CHANNEL_ID,
                               label="direct recipients (default email/SMS gateway)",
                               facts={"last_error": scrub(err) if err else ""}))
        return out

    async def sensors(self, db: AsyncSession, now: float) -> list[Reading]:
        """The sensor sweep's confirmed status: offline = 1, online/degraded = 0,
        never enrolled (pending) = unknown. Disabled sensors are not watched."""
        out = []
        for s in (await db.execute(text(
                "SELECT id::text AS id, name, status, last_heartbeat_at FROM sensors"
                " WHERE status IS DISTINCT FROM 'disabled' ORDER BY name"))).all():
            status = str(s.status or "")
            value = 1.0 if status == "offline" else 0.0 if status in ("online", "degraded") else None
            out.append(Reading(value, dimension=s.id, label=f"remote sensor {s.name}", sensor_id=s.id,
                               facts={"sensor_name": s.name, "status": status,
                                      "last_heartbeat_at": _epoch(s.last_heartbeat_at)}))
        return out

    async def login_failures(self, db: AsyncSession, now: float) -> list[Reading]:
        """Failed sign-ins (auth.login_failed audit entries) in the last
        LOGIN_WINDOW_S, per existing account, else per source address. Every
        dimension with an open incident is read too (0 once its failures left
        the window), so it can clear; the others disappear when quiet."""
        rows = (await db.execute(text(
            "WITH f AS ("
            " SELECT a.created_at, a.metadata->>'source_ip' AS ip,"
            " NULLIF(a.metadata->>'username', '') AS typed,"
            " a.metadata->>'reason' AS reason, a.metadata->>'method' AS method,"
            " (SELECT u.username FROM users u WHERE lower(u.username) = lower(a.metadata->>'username')"
            "   ORDER BY (u.username = a.metadata->>'username') DESC, u.username LIMIT 1) AS account"
            " FROM audit_logs a WHERE a.action = 'auth.login_failed'"
            " AND a.created_at > to_timestamp(:since) AND a.created_at <= to_timestamp(:now))"
            " SELECT CASE WHEN account IS NOT NULL THEN 'user:' || lower(account)"
            "             ELSE 'ip:' || COALESCE(NULLIF(ip, ''), 'unknown') END AS dim,"
            " min(account) AS account, count(*) AS n, max(created_at) AS last_at,"
            " (array_agg(reason ORDER BY created_at DESC))[1] AS last_reason,"
            " (array_agg(method ORDER BY created_at DESC))[1] AS last_method,"
            " bool_or(reason ILIKE '%temporarily locked%' OR reason ILIKE '%account locked%') AS locked,"
            " count(DISTINCT lower(typed)) AS names, count(DISTINCT ip) AS sources"
            " FROM f GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT :cap"),
            {"since": now - LOGIN_WINDOW_S, "now": now, "cap": LOGIN_MAX_DIMENSIONS})).mappings().all()
        out = [login_reading(dict(r)) for r in rows]
        seen = {r.dimension for r in out}
        for o in (await db.execute(text(
                "SELECT metadata->>'dimension' AS dim, max(metadata->>'dimension_label') AS label"
                " FROM alerts WHERE status IN ('active', 'acknowledged') AND metadata->>'source' = :src"
                " AND metadata->>'metric' = :metric GROUP BY 1"), {"src": SOURCE, "metric": LOGIN})).all():
            if o.dim and o.dim not in seen:
                seen.add(o.dim)
                out.append(Reading(0.0, dimension=o.dim, label=o.label or login_label(o.dim),
                                   facts={"window_s": LOGIN_WINDOW_S,
                                          "kind": "account" if o.dim.startswith("user:") else "address"}))
        return out

    async def admin_changes(self, db: AsyncSession, now: float) -> list[Reading]:
        """Security-relevant administrative changes (ADMIN_ACTIONS) in the last
        ADMIN_WINDOW_S, and when the newest one happened."""
        params = {"actions": list(ADMIN_ACTIONS), "now": now}
        rows = (await db.execute(text(
            "SELECT EXTRACT(EPOCH FROM created_at) AS at, action, actor_username AS actor,"
            " resource_id, metadata FROM audit_logs"
            f" WHERE {ADMIN_FILTER_SQL} AND created_at > to_timestamp(:since)"
            " AND created_at <= to_timestamp(:now) ORDER BY created_at DESC"),
            {**params, "since": now - ADMIN_WINDOW_S})).mappings().all()
        last = (await db.execute(text(
            "SELECT EXTRACT(EPOCH FROM max(created_at)) FROM audit_logs"
            f" WHERE {ADMIN_FILTER_SQL} AND created_at > to_timestamp(:since)"
            " AND created_at <= to_timestamp(:now)"),
            {**params, "since": now - ADMIN_LOOKBACK_S})).scalar()
        return [admin_reading([dict(r) for r in rows], float(last) if last is not None else None, now)]

    async def reports(self, db: AsyncSession, now: float) -> list[Reading]:
        """Every enabled report schedule and its latest finished run."""
        out = []
        for s in (await db.execute(text(
                "SELECT s.id::text AS id, s.name, r.id AS run_id, r.status, r.error, r.notes, r.generated_at"
                " FROM report_schedules s LEFT JOIN LATERAL ("
                "  SELECT id, status, error, notes, generated_at FROM report_runs"
                "  WHERE schedule_id = s.id AND status IN ('success', 'partial', 'failed', 'skipped')"
                "    AND next_retry_at IS NULL AND generated_at <= to_timestamp(:now)"
                "  ORDER BY generated_at DESC LIMIT 1) r ON TRUE"
                " WHERE s.enabled ORDER BY s.name, s.id"), {"now": now})).mappings().all():
            run = ({"id": s["run_id"], "status": s["status"], "error": s["error"], "notes": s["notes"],
                    "generated_at": s["generated_at"]} if s["run_id"] else None)
            out.append(report_reading({"id": s["id"], "name": s["name"]}, run))
        return out


_COLLECTORS = {
    DISK: "disk", DB_DOWN: "databases", BACKUP: "backup", UPDATE: "update",
    LICENSE: "license", TIME: "time_sync", DELIVERY: "deliveries", SENSOR: "sensors",
    LOGIN: "login_failures", ADMIN: "admin_changes", REPORT: "reports",
}


async def collect(db: AsyncSession, metrics, now: float, sources: Sources) -> dict[str, Optional[list]]:
    """{metric: readings, or None when its source failed this pass}."""
    out: dict[str, Optional[list]] = {}
    for metric in sorted(set(metrics)):
        try:
            async with db.begin_nested():
                out[metric] = await getattr(sources, _COLLECTORS[metric])(db, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("appliance alert: %s source unavailable this pass", metric)
            out[metric] = None
    return out


# ─── Conditions and holds (pure) ─────────────────────────────────────────────

def condition(rule) -> dict:
    """The rule's one condition, with any explicit reset_threshold."""
    conds = rule.conditions
    if isinstance(conds, str):
        conds = json.loads(conds)
    c = dict(conds[0]) if conds else {}
    return {"metric": rule.metric,
            "operator": ap.normalize_operator(rule.operator or c.get("operator")),
            "threshold": float(rule.threshold if rule.threshold is not None else c.get("threshold") or 0),
            "reset_threshold": c.get("reset_threshold")}


def reset_level(cond: dict) -> float:
    """The level an open incident must clear: explicit, automatic (2 points for
    the disk) or the trigger threshold itself."""
    if cond.get("reset_threshold") is not None:
        return float(cond["reset_threshold"])
    thr = float(cond["threshold"])
    if cond["metric"] in AUTO_CLEAR:
        if cond["operator"] in _HIGH_OPS and thr - AUTO_CLEAR_POINTS >= 0:
            return thr - AUTO_CLEAR_POINTS
        if cond["operator"] in _LOW_OPS and thr + AUTO_CLEAR_POINTS <= 100:
            return thr + AUTO_CLEAR_POINTS
    return thr


def breaches(cond: dict, value: Optional[float]) -> Optional[bool]:
    if value is None:
        return None
    fn = _OPS.get(cond["operator"])
    return bool(fn and fn(float(value), float(cond["threshold"])))


def clears(cond: dict, value: Optional[float]) -> Optional[bool]:
    """Healthy side of the clear level (never true while still breaching)."""
    if value is None:
        return None
    if breaches(cond, value):
        return False
    level = reset_level(cond)
    op = cond["operator"]
    if op == ">":
        return value <= level
    if op == ">=":
        return value < level
    if op == "<":
        return value >= level
    if op == "<=":
        return value > level
    return True


def clear_hold_seconds(clear_duration) -> int:
    return DEFAULT_CLEAR_S if clear_duration is None else max(0, int(clear_duration))


@dataclass(frozen=True)
class Hold:
    breach_since: Optional[float] = None
    clear_since: Optional[float] = None
    last_seen: Optional[float] = None


def step(hold: Hold, *, now: float, value: Optional[float], breach: Optional[bool],
         cleared: Optional[bool], open_: bool, hold_s: int, clear_s: int) -> tuple[Hold, str]:
    """Advance one rule + dimension by one reading: ("raise" | "resolve" | "none").

    Missing readings, and a gap between readings longer than MAX_GAP_S, restart
    both holds: unknown time is neither breach time nor clear time."""
    h = hold
    if h.last_seen is not None and now - h.last_seen > MAX_GAP_S:
        h = replace(h, breach_since=None, clear_since=None)
    if value is None:
        return replace(h, clear_since=None, breach_since=h.breach_since if open_ else None), "none"
    h = replace(h, last_seen=now)
    if not open_:
        if not breach:
            return replace(h, breach_since=None, clear_since=None), "none"
        since = h.breach_since if h.breach_since is not None else now
        h = replace(h, breach_since=since, clear_since=None)
        return h, ("raise" if now - since >= hold_s else "none")
    if not cleared:
        return replace(h, clear_since=None), "none"
    since = h.clear_since if h.clear_since is not None else now
    if now - since >= clear_s:
        return Hold(last_seen=now), "resolve"
    return replace(h, clear_since=since), "none"


# ─── Wording ─────────────────────────────────────────────────────────────────

def _days(value: float) -> str:
    n = int(math.floor(abs(value)))
    return f"{n} day{'' if n == 1 else 's'}"


def _hours(value: float) -> str:
    n = int(round(value))
    return f"{n} hour{'' if n == 1 else 's'}"


def _date(epoch: Optional[float], branding=None) -> str:
    if epoch is None:
        return ""
    at = datetime.fromtimestamp(epoch, timezone.utc)
    if branding is not None:
        try:
            return branding.fmt_date(at)
        except Exception:  # noqa: BLE001 - a date is decoration, never a failure
            pass
    return at.strftime("%d %b %Y")


def subject(rule, reading: Reading) -> str:
    """Who the alert is about, as the alert list and messages name it."""
    if rule.metric == SENSOR:
        name = reading.facts.get("sensor_name") or reading.label
        return f"Remote sensor {name}"
    return APPLIANCE


def sentence(rule, reading: Reading, *, recovery: bool = False, branding=None,
             now: Optional[float] = None) -> str:
    """The one sentence an operator reads, e.g.
    "ZenPlus appliance: data disk at 91% (limit 85%)"."""
    cond = condition(rule)
    metric, value, f = rule.metric, reading.value, reading.facts
    limit = ap.format_threshold(metric, cond["threshold"], cond["operator"], value)
    shown = ap.format_reading(metric, value, cond["operator"], cond["threshold"]) if value is not None else ""
    lead = f"{APPLIANCE}: "
    if metric == DISK:
        if recovery:
            return f"{lead}data disk back to {shown} (limit {limit})"
        return f"{lead}data disk at {shown} (limit {limit})"
    if metric == DB_DOWN:
        name = reading.label or "a database"
        if recovery:
            return f"{lead}{name} database is answering again"
        err = f" ({f['error']})" if f.get("error") else ""
        return f"{lead}{name} database is not answering{err}"
    if metric == BACKUP:
        if recovery:
            if not f.get("scheduled", True):
                return f"{lead}scheduled backups are turned off; no backup is due"
            return f"{lead}scheduled backups are completing again"
        if f.get("reason") == "overdue":
            return (f"{lead}no scheduled backup has completed in {_hours(f.get('hours_since') or 0)}"
                    f" (expected {f.get('frequency', 'daily')})")
        err = f" (error: {f['error']})" if f.get("error") else ""
        return f"{lead}the last scheduled backup failed{err}"
    if metric == UPDATE:
        version = f.get("version") or "the new version"
        if recovery:
            return f"{lead}update to {version} completed"
        how = "failed and was rolled back" if f.get("status") == "rolled_back" else "failed"
        err = f" (error: {f['error']})" if f.get("error") else ""
        return f"{lead}update to {version} {how}{err}"
    if metric == LICENSE:
        on = _date(f.get("expires_at"), branding)
        if f.get("expires_at") is None:
            if f.get("expired"):
                return f"{lead}licence has expired (limit {limit})"
            return f"{lead}licence has no expiry date"
        if value is not None and value < 0:
            text_ = f"{lead}licence expired {_days(value)} ago, on {on}"
        elif value is not None and value < 1:
            text_ = f"{lead}licence expires within a day, on {on}"
        else:
            text_ = f"{lead}licence expires in {_days(value or 0)}, on {on}"
        if recovery:
            return f"{lead}licence now valid until {on} ({_days(value or 0)} left)"
        return f"{text_} (limit {limit})"
    if metric == TIME:
        if recovery:
            return f"{lead}clock is synchronised again"
        service = f.get("ntp_service")
        why = " (NTP service inactive)" if service == "inactive" else ""
        return f"{lead}clock is not synchronised{why}"
    if metric == DELIVERY:
        n = int(value or 0)
        if recovery:
            return f"{lead}{reading.label} is delivering again ({n} failed in the last hour)"
        err = f" (last error: {f['last_error']})" if f.get("last_error") else ""
        return (f"{lead}{reading.label} failed {n} deliver{'y' if n == 1 else 'ies'}"
                f" in the last hour{err}")
    if metric == SENSOR:
        who = subject(rule, reading)
        if recovery:
            return f"{who} is online again"
        seen = f.get("last_heartbeat_at")
        since = f" (last heartbeat {ap.clock(seen, branding, now)})" if seen else ""
        return f"{who} is offline: checks it runs are not being made{since}"
    if metric == LOGIN:
        n = int(value or 0)
        minutes = _minutes(f.get("window_s") or LOGIN_WINDOW_S)
        account = reading.dimension.startswith("user:")
        who_ = reading.label if account else login_label(reading.dimension)
        if recovery:
            return (f"{lead}failed sign-ins for {who_} are below the limit again"
                    f" ({n} in the last {minutes})")
        names = int(f.get("names") or 0)
        tried = f" ({names} names tried)" if not account and names > 1 else ""
        sources = int(f.get("sources") or 0)
        where = f" from {sources} addresses" if account and sources > 1 else ""
        why = LOGIN_REASONS.get(f.get("last_reason") or "", "")
        why = f"; last reason: {why}" if why and why != "other" else ""
        locked = "; the account was locked" if account and f.get("locked") else ""
        return (f"{lead}{n} failed sign-in{'' if n == 1 else 's'} for {who_}{tried}{where}"
                f" in the last {minutes} (limit {limit}{why}){locked}")
    if metric == ADMIN:
        n = int(value or 0)
        if recovery:
            quiet = f.get("quiet_s")
            return (f"{lead}no further administrative changes for {_minutes(quiet)}" if quiet
                    else f"{lead}no further administrative changes")
        events = list(f.get("events") or [])
        shown_ = [f"{e.get('what')} by {e.get('by')}" for e in events[:3]]
        more = n - len(shown_)
        listed = "; ".join(shown_) + (f"; and {more} more" if more > 0 else "")
        minutes = _minutes(f.get("window_s") or ADMIN_WINDOW_S)
        head = f"{lead}{n} administrative change{'' if n == 1 else 's'} in the last {minutes}"
        return f"{head}: {listed}" if listed else head
    if metric == REPORT:
        name = reading.label or "a report schedule"
        if recovery:
            return f"{lead}{name} was delivered again"
        at = f.get("generated_at")
        when = f" (run at {ap.clock(at, branding, now)})" if at else ""
        err = f" (error: {f['error']})" if f.get("error") else ""
        if f.get("status") == "partial":
            return f"{lead}{name} was only partly delivered{when}{err}"
        return f"{lead}{name} failed: nothing was delivered{when}{err}"
    return f"{lead}{ap.metric_noun(metric)} {shown}".rstrip()


def _minutes(seconds) -> str:
    n = max(1, int(round(float(seconds or 0) / 60)))
    return f"{n} minute{'' if n == 1 else 's'}"


# ─── Alert rows ──────────────────────────────────────────────────────────────

_RULE_SQL = (
    "SELECT id, name, metric, operator, threshold, severity, priority, min_duration, clear_duration,"
    " conditions, notify_channels, cooldown, target, recovery_alert,"
    " schedule_start, schedule_end, schedule_days,"
    " email_subject, email_body, sms_template,"
    " recovery_email_subject, recovery_email_body, recovery_sms_template"
    " FROM alert_rules WHERE enabled = true AND metric IN ({metrics}) ORDER BY created_at, id"
)


async def load_rules(db: AsyncSession) -> list:
    metrics = ",".join(f"'{m}'" for m in APPLIANCE_METRICS)
    return (await db.execute(text(_RULE_SQL.format(metrics=metrics)))).all()


def target_matches(rule, reading: Reading) -> bool:
    """A dimensioned rule's optional ``target``: the channel / sensor id or name."""
    target = (rule.target or "").strip().casefold()
    if not target or rule.metric not in DIMENSIONED:
        return True
    name = str(reading.facts.get("sensor_name") or reading.label or "").casefold()
    return target == reading.dimension.casefold() or target == name or name.endswith(f" {target}")


async def _open_alert(db: AsyncSession, rule_id, dimension: str):
    return (await db.execute(text(
        "SELECT id, triggered_at FROM alerts WHERE rule_id = :rid AND status IN ('active','acknowledged')"
        " AND metadata->>'source' = :src AND COALESCE(metadata->>'dimension','') = :dim"
        " ORDER BY triggered_at DESC LIMIT 1"),
        {"rid": str(rule_id), "src": SOURCE, "dim": dimension})).first()


async def _load_hold(db: AsyncSession, rule_id, dimension: str):
    row = (await db.execute(text(
        "SELECT EXTRACT(EPOCH FROM breach_since) AS b, EXTRACT(EPOCH FROM clear_since) AS c,"
        " EXTRACT(EPOCH FROM last_seen_at) AS s, alert_id FROM appliance_alert_state"
        " WHERE rule_id = :rid AND dimension = :dim"), {"rid": str(rule_id), "dim": dimension})).first()
    if not row:
        return Hold(), None
    f = (lambda v: float(v) if v is not None else None)
    return Hold(f(row.b), f(row.c), f(row.s)), (str(row.alert_id) if row.alert_id else None)


async def _save_hold(db: AsyncSession, rule_id, dimension: str, hold: Hold, value, alert_id, now: float):
    await db.execute(text(
        "INSERT INTO appliance_alert_state (rule_id, dimension, breach_since, clear_since, last_seen_at,"
        " last_value, alert_id, updated_at) VALUES (:rid, :dim, to_timestamp(:b), to_timestamp(:c),"
        " to_timestamp(:s), :v, CAST(:aid AS uuid), to_timestamp(:now))"
        " ON CONFLICT (rule_id, dimension) DO UPDATE SET breach_since = EXCLUDED.breach_since,"
        " clear_since = EXCLUDED.clear_since, last_seen_at = EXCLUDED.last_seen_at,"
        " last_value = EXCLUDED.last_value, alert_id = EXCLUDED.alert_id, updated_at = EXCLUDED.updated_at"),
        {"rid": str(rule_id), "dim": dimension, "b": hold.breach_since, "c": hold.clear_since,
         "s": hold.last_seen, "v": value, "aid": alert_id, "now": now})


def _metadata(rule, reading: Reading, cond: dict) -> dict:
    return {
        "source": SOURCE, "rule_id": str(rule.id), "metric": rule.metric,
        "dimension": reading.dimension, "dimension_label": reading.label,
        "dedupe": f"appliance:{rule.id}:{reading.dimension}",
        "entity_name": subject(rule, reading),
        "value": reading.value, "threshold": cond["threshold"],
        "reset_threshold": reset_level(cond), "facts": reading.facts,
    }


async def _raise(db: AsyncSession, rule, reading: Reading, cond: dict, message: str, now: float):
    p_col, p_val, p_params = funnel.priority_insert(
        await funnel.priority_supported(db), rule.priority, rule.severity or "warning")
    meta = {**_metadata(rule, reading, cond), **ns.sending_metadata()}
    return (await db.execute(text(
        f"INSERT INTO alerts (rule_id, sensor_id, status, severity, message, triggered_at, metadata{p_col})"
        f" VALUES (:rid, CAST(:sid AS uuid), 'active', :sev, :msg, :ts, CAST(:meta AS jsonb){p_val})"
        " RETURNING id"),
        {"rid": str(rule.id), "sid": reading.sensor_id, "sev": rule.severity or "warning",
         "msg": message, "ts": datetime.fromtimestamp(now, timezone.utc),
         "meta": json.dumps(meta, default=str), **p_params})).scalar()


async def _resolve(db: AsyncSession, alert_id, now: float, **extra) -> int:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts,"
        " metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb)"
        " WHERE id = :id AND status IN ('active','acknowledged')"),
        {"id": alert_id, "ts": datetime.fromtimestamp(now, timezone.utc),
         "m": json.dumps({"resolved_by": "appliance_evaluator",
                          "resolved_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                          **extra}, default=str)})
    return res.rowcount or 0


async def _trigger_allowed(db: AsyncSession, rule, dimension: str, alert_id, now: float) -> bool:
    """Outside the rule's cooldown (an earlier notified incident of the same
    rule + dimension) and inside its schedule window."""
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    cooldown = int(rule.cooldown or 0)
    if cooldown > 0:
        prior = (await db.execute(text(
            "SELECT 1 FROM alerts WHERE rule_id = :rid AND id <> :aid AND metadata->>'source' = :src"
            " AND COALESCE(metadata->>'dimension','') = :dim AND metadata->>'notified' = 'true'"
            " AND triggered_at > to_timestamp(:since) LIMIT 1"),
            {"rid": str(rule.id), "aid": alert_id, "src": SOURCE, "dim": dimension,
             "since": now - cooldown})).first()
        if prior:
            return False
    tz = await get_configured_timezone(db)
    return notifications_allowed(rule.schedule_start, rule.schedule_end, rule.schedule_days, tz)


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


async def _notify(db: AsyncSession, rule, reading: Reading, *, alert_id, first_seen,
                  now: float, recovery: bool = False, duration: str = "", branding=None) -> bool:
    """Send the trigger or the all-clear to the rule's channels (and, through
    the escalation policy, its route). True when dispatched."""
    from app.services import host_alert_service as has
    cond = condition(rule)
    sev = rule.severity or "warning"
    who = subject(rule, reading)
    said = sentence(rule, reading, recovery=recovery, branding=branding, now=now)
    numeric = "state" not in ap.METRICS.get(rule.metric, {})
    v = ap.rule_phrasing(rule, hostname=who, is_recovery=recovery, duration=duration,
                         value=reading.value if numeric else None)
    at = datetime.fromtimestamp(now, timezone.utc)
    v.update({"event_sentence": said.rstrip(".") + ".", "rule_name": rule.name or "Alert",
              "hostname": who, "ip_address": "", "severity": sev.upper(),
              "status": "RESOLVED" if recovery else "ALERT",
              "timestamp": at.strftime("%H:%M UTC on %d %b %Y")})
    incident = ap.incident_context(alert_id=alert_id, first_seen=first_seen, now=at,
                                   lifecycle="recovered" if recovery else "new") if alert_id else {}
    v.update(incident)
    v.update(ap.place_variables())          # the appliance itself has no site
    # A rule's own templates win; NULL (or the wizard's old pre-filled text)
    # means the built-in wording, whose {event_sentence} is the sentence above.
    defaults = ap.default_templates("device")
    prefix = "recovery_" if recovery else ""
    subject_tpl = ap.effective_template(getattr(rule, prefix + "email_subject", None),
                                        defaults[prefix + "email_subject"])
    body_tpl = ap.effective_template(getattr(rule, prefix + "email_body", None),
                                     defaults[prefix + "email_body"])
    sms_key = "recovery_sms_template" if recovery else "sms_template"
    sms_tpl = ap.effective_template(getattr(rule, sms_key, None), defaults[sms_key])
    dimension_row = {DELIVERY: "Channel", SENSOR: "Sensor", DB_DOWN: "Database",
                     LOGIN: "Sign-ins", REPORT: "Report schedule"}.get(rule.metric)
    channels = rule.notify_channels
    if isinstance(channels, str):
        channels = json.loads(channels)
    await has.dispatch_to_channels(db, list(channels or []), {
        "subject": _render(subject_tpl, v),
        "body": _render(body_tpl, v),
        "message": _render(sms_tpl, v),
        "hostname": who, "ip_address": "",
        "status": "RESOLVED" if recovery else "ALERT", "severity": sev,
        "resolved": recovery,
        "headline_metric": (ap.headline_metric(rule.metric, reading.value, cond["operator"],
                                               cond["threshold"])
                            if numeric and reading.value is not None else None),
        "details": [("Alert rule", rule.name),
                    ("Appliance check", ap.metric_noun(rule.metric)),
                    (dimension_row, reading.label) if dimension_row else ("Appliance", APPLIANCE),
                    ("Condition", v.get("condition_label")),
                    ("Active for", duration if recovery else None)],
        "triggered_at": at.isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": "recovery" if recovery else "trigger",
        "is_recovery": recovery,
        **funnel.notification_fields(sev, rule.priority, trigger=not recovery),
        **incident,
    })
    return True


# ─── Event-like metrics ──────────────────────────────────────────────────────

def event_clear_seconds(clear_duration) -> int:
    return EVENT_DEFAULT_CLEAR_S if clear_duration is None else max(0, int(clear_duration))


def event_view(rule, reading: Reading, *, now: float, covered: Optional[float]):
    """(reading, breach, cleared) of an event-like metric.

    The count leaves out events an earlier incident of this rule + dimension
    already reported (at or before ``covered``, its newest event), so a burst
    pages once, and one that a person resolved by hand is not paged again.
    An open incident clears once no event happened for the clear hold."""
    f = dict(reading.facts)
    times = [float(t) for t in f.get("event_times") or []]
    value = reading.value
    if covered is not None:
        times = [t for t in times if t > covered]
        f["events"] = [e for e in f.get("events") or [] if float(e.get("at") or 0) > covered]
        value = float(len(times))
    f["event_times"] = times
    quiet = event_clear_seconds(rule.clear_duration)
    f["quiet_s"] = quiet
    last = f.get("last_event_at")
    cleared = last is None or now - float(last) >= quiet
    reading = replace(reading, value=value, facts=f)
    return reading, breaches(condition(rule), value), cleared


async def _covered_until(db: AsyncSession, rule_id, dimension: str) -> Optional[float]:
    """The newest event an earlier (closed) incident of this rule + dimension reported."""
    v = (await db.execute(text(
        "SELECT max(CASE WHEN jsonb_typeof(metadata->'facts'->'last_event_at') = 'number'"
        " THEN (metadata->'facts'->>'last_event_at')::float8 END) FROM alerts"
        " WHERE rule_id = :rid AND metadata->>'source' = :src"
        " AND COALESCE(metadata->>'dimension','') = :dim AND status NOT IN ('active','acknowledged')"),
        {"rid": str(rule_id), "src": SOURCE, "dim": dimension})).scalar()
    return float(v) if v is not None else None


# ─── The pass ────────────────────────────────────────────────────────────────

async def _evaluate_one(db: AsyncSession, rule, reading: Reading, *, now: float,
                        branding) -> tuple[int, int]:
    """Advance one rule + dimension. (raised, resolved)."""
    dim = reading.dimension
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"appliance:{rule.id}:{dim}"})
    cond = condition(rule)
    hold, known_alert = await _load_hold(db, rule.id, dim)
    opened = await _open_alert(db, rule.id, dim)
    value = reading.value
    if opened is None and known_alert is not None:
        # Resolved by hand: a new incident needs the breach hold again, counted
        # from the next pass, never an instant re-page in this one.
        await _save_hold(db, rule.id, dim, replace(hold, breach_since=None, clear_since=None,
                                                   last_seen=now if value is not None else hold.last_seen),
                         value, None, now)
        await db.commit()
        return 0, 0
    if rule.metric in EVENT_METRICS and value is not None:
        reading, breach, cleared = event_view(rule, reading, now=now,
                                              covered=await _covered_until(db, rule.id, dim))
        value, clear_s = reading.value, 0
    else:
        breach, cleared = breaches(cond, value), clears(cond, value)
        clear_s = clear_hold_seconds(rule.clear_duration)
    hold, action = step(hold, now=now, value=value, breach=breach, cleared=cleared,
                        open_=opened is not None, hold_s=int(rule.min_duration or 0), clear_s=clear_s)
    raised = resolved = 0
    if action == "raise":
        message = sentence(rule, reading, branding=branding, now=now)
        alert_id = await _raise(db, rule, reading, cond, message, now)
        await _save_hold(db, rule.id, dim, hold, value, str(alert_id), now)
        await db.commit()
        raised = 1
        sent = False
        if await _trigger_allowed(db, rule, dim, alert_id, now):
            sent = await _notify(db, rule, reading, alert_id=alert_id,
                                 first_seen=datetime.fromtimestamp(now, timezone.utc),
                                 now=now, branding=branding)
        await ns.stamp(db, alert_id, sent)
        await db.commit()
        return raised, resolved
    if opened is None:
        await _save_hold(db, rule.id, dim, hold, value, None, now)
        await db.commit()
        return 0, 0
    alert_id, started_at = opened
    if action == "resolve":
        notified = await ns.was_notified(db, alert_id)
        resolved = await _resolve(db, alert_id, now, recovered_value=value)
        await _save_hold(db, rule.id, dim, hold, value, None, now)
        await db.commit()
        if resolved and notified and rule.recovery_alert:
            await _notify(db, rule, reading, alert_id=alert_id, first_seen=started_at, now=now,
                          recovery=True, branding=branding,
                          duration=ap.duration_between(started_at,
                                                       datetime.fromtimestamp(now, timezone.utc)))
        return 0, resolved
    # Still open: keep the current reading on the incident; a trigger that
    # quiet hours or the cooldown held back goes out once allowed.
    await _save_hold(db, rule.id, dim, hold, value, str(alert_id), now)
    if value is not None:
        await db.execute(text(
            "UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :id"),
            {"id": alert_id, "m": json.dumps({"value": value, "facts": reading.facts}, default=str)})
    await db.commit()
    if breaches(cond, value) and await ns.is_pending(db, alert_id) \
            and await _trigger_allowed(db, rule, dim, alert_id, now) and await ns.claim_pending(db, alert_id):
        await db.commit()
        sent = await _notify(db, rule, reading, alert_id=alert_id, first_seen=started_at,
                             now=now, branding=branding)
        await ns.stamp(db, alert_id, sent)
        await db.commit()
    return 0, 0


async def _close_gone(db: AsyncSession, rule, present: set[str], now: float) -> int:
    """Close incidents (no recovery notice) and forget holds of dimensions that
    no longer exist: a deleted channel, a removed or disabled sensor."""
    rows = (await db.execute(text(
        "SELECT id, COALESCE(metadata->>'dimension','') AS dim FROM alerts WHERE rule_id = :rid"
        " AND status IN ('active','acknowledged') AND metadata->>'source' = :src"),
        {"rid": str(rule.id), "src": SOURCE})).all()
    closed = 0
    reason = {DELIVERY: "the channel was deleted",
              SENSOR: "the sensor was removed or disabled",
              REPORT: "the report schedule was deleted or turned off",
              LOGIN: "no failed sign-ins remain for it"}.get(rule.metric, "the dimension is gone")
    for r in rows:
        if r.dim not in present:
            closed += await _resolve(db, r.id, now, resolved_reason=reason)
    await db.execute(text(
        "DELETE FROM appliance_alert_state WHERE rule_id = :rid AND NOT (dimension = ANY(:dims))"),
        {"rid": str(rule.id), "dims": sorted(present)})
    await db.commit()
    return closed


async def evaluate_appliance_rules(db: AsyncSession, *, now: Optional[float] = None,
                                   sources: Optional[Sources] = None) -> dict[str, int]:
    """One pass over every enabled appliance rule. ``now`` (epoch seconds) and
    ``sources`` are for tests."""
    rules = await load_rules(db)
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    readings = await collect(db, {r.metric for r in rules}, now, sources or Sources())
    from app.services.regional_time import notification_branding
    branding = await notification_branding(db)
    await db.commit()
    raised = resolved = 0
    for rule in rules:
        batch = readings.get(rule.metric)
        if batch is None:
            continue        # source unavailable: missing data, nothing changes
        present: set[str] = set()
        for reading in batch:
            if not target_matches(rule, reading):
                continue
            present.add(reading.dimension)
            try:
                r, s = await _evaluate_one(db, rule, reading, now=now, branding=branding)
            except asyncio.CancelledError:
                raise
            except Exception:
                await db.rollback()
                logger.exception("appliance alert: rule %s / %s failed", rule.id, reading.dimension or "-")
                continue
            raised += r
            resolved += s
        if rule.metric in DIMENSIONED:
            resolved += await _close_gone(db, rule, present, now)
    if raised or resolved:
        logger.info("appliance alert eval: %d rules, %d raised, %d resolved", len(rules), raised, resolved)
    return {"rules": len(rules), "raised": raised, "resolved": resolved}


async def run_appliance_alert_pass(session_factory=None):
    """One evaluation pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import APPLIANCE_ALERT_PASS, run_pass
    return await run_pass(APPLIANCE_ALERT_PASS, evaluate_appliance_rules, session_factory)


async def appliance_alert_evaluator_loop() -> None:
    await asyncio.sleep(60)  # let the app, the watchdog and the sensor sweep settle
    while True:
        try:
            await run_appliance_alert_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("appliance alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)


def preview_variables(rule: dict, *, is_recovery: bool, branding=None) -> dict:
    """Sample wording for the rule editor's preview / test send."""
    from types import SimpleNamespace
    metric = rule.get("metric")
    samples = {
        DISK: Reading(91.0, label="data disk", facts={"mount": "/ /data"}),
        DB_DOWN: Reading(1.0, dimension="clickhouse", label="ClickHouse", facts={"error": "connection"}),
        BACKUP: Reading(1.0, facts={"scheduled": True, "reason": "failed", "error": "No space left on device"}),
        UPDATE: Reading(1.0, facts={"version": "1.25.18", "status": "rolled_back",
                                    "error": "schema gate failed"}),
        LICENSE: Reading(12.0, facts={"expires_at": datetime.now(timezone.utc).timestamp() + 12.5 * 86400}),
        TIME: Reading(1.0, facts={"ntp_service": "inactive"}),
        DELIVERY: Reading(5.0, dimension="c", label="email channel NOC mail",
                          facts={"last_error": "connection: Connection refused"}),
        SENSOR: Reading(1.0, dimension="s", label="remote sensor branch-01",
                        facts={"sensor_name": "branch-01",
                               "last_heartbeat_at": datetime.now(timezone.utc).timestamp() - 600}),
        LOGIN: Reading(12.0, dimension="user:admin", label="user admin",
                       facts={"window_s": LOGIN_WINDOW_S, "kind": "account", "last_reason": "invalid",
                              "locked": True, "sources": 1, "names": 1}),
        ADMIN: Reading(2.0, facts={"window_s": ADMIN_WINDOW_S, "quiet_s": EVENT_DEFAULT_CLEAR_S, "events": [
            {"what": "user jsmith role set to admin", "by": "admin"},
            {"what": "alert rule Device Down turned off", "by": "admin"}]}),
        REPORT: Reading(1.0, dimension="s", label='report schedule "Weekly SLA"',
                        facts={"status": "failed", "error": "SMTP 550 mailbox unavailable"}),
    }
    reading = samples.get(metric)
    if reading is None:
        return {}
    if is_recovery and metric in (DISK, DELIVERY, LICENSE, LOGIN, ADMIN):
        reading = replace(reading, value={DISK: 72.0, DELIVERY: 0.0, LICENSE: 365.0,
                                          LOGIN: 0.0, ADMIN: 0.0}[metric])
    ns_rule = SimpleNamespace(**{"conditions": None, "target": None, **rule})
    who = subject(ns_rule, reading)
    return {"hostname": who, "ip_address": "",
            "event_sentence": sentence(ns_rule, reading, recovery=is_recovery, branding=branding) + "."}
