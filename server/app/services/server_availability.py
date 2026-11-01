"""Server availability: one Up/Down answer per agent server.

A server can be watched two ways: its agent's heartbeat, and ICMP when the
server is also a ping-monitored device (linked by ``servers.device_id`` or by
the same IP). Before this module only the device path could page: "Device
Down" read ``ping_status`` from the poller, so a server watched only by its
agent went down without a rule noticing, and the staleness sweep's built-in
*agent offline* alert had no rule, no channel and no escalation policy.

``judge`` combines the signals into one state per server:

==============  =================  =====================================
heartbeat       ping device        state
==============  =================  =====================================
current         any                ``up``
silent          none / unknown     ``down``           (Device Down rules)
silent          up / degraded      ``agent_offline``  (Agent offline rules)
silent          down               ``device_down``    (the device path pages)
silent          maintenance        ``maintenance``    (nobody pages)
never seen      any                ``unknown``
==============  =================  =====================================

A heartbeat is silent once it is older than the rule's ``min_duration``, or
``DOWN_AFTER_S`` when that is 0. ``MIN_DOWN_AFTER_S`` keeps a rule from paging
on a couple of late heartbeats (the agent beats every 30 s).

Which rules apply to a server is decided here too, so the evaluator and the
staleness sweep's fallback agree:

* ``host_agent_offline`` rules: every agent server, or the rule's one server.
* ``ping_status`` rules that fire on *down* — ``trigger_on = 'down'``, or an
  'any' rule whose only condition is ``ping_status == 0`` (the seeded Device
  Down) — whose scope is not tied to a device (no device, group, type or
  location filter): every agent server, narrowed by ``scope_tag`` like
  devices are. These are the "Device Down" rules, and they now cover servers
  as well as devices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DOWN_AFTER_S = 180
MIN_DOWN_AFTER_S = 90

AGENT_OFFLINE_METRIC = "host_agent_offline"


@dataclass
class ServerSignals:
    """What ZenPlus knows about one agent server's reachability."""
    server_id: str
    hostname: str
    tags: set[str] = field(default_factory=set)
    heartbeat_at: Optional[datetime] = None
    # Status of the linked ping-enabled device, or None when there is none.
    ping: Optional[str] = None
    ping_device_id: Optional[str] = None


def _get(rule: Any, key: str):
    if isinstance(rule, dict):
        return rule.get(key)
    return getattr(rule, key, None)


def down_after_seconds(rule: Any) -> int:
    """How long a heartbeat may be silent before the rule counts the server
    as gone: the rule's ``min_duration``, else the default, never below the
    floor."""
    configured = int(_get(rule, "min_duration") or 0)
    return max(configured or DOWN_AFTER_S, MIN_DOWN_AFTER_S)


def judge(signals: ServerSignals, now: float, after_s: int) -> str:
    """The server's state for a rule with this silence allowance (see the
    module table)."""
    if signals.heartbeat_at is None:
        return "unknown"
    if now - signals.heartbeat_at.timestamp() < after_s:
        return "up"
    if signals.ping == "maintenance":
        return "maintenance"
    if signals.ping == "down":
        return "device_down"
    if signals.ping in ("up", "degraded"):
        return "agent_offline"
    return "down"


_CMP = {
    "eq": lambda v, t: v == t, "==": lambda v, t: v == t,
    "neq": lambda v, t: v != t, "!=": lambda v, t: v != t,
    "lt": lambda v, t: v < t, "<": lambda v, t: v < t,
    "lte": lambda v, t: v <= t, "<=": lambda v, t: v <= t,
    "gt": lambda v, t: v > t, ">": lambda v, t: v > t,
    "gte": lambda v, t: v >= t, ">=": lambda v, t: v >= t,
}


def _fires_on_down_only(operator, threshold) -> bool:
    """Is this ping_status condition true at 0 (down) and false at 1 (up)?"""
    cmp = _CMP.get((operator or "").strip().lower())
    try:
        t = float(threshold)
    except (TypeError, ValueError):
        return False
    return cmp is not None and cmp(0.0, t) and not cmp(1.0, t)


def _selects_down(rule: Any) -> bool:
    """Does the rule's trigger mean "goes down"? Explicitly (trigger_on
    down), or as the alert engine reads an 'any' rule: its ping_status
    condition is the only thing selecting the state, so ``ping_status == 0``
    (the seeded Device Down) means down. A condition on another metric
    (latency, loss) makes it a different rule."""
    trigger = (_get(rule, "trigger_on") or "any").strip().lower()
    if trigger == "down":
        return True
    if trigger != "any":
        return False
    conditions = _get(rule, "conditions")
    if isinstance(conditions, str):
        import json
        try:
            conditions = json.loads(conditions)
        except ValueError:
            conditions = None
    if conditions:
        return all(isinstance(c, dict) and c.get("metric") == "ping_status"
                   and _fires_on_down_only(c.get("operator"), c.get("threshold"))
                   for c in conditions)
    return _fires_on_down_only(_get(rule, "operator"), _get(rule, "threshold"))


def is_down_rule(rule: Any) -> bool:
    """A ping_status rule that fires on down and is not tied to a device."""
    if (_get(rule, "metric") or "") != "ping_status":
        return False
    if not _selects_down(rule):
        return False
    if _get(rule, "service_check_id") or _get(rule, "service_check_group_id"):
        return False
    if _get(rule, "device_id") or _get(rule, "group_id") or _get(rule, "device_type"):
        return False
    if (_get(rule, "location") or "").strip():
        return False
    return True


def is_availability_rule(rule: Any) -> bool:
    return (_get(rule, "metric") or "") == AGENT_OFFLINE_METRIC or is_down_rule(rule)


def rule_covers(rule: Any, signals: ServerSignals) -> bool:
    """Does this availability rule apply to this server?"""
    server_id = _get(rule, "server_id")
    if server_id and str(server_id) != signals.server_id:
        return False
    tag = (_get(rule, "scope_tag") or "").strip().lower()
    if tag and tag not in signals.tags:
        return False
    return True


def breach_for(rule: Any, state: str) -> Optional[bool]:
    """True raises (or continues), False resolves, None leaves things as they
    are: a state this rule does not own neither opens nor clears it."""
    if state == "up":
        return False
    if (_get(rule, "metric") or "") == AGENT_OFFLINE_METRIC:
        if state == "agent_offline":
            return True
        return None
    if state == "down":
        return True
    if state == "agent_offline":
        # Ping proves the host answers: whatever is wrong, it is not down.
        return False
    return None


def describe(rule: Any, state: str, signals: ServerSignals, after_s: int) -> str:
    """What was measured, for the alert message and the Measured row."""
    mins = max(1, round(after_s / 60))
    if state == "up":
        return "agent reporting again"
    if state == "agent_offline":
        return f"agent silent for over {mins} min; server still answers ping"
    if state == "down":
        how = "ping not monitored" if signals.ping is None else "ping status unknown"
        return f"agent silent for over {mins} min; {how}"
    return state.replace("_", " ")


_SIGNALS_SQL = """
SELECT s.id::text, COALESCE(s.hostname, s.display_name, 'server'), s.tags,
       hb.last_heartbeat_at, d.status, d.id::text
FROM servers s
LEFT JOIN LATERAL (
    SELECT max(a.last_heartbeat_at) AS last_heartbeat_at
    FROM agents a
    WHERE a.server_id = s.id AND a.status <> 'disabled'
) hb ON TRUE
LEFT JOIN LATERAL (
    SELECT dv.id, dv.status
    FROM devices dv
    WHERE dv.ping_enabled = TRUE
      AND (dv.id = s.device_id OR (s.primary_ip IS NOT NULL AND dv.ip_address = s.primary_ip))
    ORDER BY (dv.id = s.device_id) DESC NULLS LAST
    LIMIT 1
) d ON TRUE
WHERE s.collection_mode = 'agent' AND s.status <> 'disabled'
"""


def _tags(raw) -> set[str]:
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return set()
    return {str(t).strip().lower() for t in raw or [] if str(t).strip()}


async def load_signals(db: AsyncSession) -> dict[str, ServerSignals]:
    rows = (await db.execute(text(_SIGNALS_SQL))).all()
    return {r[0]: ServerSignals(server_id=r[0], hostname=r[1] or "server", tags=_tags(r[2]),
                                heartbeat_at=r[3], ping=r[4], ping_device_id=r[5])
            for r in rows}


_COVER_RULES_SQL = """
SELECT id, metric, trigger_on, operator, threshold, conditions, server_id, device_id,
       group_id, device_type, location, scope_tag, service_check_id, service_check_group_id
FROM alert_rules
WHERE enabled = TRUE AND metric IN (:agent, 'ping_status')
"""


async def covering_rules(db: AsyncSession) -> list:
    """Enabled availability rules (both kinds)."""
    rows = (await db.execute(text(_COVER_RULES_SQL), {"agent": AGENT_OFFLINE_METRIC})).all()
    return [r for r in rows if is_availability_rule(r)]


def covered(rules: Iterable, signals: ServerSignals, state: str) -> bool:
    """Whether an enabled rule pages for this server in this state. The
    staleness sweep raises its built-in, unrouted *agent offline* alert only
    when none does, so a covered server never gets two alerts for one outage.
    A server in maintenance is never paged for."""
    if state == "maintenance":
        return True
    if state == "agent_offline":
        def wanted(r):
            return (_get(r, "metric") or "") == AGENT_OFFLINE_METRIC
    elif state in ("down", "device_down"):
        # A device that is down is paged by the same Device Down rules.
        wanted = is_down_rule
    else:
        return False
    return any(wanted(r) and rule_covers(r, signals) for r in rules)
