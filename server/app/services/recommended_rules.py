"""Recommended default alert rules for the whole appliance (alerting audit 2026-11-19,
plan step 2.1).

The product ships only a handful of rules (device down, latency, loss, interfaces,
CPU / memory / filesystem, agent offline, restarts). Professional monitoring
products also alert by default on service checks, certificates, critical device
CPU and memory, discards and disk saturation. These are offered here as
recommendations, like a template's recommended rules (``rule_packs``): nothing is
created or enabled until an operator accepts one, which creates one ordinary rule
remembering where it came from (``alert_rules.pack_key`` = ``default:<key>``,
``profile_id`` NULL, ``pack_accepted``).

A recommendation is shown as *covered* when an enabled, unscoped rule already
watches the same condition (for example a customer's own "Service Down" rule), so
accepting it would only duplicate alerts.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PACK_PREFIX = "default:"

# Ordered as the page shows them. ``family`` decides which create path is used:
# server metrics go through the host rule API, everything else the rule API.
CATALOGUE: list[dict[str, Any]] = [
    {"key": "service_down", "family": "service", "name": "Service check down",
     "description": "Any service check confirmed down for 2 minutes (after its own retries). "
                    "Also a TLS certificate within its critical days (default 7).",
     "metric": "service_status", "operator": "==", "threshold": 0, "trigger_on": "down",
     "min_duration": 120, "clear_duration": None, "severity": "critical", "priority": "P2",
     "recovery_alert": True, "cooldown": 0},
    {"key": "service_degraded", "family": "service", "name": "Service check degraded",
     "description": "Any service check in warning state for 5 minutes, including a TLS "
                    "certificate within its warning days (default 30).",
     "metric": "service_status", "operator": "==", "threshold": 0, "trigger_on": "degraded",
     "min_duration": 300, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0},
    {"key": "certificate_expiring", "family": "service", "name": "Certificate expiring",
     "description": "A certificate checked by a service check expires within 30 days.",
     "metric": "service_tls_days", "operator": "<", "threshold": 30, "trigger_on": "any",
     "min_duration": 0, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0},
    {"key": "certificate_expiring_soon", "family": "service", "name": "Certificate expiring soon",
     "description": "A certificate checked by a service check expires within 7 days.",
     "metric": "service_tls_days", "operator": "<", "threshold": 7, "trigger_on": "any",
     "min_duration": 0, "clear_duration": None, "severity": "critical", "priority": "P2",
     "recovery_alert": True, "cooldown": 0},
    {"key": "f5_certificate_expiring", "family": "device", "name": "F5 certificate expiring",
     "description": "A certificate in an F5 device's inventory expires within 30 days.",
     "metric": "f5_cert_days", "operator": "<", "threshold": 30, "trigger_on": "any",
     "min_duration": 0, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0},
    {"key": "device_degraded", "family": "device", "name": "Device degraded",
     "description": "A device whose confirmed status is Degraded (slow or lossy replies, "
                    "Settings > Monitoring limits) for 5 minutes.",
     "metric": "ping_status", "operator": "==", "threshold": 0, "trigger_on": "degraded",
     "min_duration": 300, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0},
    {"key": "device_cpu_critical", "family": "device", "name": "Critical device CPU (SNMP)",
     "description": "SNMP-reported CPU above 95% for 5 minutes (all devices).",
     "metric": "cpu", "operator": ">", "threshold": 95, "trigger_on": "any",
     "min_duration": 300, "clear_duration": 300, "severity": "critical", "priority": "P2",
     "recovery_alert": True, "cooldown": 300},
    {"key": "device_memory_critical", "family": "device", "name": "Critical device memory (SNMP)",
     "description": "SNMP-reported memory above 95% for 5 minutes (all devices).",
     "metric": "memory", "operator": ">", "threshold": 95, "trigger_on": "any",
     "min_duration": 300, "clear_duration": 300, "severity": "critical", "priority": "P2",
     "recovery_alert": True, "cooldown": 300},
    {"key": "interface_discards", "family": "device", "name": "Interface discards high",
     "description": "More than 100 discarded packets per poll on an interface for 10 minutes.",
     "metric": "if_discards", "operator": ">", "threshold": 100, "trigger_on": "any",
     "min_duration": 600, "clear_duration": 300, "severity": "warning", "priority": "P3",
     "recovery_alert": False, "cooldown": 300},
    {"key": "interface_error_rate", "family": "device", "name": "Interface error rate high",
     "description": "More than 1% of an interface's packets in error, poll after poll for 10 minutes "
                    "(the worse direction; quiet intervals under 1,000 packets are not judged).",
     "metric": "if_error_rate_pct", "operator": ">", "threshold": 1, "trigger_on": "any",
     "min_duration": 600, "clear_duration": 300, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 300},
    {"key": "interface_flapping", "family": "device", "name": "Interface flapping",
     "description": "An interface went down or came up at least 5 times in 15 minutes. "
                    "Clears once it has not changed state for 15 minutes.",
     "metric": "if_flaps", "operator": ">=", "threshold": 5, "trigger_on": "any",
     "min_duration": 900, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0},
    {"key": "disk_io_saturated", "family": "server", "name": "Disk I/O saturated",
     "description": "Disk busy time above 90% averaged over 15 minutes (all servers).",
     "metric": "host_disk_util_pct", "operator": "gt", "threshold": 90, "trigger_on": "any",
     "min_duration": 900, "clear_duration": 900, "severity": "warning", "priority": "P3",
     "recovery_alert": False, "cooldown": 300},
    # Server depth metrics (alerting plan G-9/G-10, app.services.host_depth_metrics).
    {"key": "swap_high", "family": "server", "name": "Swap usage high",
     "description": "More than 80% of a server's swap (page file) in use, averaged over 15 minutes "
                    "(all servers; servers without swap are not judged).",
     "metric": "host_swap_pct", "operator": "gt", "threshold": 80, "trigger_on": "any",
     "min_duration": 900, "clear_duration": 900, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 300},
    {"key": "filesystem_full_soon", "family": "server", "name": "Filesystem full within 7 days",
     "description": "A filesystem will be full within 7 days at its growth over the last week "
                    "(least-squares trend; needs a day of history). Clears at 8 days.",
     "metric": "host_fs_days_left", "operator": "lt", "threshold": 7, "trigger_on": "any",
     "min_duration": 3600, "clear_duration": 3600, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 300},
    {"key": "clock_skew", "family": "server", "name": "Server clock wrong",
     "description": "A server's clock is more than 60 seconds away from the appliance's, upload after "
                    "upload for 10 minutes (wrong time breaks logs, certificates and sign-in).",
     "metric": "host_clock_skew_s", "operator": "gt", "threshold": 60, "trigger_on": "any",
     "min_duration": 600, "clear_duration": 600, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 300},
    {"key": "auto_service_stopped", "family": "server", "name": "Automatic service stopped",
     "description": "A service set to start automatically stopped unexpectedly (non-zero exit code, "
                    "or failed) for 5 minutes, one alert per service. Services that finish cleanly "
                    "(trigger-start, delayed-start, one-shot) are not reported.",
     "metric": "host_auto_service_stopped", "operator": "gt", "threshold": 0, "trigger_on": "any",
     "min_duration": 300, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 300},
    {"key": "device_cold_start", "family": "trap", "name": "Device restarted (SNMP trap)",
     "description": "A device sent a coldStart trap (it restarted). One page per device per 15 minutes.",
     "metric": "trap", "operator": "==", "threshold": 1, "trap_oid": "1.3.6.1.6.3.1.1.5.1",
     "trigger_on": "any", "min_duration": 0, "clear_duration": None, "severity": "warning",
     "priority": "P3", "recovery_alert": False, "cooldown": 900},
    {"key": "syslog_critical", "family": "syslog", "name": "Critical syslog",
     "description": "A device logged an emergency, alert or critical message (syslog severity 0-2). "
                    "One page per device per 15 minutes.",
     "metric": "syslog", "operator": "<=", "threshold": 2, "trigger_on": "any",
     "min_duration": 0, "clear_duration": None, "severity": "critical", "priority": "P2",
     "recovery_alert": False, "cooldown": 900},
    # Combined rules (multi-condition rules phase 3): one alert naming both readings.
    {"key": "server_under_pressure", "family": "server", "name": "Server under pressure",
     "description": "CPU and memory both above 90% for 5 minutes on a server: one alert naming both readings.",
     "metric": "host_cpu_pct", "operator": ">", "threshold": 90, "trigger_on": "any",
     "min_duration": 300, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0, "subject_type": "server",
     "condition_tree": {"logic": "AND", "items": [
         {"metric": "host_cpu_pct", "operator": ">", "threshold": 90},
         {"metric": "host_memory_pct", "operator": ">", "threshold": 90}]}},
    {"key": "device_overloaded", "family": "device", "name": "Device overloaded",
     "description": "A device's CPU and memory both above 85% for 10 minutes: one alert naming both readings.",
     "metric": "combined", "operator": ">", "threshold": 0, "trigger_on": "any",
     "min_duration": 600, "clear_duration": None, "severity": "warning", "priority": "P3",
     "recovery_alert": True, "cooldown": 0, "subject_type": "device",
     "condition_tree": {"logic": "AND", "items": [
         {"metric": "cpu", "operator": ">", "threshold": 85},
         {"metric": "memory", "operator": ">", "threshold": 85}]}},
]

_LEVELLED_METRICS = frozenset({"service_tls_days", "cpu", "memory"})

_SCOPE_COLUMNS = ("device_id", "group_id", "device_type", "location", "scope_tag", "server_id",
                  "service_check_id", "service_check_group_id", "profile_id", "if_role", "target")

_RULES_SQL = """
    SELECT id::text, name, enabled, metric, operator, threshold, trigger_on, trap_oid, severity,
           pack_key, pack_accepted, device_id, group_id, device_type, location, scope_tag,
           server_id, service_check_id, service_check_group_id, profile_id, if_role, target
      FROM alert_rules
"""


def find(key: str) -> Optional[dict]:
    return next((it for it in CATALOGUE if it["key"] == key), None)


def _unscoped(rule: dict) -> bool:
    return all(rule.get(c) in (None, "") for c in _SCOPE_COLUMNS)


def _norm_op(op: Optional[str]) -> str:
    return {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "neq": "!="}.get(op or "", op or "")


def covers(rule: dict, item: dict) -> bool:
    """Does an existing enabled, unscoped rule already watch this recommendation's
    condition? Thresholds do not have to match: the operator chose theirs."""
    if item.get("condition_tree"):
        return False        # a combined recommendation is only ever accepted, never covered
    if not rule.get("enabled") or not _unscoped(rule) or rule.get("metric") != item["metric"]:
        return False
    if item["metric"] in ("service_status", "ping_status"):
        trig = rule.get("trigger_on") or "any"
        return trig == item["trigger_on"] or (trig == "any" and item["trigger_on"] == "down")
    if item["metric"] == "trap":
        return (rule.get("trap_oid") or "") in ("", item.get("trap_oid"))
    if item["metric"] in _LEVELLED_METRICS and rule.get("severity") != item["severity"]:
        # Warning and critical recommendations on one metric are separate rules.
        return False
    return _norm_op(rule.get("operator")) == _norm_op(item["operator"])


def _degraded_covered(rules: list[dict]) -> Optional[dict]:
    """Device degraded is also covered by an unscoped latency or loss rule on Degraded."""
    for r in rules:
        if (r.get("enabled") and _unscoped(r) and r.get("metric") in ("rtt", "packet_loss")
                and (r.get("trigger_on") or "any") in ("degraded", "any")):
            return r
    return None


async def state(db: AsyncSession) -> list[dict]:
    rows = [dict(r) for r in (await db.execute(text(_RULES_SQL))).mappings().all()]
    by_pack = {r["pack_key"]: r for r in rows if r.get("pack_key", "") and str(r["pack_key"]).startswith(PACK_PREFIX)}
    out = []
    for it in CATALOGUE:
        entry = {**it, "status": "available", "rule": None, "covered_by": None}
        accepted = by_pack.get(PACK_PREFIX + it["key"])
        if accepted:
            entry["status"] = "accepted"
            entry["rule"] = {"id": accepted["id"], "name": accepted["name"], "enabled": bool(accepted["enabled"])}
        else:
            cover = next((r for r in rows if covers(r, it)), None)
            if cover is None and it["key"] == "device_degraded":
                cover = _degraded_covered(rows)
            if cover is not None:
                entry["status"] = "covered"
                entry["covered_by"] = {"id": cover["id"], "name": cover["name"]}
        out.append(entry)
    return out


def accepted_record(item: dict) -> str:
    keep = ("metric", "operator", "threshold", "trigger_on", "trap_oid", "min_duration",
            "clear_duration", "severity", "priority", "recovery_alert", "cooldown", "condition_tree",
            "subject_type")
    return json.dumps({k: item.get(k) for k in keep})
