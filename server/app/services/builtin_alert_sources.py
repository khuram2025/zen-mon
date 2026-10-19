"""Built-in (rule-less) alert families and how they are routed (1.24.21).

Some alerts are raised by the platform itself rather than by an alert rule:
an agent that stopped reporting, a remote sensor that went offline, the fixed
server health thresholds, software baseline drift and the configuration-backup
(NCM) events. They have no ``alert_rules`` row, so before this release the
escalation sweep (which joined the rule) never saw them and the coverage page
still reported every priority as covered.

This registry names each family, how a row is recognised (``metadata.source``
or the NCM event kind; the row never has a rule), and the incident priority
stamped when it is raised:

=====================  ==================================  =========
family                 recognised by                       priority
=====================  ==================================  =========
``agent_offline``      server alert, source agent_offline  P2
``sensor_offline``     sensor alert, source sensor_offline P2
``sensor_degraded``    sensor alert, source sensor_degraded P3
``ncm_backup_failed``  NCM event backup.failed             P3
``ncm_config_changed`` NCM event config.changed            P4
``baseline_drift``     server alert, source baseline       P4
``health_threshold``   server alert, source health_threshold  critical P3 / warning P4
=====================  ==================================  =========

Health thresholds rank lower than their severity would derive on purpose:
they are hard-coded, unconfirmed and recomputed on every metric upload, and
the rule-based server alerts (host_* rules) already page for the same
condition with a breach hold.

Routing: a built-in incident is routed only by the default escalation policy
of its priority (never a rule policy or legacy tiers — there is no rule), and
only while its family is enabled in the ``builtin_alert_routing`` system
setting (``{"enabled_families": [...]}``). Every family is enabled by default
except ``health_threshold``, which duplicates the server alert rules, and
``ncm_config_changed``, whose incidents never resolve (one per change; the
device's NCM channels are notified regardless); health
incidents are additionally routed only once they have been open for
:data:`HEALTH_MIN_AGE_S` so a reading that flaps across the fixed threshold
does not page.

Pure helpers plus two small settings accessors; the SQL fragments are built
from this fixed registry only (no user input reaches them).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import text

from app.services import alert_priority as prio

SETTING_KEY = "builtin_alert_routing"

# Health threshold incidents are routed only once they have been open this long.
HEALTH_MIN_AGE_S = 300


@dataclass(frozen=True)
class BuiltinSource:
    key: str
    label: str
    description: str
    # The family's priority; for severity-ranked families the most urgent one.
    priority: str
    # SQL predicate on alert alias ``{a}`` (rule-less rows only).
    match_sql: str
    # severity -> priority for families ranked by severity (else None).
    severity_priorities: Optional[dict[str, str]] = None
    default_enabled: bool = True
    min_age_s: int = 0
    # Why the default is what it is (shown beside the toggle).
    note: str = ""
    # Channels already notified by the source itself ("ncm": the device's NCM
    # channels), so the policy's step 0 does not page them twice.
    base_channels: Optional[str] = None
    _meta: tuple = field(default=(), repr=False)

    def priority_for(self, severity: Any = None) -> str:
        if self.severity_priorities:
            sev = str(severity or "").strip().lower()
            return self.severity_priorities.get(sev, self.severity_priorities.get("warning", self.priority))
        return self.priority

    @property
    def priorities(self) -> list[str]:
        if not self.severity_priorities:
            return [self.priority]
        return sorted(set(self.severity_priorities.values()), key=prio.priority_rank)

    def variants(self) -> list[tuple[Optional[str], str]]:
        """(severity, priority) pairs, most urgent first (severity None when
        the family has one priority)."""
        if not self.severity_priorities:
            return [(None, self.priority)]
        return sorted(self.severity_priorities.items(), key=lambda kv: prio.priority_rank(kv[1]))


def _meta_eq(a: str, key: str, value: str) -> str:
    return f"{a}.metadata->>'{key}' = '{value}'"


SOURCES: tuple[BuiltinSource, ...] = (
    BuiltinSource(
        key="agent_offline", label="Agent offline",
        description="A server agent stopped sending heartbeats for 10 minutes.",
        priority="P2",
        match_sql="{a}.server_id IS NOT NULL AND " + _meta_eq("{a}", "source", "agent_offline"),
        _meta=(("source", "agent_offline"),)),
    BuiltinSource(
        key="sensor_offline", label="Remote sensor offline",
        description="A remote sensor stopped sending heartbeats; its targets may be stale.",
        priority="P2",
        match_sql="{a}.sensor_id IS NOT NULL AND " + _meta_eq("{a}", "source", "sensor_offline"),
        _meta=(("source", "sensor_offline"),)),
    BuiltinSource(
        key="sensor_degraded", label="Remote sensor degraded",
        description="A remote sensor's heartbeat is late or it reports a degraded state.",
        priority="P3",
        match_sql="{a}.sensor_id IS NOT NULL AND " + _meta_eq("{a}", "source", "sensor_degraded"),
        _meta=(("source", "sensor_degraded"),)),
    BuiltinSource(
        key="ncm_backup_failed", label="Configuration backup failed",
        description="A device's configuration backup failed (one incident per device until the next good backup).",
        priority="P3",
        match_sql=("{a}.device_id IS NOT NULL AND " + _meta_eq("{a}", "ncm", "true")
                   + " AND " + _meta_eq("{a}", "ncm_event", "backup.failed")),
        base_channels="ncm",
        _meta=(("ncm", "true"), ("ncm_event", "backup.failed"))),
    BuiltinSource(
        key="ncm_config_changed", label="Configuration changed",
        description="A device's running configuration changed (devices with change alerts on).",
        priority="P4",
        match_sql=("{a}.device_id IS NOT NULL AND " + _meta_eq("{a}", "ncm", "true")
                   + " AND " + _meta_eq("{a}", "ncm_event", "config.changed")),
        base_channels="ncm",
        # One incident per change that nothing ever resolves: routed, it would
        # keep escalating until someone acknowledged every change.
        default_enabled=False,
        note=("Off by default: each change opens an incident that never resolves on its own, so a "
              "routed change would keep escalating until acknowledged. The device's NCM channels "
              "are still notified."),
        _meta=(("ncm", "true"), ("ncm_event", "config.changed"))),
    BuiltinSource(
        key="baseline_drift", label="Software baseline drift",
        description="A server is missing required software, runs an outdated version or has prohibited software.",
        priority="P4",
        match_sql="{a}.server_id IS NOT NULL AND " + _meta_eq("{a}", "source", "baseline"),
        _meta=(("source", "baseline"),)),
    BuiltinSource(
        key="health_threshold", label="Server health threshold",
        description="The fixed server health thresholds (CPU 90/98%, memory 90/97%, filesystem 85/95%).",
        priority="P3", severity_priorities={"critical": "P3", "warning": "P4", "info": "P4"},
        match_sql="{a}.server_id IS NOT NULL AND " + _meta_eq("{a}", "source", "health_threshold"),
        default_enabled=False, min_age_s=HEALTH_MIN_AGE_S,
        note=("Off by default: these duplicate the server alert rules and change with every metric "
              "upload. When enabled they are routed only after 5 minutes open."),
        _meta=(("source", "health_threshold"),)),
)

BY_KEY: dict[str, BuiltinSource] = {s.key: s for s in SOURCES}
KEYS: tuple[str, ...] = tuple(BY_KEY)
DEFAULT_ENABLED: tuple[str, ...] = tuple(s.key for s in SOURCES if s.default_enabled)


# ── Recognition ──────────────────────────────────────────────────────────────

def family_of(meta: Optional[dict], *, rule_id: Any = None) -> Optional[str]:
    """The built-in family of an alert row (Python twin of :func:`family_sql`).
    A row linked to an alert rule is never built-in."""
    if rule_id:
        return None
    m = meta or {}
    for s in SOURCES:
        if all(str(m.get(k, "")).lower() == v for k, v in s._meta):
            return s.key
    return None


def source(key: Any) -> Optional[BuiltinSource]:
    return BY_KEY.get(str(key)) if key else None


def priority_for(key: str, severity: Any = None) -> str:
    """The priority a new built-in alert of family ``key`` is stamped with
    (unknown family: derived from severity)."""
    s = BY_KEY.get(key)
    return s.priority_for(severity) if s else prio.severity_priority(severity)


def label(key: Any) -> Optional[str]:
    s = source(key)
    return s.label if s else None


def family_sql(a: str = "a") -> str:
    """SQL: the family key of alert ``a`` (NULL when it has a rule or is not
    built-in)."""
    whens = " ".join(f"WHEN {s.match_sql.format(a=a)} THEN '{s.key}'" for s in SOURCES)
    return f"(CASE WHEN {a}.rule_id IS NOT NULL THEN NULL {whens} END)"


def label_sql(a: str = "a") -> str:
    """SQL: the family label of alert ``a`` (NULL when not built-in)."""
    whens = " ".join(f"WHEN {s.match_sql.format(a=a)} THEN '{s.label}'" for s in SOURCES)
    return f"(CASE WHEN {a}.rule_id IS NOT NULL THEN NULL {whens} END)"


def routable_sql(a: str, enabled: Iterable[str], since: Optional[dict] = None) -> str:
    """SQL: alert ``a`` is a built-in incident the escalation sweep may route
    now (family enabled, old enough for families with a minimum age, and
    raised after its family's routing was enabled — ``since`` maps family ->
    ISO time — so enabling a family or upgrading never pages the backlog of
    incidents that were already open)."""
    parts = []
    for key in clean_families(enabled):
        s = BY_KEY[key]
        cond = s.match_sql.format(a=a)
        if s.min_age_s:
            cond += f" AND {a}.triggered_at <= now() - interval '{int(s.min_age_s)} seconds'"
        cutoff = _iso_or_none((since or {}).get(key))
        if cutoff:
            cond += f" AND {a}.triggered_at >= '{cutoff}'::timestamptz"
        parts.append(f"({cond})")
    if not parts:
        return "FALSE"
    return f"({a}.rule_id IS NULL AND ({' OR '.join(parts)}))"


def base_channels_sql(a: str = "a") -> str:
    """SQL (jsonb array): channels the source itself already notified for
    alert ``a`` (the device's NCM channels for NCM events), else NULL."""
    keys = [f"'{s.key}'" for s in SOURCES if s.base_channels == "ncm"]
    return (f"(CASE WHEN {family_sql(a)} IN ({', '.join(keys)}) THEN"
            f" (SELECT to_jsonb(_n.notify_channels) FROM device_ncm _n WHERE _n.device_id = {a}.device_id)"
            f" END)")


def notifies_own_recovery_sql(a: str = "a") -> str:
    """SQL: the source sends its own recovery to its base channels (the NCM
    backup incident announces 'backup recovered'), so the all-clear leaves
    those channels out like a rule's recovery_alert."""
    return f"({family_sql(a)} = 'ncm_backup_failed')"


# ── Setting ──────────────────────────────────────────────────────────────────

def clean_families(raw: Any) -> list[str]:
    """Known family keys in registry order, each once."""
    if raw is None:
        return []
    given = {str(k) for k in raw} if isinstance(raw, (list, tuple, set, frozenset)) else set()
    return [k for k in KEYS if k in given]


def unknown_families(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return [str(k) for k in raw if str(k) not in BY_KEY]


async def enabled_families(db) -> list[str]:
    """The enabled families (the default when the setting was never saved)."""
    try:
        row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                {"k": SETTING_KEY})).first()
    except Exception:
        await db.rollback()
        return list(DEFAULT_ENABLED)
    if not row:
        return list(DEFAULT_ENABLED)
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return list(DEFAULT_ENABLED)
    if not isinstance(value, dict) or not isinstance(value.get("enabled_families"), list):
        return list(DEFAULT_ENABLED)
    return clean_families(value["enabled_families"])


def _iso_or_none(value: Any) -> Optional[str]:
    """A validated ISO-8601 timestamp (safe to inline in SQL), else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


async def _stored_since(db) -> dict:
    try:
        row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                                {"k": SETTING_KEY})).first()
    except Exception:
        await db.rollback()
        return {}
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    since = value.get("enabled_since") if isinstance(value, dict) else None
    return {k: v for k, v in since.items() if k in BY_KEY and _iso_or_none(v)} if isinstance(since, dict) else {}


async def routing_state(db) -> tuple[list[str], dict]:
    """(enabled families, {family: enabled-since ISO}). A family enabled
    without a recorded start (the default after the upgrade) is stamped now
    and persisted, so incidents already open are never routed (caller commits)."""
    enabled = await enabled_families(db)
    since = await _stored_since(db)
    missing = [k for k in enabled if k not in since]
    if missing:
        stamp = datetime.now(timezone.utc).isoformat()
        since.update({k: stamp for k in missing})
        await _write(db, enabled, since)
    return enabled, {k: since[k] for k in enabled}


async def _write(db, enabled: list[str], since: dict) -> None:
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    ), {"k": SETTING_KEY, "v": json.dumps({"enabled_families": enabled,
                                           "enabled_since": {k: since[k] for k in enabled if k in since}})})


async def save_enabled_families(db, keys: Iterable[str]) -> list[str]:
    """Store the enabled families (caller commits). A family switched on now
    starts routing from now; one that stays on keeps its start."""
    clean = clean_families(list(keys))
    since = await _stored_since(db)
    stamp = datetime.now(timezone.utc).isoformat()
    await _write(db, clean, {k: since.get(k, stamp) for k in clean})
    return clean


def describe(enabled: Iterable[str]) -> list[dict]:
    """The registry with each family's enabled flag (API shape)."""
    on = set(enabled)
    return [{
        "key": s.key, "label": s.label, "description": s.description,
        "priority": s.priority, "priorities": s.priorities,
        "severity_priorities": dict(s.severity_priorities) if s.severity_priorities else None,
        "enabled": s.key in on, "default_enabled": s.default_enabled,
        "min_age_minutes": s.min_age_s // 60 if s.min_age_s else 0,
        "note": s.note or None,
    } for s in SOURCES]
