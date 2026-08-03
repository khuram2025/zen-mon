"""Network-device (SNMP) alert evaluation.

Ping/trap alerting is event-driven (the Go poller pushes status changes to
alert_engine); host metrics use a periodic ClickHouse evaluator
(host_alert_service). Polled SNMP metrics — interface utilization/errors/
oper-status and device cpu/memory/temperature — also arrive continuously in
ClickHouse, so they get the same periodic treatment here.

Every ``EVAL_INTERVAL_S`` this loads the enabled alert rules whose ``metric`` is
one of the network keys, computes the current value for every in-scope device
(and interface, for per-interface metrics), and raises/resolves a device-scoped
row in ``alerts``. Rules dedupe on (rule_id, device_id, if_index): an alert is
only raised when no matching active alert already exists, so running in both
uvicorn workers is harmless (mirrors host_alert_service).

Scope: a rule with no device_id/group_id/device_type/location applies to every
SNMP-monitored device; otherwise the usual scope filters apply. For interface
metrics, the optional ``target`` column narrows to interfaces whose
name/descr/alias contains it (case-insensitive) or whose if_index matches
exactly; empty target = all monitored interfaces.

BGP/HA/VPN/PSU/fan-state keys are whitelisted but not yet collected by the
poller, so they are intentionally not handled here (Phase 2/3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.host_alert_service import dispatch_to_channels

logger = logging.getLogger("zenplus.network_alerts")

EVAL_INTERVAL_S = 60
DEFAULT_WINDOW_S = 120       # sustained-window when a rule has no min_duration
UPTIME_LOOKBACK_S = 900      # how far back to look for a sysUpTime reset

# Per-interface metrics (computed from snmp_if_metrics, joined to interface speed).
INTERFACE_METRICS = {
    "if_in_bps", "if_out_bps", "if_util_pct",
    "if_errors", "if_discards", "if_oper_status",
}
# Device scalar metrics (from snmp_metrics, keyed by metric_key).
SCALAR_METRICS = {"cpu", "memory", "temperature", "session_count"}
SPECIAL_METRICS = {"uptime_reset"}
NETWORK_METRICS = INTERFACE_METRICS | SCALAR_METRICS | SPECIAL_METRICS

_OPS = {
    "gt": lambda a, b: a > b, ">": lambda a, b: a > b,
    "gte": lambda a, b: a >= b, ">=": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "<": lambda a, b: a < b,
    "lte": lambda a, b: a <= b, "<=": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "==": lambda a, b: a == b,
    "neq": lambda a, b: a != b, "!=": lambda a, b: a != b,
}

# oper_status code that means the interface is down (IF-MIB ifOperStatus).
_OPER_DOWN = 2


def _cmp(value: float, operator: str, threshold: float) -> bool:
    fn = _OPS.get((operator or "").strip())
    return bool(fn and fn(value, threshold))


def _since(window_s: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")


# ─── ClickHouse fetchers ────────────────────────────────────────────────────

def _if_fleet(window_s: int) -> dict[tuple[str, int], dict]:
    """{(device_id, if_index): {avg_in, avg_out, peak, in_err, out_err, ...}}."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id, if_index,
                   avg(in_bps), avg(out_bps),
                   max(greatest(in_bps, out_bps)),
                   max(in_errors) - min(in_errors),
                   max(out_errors) - min(out_errors),
                   max(in_discards) - min(in_discards),
                   max(out_discards) - min(out_discards),
                   argMax(oper_status, timestamp)
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(s)s
            GROUP BY device_id, if_index
            """,
            parameters={"s": _since(window_s)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse if-metrics query failed: %s", exc)
        return {}

    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        out[(str(r[0]), int(r[1]))] = {
            "avg_in": float(r[2] or 0), "avg_out": float(r[3] or 0),
            "peak": float(r[4] or 0),
            "errors": float((r[5] or 0) + (r[6] or 0)),
            "discards": float((r[7] or 0) + (r[8] or 0)),
            "oper": int(r[9] or 0),
        }
    return out


def _scalar_fleet(window_s: int) -> dict[str, dict]:
    """{device_id: {cpu, memory, temperature, sessions}} aggregated over window."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id,
                   avgIf(value, metric_key = 'cpu'),
                   avgIf(value, metric_key = 'memory'),
                   maxIf(value, metric_key LIKE 'temperature_%%'),
                   argMaxIf(value, timestamp, metric_key = 'sessions')
            FROM zenplus.snmp_metrics
            WHERE timestamp >= %(s)s
            GROUP BY device_id
            """,
            parameters={"s": _since(window_s)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse scalar query failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        out[str(r[0])] = {
            "cpu": (None if r[1] is None else float(r[1])),
            "memory": (None if r[2] is None else float(r[2])),
            "temperature": (None if r[3] is None else float(r[3])),
            "session_count": (None if r[4] is None else float(r[4])),
        }
    return out


def _uptime_resets() -> set[str]:
    """device_ids whose sysUpTime dropped within the lookback window (reboot)."""
    from app.core.database import get_clickhouse_client

    try:
        client = get_clickhouse_client()
        rows = client.query(
            """
            SELECT device_id, max(value), argMax(value, timestamp)
            FROM zenplus.snmp_metrics
            WHERE metric_key = 'uptime' AND timestamp >= %(s)s
            GROUP BY device_id
            """,
            parameters={"s": _since(UPTIME_LOOKBACK_S)},
        ).result_rows
    except Exception as exc:
        logger.warning("network alert: clickhouse uptime query failed: %s", exc)
        return set()

    # latest sample materially lower than an earlier one ⇒ the counter reset.
    return {str(r[0]) for r in rows if (float(r[1] or 0) - float(r[2] or 0)) > 60}


# ─── Postgres fetchers ──────────────────────────────────────────────────────

async def _snmp_devices(db: AsyncSession) -> dict[str, dict]:
    """{device_id: {hostname, device_type, location, group_id}} for SNMP devices."""
    rows = (await db.execute(text(
        "SELECT id, hostname, device_type, location, group_id FROM devices "
        "WHERE snmp_enabled = true AND status <> 'maintenance'"
    ))).all()
    return {
        str(r[0]): {
            "hostname": r[1] or "device",
            "device_type": r[2],
            "location": r[3],
            "group_id": str(r[4]) if r[4] else None,
        }
        for r in rows
    }


async def _interfaces(db: AsyncSession) -> dict[str, list[dict]]:
    """{device_id: [interface dicts]} for monitored interfaces with a known speed."""
    rows = (await db.execute(text(
        "SELECT device_id, if_index, if_name, if_descr, if_alias, "
        "COALESCE(configured_speed_bps, if_speed) AS speed, admin_status, monitored "
        "FROM device_interfaces WHERE monitored = true"
    ))).all()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append({
            "if_index": int(r[1]),
            "if_name": r[2] or "", "if_descr": r[3] or "", "if_alias": r[4] or "",
            "speed": int(r[5]) if r[5] else 0,
            "admin_up": (str(r[6] or "").lower() == "up"),
        })
    return out


# ─── Scope / target matching ────────────────────────────────────────────────

def _device_in_scope(rule, dev_id: str, dev: dict) -> bool:
    if rule.device_id and str(rule.device_id) != dev_id:
        return False
    if rule.group_id and (dev["group_id"] != str(rule.group_id)):
        return False
    if rule.device_type and rule.device_type != dev["device_type"]:
        return False
    if rule.location and (not dev["location"] or rule.location.lower() not in dev["location"].lower()):
        return False
    return True


def _iface_matches_target(iface: dict, target: str | None) -> bool:
    if not target:
        return True
    t = target.strip().lower()
    if t.isdigit() and int(t) == iface["if_index"]:
        return True
    return any(t in (iface[k] or "").lower() for k in ("if_name", "if_descr", "if_alias"))


def _iface_label(iface: dict) -> str:
    return iface["if_name"] or iface["if_descr"] or f"if {iface['if_index']}"


# ─── Alert raise / resolve (device-scoped rows in `alerts`) ──────────────────

async def _active_alert_id(db: AsyncSession, rule_id, device_id: str, if_index):
    row = (await db.execute(text(
        "SELECT id FROM alerts "
        "WHERE rule_id = :rid AND device_id = :did AND status IN ('active','acknowledged') "
        "  AND COALESCE(metadata->>'if_index','') = :ifx "
        "ORDER BY triggered_at DESC LIMIT 1"
    ), {"rid": str(rule_id), "did": device_id, "ifx": "" if if_index is None else str(if_index)})).first()
    return row[0] if row else None


async def _raise(db: AsyncSession, rule, device_id: str, message: str,
                 value: float, if_index, extra: dict) -> bool:
    now = datetime.now(timezone.utc)
    meta = {"rule_id": str(rule.id), "metric": rule.metric,
            "value": round(value, 2), "threshold": float(rule.threshold or 0)}
    if if_index is not None:
        meta["if_index"] = str(if_index)
    meta.update(extra)
    await db.execute(text(
        "INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata) "
        "VALUES (:did, :rid, 'active', :sev, :msg, :ts, CAST(:meta AS jsonb))"
    ), {"did": device_id, "rid": str(rule.id), "sev": rule.severity or "warning",
        "msg": message, "ts": now, "meta": json.dumps(meta)})
    return True


async def _resolve(db: AsyncSession, alert_id) -> int:
    now = datetime.now(timezone.utc)
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = :ts, "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"
    ), {"ts": now, "id": alert_id, "m": json.dumps({"resolved_by": "network_evaluator",
                                                     "resolved_at": now.isoformat()})})
    return res.rowcount or 0


# ─── Per-metric value resolution ─────────────────────────────────────────────

def _eval_interface(rule, m: dict, iface: dict) -> tuple[bool, float, str] | None:
    """(breach, value, detail) for an interface metric, or None to skip."""
    metric, op, thr = rule.metric, rule.operator, float(rule.threshold or 0)
    if metric == "if_in_bps":
        v = m["avg_in"]; return _cmp(v, op, thr), v, f"{v/1e6:.1f} Mbps in"
    if metric == "if_out_bps":
        v = m["avg_out"]; return _cmp(v, op, thr), v, f"{v/1e6:.1f} Mbps out"
    if metric == "if_util_pct":
        if not iface["speed"]:
            return None  # cannot compute utilization without a link speed
        v = min(100.0, m["peak"] / iface["speed"] * 100.0)
        return _cmp(v, op, thr), v, f"{v:.0f}% of {iface['speed']/1e6:.0f}Mb"
    if metric == "if_errors":
        v = m["errors"]; return _cmp(v, op, thr), v, f"{int(v)} errors"
    if metric == "if_discards":
        v = m["discards"]; return _cmp(v, op, thr), v, f"{int(v)} discards"
    if metric == "if_oper_status":
        # Only alert on interfaces that are administratively up — an admin-down
        # port reporting oper-down is intentional, not a fault.
        if not iface["admin_up"]:
            return None
        v = float(m["oper"])
        return _cmp(v, op, thr), v, ("down" if int(v) == _OPER_DOWN else f"oper={int(v)}")
    return None


def _eval_scalar(rule, vals: dict) -> tuple[bool, float, str] | None:
    metric, op, thr = rule.metric, rule.operator, float(rule.threshold or 0)
    v = vals.get(metric)
    if v is None:
        return None
    if metric in ("cpu", "memory"):
        return _cmp(v, op, thr), v, f"{v:.0f}%"
    if metric == "temperature":
        return _cmp(v, op, thr), v, f"{v:.0f}°"
    if metric == "session_count":
        return _cmp(v, op, thr), v, f"{int(v)} sessions"
    return None


def _message(rule, hostname: str, detail: str, iface_label: str | None) -> str:
    where = f"{hostname}" + (f" / {iface_label}" if iface_label else "")
    return f"{rule.name}: {where} — {detail}"


async def _notify(db: AsyncSession, rule, hostname: str, message: str) -> None:
    # Quiet hours: the alert row is already recorded; only the outbound
    # notification is gated by the rule's schedule window.
    from app.services.alert_schedule import notifications_allowed, get_configured_timezone
    tz = await get_configured_timezone(db)
    if not notifications_allowed(
        getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
        getattr(rule, "schedule_days", None), tz,
    ):
        return
    sev = (rule.severity or "warning")
    await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": f"[{sev.upper()}] {rule.name} — {hostname}",
        "body": message, "message": message,
        "hostname": hostname, "ip_address": "",
        "status": "ALERT", "severity": sev,
        "details": [("Metric", rule.metric),
                    ("Threshold", f"{rule.operator} {rule.threshold}")],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
    })


# ─── Main evaluation pass ────────────────────────────────────────────────────

async def evaluate_network_rules(db: AsyncSession) -> dict[str, int]:
    metric_list = ",".join(f"'{m}'" for m in sorted(NETWORK_METRICS))
    rules = (await db.execute(text(
        f"SELECT id, name, metric, operator, threshold, severity, min_duration, "
        f"notify_channels, device_id, group_id, device_type, location, target, "
        f"schedule_start, schedule_end, schedule_days "
        f"FROM alert_rules WHERE enabled = true AND metric IN ({metric_list})"
    ))).all()
    if not rules:
        return {"rules": 0, "raised": 0, "resolved": 0}

    devices = await _snmp_devices(db)
    if not devices:
        return {"rules": len(rules), "raised": 0, "resolved": 0}
    interfaces = await _interfaces(db)

    # Active device-scoped silences (snoozes), loaded once per pass. Keys match
    # the dedupe written by the snooze endpoint: rule:<id>[:if:<idx>].
    silences: set[tuple[str, str]] = {
        (r.did, r.dedupe) for r in (await db.execute(text(
            "SELECT device_id::text AS did, dedupe FROM alert_silences "
            "WHERE device_id IS NOT NULL AND (until IS NULL OR until > NOW())"
        ))).all()
    }

    # Pre-fetch ClickHouse fleet data once per distinct window.
    windows = {max(r.min_duration or 0, DEFAULT_WINDOW_S) for r in rules}
    if_cache = {w: _if_fleet(w) for w in windows}
    scalar_cache = {w: _scalar_fleet(w) for w in windows}
    uptime_reset_ids = _uptime_resets() if any(r.metric == "uptime_reset" for r in rules) else set()

    raised = resolved = 0
    for rule in rules:
        window = max(rule.min_duration or 0, DEFAULT_WINDOW_S)
        in_scope = [(did, d) for did, d in devices.items() if _device_in_scope(rule, did, d)]

        if rule.metric in INTERFACE_METRICS:
            if_fleet = if_cache[window]
            for did, dev in in_scope:
                for iface in interfaces.get(did, []):
                    if not _iface_matches_target(iface, rule.target):
                        continue
                    m = if_fleet.get((did, iface["if_index"]))
                    if not m:
                        continue
                    res = _eval_interface(rule, m, iface)
                    if res is None:
                        continue
                    breach, value, detail = res
                    raised, resolved = await _apply(
                        db, rule, did, dev["hostname"], iface["if_index"],
                        _iface_label(iface), breach, value, detail,
                        {"if_name": iface["if_name"]}, raised, resolved,
                        silences,
                    )
            await db.commit()
            continue

        # Device-scalar / special metrics — one value per device.
        for did, dev in in_scope:
            if rule.metric in SCALAR_METRICS:
                res = _eval_scalar(rule, scalar_cache[window].get(did, {}))
            elif rule.metric == "uptime_reset":
                is_reset = did in uptime_reset_ids
                res = (_cmp(1.0 if is_reset else 0.0, rule.operator, float(rule.threshold or 0)),
                       1.0 if is_reset else 0.0,
                       "reboot detected" if is_reset else "no reboot")
            else:
                res = None
            if res is None:
                continue
            breach, value, detail = res
            raised, resolved = await _apply(
                db, rule, did, dev["hostname"], None, None,
                breach, value, detail, {}, raised, resolved,
                silences,
            )
        await db.commit()

    if raised or resolved:
        logger.info("network alert eval: %d rules, %d raised, %d resolved", len(rules), raised, resolved)
    return {"rules": len(rules), "raised": raised, "resolved": resolved}


async def _apply(db, rule, device_id, hostname, if_index, iface_label,
                 breach, value, detail, extra, raised, resolved,
                 silences: set[tuple[str, str]] | None = None):
    """Raise (if breaching and not already open) or resolve one rule/device/iface."""
    existing = await _active_alert_id(db, rule.id, device_id, if_index)
    if breach:
        # An active snooze suppresses re-raising this exact condition; the
        # resolve branch below still runs so a snoozed condition that clears
        # closes out any open alert.
        dedupe = f"rule:{rule.id}" + (f":if:{if_index}" if if_index is not None else "")
        if silences and (device_id, dedupe) in silences:
            return raised, resolved
        if existing is None:
            msg = _message(rule, hostname, detail, iface_label)
            if await _raise(db, rule, device_id, msg, value, if_index, extra):
                raised += 1
                await db.commit()
                await _notify(db, rule, hostname, msg)
    else:
        if existing is not None:
            resolved += await _resolve(db, existing)
    return raised, resolved


async def network_alert_evaluator_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(25)  # let the app boot and first SNMP metrics land
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await evaluate_network_rules(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("network alert evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
