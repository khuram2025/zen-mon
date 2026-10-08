"""Network-device health sections: CPU and memory, environment (temperature,
fans, power), HA and redundancy, VPN tunnels, BGP peers, F5 pools and
certificates, optics and PoE, firewall sessions and SNMP monitoring coverage.

Shared rules in this pack (see report_packs/__init__.py for the contract):
- Every section is device-scoped: Postgres queries carry ctx.pg_device_clause
  and ClickHouse queries ctx.ch_device_clause.
- CPU, memory, sensor and session history comes from zenplus.snmp_metrics_5m
  (90 days; snmp_metrics_1h is never populated), weighted by sample_count.
- F5 BIG-IP overall RAM is f5_system_memory_pct (sysGlobalHostMemUsed /
  sysGlobalHostMemTotal). Older 'memory' rows of an F5 may hold the highest
  TMM / non-TMM percentage, so they are never used for an F5; TMM, non-TMM and
  swap are reported separately.
- Template state (device_template_values) is shaped and graded exactly like the
  device page (/devices/{id}/template-insights): each device template's label
  maps and thresholds decide ok / warning / critical, and F5 groups go through
  f5_insights.enrich_f5_groups. Values not refreshed for STALE_HOURS are
  'stale' and counted neither as up nor as down.
- Missing readings are 'not measured' (—), never zero and never healthy.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _fmt_bps,
    _fmt_bytes,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

logger = logging.getLogger("zenplus.reports")

REQUIRES = ("devices.view",)
ROLLUP_DAYS = 90            # snmp_metrics_5m TTL
STALE_HOURS = 24            # template values older than this are 'stale'
COVERAGE_LOOKBACK_DAYS = 90
SNMP_HISTORY = "5-minute SNMP history"

F5_SYSTEM_MEMORY = "f5_system_memory_pct"
F5_MEMORY_DOMAINS = (("f5_system_memory_pct", "System RAM"), ("f5_tmm_memory_pct", "TMM"),
                     ("f5_host_memory_pct", "Non-TMM"), ("f5_swap_memory_pct", "Swap"))

SEV_RANK = {"none": 0, "info": 1, "ok": 2, "warn": 3, "crit": 4}
SEV_WORD = {"ok": "ok", "info": "info", "warn": "warning", "crit": "critical"}
# Worst first: how rows of a state table are ordered.
HEALTH_ORDER = {"critical": 0, "warning": 1, "stale": 2, "n/a": 3, "info": 4, "ok": 5}

# Standard MIB groups the poller adds to router/switch/firewall/AP devices on
# top of their vendor template (poller standard_network_groups.go). They are
# not stored in device_profiles, so their RFC-defined enum labels live here.
STD_GROUPS: dict[str, dict] = {
    "std_bgp_peers": {
        "key": "std_bgp_peers", "name": "BGP Peers", "kind": "table",
        "metrics": [
            {"key": "std_bgp_state", "name": "State", "type": "enum", "labels": {
                "1": {"text": "Idle", "sev": "warn"}, "2": {"text": "Connect", "sev": "warn"},
                "3": {"text": "Active", "sev": "warn"}, "4": {"text": "OpenSent", "sev": "warn"},
                "5": {"text": "OpenConfirm", "sev": "warn"},
                "6": {"text": "Established", "sev": "ok"}}},
            {"key": "std_bgp_admin", "name": "Administrative State", "type": "enum", "labels": {
                "1": {"text": "Stop", "sev": "info"}, "2": {"text": "Start", "sev": "ok"}}},
            {"key": "std_bgp_remote_as", "name": "Remote AS", "type": "gauge", "unit": "asn"},
            {"key": "std_bgp_established_seconds", "name": "Established Time", "type": "gauge",
             "unit": "seconds"},
        ]},
    "std_vrrp_routers": {
        "key": "std_vrrp_routers", "name": "VRRP Routers", "kind": "table",
        "metrics": [
            {"key": "std_vrrp_state", "name": "State", "type": "enum", "labels": {
                "1": {"text": "Initialize", "sev": "warn"}, "2": {"text": "Backup", "sev": "ok"},
                "3": {"text": "Master", "sev": "ok"}}},
            {"key": "std_vrrp_admin_state", "name": "Administrative State", "type": "enum",
             "labels": {"1": {"text": "Up", "sev": "ok"}, "2": {"text": "Down", "sev": "info"}}},
            {"key": "std_vrrp_priority", "name": "Priority", "type": "gauge"},
            {"key": "std_vrrp_uptime", "name": "Master Uptime", "type": "gauge", "unit": "centiseconds"},
        ]},
    "std_vrrpv3_routers": {
        "key": "std_vrrpv3_routers", "name": "VRRPv3 Routers", "kind": "table",
        "metrics": [
            {"key": "std_vrrpv3_state", "name": "State", "type": "enum", "labels": {
                "1": {"text": "Initialize", "sev": "warn"}, "2": {"text": "Backup", "sev": "ok"},
                "3": {"text": "Master", "sev": "ok"}}},
            {"key": "std_vrrpv3_priority", "name": "Priority", "type": "gauge"},
            {"key": "std_vrrpv3_adv_interval", "name": "Advertisement Interval", "type": "gauge",
             "unit": "centiseconds"},
            {"key": "std_vrrpv3_preempt", "name": "Preempt Mode", "type": "enum", "labels": {
                "1": {"text": "Enabled", "sev": "info"}, "2": {"text": "Disabled", "sev": "info"}}},
            {"key": "std_vrrpv3_uptime", "name": "Uptime", "type": "gauge", "unit": "centiseconds"},
        ]},
}

NO_DEVICES = "No monitored devices in scope."
UNLABELED_NOTE = ("Some templates report states as numeric codes without a label map; those are "
                  "shown as codes and are not graded (n/a).")


# ─── Small helpers ──────────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    """float, or None for NULL / NaN / ±inf (ClickHouse returns nan for an empty
    quantile or average)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def _count(n: float) -> str:
    return f"{int(round(n)):,}"


def _kpi(label: str, value: str, accent: str, subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


def _truncated(title: str, shown: int, total: int, noun: str) -> str:
    return f"{title} — top {shown:,} of {total:,} {noun}" if total > shown else title


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Any) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.0f} s"
    m = int(round(s / 60))
    if m < 60:
        return f"{m} min"
    if m < 1440:
        h, mm = divmod(m, 60)
        return f"{h} h {mm:02d} min"
    d, rem = divmod(m, 1440)
    return f"{d} d {rem // 60} h"


def _change_pts(curr: Optional[float], prev: Optional[float]) -> str:
    if curr is None:
        return "Not measured"
    if prev is None:
        return "No data for the previous period"
    d = round(curr - prev, 2)
    if d == 0:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.2f} pts vs previous period"


def _delta_pts(curr: Optional[float], prev: Optional[float]) -> str:
    if curr is None or prev is None:
        return "—"
    return f"{curr - prev:+.2f} pts"


def _level(v: Optional[float], warn: float, crit: float) -> str:
    if v is None:
        return "unknown"
    return "critical" if v >= crit else "warning" if v >= warn else "ok"


def _accent(level: str) -> str:
    return {"critical": "danger", "warning": "warning", "ok": "success"}.get(level, "neutral")


def _interval_label(interval: str) -> str:
    n, unit = interval.split()
    return f"{n}-{unit.lower()}"


def _fmt_value(v: float, unit: str) -> str:
    """A template gauge with its unit, readable ('1.2 Mbps', '45.0 °C', '3 d 4 h')."""
    u = (unit or "").strip()
    ul = u.lower()
    if ul == "bps":
        return _fmt_bps(v)
    if ul == "kbps":
        return _fmt_bps(v * 1000)
    if ul in ("bytes", "b"):
        return _fmt_bytes(v)
    if ul == "kb":
        return _fmt_bytes(v * 1024)
    if u == "%":
        return f"{v:.1f}%"
    if ul in ("seconds", "s"):
        return _duration(v)
    if ul == "centiseconds":
        return _duration(v / 100.0)
    if ul in ("°c", "c", "celsius"):
        return f"{v:.1f} °C"
    if ul == "dbm":
        return f"{v:.2f} dBm"
    if ul in ("w", "watts"):
        return f"{v:,.1f} W"
    if ul in ("v", "voltsdc", "voltsac"):
        return f"{v:,.2f} V"
    if ul == "rpm":
        return f"{v:,.0f} RPM"
    if ul == "asn":
        return f"AS{int(v)}"
    num = f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return f"{num} {u}".strip()


def _time_points(ctx: SectionCtx, pts: list[tuple[datetime, Optional[float]]],
                 step: int) -> list[dict]:
    """Chart points with a null where buckets are missing (gaps stay gaps)."""
    out: list[dict] = []
    prev: Optional[datetime] = None
    for ts, v in pts:
        ts_a = _aware(ts)
        if ts_a is None:
            continue
        if prev is not None and (ts_a - prev).total_seconds() > step * 1.5:
            out.append({"t": ctx.iso(prev + timedelta(seconds=step)), "v": None})
        out.append({"t": ctx.iso(ts_a), "v": None if v is None else round(v, 2)})
        prev = ts_a
    return out


def _step_seconds(interval: str) -> int:
    n, unit = interval.split()
    return int(n) * {"MINUTE": 60, "HOUR": 3600, "DAY": 86400}.get(unit.upper(), 60)


def _empty(sid: str, title: str, note: str, description: Optional[str] = None) -> dict:
    out = _section(sid, title, notes=[note])
    if description:
        out["description"] = description
    return out


# ─── Device inventory (Postgres) ────────────────────────────────────────────

async def _devices(ctx: SectionCtx) -> dict[str, dict]:
    """device_id -> identity, polling and template facts for every device in
    scope (cached for the render)."""
    if "hwpack_devices" in ctx._cache:
        return ctx._cache["hwpack_devices"]
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        "SELECT d.id::text, COALESCE(NULLIF(d.hostname, ''), host(d.ip_address), d.id::text), "
        "COALESCE(host(d.ip_address), ''), COALESCE(NULLIF(d.vendor, ''), ''), "
        "COALESCE(NULLIF(d.model, ''), ''), COALESCE(d.device_type, ''), d.profile_id::text, "
        "COALESCE(p.name, ''), COALESCE(p.vendor, ''), COALESCE(d.snmp_enabled, FALSE), "
        "COALESCE(d.ping_enabled, FALSE), COALESCE(d.poll_mode, 'direct') "
        "FROM devices d LEFT JOIN device_profiles p ON p.id = d.profile_id "
        f"WHERE TRUE{clause} ORDER BY 2"), params)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[str(r[0])] = {
            "id": str(r[0]), "name": r[1] or str(r[0])[:8], "ip": r[2] or "", "vendor": r[3] or "",
            "model": r[4] or "", "type": (r[5] or "").lower(), "profile_id": r[6],
            "profile": r[7] or "", "profile_vendor": r[8] or "", "snmp": bool(r[9]),
            "ping": bool(r[10]), "poll_mode": r[11] or "direct",
        }
    ctx._cache["hwpack_devices"] = out
    return out


def _is_f5(dev: dict) -> bool:
    hay = " ".join((dev.get("vendor") or "", dev.get("profile_vendor") or "",
                    dev.get("profile") or "")).lower()
    return bool(re.search(r"\bf5\b|big-?ip", hay))


def _direct_snmp(dev: dict) -> bool:
    """SNMP-polled by a poller (controller-managed children report through
    their controller and are never polled directly)."""
    return dev["snmp"] and dev["poll_mode"] != "via_controller"


def _vendor(dev: dict) -> str:
    return " ".join(x for x in (dev.get("vendor") or dev.get("profile_vendor"), dev.get("model")) if x) or "—"


# ─── Template state (device_template_values shaped like the device page) ────

async def _profiles(ctx: SectionCtx) -> dict[str, dict[str, dict]]:
    """profile_id -> {group key: group definition} for templates attached to
    devices in scope."""
    if "hwpack_profiles" in ctx._cache:
        return ctx._cache["hwpack_profiles"]
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        "SELECT p.id::text, p.oid_groups FROM device_profiles p WHERE p.id IN "
        f"(SELECT d.profile_id FROM devices d WHERE d.profile_id IS NOT NULL{clause})"),
        params)).fetchall()
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        groups = r[1] or []
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except ValueError:
                groups = []
        out[str(r[0])] = {g["key"]: g for g in groups
                          if isinstance(g, dict) and isinstance(g.get("key"), str)}
    ctx._cache["hwpack_profiles"] = out
    return out


async def _template_values(ctx: SectionCtx, groups: Iterable[str]) -> list[dict]:
    clause, params = ctx.pg_device_clause("v.device_id")
    rows = (await ctx.db.execute(text(
        "SELECT v.device_id::text, v.group_key, v.metric_key, v.instance, v.label, v.unit, "
        "v.value_num, v.value_text, v.series_key, v.updated_at "
        "FROM device_template_values v WHERE v.group_key = ANY(:groups)"
        f"{clause}"), {"groups": list(groups), **params})).fetchall()
    return [{"device": str(r[0]), "group": r[1], "metric": r[2], "instance": r[3] or "",
             "label": r[4] or "", "unit": r[5] or "", "num": _num(r[6]), "text": r[7] or "",
             "series": r[8] or "", "at": _aware(r[9])} for r in rows]


def _group_defs(dev: dict, profiles: dict[str, dict[str, dict]]) -> dict[str, dict]:
    defs = dict(profiles.get(dev.get("profile_id") or "", {}))
    for key, g in STD_GROUPS.items():
        defs.setdefault(key, g)
    return defs


def _shape_groups(defs: dict[str, dict], values: list[dict], wanted: Iterable[str]) -> list[dict]:
    """The device page's template-insights shaping for the wanted groups: table
    rows keyed by instance with graded cells, scalar metrics with has_data."""
    from app.api.v1.devices import _STANDALONE_RING_METRICS, _metric_display, _metric_status

    by_group: dict[str, list[dict]] = defaultdict(list)
    for v in values:
        by_group[v["group"]].append(v)
    out: list[dict] = []
    for gkey in wanted:
        gdef = defs.get(gkey)
        if not gdef:
            continue
        metrics = [m for m in gdef.get("metrics") or [] if isinstance(m, dict) and m.get("key")]
        mdefs = {m["key"]: m for m in metrics}
        rows = by_group.get(gkey, [])
        if gdef.get("kind") == "table":
            by_inst: dict[str, dict] = {}
            for v in rows:
                inst = by_inst.setdefault(v["instance"], {"label": v["label"], "cells": {}})
                if v["label"]:
                    inst["label"] = v["label"]
                m = mdefs.get(v["metric"], {})
                inst["cells"][v["metric"]] = {
                    "value": v["num"], "text": _metric_display(m, v["num"], v["text"]),
                    "status": _metric_status(m, v["num"], v["text"]), "series_key": v["series"],
                    "updated_at": v["at"], "name": m.get("name") or v["metric"],
                    "unit": m.get("unit") or v["unit"], "type": m.get("type", "gauge"),
                }
            out.append({
                "key": gkey, "name": gdef.get("name") or gkey, "kind": "table",
                "description": gdef.get("description"),
                "columns": [{"key": m["key"], "name": m.get("name"), "unit": m.get("unit"),
                             "type": m.get("type", "gauge")} for m in metrics],
                "rows": [{"instance": inst, "label": d["label"] or inst, "cells": d["cells"]}
                         for inst, d in sorted(by_inst.items(),
                                               key=lambda kv: (kv[1]["label"] or kv[0]).lower())],
            })
        else:
            by_metric = {v["metric"]: v for v in rows}
            out_metrics = []
            for m in metrics:
                v = by_metric.get(m["key"])
                status = _metric_status(m, v["num"], v["text"]) if v else "none"
                text_out = _metric_display(m, v["num"], v["text"]) if v else ""
                members_group = _STANDALONE_RING_METRICS.get(m["key"])
                if v and members_group and status == "warn" and v["num"] == 2:
                    members = {r["instance"] for r in by_group.get(members_group, [])}
                    if len(members) <= 1:
                        status, text_out = "info", "Standalone — no stack ring"
                out_metrics.append({
                    "key": m["key"], "name": m.get("name") or m["key"],
                    "type": m.get("type", "gauge"), "unit": m.get("unit") or (v and v["unit"]) or "",
                    "value": v["num"] if v else None, "text": text_out, "status": status,
                    "series_key": v["series"] if v else f"tpl_{m['key']}", "has_data": v is not None,
                    "updated_at": v["at"] if v else None,
                })
            out.append({"key": gkey, "name": gdef.get("name") or gkey, "kind": "scalar",
                        "description": gdef.get("description"), "metrics": out_metrics})
    return out


async def _template_state(ctx: SectionCtx, wanted: tuple[str, ...]) -> dict:
    """Shaped template groups per device for the wanted group keys:
    {'devices', 'groups': {device_id: [group]}, 'defined': {device_id: {keys}}}.
    'defined' lists the wanted groups each device's own template declares (the
    standard MIB groups count only where the device returned rows)."""
    key = f"hwpack_tpl:{','.join(wanted)}"
    if key in ctx._cache:
        return ctx._cache[key]
    from app.services.f5_insights import enrich_f5_groups

    devs = await _devices(ctx)
    profiles = await _profiles(ctx) if any(d["profile_id"] for d in devs.values()) else {}
    values = await _template_values(ctx, wanted) if devs else []
    by_dev: dict[str, list[dict]] = defaultdict(list)
    for v in values:
        if v["device"] in devs:
            by_dev[v["device"]].append(v)
    now = _now()
    groups: dict[str, list[dict]] = {}
    defined: dict[str, set[str]] = {}
    for did, dev in devs.items():
        own = profiles.get(dev.get("profile_id") or "", {})
        defs = _group_defs(dev, profiles)
        have = {v["group"] for v in by_dev.get(did, [])}
        declared = {g for g in wanted if g in own or (g in STD_GROUPS and g in have)}
        if not declared and not have:
            continue
        defined[did] = declared
        shaped = _shape_groups(defs, by_dev.get(did, []), wanted)
        memory = own.get("memory")
        if memory and any(isinstance(m, dict) and m.get("key") == "f5_tmm_mem_total"
                          for m in memory.get("metrics") or []):
            # enrich_f5_groups recognises a BIG-IP by its memory group; shape it
            # from the definition (no values needed) so the F5 rules apply.
            shaped += _shape_groups({"memory": memory}, [], ("memory",))
            try:
                shaped = enrich_f5_groups(shaped, now=now)
            except Exception:
                logger.exception("hardware pack: F5 enrichment failed for %s", did)
            shaped = [g for g in shaped if g["key"] in wanted]
        groups[did] = shaped
    out = {"devices": devs, "groups": groups, "defined": defined}
    ctx._cache[key] = out
    return out


def _worst(statuses: Iterable[str]) -> str:
    return max(statuses, key=lambda s: SEV_RANK.get(s, 0), default="none")


def _is_stale(updated: Optional[datetime]) -> bool:
    return updated is not None and updated < _now() - timedelta(hours=STALE_HOURS)


def _health(sev: str, stale: bool) -> str:
    return "stale" if stale else SEV_WORD.get(sev, "n/a")


def _cell_text(c: dict) -> str:
    if c.get("text"):
        return str(c["text"])
    v = _num(c.get("value"))
    if v is None:
        return "—"
    if c.get("type") == "enum":
        return f"code {v:g}"
    return _fmt_value(v, c.get("unit") or "")


def _components(groups: list[dict], scalar_per_metric: bool = False) -> list[dict]:
    """Flatten shaped groups into rows: one per table instance, and one per
    scalar group (or per scalar metric) that returned data."""
    out: list[dict] = []
    for g in groups:
        if g["kind"] == "table":
            cols = {c["key"]: c for c in g.get("columns") or []}
            order = {k: i for i, k in enumerate(cols)}
            for r in g.get("rows") or []:
                cells = []
                for k, c in sorted(r["cells"].items(), key=lambda kv: order.get(kv[0], 999)):
                    col = cols.get(k, {})
                    cells.append({**c, "key": k, "name": c.get("name") or col.get("name") or k,
                                  "unit": c.get("unit") or col.get("unit") or "",
                                  "type": c.get("type") or col.get("type") or "gauge"})
                if cells:
                    out.append({"group": g["key"], "group_name": g["name"], "member": r["label"],
                                "instance": r["instance"], "cells": cells})
        else:
            cells = [dict(m) for m in g.get("metrics") or [] if m.get("has_data")]
            if not cells:
                continue
            if scalar_per_metric:
                for c in cells:
                    out.append({"group": g["key"], "group_name": g["name"], "member": c["name"],
                                "instance": "", "cells": [c]})
            else:
                out.append({"group": g["key"], "group_name": g["name"], "member": "",
                            "instance": "", "cells": cells})
    for row in out:
        stamps = [c["updated_at"] for c in row["cells"] if isinstance(c.get("updated_at"), datetime)]
        row["updated"] = max(stamps) if stamps else None
        row["stale"] = _is_stale(row["updated"])
        row["sev"] = _worst(c.get("status") or "none" for c in row["cells"])
        row["health"] = _health(row["sev"], row["stale"])
    return out


def _pairs(cells: Iterable[dict]) -> str:
    return " · ".join(f"{c['name']}: {_cell_text(c)}" for c in cells) or "—"


def _state_and_details(cells: list[dict]) -> tuple[str, str]:
    """(state text from enum/string cells, details from the rest)."""
    states = [c for c in cells if c.get("type") in ("enum", "string")]
    rest = [c for c in cells if c.get("type") not in ("enum", "string")]
    enums = [c for c in states if c.get("type") == "enum"]
    if len(states) == 1 and enums:
        state = _cell_text(states[0])
    else:
        state = _pairs(states) if states else "—"
    return state, _pairs(rest) if rest else "—"


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (HEALTH_ORDER.get(r["health"], 3), r["device_name"].lower(),
                                       r["group_name"].lower(), (r["member"] or "").lower()))


def _template_rows(state: dict, scalar_per_metric: bool = False) -> list[dict]:
    devs = state["devices"]
    out = []
    for did, groups in state["groups"].items():
        for row in _components(groups, scalar_per_metric):
            row["device"] = did
            row["device_name"] = devs[did]["name"]
            out.append(row)
    return out


def _has_unlabeled(rows: list[dict]) -> bool:
    return any(c.get("type") == "enum" and not c.get("text") and _num(c.get("value")) is not None
               for r in rows for c in r["cells"])


def _stale_note(n: int, noun: str) -> Optional[str]:
    if not n:
        return None
    return (f"{n:,} {noun} were not refreshed in the last {STALE_HOURS} hours; they are shown as "
            "stale with their last update time and are not counted as up or down.")


# ─── CPU and memory ─────────────────────────────────────────────────────────

RESOURCE_METRICS = {"cpu": {"label": "CPU Load", "noun": "CPU", "Noun": "CPU", "color": "primary",
                            "trend": "CPU Load Trend"},
                    "memory": {"label": "Memory Utilization", "noun": "memory", "Noun": "Memory",
                               "color": "info", "trend": "Memory Utilization Trend"}}
SORTS = {"p95": ("p95", "95th percentile"), "avg": ("avg", "Average"), "peak": ("peak", "Peak")}


def _resource_keys(metric: str) -> list[str]:
    return ["cpu"] if metric == "cpu" else ["memory", *(k for k, _ in F5_MEMORY_DOMAINS)]


def _pick_memory(did: str, keyed: dict[str, Any], f5: set[str]) -> Any:
    """A device's overall memory figure: f5_system_memory_pct on an F5 (never
    its legacy 'memory' rows), the canonical 'memory' series elsewhere."""
    if did in f5 or F5_SYSTEM_MEMORY in keyed:
        return keyed.get(F5_SYSTEM_MEMORY)
    return keyed.get("memory")


@register_section(
    "device_resource_utilization", title="Top Devices by CPU or Memory", category="Performance",
    description=("Network devices ranked by average, 95th-percentile or peak CPU or memory "
                 "utilization from SNMP, with the change against the previous period."),
    scope="device", requires=REQUIRES,
    options=[
        {"key": "metric", "label": "Metric", "type": "select", "default": "cpu",
         "choices": [["cpu", "CPU"], ["memory", "Memory"]]},
        {"key": "sort", "label": "Rank by", "type": "select", "default": "p95",
         "choices": [["p95", "95th percentile"], ["avg", "Average"], ["peak", "Peak"]]},
        top_n_option(10, 100),
        {"key": "threshold", "label": "Warning threshold (%)", "type": "number", "default": 80,
         "min": 1, "max": 100,
         "help": "Devices whose 95th percentile reaches this are flagged; critical is 10 points higher."},
    ],
)
async def device_resource_utilization(ctx: SectionCtx) -> dict:
    sid = "device_resource_utilization"
    metric = ctx.opt("metric") if ctx.opt("metric") in RESOURCE_METRICS else "cpu"
    sort_key, sort_label = SORTS.get(ctx.opt("sort"), SORTS["p95"])
    top_n = int(ctx.opt("top_n"))
    warn = float(ctx.opt("threshold"))
    crit = min(100.0, warn + 10)
    m = RESOURCE_METRICS[metric]
    title = f"Top {top_n} Devices by {m['label']}"
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)

    keys = _resource_keys(metric)
    dsql, dparams = ctx.ch_device_clause("device_id")
    rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, metric_key, sum(avg_value * sample_count) AS s, "
        "sum(sample_count) AS n, quantile(0.95)(avg_value) AS p95, max(max_value) AS peak, "
        "argMax(timestamp, max_value) AS peak_at FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND metric_key IN %(keys)s"
        f"{dsql} GROUP BY did, metric_key",
        {"f": ctx.frm, "t": ctx.to, "keys": keys, **dparams})
    pf, pt = ctx.prior_window()
    prior_rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, metric_key, sum(avg_value * sample_count) AS s, "
        "sum(sample_count) AS n FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND metric_key IN %(keys)s"
        f"{dsql} GROUP BY did, metric_key",
        {"f": pf, "t": pt, "keys": keys, **dparams})

    cur: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        did = str(r[0])
        n = _num(r[3]) or 0.0
        if did not in devs or n <= 0:
            continue
        cur[did][r[1]] = {"s": _num(r[2]) or 0.0, "n": n, "avg": (_num(r[2]) or 0.0) / n,
                          "p95": _num(r[4]), "peak": _num(r[5]), "peak_at": r[6]}
    prev: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in prior_rows:
        did = str(r[0])
        n = _num(r[3]) or 0.0
        if did in devs and n > 0:
            prev[did][r[1]] = {"s": _num(r[2]) or 0.0, "n": n}

    f5 = {did for did, d in devs.items() if _is_f5(d)}
    f5 |= {did for did, keyed in cur.items() if F5_SYSTEM_MEMORY in keyed}
    pick = (lambda did, keyed: keyed.get("cpu")) if metric == "cpu" else \
        (lambda did, keyed: _pick_memory(did, keyed, f5))

    stats: list[dict] = []
    fleet_s = fleet_n = prev_s = prev_n = 0.0
    for did, keyed in cur.items():
        st = pick(did, keyed)
        if not st:
            continue
        p = pick(did, prev.get(did, {}))
        fleet_s += st["s"]
        fleet_n += st["n"]
        if p:
            prev_s += p["s"]
            prev_n += p["n"]
        stats.append({"id": did, "name": devs[did]["name"], "vendor": _vendor(devs[did]),
                      "avg": st["avg"], "p95": st["p95"], "peak": st["peak"],
                      "peak_at": st["peak_at"], "prev": p["s"] / p["n"] if p else None})
    stats.sort(key=lambda x: (-(x[sort_key] if x[sort_key] is not None else -1.0), x["name"].lower()))
    listed = stats[:top_n]
    flagged = [x for x in stats if x["p95"] is not None and x["p95"] >= warn]
    critical = [x for x in flagged if x["p95"] >= crit]
    fleet = fleet_s / fleet_n if fleet_n > 0 else None
    fleet_prev = prev_s / prev_n if prev_n > 0 else None
    snmp_devs = [d for d in devs.values() if _direct_snmp(d)]
    expected = len(snmp_devs) or len(devs)
    top = listed[0] if listed else None

    kpis = [
        _kpi(f"Fleet average {m['noun']}", _pct(fleet), m["color"] if fleet is not None else "neutral",
             _change_pts(fleet, fleet_prev)),
        _kpi(f"Highest {sort_label.lower()}", _pct(top[sort_key]) if top else "—",
             _accent(_level(top["p95"], warn, crit)) if top else "neutral",
             top["name"] if top else "Not measured"),
        _kpi(f"Devices ≥ {warn:g}% (p95)", _count(len(flagged)),
             "danger" if critical else "warning" if flagged else ("success" if stats else "neutral"),
             f"{len(critical):,} at or above {crit:g}%"),
        _kpi("Devices measured", f"{len(stats):,} of {expected:,}", "neutral",
             "SNMP-polled devices in scope"),
    ]
    styles = ["text", "text", "pct", "pct", "pct", "text", "num", "status"]
    styles[2 + ("avg", "p95", "peak").index(sort_key)] = "pct-bar"
    table_rows = [[x["name"], x["vendor"], _pct(x["avg"]), _pct(x["p95"]), _pct(x["peak"]),
                   ctx.fmt_short(x["peak_at"]) if x["peak"] is not None else "—",
                   _delta_pts(x["avg"], x["prev"]), _level(x["p95"], warn, crit)] for x in listed]
    tables = [{"title": _truncated(f"Devices by {sort_label.lower()} {m['noun']}", len(listed),
                                   len(stats), "measured devices"),
               "headers": ["Device", "Vendor / model", f"Avg {m['noun']}", f"P95 {m['noun']}",
                           f"Peak {m['noun']}", "Peak at", "Avg change", "Load"],
               "styles": styles, "rows": table_rows}] if listed else []
    charts = [{"title": f"{sort_label} {m['noun']} by device (%)",
               "series": {"kind": "rank", "unit": "%", "color": m["color"],
                          "points": [{"label": x["name"], "value": round(x[sort_key], 2)}
                                     for x in listed if x[sort_key] is not None]}}] if listed else []

    notes = ["Source: 5-minute SNMP rollups. The average is weighted by sample count, the 95th "
             "percentile is taken over 5-minute averages and the peak is the highest single poll. "
             f"Load is graded on the 95th percentile (warning {warn:g}%, critical {crit:g}%); "
             "'Avg change' compares the average with the previous period of equal length."]
    if metric == "cpu":
        notes.append("CPU is each device's headline processor load; on chassis and multi-processor "
                     "devices it is the busiest processor (as on the device page), so one saturated "
                     "route processor or data plane is not averaged away.")
    else:
        f5_rows = [(did, cur[did]) for did in sorted(f5, key=lambda d: devs[d]["name"].lower())
                   if did in cur]
        f5_missing = [did for did in f5 if F5_SYSTEM_MEMORY not in cur.get(did, {})
                      and _direct_snmp(devs[did])]
        if f5:
            notes.append("F5 BIG-IP memory is system RAM (sysGlobalHostMemUsed ÷ sysGlobalHostMemTotal); "
                         "TMM, non-TMM and swap are separate measurements and never stand in for overall RAM.")
        if f5_missing:
            notes.append(f"{len(f5_missing):,} F5 device(s) have no system RAM samples in this period. "
                         "Their legacy memory values may hold the TMM maximum and are not used (not measured).")
        if f5_rows:
            def dom(keyed: dict, key: str) -> str:
                d = keyed.get(key)
                return f"{_pct(d['avg'])} / {_pct(d['peak'])}" if d else "—"
            tables.append({
                "title": "F5 BIG-IP memory domains (average / peak)",
                "headers": ["Device", *(label for _k, label in F5_MEMORY_DOMAINS)],
                "styles": ["text", "text", "text", "text", "text"],
                "rows": [[devs[did]["name"], *(dom(keyed, k) for k, _l in F5_MEMORY_DOMAINS)]
                         for did, keyed in f5_rows]})
    missing = sum(1 for d in snmp_devs if d["id"] not in {x["id"] for x in stats})
    if missing:
        notes.append(f"{missing:,} SNMP-polled device(s) in scope sent no {m['noun']} samples in this "
                     "period (not measured).")
    if not stats:
        notes.insert(0, f"No device {m['noun']} samples in this period.")
    wn = ctx.window_note(ROLLUP_DAYS, SNMP_HISTORY)
    if wn:
        notes.append(wn)
    return _section(
        sid, title,
        description=(f"Top {top_n} devices by {sort_label.lower()} {m['noun']} utilization over the "
                     f"period ({len(stats):,} measured device(s))."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


def _memory_condition(f5_ids: set[str], dsql: str) -> tuple[str, dict]:
    """ClickHouse condition selecting each device's overall memory series:
    f5_system_memory_pct on F5 devices, 'memory' everywhere else."""
    excl = "toString(device_id) NOT IN %(f5_ids)s AND " if f5_ids else ""
    cond = ("((metric_key = 'memory' AND " + excl + "device_id NOT IN (SELECT device_id FROM "
            "zenplus.snmp_metrics_5m WHERE metric_key = 'f5_system_memory_pct' AND timestamp >= %(f)s "
            f"AND timestamp < %(t)s{dsql})) OR metric_key = 'f5_system_memory_pct')")
    return cond, ({"f5_ids": sorted(f5_ids)} if f5_ids else {})


@register_section(
    "device_resource_trend", title="CPU and Memory Trend", category="Performance",
    description=("Average, 95th-percentile and peak CPU and memory of network devices over time, "
                 "for the whole scope or per device."),
    scope="device", requires=REQUIRES,
    options=[
        {"key": "metric", "label": "Metric", "type": "select", "default": "both",
         "choices": [["both", "CPU and memory"], ["cpu", "CPU"], ["memory", "Memory"]]},
        {"key": "breakdown", "label": "Show", "type": "select", "default": "fleet",
         "choices": [["fleet", "Scope average, 95th percentile and peak"],
                     ["devices", "One line per device (busiest first)"]]},
        {"key": "top_n", "label": "Devices to chart", "type": "int", "default": 5, "min": 1,
         "max": 10, "help": "Lines in the per-device view."},
    ],
)
async def device_resource_trend(ctx: SectionCtx) -> dict:
    sid, title = "device_resource_trend", "CPU and Memory Trend"
    choice = ctx.opt("metric")
    metrics = ["cpu", "memory"] if choice not in ("cpu", "memory") else [choice]
    if len(metrics) == 1:
        title = RESOURCE_METRICS[metrics[0]]["trend"]
    per_device = ctx.opt("breakdown") == "devices"
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)

    interval = ctx.bucket()
    step = _step_seconds(interval)
    bucket, bparams = ctx.ch_bucket("timestamp", interval)
    dsql, dparams = ctx.ch_device_clause("device_id")
    f5_ids = {did for did, d in devs.items() if _is_f5(d)}
    pf, pt = ctx.prior_window()

    kpis: list[dict] = []
    charts: list[dict] = []
    reporting: set[str] = set()
    any_data = False
    for metric in metrics:
        m = RESOURCE_METRICS[metric]
        if metric == "cpu":
            cond, cparams = "metric_key = 'cpu'", {}
        else:
            cond, cparams = _memory_condition(f5_ids, dsql)
        base = {"f": ctx.frm, "t": ctx.to, **dparams, **cparams}
        where = f"timestamp >= %(f)s AND timestamp < %(t)s AND {cond}{dsql}"
        prior = await ctx.ch_rows(
            "SELECT sum(avg_value * sample_count) AS s, sum(sample_count) AS n "
            f"FROM zenplus.snmp_metrics_5m WHERE {where}", {**base, "f": pf, "t": pt})
        prev = None
        if prior and (_num(prior[0][1]) or 0) > 0:
            prev = (_num(prior[0][0]) or 0.0) / _num(prior[0][1])

        if per_device:
            ranked = await ctx.ch_rows(
                "SELECT toString(device_id) AS did, sum(avg_value * sample_count) AS s, "
                f"sum(sample_count) AS n FROM zenplus.snmp_metrics_5m WHERE {where} "
                "GROUP BY did HAVING n > 0 ORDER BY s / n DESC", base)
            ranked = [r for r in ranked if str(r[0]) in devs and (_num(r[2]) or 0) > 0]
            reporting |= {str(r[0]) for r in ranked}
            tot_s = sum(_num(r[1]) or 0.0 for r in ranked)
            tot_n = sum(_num(r[2]) or 0.0 for r in ranked)
            fleet = tot_s / tot_n if tot_n > 0 else None
            chosen = [str(r[0]) for r in ranked[:top_n]]
            buckets: dict[str, dict] = defaultdict(dict)
            if chosen:
                for r in await ctx.ch_rows(
                        f"SELECT {bucket} AS b, toString(device_id) AS did, "
                        "sum(avg_value * sample_count) AS s, sum(sample_count) AS n "
                        f"FROM zenplus.snmp_metrics_5m WHERE {where} AND toString(device_id) IN %(chosen)s "
                        "GROUP BY b, did ORDER BY b", {**base, **bparams, "chosen": chosen}):
                    n = _num(r[3]) or 0.0
                    if n > 0:
                        buckets[str(r[1])][_aware(r[0])] = (_num(r[2]) or 0.0) / n
            stamps = sorted({ts for per in buckets.values() for ts in per if ts is not None})
            series = []
            for did in chosen:
                pts = _time_points(ctx, [(ts, buckets[did].get(ts)) for ts in stamps], step)
                series.append({"name": devs[did]["name"], "points": pts})
            if series and stamps:
                any_data = True
                charts.append({"title": f"{m['Noun']} by device — busiest {len(chosen)} (%)",
                               "series": {"kind": "line", "unit": "%", "color": m["color"],
                                          "y_domain": [0, 100], "points": series[0]["points"],
                                          "series": series}})
            busiest = ranked[0] if ranked else None
            kpis.append(_kpi(f"Average {m['noun']}", _pct(fleet),
                             m["color"] if fleet is not None else "neutral", _change_pts(fleet, prev)))
            kpis.append(_kpi(f"Top device {m['noun']}",
                             _pct((_num(busiest[1]) or 0) / _num(busiest[2])) if busiest else "—",
                             "warning" if busiest else "neutral",
                             devs[str(busiest[0])]["name"] if busiest else "Not measured"))
            continue

        rows = await ctx.ch_rows(
            f"SELECT {bucket} AS b, sum(avg_value * sample_count) AS s, sum(sample_count) AS n, "
            "quantile(0.95)(avg_value) AS p95, max(max_value) AS peak, uniqExact(device_id) AS devs "
            f"FROM zenplus.snmp_metrics_5m WHERE {where} GROUP BY b ORDER BY b",
            {**base, **bparams})
        avg_pts, p95_pts, peak_pts = [], [], []
        tot_s = tot_n = 0.0
        worst: Optional[tuple[float, Any]] = None
        top_peak: Optional[tuple[float, Any]] = None
        for r in rows:
            n = _num(r[2]) or 0.0
            if n <= 0:
                continue
            s = _num(r[1]) or 0.0
            tot_s += s
            tot_n += n
            p95, peak = _num(r[3]), _num(r[4])
            avg_pts.append((r[0], s / n))
            p95_pts.append((r[0], p95))
            peak_pts.append((r[0], peak))
            if p95 is not None and (worst is None or p95 > worst[0]):
                worst = (p95, r[0])
            if peak is not None and (top_peak is None or peak > top_peak[0]):
                top_peak = (peak, r[0])
        fleet = tot_s / tot_n if tot_n > 0 else None
        kpis.append(_kpi(f"Average {m['noun']}", _pct(fleet),
                         m["color"] if fleet is not None else "neutral", _change_pts(fleet, prev)))
        kpis.append(_kpi(f"Highest p95 {m['noun']}", _pct(worst[0]) if worst else "—",
                         _accent(_level(worst[0], 80, 90)) if worst else "neutral",
                         ctx.fmt_short(worst[1]) if worst else "Not measured"))
        if avg_pts:
            any_data = True
            avg = _time_points(ctx, avg_pts, step)
            charts.append({"title": f"{m['Noun']} — average, 95th percentile and peak (%)",
                           "series": {"kind": "line", "unit": "%", "color": m["color"],
                                      "y_domain": [0, 100], "points": avg,
                                      "series": [{"name": "Average", "points": avg},
                                                 {"name": "95th percentile",
                                                  "points": _time_points(ctx, p95_pts, step)},
                                                 {"name": "Peak",
                                                  "points": _time_points(ctx, peak_pts, step)}]}})
        if top_peak and len(metrics) == 1:
            kpis.append(_kpi(f"Peak {m['noun']}", _pct(top_peak[0]), "warning",
                             ctx.fmt_short(top_peak[1])))

    if not per_device:
        rep = await ctx.ch_rows(
            "SELECT toString(device_id) AS did FROM zenplus.snmp_metrics_5m "
            "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND metric_key IN %(keys)s"
            f"{dsql} GROUP BY did HAVING sum(sample_count) > 0",
            {"f": ctx.frm, "t": ctx.to, "keys": [k for mm in metrics for k in _resource_keys(mm)],
             **dparams})
        reporting = {str(r[0]) for r in rep if str(r[0]) in devs}
    snmp_devs = [d for d in devs.values() if _direct_snmp(d)]
    kpis.append(_kpi("Devices reporting", f"{len(reporting):,} of {len(snmp_devs) or len(devs):,}",
                     "neutral", "SNMP-polled devices in scope"))

    what = " and ".join(RESOURCE_METRICS[mm]["noun"] for mm in metrics)
    notes = [f"Each point is a {_interval_label(interval)} interval ({ctx.tz_label()}). "
             + ("Each line is one device's sample-weighted average; the busiest devices by average "
                "over the period are drawn." if per_device else
                "The average is weighted by sample count, the 95th percentile is taken across every "
                "device's 5-minute averages in the interval and the peak is the highest single poll.")
             + " Intervals without samples are left as gaps."]
    if "memory" in metrics and f5_ids:
        notes.append("F5 BIG-IP memory is system RAM (f5_system_memory_pct); legacy memory values that "
                     "may hold the TMM maximum are not used.")
    missing = len([d for d in snmp_devs if d["id"] not in reporting])
    if missing:
        notes.append(f"{missing:,} SNMP-polled device(s) in scope sent no {what} samples in this "
                     "period (not measured).")
    wn = ctx.window_note(ROLLUP_DAYS, SNMP_HISTORY)
    if wn:
        notes.append(wn)
    if not any_data:
        notes.insert(0, f"No device {what} samples in this period.")
    scope_txt = f"the {len(devs):,} device(s) in scope" if ctx.device_ids else "all devices"
    return _section(
        sid, title,
        description=(f"{what[:1].upper()}{what[1:]} utilization of {scope_txt} per "
                     f"{_interval_label(interval)} interval over the period."),
        kpis=kpis, charts=charts, notes=notes)


# ─── Environment: temperature, fans and power ───────────────────────────────

ENV_GROUPS = ("fans", "psus", "psu", "temperature", "sensors", "health", "alarms")
ENTITY_RE = re.compile(r"^(temperature|fan|power|voltage|sensor_oper_status)_(\d+)$")
ENTITY_KIND = {"temperature": "Temperature", "fan": "Fan", "power": "Power", "voltage": "Voltage"}
ENTITY_UNIT = {"temperature": "°C", "fan": "RPM", "power": "W", "voltage": "V"}
SENSOR_TYPE_KIND = {"celsius": "Temperature", "rpm": "Fan", "voltsdc": "Voltage", "voltsac": "Voltage",
                    "watts": "Power", "amperes": "Current", "percentrh": "Humidity"}
OPER_STATUS = {2: ("Unavailable", "warning"), 3: ("Nonoperational", "critical")}


def _component_class(group: str, name: str) -> str:
    if group == "fans":
        return "Fan"
    if group in ("psus", "psu"):
        return "Power supply"
    if group == "temperature":
        return "Temperature"
    if group == "alarms":
        return "Chassis alarm"
    n = name.lower()
    if "temp" in n:          # 'PS1 Temp' is a temperature reading, not the supply
        return "Temperature"
    if "fan" in n:
        return "Fan"
    if re.search(r"\b(ps|psu|pem)\s?\d*\b|power|supply", n):
        return "Power supply"
    if "volt" in n:
        return "Voltage"
    return "Component"


def _is_temperature(cell: dict, row_label: str) -> bool:
    unit = (cell.get("unit") or "").strip().lower()
    if unit in ("°c", "c", "celsius"):
        return True
    return not unit and cell.get("type") == "gauge" and \
        "temp" in f"{cell.get('name', '')} {row_label}".lower()


@register_section(
    "hardware_environment", title="Hardware Health: Temperature, Fans and Power",
    category="Hardware",
    description=("Temperature sensors over the period, fan, power-supply and chassis states as of "
                 "report generation, and the devices that return no hardware readings."),
    scope="device", requires=REQUIRES,
    options=[
        {"key": "temp_threshold", "label": "Temperature threshold (°C)", "type": "number",
         "default": 60, "min": 20, "max": 120,
         "help": "Temperature sensors whose peak reaches this are flagged."},
        top_n_option(25, 200),
    ],
)
async def hardware_environment(ctx: SectionCtx) -> dict:
    sid, title = "hardware_environment", "Hardware Health: Temperature, Fans and Power"
    thr = float(ctx.opt("temp_threshold"))
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, ENV_GROUPS)
    comps = _template_rows(state, scalar_per_metric=True)

    # Template temperature series (vendor tables) are charted with the
    # ENTITY-SENSOR temperatures; their history lives under series_key.
    tpl_temp: dict[tuple[str, str], str] = {}
    for did, groups in state["groups"].items():
        for g in groups:
            if g["kind"] == "table":
                rows = g.get("rows") or []
            else:
                rows = [{"label": "", "cells": {m["key"]: m for m in g.get("metrics") or []
                                                if m.get("has_data")}}]
            for r in rows:
                for c in r["cells"].values():
                    sk = c.get("series_key")
                    if sk and _is_temperature(c, r.get("label", "")):
                        label = " ".join(x for x in (r.get("label") or "", c.get("name") or "") if x)
                        tpl_temp[(did, sk)] = label or sk

    dsql, dparams = ctx.ch_device_clause("device_id")
    tpl_keys = sorted({sk for _did, sk in tpl_temp}) or ["__none__"]
    numeric = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, metric_key, "
        "sum(avg_value * sample_count) / sum(sample_count) AS a, min(min_value) AS lo, "
        "max(max_value) AS hi, argMax(avg_value, timestamp) AS last_v, "
        "argMax(timestamp, max_value) AS hi_at, max(timestamp) AS last_at FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND (match(metric_key, "
        "'^(temperature|fan|power|voltage|sensor_oper_status)_[0-9]+$') OR metric_key IN %(tpl)s)"
        f"{dsql} GROUP BY did, metric_key HAVING sum(sample_count) > 0",
        {"f": ctx.frm, "t": ctx.to, "tpl": tpl_keys, **dparams})

    clause, sparams = ctx.pg_device_clause("s.device_id")
    sensor_rows = (await ctx.db.execute(text(
        "SELECT s.device_id::text, s.sensor_index, COALESCE(NULLIF(s.description, ''), ''), "
        "COALESCE(s.sensor_type, ''), COALESCE(s.unit, '') FROM device_sensors s "
        f"WHERE TRUE{clause}"), sparams)).fetchall()
    sensor_names = {(str(r[0]), int(r[1])): (r[2], r[3], r[4]) for r in sensor_rows}

    temps: list[dict] = []
    others: list[dict] = []
    problems: list[dict] = []
    with_readings: set[str] = set()
    for r in numeric:
        did, key = str(r[0]), r[1]
        if did not in devs:
            continue
        a, lo, hi, last = _num(r[2]), _num(r[3]), _num(r[4]), _num(r[5])
        mt = ENTITY_RE.match(key)
        if mt:
            kind, idx = mt.group(1), int(mt.group(2))
            desc, stype, unit = sensor_names.get((did, idx), ("", "", ""))
            name = desc or f"Sensor {idx}"
            if kind == "sensor_oper_status":
                code = int(last) if last is not None else None
                if code in OPER_STATUS:
                    word, health = OPER_STATUS[code]
                    problems.append({"device": did, "component": name,
                                     "kind": f"{SENSOR_TYPE_KIND.get(stype.lower(), 'Other')} sensor",
                                     "reading": word, "health": health,
                                     "updated": r[7] if len(r) > 7 else None})
                with_readings.add(did)
                continue
            entry = {"device": did, "name": name, "kind": ENTITY_KIND[kind],
                     "unit": unit or ENTITY_UNIT[kind], "avg": a, "lo": lo, "hi": hi, "last": last,
                     "hi_at": r[6]}
        elif (did, key) in tpl_temp:
            entry = {"device": did, "name": tpl_temp[(did, key)], "kind": "Temperature", "unit": "°C",
                     "avg": a, "lo": lo, "hi": hi, "last": last, "hi_at": r[6]}
        else:
            continue
        with_readings.add(did)
        (temps if entry["kind"] == "Temperature" else others).append(entry)

    fans = psus = 0
    fan_bad = psu_bad = 0
    for c in comps:
        kind = _component_class(c["group"], f"{c['member']} {c['cells'][0].get('name', '')}")
        c["kind"] = kind
        with_readings.add(c["device"])
        bad = c["health"] in ("warning", "critical")
        if kind == "Fan":
            fans += 1
            fan_bad += bad
        elif kind == "Power supply":
            psus += 1
            psu_bad += bad
        if bad or c["health"] == "stale":
            state_txt, _details = _state_and_details(c["cells"])
            reading = state_txt if state_txt != "—" else _pairs(c["cells"])
            problems.append({"device": c["device"], "component": c["member"] or c["group_name"],
                             "kind": kind, "reading": reading, "health": c["health"],
                             "updated": c["updated"]})

    for t in temps:
        t["status"] = ("critical" if t["last"] is not None and t["last"] >= thr else
                       "warning" if t["hi"] is not None and t["hi"] >= thr else "ok")
    temps.sort(key=lambda t: (-(t["hi"] if t["hi"] is not None else -1e9),
                              devs[t["device"]]["name"].lower(), t["name"].lower()))
    hot = [t for t in temps if t["hi"] is not None and t["hi"] >= thr]
    hot_now = [t for t in hot if t["last"] is not None and t["last"] >= thr]
    hottest = temps[0] if temps and temps[0]["hi"] is not None else None

    snmp_devs = [d for d in devs.values() if _direct_snmp(d)]
    without = [d for d in snmp_devs if d["id"] not in with_readings]
    ping_only = sum(1 for d in devs.values() if not d["snmp"])

    kpis = [
        _kpi("Devices with readings", f"{len([d for d in snmp_devs if d['id'] in with_readings]):,} "
             f"of {len(snmp_devs):,}", "primary", "SNMP-polled devices in scope"),
        _kpi("Hottest sensor", f"{hottest['hi']:.1f} °C" if hottest else "—",
             ("danger" if hottest["hi"] >= thr else "success") if hottest else "neutral",
             f"{devs[hottest['device']]['name']} · {hottest['name']}" if hottest else "Not measured"),
        _kpi(f"Sensors ≥ {thr:g} °C", _count(len(hot)),
             "danger" if hot_now else "warning" if hot else ("success" if temps else "neutral"),
             f"{len(hot_now):,} at or above it at the latest reading"),
        _kpi("Fan faults", _count(fan_bad), "danger" if fan_bad else ("success" if fans else "neutral"),
             f"of {fans:,} fan(s) reporting state"),
        _kpi("Power supply faults", _count(psu_bad),
             "danger" if psu_bad else ("success" if psus else "neutral"),
             f"of {psus:,} power supplies reporting state"),
        _kpi("Devices without readings", _count(len(without)), "warning" if without else "neutral",
             "Not measured — not a healthy result"),
    ]

    charts: list[dict] = []
    shown_t = temps[:top_n]
    if shown_t:
        charts.append({"title": "Highest temperatures in the period (°C)",
                       "series": {"kind": "rank", "unit": "°C", "color": "warning",
                                  "points": [{"label": f"{devs[t['device']]['name']} · {t['name']}",
                                              "value": round(t["hi"], 1)}
                                             for t in shown_t[:10] if t["hi"] is not None]}})
        interval = ctx.bucket()
        bucket, bparams = ctx.ch_bucket("timestamp", interval)
        trend = await ctx.ch_rows(
            f"SELECT {bucket} AS b, max(max_value) AS hi, "
            "sum(avg_value * sample_count) / sum(sample_count) AS a FROM zenplus.snmp_metrics_5m "
            "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND (match(metric_key, "
            "'^temperature_[0-9]+$') OR metric_key IN %(tpl)s)"
            f"{dsql} GROUP BY b HAVING sum(sample_count) > 0 ORDER BY b",
            {"f": ctx.frm, "t": ctx.to, "tpl": tpl_keys, **dparams, **bparams})
        step = _step_seconds(interval)
        hi_pts = _time_points(ctx, [(r[0], _num(r[1])) for r in trend], step)
        if hi_pts:
            charts.append({"title": "Temperature over time — hottest sensor and sensor average (°C)",
                           "series": {"kind": "line", "unit": "°C", "color": "warning",
                                      "points": hi_pts,
                                      "series": [{"name": "Hottest sensor", "points": hi_pts},
                                                 {"name": "Average of sensors",
                                                  "points": _time_points(
                                                      ctx, [(r[0], _num(r[2])) for r in trend], step)}]}})

    tables: list[dict] = []
    problems.sort(key=lambda p: (HEALTH_ORDER.get(p["health"], 3), devs[p["device"]]["name"].lower(),
                                 p["component"].lower()))
    if problems:
        shown = problems[:top_n]
        tables.append({
            "title": _truncated("Components needing attention", len(shown), len(problems), "components"),
            "headers": ["Device", "Component", "Type", "Reading", "Health", "Updated"],
            "styles": ["text", "text", "text", "text", "status", "text"],
            "rows": [[devs[p["device"]]["name"], p["component"], p["kind"], p["reading"], p["health"],
                      ctx.fmt_short(p["updated"]) if p["updated"] else "—"] for p in shown]})
    if shown_t:
        tables.append({
            "title": _truncated("Temperature sensors (hottest first)", len(shown_t), len(temps), "sensors"),
            "headers": ["Device", "Sensor", "Latest", "Average", "Peak", "Peak at", "Status"],
            "styles": ["text", "text", "num", "num", "num", "text", "status"],
            "rows": [[devs[t["device"]]["name"], t["name"],
                      *(_fmt_value(v, "°C") if v is not None else "—" for v in (t["last"], t["avg"], t["hi"])),
                      ctx.fmt_short(t["hi_at"]) if t["hi"] is not None else "—", t["status"]]
                     for t in shown_t]})
    if others:
        others.sort(key=lambda o: (devs[o["device"]]["name"].lower(), o["kind"], o["name"].lower()))
        shown_o = others[:top_n]
        tables.append({
            "title": _truncated("Fan, power and voltage readings", len(shown_o), len(others), "sensors"),
            "headers": ["Device", "Sensor", "Type", "Latest", "Minimum", "Maximum"],
            "styles": ["text", "text", "text", "num", "num", "num"],
            "rows": [[devs[o["device"]]["name"], o["name"], o["kind"],
                      *(_fmt_value(v, o["unit"]) if v is not None else "—"
                        for v in (o["last"], o["lo"], o["hi"]))] for o in shown_o]})
    if without:
        shown_w = without[:top_n]

        def reason(d: dict) -> str:
            # f5_insights.enrich_f5_groups explains empty BIG-IP chassis groups.
            for g in state["groups"].get(d["id"], []):
                if g.get("no_data_reason"):
                    return g["no_data_reason"]
            if state["defined"].get(d["id"]):
                return "The template defines chassis health, but the device returned no rows."
            return "No ENTITY-SENSOR readings and no chassis-health template data."
        tables.append({
            "title": _truncated("Devices without hardware readings", len(shown_w), len(without), "devices"),
            "headers": ["Device", "Vendor / model", "Template", "Reason"],
            "styles": ["text", "text", "text", "text"],
            "rows": [[d["name"], _vendor(d), d["profile"] or "—", reason(d)] for d in shown_w]})

    notes = ["Temperatures and fan, power and voltage readings come from ENTITY-SENSOR-MIB and vendor "
             "template series (5-minute rollups over the period). Fan, power-supply and chassis states "
             "are the latest template values as of report generation, graded with each template's own "
             f"thresholds as on the device page; the {thr:g} °C threshold applies to the temperature table.",
             "A device that returns no hardware readings is 'not measured', not healthy: virtual "
             "appliances (for example BIG-IP Virtual Edition) commonly expose none."]
    if not temps and not comps and not problems:
        notes.insert(0, "No hardware readings for devices in scope in this period.")
    sn = _stale_note(sum(1 for c in comps if c["stale"]), "component state(s)")
    if sn:
        notes.append(sn)
    if _has_unlabeled(comps):
        notes.append(UNLABELED_NOTE)
    if ping_only:
        notes.append(f"{ping_only:,} device(s) in scope are not polled by SNMP and are not expected to "
                     "return hardware readings.")
    wn = ctx.window_note(ROLLUP_DAYS, SNMP_HISTORY)
    if wn:
        notes.append(wn)
    return _section(
        sid, title,
        description=(f"Temperature, fan and power health of {len(snmp_devs):,} SNMP-polled device(s): "
                     "sensor peaks over the period and component states as of report generation."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── High availability and redundancy ───────────────────────────────────────

HA_GROUPS = ("ha", "failover", "redundancy", "stack", "ha_members", "stack_members",
             "vsf_members", "std_vrrp_routers", "std_vrrpv3_routers")
STATE_OPTIONS = [top_n_option(50, 500),
                 {"key": "problems_only", "label": "Only list items that are not healthy",
                  "type": "bool", "default": False}]


def _state_kpis(per_dev: dict[str, str], noun: str) -> list[dict]:
    counts = Counter(per_dev.values())
    healthy = counts.get("ok", 0) + counts.get("info", 0)
    return [
        _kpi("Healthy", _count(healthy), "success" if healthy else "neutral", f"{noun} ok or standalone"),
        _kpi("Degraded", _count(counts.get("warning", 0)),
             "warning" if counts.get("warning") else "neutral"),
        _kpi("Critical", _count(counts.get("critical", 0)),
             "danger" if counts.get("critical") else ("success" if per_dev else "neutral")),
        _kpi("Stale", _count(counts.get("stale", 0)), "warning" if counts.get("stale") else "neutral",
             f"Not refreshed in {STALE_HOURS} h"),
    ]


def _device_health(rows: list[dict]) -> dict[str, str]:
    """Device -> worst health across its fresh rows ('stale' when all are stale)."""
    out: dict[str, str] = {}
    by_dev: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_dev[r["device"]].append(r)
    for did, rs_ in by_dev.items():
        fresh = [r for r in rs_ if not r["stale"]]
        if not fresh:
            out[did] = "stale"
            continue
        out[did] = _health(_worst(r["sev"] for r in fresh), False)
    return out


def _health_donut(per_dev: dict[str, str], unit: str) -> dict:
    counts = Counter(per_dev.values())
    order = ["ok", "info", "warning", "critical", "stale", "n/a"]
    labels = {"ok": "Healthy", "info": "Standalone / informational", "warning": "Degraded",
              "critical": "Critical", "stale": "Stale", "n/a": "Not graded"}
    return {"kind": "donut", "unit": unit,
            "points": [{"label": labels[k], "value": counts[k]} for k in order if counts.get(k)]}


@register_section(
    "ha_redundancy_state", title="High Availability and Redundancy Status", category="Availability",
    description=("HA cluster, failover, stack, VSF, routing-engine and VRRP state of each device, "
                 "graded with its template, as of report generation."),
    scope="device", requires=REQUIRES, semantics="snapshot", options=STATE_OPTIONS,
)
async def ha_redundancy_state(ctx: SectionCtx) -> dict:
    sid, title = "ha_redundancy_state", "High Availability and Redundancy Status"
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, HA_GROUPS)
    rows = _template_rows(state)
    if not rows:
        return _empty(sid, title, "No HA, failover, stack or VRRP data for devices in scope.",
                      "High-availability and redundancy state as of report generation.")
    per_dev = _device_health(rows)
    members = sum(1 for r in rows if r["instance"])
    listed = [r for r in rows if r["health"] not in ("ok", "info")] if ctx.opt("problems_only") else rows
    listed = _sort_rows(listed)
    shown = listed[:top_n]
    kpis = [_kpi("Devices with HA data", _count(len(per_dev)), "primary",
                 f"{members:,} member(s), peer(s) or VRRP group(s)")] + _state_kpis(per_dev, "Devices")
    table_rows = []
    for r in shown:
        st, details = _state_and_details(r["cells"])
        table_rows.append([r["device_name"], r["group_name"], r["member"] or "—", st, details,
                           r["health"], ctx.fmt_short(r["updated"]) if r["updated"] else "—"])
    notes = ["Each state is graded with the device template's label map, as on the device page. "
             "Standalone units are informational, not faults; a single-member Cisco stack cannot "
             "close its ring and is shown as standalone.",
             "VRRP rows come from the standard VRRP MIBs on routers, switches and firewalls."]
    sn = _stale_note(sum(1 for r in rows if r["stale"]), "item(s)")
    if sn:
        notes.append(sn)
    if _has_unlabeled(rows):
        notes.append(UNLABELED_NOTE)
    if ctx.opt("problems_only") and not listed:
        notes.insert(0, "Every HA, stack and VRRP item in scope is healthy.")
    return _section(
        sid, title,
        description=(f"High-availability and redundancy state of {len(per_dev):,} device(s) as of "
                     "report generation."),
        kpis=kpis,
        charts=[{"title": "Devices by HA health", "series": _health_donut(per_dev, "devices")}],
        tables=[{"title": _truncated("HA, stack and redundancy members", len(shown), len(listed), "items"),
                 "headers": ["Device", "Component", "Member", "State", "Details", "Health", "Updated"],
                 "styles": ["text", "text", "text", "text", "text", "status", "text"],
                 "rows": table_rows}] if shown else [],
        notes=notes)


# ─── VPN tunnels ────────────────────────────────────────────────────────────

VPN_GROUPS = ("ipsec", "sdwan", "vpn", "sslvpn", "globalprotect")
TUNNEL_GROUPS = {"ipsec": "IPsec", "sdwan": "SD-WAN"}
UP_WORD = {"ok": "up", "crit": "down", "warn": "degraded"}


def _state_cell(cells: list[dict]) -> Optional[dict]:
    """The row's state enum: the first graded enum, else the first enum."""
    enums = [c for c in cells if c.get("type") == "enum"]
    graded = [c for c in enums if c.get("status") not in (None, "none")]
    return (graded or enums or [None])[0]


@register_section(
    "vpn_tunnel_state", title="VPN Tunnel Status", category="Availability",
    description=("IPsec tunnels, SD-WAN links and remote-access VPN usage reported by firewalls, "
                 "as of report generation."),
    scope="device", requires=REQUIRES, semantics="snapshot", options=STATE_OPTIONS,
)
async def vpn_tunnel_state(ctx: SectionCtx) -> dict:
    sid, title = "vpn_tunnel_state", "VPN Tunnel Status"
    top_n = int(ctx.opt("top_n"))
    problems_only = bool(ctx.opt("problems_only"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, VPN_GROUPS)
    rows = _template_rows(state)
    if not rows:
        return _empty(sid, title, "No VPN, IPsec or SD-WAN data for devices in scope.",
                      "VPN tunnel and SD-WAN state as of report generation.")
    tunnels = [r for r in rows if r["group"] in TUNNEL_GROUPS]
    remote = [r for r in rows if r["group"] not in TUNNEL_GROUPS]
    for r in tunnels:
        sc = _state_cell(r["cells"])
        r["state_cell"] = sc
        r["state"] = "stale" if r["stale"] else UP_WORD.get(sc.get("status") if sc else "", "unknown")
    ipsec = [r for r in tunnels if r["group"] == "ipsec"]
    sdwan = [r for r in tunnels if r["group"] == "sdwan"]
    up = sum(1 for r in ipsec if r["state"] == "up")
    down = sum(1 for r in ipsec if r["state"] == "down")
    sd_bad = sum(1 for r in sdwan if r["health"] in ("warning", "critical"))
    users = sum(_num(c.get("value")) or 0 for r in remote if not r["stale"] for c in r["cells"]
                if (c.get("unit") or "").lower() == "users")
    stale = sum(1 for r in tunnels if r["stale"])
    kpis = [
        _kpi("IPsec tunnels", _count(len(ipsec)), "primary",
             f"on {len({r['device'] for r in ipsec}):,} device(s)"),
        _kpi("Tunnels up", _count(up), "success" if up else "neutral",
             f"{stale:,} stale" if stale else None),
        _kpi("Tunnels down", _count(down), "danger" if down else ("success" if ipsec else "neutral")),
        _kpi("SD-WAN links degraded or dead", _count(sd_bad),
             "warning" if sd_bad else ("success" if sdwan else "neutral"), f"of {len(sdwan):,} link(s)"),
        _kpi("Remote-access users", _count(users) if remote else "—", "info" if remote else "neutral",
             "Latest reported" if remote else "Not measured"),
    ]
    listed_t = [r for r in tunnels if r["health"] not in ("ok", "info")] if problems_only else tunnels
    listed_t = sorted(listed_t, key=lambda r: ({"down": 0, "degraded": 1, "stale": 2, "unknown": 3,
                                                 "up": 4}.get(r["state"], 3),
                                                HEALTH_ORDER.get(r["health"], 3),
                                                r["device_name"].lower(), (r["member"] or "").lower()))
    shown_t = listed_t[:top_n]
    tables = []
    if shown_t:
        trows = []
        for r in shown_t:
            sc = r["state_cell"]
            rest = [c for c in r["cells"] if c is not sc]
            trows.append([r["device_name"], r["member"] or "—", TUNNEL_GROUPS[r["group"]], r["state"],
                          _pairs(rest), r["health"], ctx.fmt_short(r["updated"]) if r["updated"] else "—"])
        tables.append({"title": _truncated("IPsec tunnels and SD-WAN links", len(shown_t), len(listed_t),
                                           "tunnels and links"),
                       "headers": ["Device", "Tunnel / link", "Type", "State", "Details", "Health", "Updated"],
                       "styles": ["text", "text", "text", "status", "text", "status", "text"],
                       "rows": trows})
    listed_r = [r for r in remote if r["health"] not in ("ok", "info", "n/a")] if problems_only else remote
    listed_r = _sort_rows(listed_r)
    shown_r = listed_r[:top_n]
    if shown_r:
        tables.append({"title": _truncated("Remote-access VPN", len(shown_r), len(listed_r), "entries"),
                       "headers": ["Device", "Service", "Instance", "Usage", "Health", "Updated"],
                       "styles": ["text", "text", "text", "text", "status", "text"],
                       "rows": [[r["device_name"], r["group_name"], r["member"] or "—", _pairs(r["cells"]),
                                 r["health"], ctx.fmt_short(r["updated"]) if r["updated"] else "—"]
                                for r in shown_r]})
    counts = Counter(r["state"] for r in tunnels)
    charts = [{"title": "Tunnels and SD-WAN links by state",
               "series": {"kind": "donut", "unit": "tunnels",
                          "points": [{"label": k.capitalize(), "value": counts[k]}
                                     for k in ("up", "degraded", "down", "unknown", "stale") if counts.get(k)]}}
              ] if tunnels else []
    notes = ["Each row is a tunnel as the device reports it (FortiGate lists one row per phase-2 "
             "selector). States are graded with the device template, as on the device page; traffic "
             "and latency are the latest values the device reported."]
    if problems_only and not listed_t and not listed_r:
        notes.insert(0, "Every tunnel, link and VPN service in scope is healthy.")
    sn = _stale_note(stale, "tunnel(s) or link(s)")
    if sn:
        notes.append(sn)
    if _has_unlabeled(rows):
        notes.append(UNLABELED_NOTE)
    return _section(
        sid, title,
        description=(f"{len(ipsec):,} IPsec tunnel(s), {len(sdwan):,} SD-WAN link(s) and remote-access VPN "
                     "usage on devices in scope, as of report generation."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── BGP peers ──────────────────────────────────────────────────────────────

BGP_GROUPS = ("bgp_peers", "std_bgp_peers")


def _cell_by(cells: list[dict], pred) -> Optional[dict]:
    return next((c for c in cells if pred(c)), None)


@register_section(
    "bgp_peer_state", title="BGP Peer Status", category="Availability",
    description="BGP sessions on routers, switches and firewalls with their state, as of report generation.",
    scope="device", requires=REQUIRES, semantics="snapshot", options=STATE_OPTIONS,
)
async def bgp_peer_state(ctx: SectionCtx) -> dict:
    sid, title = "bgp_peer_state", "BGP Peer Status"
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, BGP_GROUPS)
    rows = [r for r in _template_rows(state) if r["instance"]]
    if not rows:
        return _empty(sid, title, "No BGP peer data for devices in scope.",
                      "BGP peer state as of report generation.")
    peers = []
    for r in rows:
        cells = r["cells"]
        st = _cell_by(cells, lambda c: c["key"].endswith("_bgp_state"))
        admin = _cell_by(cells, lambda c: c["key"].endswith("_bgp_admin"))
        asn = _cell_by(cells, lambda c: c["key"].endswith("remote_as") or (c.get("unit") or "") == "asn")
        est = _cell_by(cells, lambda c: c["key"].endswith("established_seconds"))
        admin_down = bool(admin and _num(admin.get("value")) == 1)
        established = bool(st and st.get("status") == "ok")
        if r["stale"]:
            health = "stale"
        elif admin_down:
            health = "info"
        else:
            health = r["health"]
        peers.append({**r, "state_txt": _cell_text(st) if st else "—",
                      "admin_txt": _cell_text(admin) if admin else "—",
                      "asn": _cell_text(asn) if asn else "—",
                      "est": _cell_text(est) if est else "—",
                      "admin_down": admin_down, "established": established, "health": health})
    fresh = [p for p in peers if not p["stale"]]
    est_n = sum(1 for p in fresh if p["established"])
    admin_n = sum(1 for p in fresh if p["admin_down"])
    not_est = sum(1 for p in fresh if not p["established"] and not p["admin_down"])
    stale = len(peers) - len(fresh)
    kpis = [
        _kpi("BGP peers", _count(len(peers)), "primary", f"on {len({p['device'] for p in peers}):,} device(s)"),
        _kpi("Established", _count(est_n), "success" if est_n else "neutral",
             f"{est_n / len(fresh) * 100:.1f}% of peers with current data" if fresh else None),
        _kpi("Not established", _count(not_est), "danger" if not_est else ("success" if fresh else "neutral"),
             "Administratively enabled"),
        _kpi("Administratively down", _count(admin_n), "info" if admin_n else "neutral",
             "Stopped by configuration — not a fault"),
        _kpi("Stale", _count(stale), "warning" if stale else "neutral", f"Not refreshed in {STALE_HOURS} h"),
    ]
    listed = [p for p in peers if p["health"] not in ("ok", "info")] if ctx.opt("problems_only") else peers
    listed = sorted(listed, key=lambda p: (HEALTH_ORDER.get(p["health"], 3), p["device_name"].lower(),
                                           p["member"].lower()))
    shown = listed[:top_n]
    counts = Counter("Stale" if p["stale"] else p["state_txt"] for p in peers)
    notes = ["States come from BGP4-MIB (bgpPeerTable), graded with the device template; an "
             "administratively stopped peer is shown separately and not counted as a failure. "
             "'Established time' is how long the session has been up, or for a peer that is not "
             "established, how long since it was last up.",
             "BGP4-MIB lists IPv4 peers of the default routing instance only; IPv6 and VRF peers are "
             "not included."]
    if ctx.opt("problems_only") and not listed:
        notes.insert(0, "Every BGP peer in scope is established or administratively stopped.")
    sn = _stale_note(stale, "peer(s)")
    if sn:
        notes.append(sn)
    return _section(
        sid, title,
        description=f"{len(peers):,} BGP peer(s) on devices in scope, as of report generation.",
        kpis=kpis,
        charts=[{"title": "BGP peers by state",
                 "series": {"kind": "donut", "unit": "peers",
                            "points": [{"label": k, "value": v} for k, v in counts.most_common()]}}],
        tables=[{"title": _truncated("BGP peers", len(shown), len(listed), "peers"),
                 "headers": ["Device", "Peer", "Remote AS", "State", "Admin state", "Established time",
                             "Health", "Updated"],
                 "styles": ["text", "mono", "text", "text", "text", "num", "status", "text"],
                 "rows": [[p["device_name"], p["member"], p["asn"], p["state_txt"], p["admin_txt"], p["est"],
                           p["health"], ctx.fmt_short(p["updated"]) if p["updated"] else "—"]
                          for p in shown]}] if shown else [],
        notes=notes)


# ─── F5 BIG-IP pools, members, nodes and virtual servers ────────────────────

F5_GROUPS = ("virtual_servers", "pools", "pool_members", "nodes")
F5_AVAIL_WORD = {"available": "up", "offline": "down", "degraded": "degraded", "unknown": "unknown",
                 "no monitor": "no monitor", "unlicensed": "unlicensed"}
F5_AVAIL_ORDER = {"down": 0, "degraded": 1, "stale": 2, "unknown": 3, "no monitor": 4,
                  "unlicensed": 5, "up": 6}


def _f5_object(r: dict) -> dict:
    cells = r["cells"]
    avail = _cell_by(cells, lambda c: c["key"].endswith("_avail"))
    enabled = _cell_by(cells, lambda c: c["key"].endswith("_enabled"))
    conns = _cell_by(cells, lambda c: c["key"].endswith(("_conns", "_connections")))
    reason = _cell_by(cells, lambda c: c["key"].endswith("_reason"))
    active = _cell_by(cells, lambda c: c["key"] == "f5_pool_active")
    configured = _cell_by(cells, lambda c: c["key"] == "f5_pool_members")
    if r["stale"]:
        word = "stale"
    elif avail is None or (avail.get("value") is None and not avail.get("text")):
        word = "unknown"
    else:
        txt = (avail.get("text") or "").strip().lower()
        word = F5_AVAIL_WORD.get(txt, txt or _cell_text(avail))
    admin = _cell_text(enabled) if enabled else "—"
    admin_disabled = bool(enabled and _num(enabled.get("value")) in (2.0, 3.0))

    def count(c: Optional[dict]) -> str:
        v = _num(c.get("value")) if c else None
        return "—" if v is None else _count(v)
    members = f"{count(active)} / {count(configured)}" if active or configured else "—"
    return {"device": r["device"], "device_name": r["device_name"], "name": r["member"] or r["instance"],
            "avail": word, "admin": admin, "admin_disabled": admin_disabled,
            "conns": count(conns), "members": members,
            "detail": (_cell_text(reason) if reason else "—") or "—"}


@register_section(
    "f5_pool_health", title="F5 Load Balancer Health", category="Applications",
    description=("Availability of F5 BIG-IP virtual servers, pools, pool members and nodes, with "
                 "administrative state kept separate, as of report generation."),
    scope="device", requires=REQUIRES, semantics="snapshot",
    options=[top_n_option(50, 500),
             {"key": "show_all", "label": "List every member and node, not only those not available",
              "type": "bool", "default": False}],
)
async def f5_pool_health(ctx: SectionCtx) -> dict:
    sid, title = "f5_pool_health", "F5 Load Balancer Health"
    top_n = int(ctx.opt("top_n"))
    show_all = bool(ctx.opt("show_all"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, F5_GROUPS)
    rows = [r for r in _template_rows(state) if r["instance"]]
    if not rows:
        return _empty(sid, title, "No F5 BIG-IP virtual server, pool, member or node data for devices in scope.",
                      "F5 BIG-IP load-balancing health as of report generation.")
    objs: dict[str, list[dict]] = {g: [] for g in F5_GROUPS}
    for r in rows:
        objs[r["group"]].append(_f5_object(r))
    for g in objs:
        objs[g].sort(key=lambda o: (F5_AVAIL_ORDER.get(o["avail"], 3), o["device_name"].lower(),
                                    o["name"].lower()))

    def n(group: str, word: str) -> int:
        return sum(1 for o in objs[group] if o["avail"] == word)
    vs, pools, members, nodes = (objs[g] for g in F5_GROUPS)
    members_down = [o for o in members if o["avail"] == "down" and not o["admin_disabled"]]
    members_disabled = [o for o in members if o["admin_disabled"]]
    nodes_down = [o for o in nodes if o["avail"] == "down" and not o["admin_disabled"]]
    kpis = [
        _kpi("Virtual servers available", f"{n('virtual_servers', 'up'):,} of {len(vs):,}" if vs else "—",
             "success" if vs and n("virtual_servers", "up") == len(vs) else ("warning" if vs else "neutral"),
             f"{n('virtual_servers', 'down'):,} offline · {n('virtual_servers', 'degraded'):,} degraded"
             if vs else "Not measured"),
        _kpi("Pools available", f"{n('pools', 'up'):,} of {len(pools):,}" if pools else "—",
             "success" if pools and n("pools", "up") == len(pools) else ("warning" if pools else "neutral"),
             f"{n('pools', 'down'):,} offline · {n('pools', 'degraded'):,} degraded" if pools else "Not measured"),
        _kpi("Members offline", _count(len(members_down)),
             "danger" if members_down else ("success" if members else "neutral"),
             f"Enabled members, of {len(members):,} in all"),
        _kpi("Members disabled", _count(len(members_disabled)), "info" if members_disabled else "neutral",
             "Administrative state — not a fault"),
        _kpi("Nodes offline", _count(len(nodes_down)),
             "danger" if nodes_down else ("success" if nodes else "neutral"),
             f"of {len(nodes):,} node(s)"),
    ]
    mcount = Counter(o["avail"] for o in members)
    charts = [{"title": "Pool members by availability",
               "series": {"kind": "donut", "unit": "members",
                          "points": [{"label": k.capitalize(), "value": v} for k, v in mcount.most_common()]}}
              ] if members else []

    def table(title_: str, items: list[dict], noun: str, name_header: str,
              with_members: bool = False) -> Optional[dict]:
        if not items:
            return None
        shown = items[:top_n]
        headers = ["Device", name_header, "Availability", "Admin state",
                   "Active / configured" if with_members else "Connections", "Detail"]
        return {"title": _truncated(title_, len(shown), len(items), noun), "headers": headers,
                "styles": ["text", "text", "status", "text", "num", "text"],
                "rows": [[o["device_name"], o["name"], o["avail"], o["admin"],
                          o["members"] if with_members else o["conns"], o["detail"]] for o in shown]}

    def problems(items: list[dict]) -> list[dict]:
        return items if show_all else [o for o in items if o["avail"] != "up"]
    tables = [t for t in (
        table("Virtual servers", vs, "virtual servers", "Virtual server"),
        table("Pools", pools, "pools", "Pool", with_members=True),
        table("Pool members" if show_all else "Pool members not available", problems(members),
              "members", "Member (pool · node:port)"),
        table("Nodes" if show_all else "Nodes not available", problems(nodes), "nodes", "Node"),
    ) if t]
    notes = ["Availability is the BIG-IP's own status (health-monitor result); administrative state "
             "(enabled / disabled) is listed separately. A disabled member that is offline was drained "
             "on purpose and is not counted as offline.",
             "'No monitor' means no health monitor is assigned, so the BIG-IP cannot judge "
             "availability; it is not a failure."]
    if not show_all and members and not problems(members) and not problems(nodes):
        notes.insert(0, "Every pool member and node in scope is available.")
    sn = _stale_note(sum(1 for r in rows if r["stale"]), "object(s)")
    if sn:
        notes.append(sn)
    return _section(
        sid, title,
        description=(f"{len(vs):,} virtual server(s), {len(pools):,} pool(s), {len(members):,} pool member(s) "
                     f"and {len(nodes):,} node(s) on F5 BIG-IP devices in scope, as of report generation."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── F5 certificate inventory ───────────────────────────────────────────────

CERT_INVENTORY_NOTE = ("This is the inventory of certificate objects installed on each BIG-IP, "
                       "including unused and default certificates. It does not prove that a "
                       "certificate is bound to an active virtual server or presented to clients; "
                       "use the TLS certificate checks for certificates live traffic actually uses.")


@register_section(
    "f5_certificate_inventory", title="F5 Certificate Inventory", category="Security",
    description=("Certificates installed on F5 BIG-IP devices with their expiry and days remaining, "
                 "as of report generation (inventory, not proof of active TLS use)."),
    scope="device", requires=REQUIRES, semantics="snapshot",
    options=[{"key": "warn_days", "label": "Expiring within (days)", "type": "int", "default": 30,
              "min": 1, "max": 365},
             top_n_option(50, 500)],
)
async def f5_certificate_inventory(ctx: SectionCtx) -> dict:
    sid, title = "f5_certificate_inventory", "F5 Certificate Inventory"
    warn_days = int(ctx.opt("warn_days"))
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, ("certificates",))
    rows = [r for r in _template_rows(state) if r["instance"]]
    if not rows:
        return _empty(sid, title, "No F5 BIG-IP certificate inventory for devices in scope.",
                      "Certificates installed on F5 BIG-IP devices as of report generation.")
    certs = []
    for r in rows:
        cells = {c["key"]: c for c in r["cells"]}
        days = _num((cells.get("f5_cert_days_remaining") or {}).get("value"))
        epoch = _num((cells.get("f5_cert_expiry_epoch") or {}).get("value"))
        if days is None and epoch is not None and epoch > 0:
            # Same rule as f5_insights for templates it does not recognise.
            days = float(math.floor((epoch - _now().timestamp()) / 86400))
        if epoch is not None and epoch > 0:
            expires = ctx.fmt_date(datetime.fromtimestamp(epoch, timezone.utc))
        else:
            expires = (cells.get("f5_cert_expiry_text") or {}).get("text") or "—"
        status = ("unknown" if days is None else "expired" if days < 0 else
                  "warning" if days <= warn_days else "ok")
        certs.append({"device_name": r["device_name"], "device": r["device"], "name": r["member"],
                      "days": days, "expires": expires, "status": status})
    certs.sort(key=lambda c: (c["days"] is None, c["days"] if c["days"] is not None else 0,
                              c["device_name"].lower(), c["name"].lower()))
    expired = [c for c in certs if c["status"] == "expired"]
    soon = [c for c in certs if c["status"] == "warning"]
    unknown = [c for c in certs if c["status"] == "unknown"]
    upcoming = next((c for c in certs if c["days"] is not None and c["days"] >= 0), None)
    kpis = [
        _kpi("Certificates", _count(len(certs)), "primary",
             f"on {len({c['device'] for c in certs}):,} device(s)"),
        _kpi("Expired", _count(len(expired)), "danger" if expired else "success"),
        _kpi(f"Expiring within {warn_days} days", _count(len(soon)), "warning" if soon else "success"),
        _kpi("Next expiry", f"{int(upcoming['days']):,} days" if upcoming else "—",
             ("warning" if upcoming["days"] <= warn_days else "success") if upcoming else "neutral",
             f"{upcoming['name']} · {upcoming['device_name']}" if upcoming else "Not measured"),
        _kpi("No expiry date", _count(len(unknown)), "neutral", "Not measured"),
    ]
    later = len(certs) - len(expired) - len(soon) - len(unknown)
    shown = certs[:top_n]
    return _section(
        sid, title,
        description=(f"{len(certs):,} certificate(s) installed on F5 BIG-IP devices in scope, soonest "
                     "expiry first, as of report generation."),
        kpis=kpis,
        charts=[{"title": "Certificates by expiry",
                 "series": {"kind": "donut", "unit": "certificates",
                            "points": [p for p in (
                                {"label": "Expired", "value": len(expired)},
                                {"label": f"Within {warn_days} days", "value": len(soon)},
                                {"label": "Later", "value": later},
                                {"label": "No expiry date", "value": len(unknown)}) if p["value"]]}}],
        tables=[{"title": _truncated("Certificates (soonest expiry first)", len(shown), len(certs),
                                     "certificates"),
                 "headers": ["Device", "Certificate", "Expires", "Days remaining", "Status"],
                 "styles": ["text", "text", "text", "num", "status"],
                 "rows": [[c["device_name"], c["name"], c["expires"],
                           f"{int(c['days']):,}" if c["days"] is not None else "—", c["status"]]
                          for c in shown]}],
        notes=[CERT_INVENTORY_NOTE,
               "Days remaining are counted from report generation using the expiry the BIG-IP reports."])


# ─── Optics and PoE ─────────────────────────────────────────────────────────

def _w_cell(cells: list[dict], words: tuple[str, ...], exclude: tuple[str, ...] = ()) -> Optional[dict]:
    return _cell_by(cells, lambda c: (c.get("unit") or "").lower() in ("w", "watts")
                    and any(w in c["name"].lower() for w in words)
                    and not any(x in c["name"].lower() for x in exclude))


@register_section(
    "optics_poe_health", title="Optics and PoE Health", category="Hardware",
    description=("Optical transceiver power, temperature and faults, and PoE budget and draw, as "
                 "of report generation."),
    scope="device", requires=REQUIRES, semantics="snapshot",
    options=[top_n_option(50, 500),
             {"key": "rx_low_dbm", "label": "Low receive power (dBm)", "type": "number",
              "default": -20, "min": -40, "max": 0,
              "help": "Optics receiving less than this are flagged."},
             {"key": "poe_threshold", "label": "PoE budget warning (%)", "type": "number",
              "default": 90, "min": 1, "max": 100}],
)
async def optics_poe_health(ctx: SectionCtx) -> dict:
    sid, title = "optics_poe_health", "Optics and PoE Health"
    top_n = int(ctx.opt("top_n"))
    rx_low = float(ctx.opt("rx_low_dbm"))
    poe_thr = float(ctx.opt("poe_threshold"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    state = await _template_state(ctx, ("optics", "poe"))
    rows = _template_rows(state)
    if not rows:
        return _empty(sid, title, "No optical-module or PoE data for devices in scope.",
                      "Optics and PoE health as of report generation.")

    optics = []
    for r in (x for x in rows if x["group"] == "optics"):
        cells = r["cells"]
        dbm = [c for c in cells if (c.get("unit") or "").lower() == "dbm"]
        rx = _cell_by(dbm, lambda c: "rx" in f"{c['key']} {c['name']}".lower())
        tx = _cell_by(dbm, lambda c: "tx" in f"{c['key']} {c['name']}".lower())
        temp = _cell_by(cells, lambda c: (c.get("unit") or "").lower() in ("c", "°c"))
        faults = [c for c in cells if c.get("type") == "enum"
                  and any(w in c["name"].lower() for w in ("loss", "fault", "alarm"))]
        rx_v = _num(rx.get("value")) if rx else None
        health = r["health"]
        if not r["stale"] and rx_v is not None and rx_v < rx_low and HEALTH_ORDER.get(health, 3) > 1:
            health = "warning"
        optics.append({**r, "rx": rx_v, "tx": _num(tx.get("value")) if tx else None,
                       "temp": _cell_text(temp) if temp else "—", "faults": _pairs(faults) if faults else "—",
                       "health": health})
    poe = []
    for r in (x for x in rows if x["group"] == "poe"):
        cells = r["cells"]
        budget = _w_cell(cells, ("budget", "available", "max"))
        draw = _w_cell(cells, ("consum", "power", "draw", "used"), exclude=("budget", "available", "max"))
        b, d = (_num(budget.get("value")) if budget else None), (_num(draw.get("value")) if draw else None)
        used = d / b * 100 if b and d is not None and b > 0 else None
        st = _state_cell(cells)
        health = r["health"]
        if not r["stale"] and used is not None and used >= poe_thr and HEALTH_ORDER.get(health, 3) > 1:
            health = "warning"
        poe.append({**r, "budget": b, "draw": d, "used": used, "state_txt": _cell_text(st) if st else "—",
                    "health": health})

    low_rx = [o for o in optics if o["rx"] is not None and o["rx"] < rx_low and not o["stale"]]
    lowest = min((o for o in optics if o["rx"] is not None), key=lambda o: o["rx"], default=None)
    tot_b = sum(p["budget"] or 0 for p in poe if p["budget"] and not p["stale"])
    tot_d = sum(p["draw"] or 0 for p in poe if p["budget"] and p["draw"] is not None and not p["stale"])
    poe_faults = [p for p in poe if not p["stale"] and (
        p["health"] == "critical" or (_state_cell(p["cells"]) or {}).get("status") in ("warn", "crit"))]
    poe_hot = [p for p in poe if p["used"] is not None and p["used"] >= poe_thr]
    kpis = [
        _kpi("Optical modules", _count(len(optics)), "primary" if optics else "neutral",
             f"on {len({o['device'] for o in optics}):,} device(s)" if optics else "Not measured"),
        _kpi(f"Receive power below {rx_low:g} dBm", _count(len(low_rx)),
             "warning" if low_rx else ("success" if optics else "neutral")),
        _kpi("Lowest receive power", f"{lowest['rx']:.2f} dBm" if lowest else "—",
             ("warning" if lowest["rx"] < rx_low else "success") if lowest else "neutral",
             f"{lowest['device_name']} · {lowest['member']}" if lowest else "Not measured"),
        _kpi("PoE budget used", _pct(tot_d / tot_b * 100) if tot_b > 0 else "—",
             ("warning" if tot_b > 0 and tot_d / tot_b * 100 >= poe_thr else "info") if tot_b > 0 else "neutral",
             f"{tot_d:,.0f} W of {tot_b:,.0f} W" if tot_b > 0 else "Not measured"),
        _kpi(f"PoE at or above {poe_thr:g}%", _count(len(poe_hot)),
             "warning" if poe_hot else ("success" if poe else "neutral")),
        _kpi("PoE faults", _count(len(poe_faults)),
             "danger" if poe_faults else ("success" if poe else "neutral"),
             f"of {len(poe):,} PSE(s) or port(s)"),
    ]
    tables: list[dict] = []
    charts: list[dict] = []
    if optics:
        optics.sort(key=lambda o: (HEALTH_ORDER.get(o["health"], 3),
                                   o["rx"] if o["rx"] is not None else 1e9, o["device_name"].lower(),
                                   (o["member"] or "").lower()))
        shown = optics[:top_n]
        tables.append({"title": _truncated("Optical modules (weakest receive power first)", len(shown),
                                           len(optics), "modules"),
                       "headers": ["Device", "Module", "Receive power", "Transmit power", "Temperature",
                                   "Faults", "Health"],
                       "styles": ["text", "text", "num", "num", "num", "text", "status"],
                       "rows": [[o["device_name"], o["member"] or "—",
                                 f"{o['rx']:.2f} dBm" if o["rx"] is not None else "—",
                                 f"{o['tx']:.2f} dBm" if o["tx"] is not None else "—",
                                 o["temp"], o["faults"], o["health"]] for o in shown]})
    if poe:
        poe.sort(key=lambda p: (HEALTH_ORDER.get(p["health"], 3), -(p["used"] or -1), p["device_name"].lower(),
                                (p["member"] or "").lower()))
        shown_p = poe[:top_n]
        tables.append({"title": _truncated("PoE power sourcing (highest use first)", len(shown_p), len(poe),
                                           "PSEs and ports"),
                       "headers": ["Device", "PSE / port", "Budget", "Draw", "Used", "Status", "Health"],
                       "styles": ["text", "text", "num", "num", "pct-bar", "text", "status"],
                       "rows": [[p["device_name"], p["member"] or p["group_name"],
                                 _fmt_value(p["budget"], "W") if p["budget"] is not None else "—",
                                 _fmt_value(p["draw"], "W") if p["draw"] is not None else "—",
                                 _pct(p["used"]), p["state_txt"], p["health"]] for p in shown_p]})
        ranked = [p for p in poe if p["used"] is not None][:10]
        if ranked:
            charts.append({"title": "PoE budget used (%)",
                           "series": {"kind": "rank", "unit": "%", "color": "warning",
                                      "points": [{"label": f"{p['device_name']} · {p['member'] or p['group_name']}",
                                                  "value": round(p["used"], 2)} for p in ranked]}})
    notes = ["Readings are the latest values each device reported (as of report generation), graded "
             "with its template. Receive-power limits vary by optic; the "
             f"{rx_low:g} dBm floor is a general guide, not the transceiver's own alarm threshold."]
    sn = _stale_note(sum(1 for r in rows if r["stale"]), "module(s) or PoE entries")
    if sn:
        notes.append(sn)
    if _has_unlabeled(rows):
        notes.append(UNLABELED_NOTE)
    return _section(
        sid, title,
        description=(f"{len(optics):,} optical module(s) and {len(poe):,} PoE port(s) or power budget(s) "
                     "on devices in scope, as of report generation."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Firewall sessions ──────────────────────────────────────────────────────

SESSION_KEYS = {"sessions": "FortiGate sessions", "tpl_fgt_sessions": "FortiGate sessions",
                "tpl_pan_sess_active": "PAN-OS active sessions", "tpl_jnx_sessions": "SRX flow sessions",
                "tpl_asa_conns": "ASA connections"}
SESSION_CAPACITY_KEYS = ("tpl_pan_sess_max", "tpl_jnx_sessions_max")


@register_section(
    "firewall_sessions", title="Firewall Session Capacity", category="Capacity",
    description=("Concurrent firewall sessions over the period: average, peak and, where the device "
                 "reports its session-table size, peak use of capacity."),
    scope="device", requires=REQUIRES,
    options=[top_n_option(10, 100),
             {"key": "threshold", "label": "Session-table warning (%)", "type": "number", "default": 80,
              "min": 1, "max": 100, "help": "Peak use of capacity at or above this is flagged."}],
)
async def firewall_sessions(ctx: SectionCtx) -> dict:
    sid = "firewall_sessions"
    top_n = int(ctx.opt("top_n"))
    warn = float(ctx.opt("threshold"))
    crit = min(100.0, warn + 10)
    title = "Firewall Session Capacity"
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    dsql, dparams = ctx.ch_device_clause("device_id")
    keys = [*SESSION_KEYS, *SESSION_CAPACITY_KEYS]
    rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, metric_key, sum(avg_value * sample_count) AS s, "
        "sum(sample_count) AS n, max(max_value) AS peak, argMax(timestamp, max_value) AS peak_at, "
        "argMax(avg_value, timestamp) AS last_v FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND metric_key IN %(keys)s"
        f"{dsql} GROUP BY did, metric_key",
        {"f": ctx.frm, "t": ctx.to, "keys": keys, **dparams})
    by_dev: dict[str, dict[str, tuple]] = defaultdict(dict)
    for r in rows:
        if str(r[0]) in devs and (_num(r[3]) or 0) > 0:
            by_dev[str(r[0])][r[1]] = r
    stats = []
    for did, keyed in by_dev.items():
        key = next((k for k in SESSION_KEYS if k in keyed), None)
        if key is None:
            continue
        r = keyed[key]
        cap_row = next((keyed[k] for k in SESSION_CAPACITY_KEYS if k in keyed), None)
        cap = _num(cap_row[6]) if cap_row else None
        peak = _num(r[4])
        stats.append({"id": did, "name": devs[did]["name"], "key": key, "source": SESSION_KEYS[key],
                      "avg": (_num(r[2]) or 0.0) / _num(r[3]), "peak": peak, "peak_at": r[5],
                      "cap": cap if cap and cap > 0 else None,
                      "use": peak / cap * 100 if peak is not None and cap and cap > 0 else None})
    firewalls = [d for d in devs.values() if d["type"] == "firewall"]
    if not stats:
        notes = ["No firewall session samples in this period."]
        if firewalls:
            notes.append(f"{len(firewalls):,} firewall(s) in scope reported no session count "
                         "(not measured).")
        return _section(sid, title, description="Concurrent firewall sessions over the period.",
                        kpis=[_kpi("Firewalls reporting sessions", "0", "neutral",
                                   f"of {len(firewalls):,} firewall(s) in scope")], notes=notes)
    stats.sort(key=lambda x: (-(x["peak"] if x["peak"] is not None else -1), x["name"].lower()))
    listed = stats[:top_n]
    with_cap = [x for x in stats if x["use"] is not None]
    over = [x for x in with_cap if x["use"] >= warn]
    top_use = max(with_cap, key=lambda x: x["use"], default=None)
    top = stats[0]
    total_avg = sum(x["avg"] for x in stats)
    kpis = [
        _kpi("Firewalls reporting sessions", _count(len(stats)), "primary",
             f"of {len(firewalls):,} firewall(s) in scope" if firewalls else None),
        _kpi("Peak concurrent sessions", _count(top["peak"]) if top["peak"] is not None else "—", "info",
             f"{top['name']} · {ctx.fmt_short(top['peak_at'])}" if top["peak"] is not None else None),
        _kpi("Average sessions (all firewalls)", _count(total_avg), "neutral", "Sum of device averages"),
        _kpi("Highest session-table use", _pct(top_use["use"]) if top_use else "—",
             _accent(_level(top_use["use"], warn, crit)) if top_use else "neutral",
             top_use["name"] if top_use else "Capacity not reported"),
        _kpi(f"At or above {warn:g}% of capacity", _count(len(over)),
             "danger" if any(x["use"] >= crit for x in over) else "warning" if over else
             ("success" if with_cap else "neutral"), f"of {len(with_cap):,} with known capacity"),
    ]

    charts = [{"title": "Peak concurrent sessions by firewall",
               "series": {"kind": "rank", "unit": "sessions", "color": "info",
                          "points": [{"label": x["name"], "value": round(x["peak"])}
                                     for x in listed if x["peak"] is not None]}}]
    chosen = listed[:5]
    interval = ctx.bucket()
    step = _step_seconds(interval)
    bucket, bparams = ctx.ch_bucket("timestamp", interval)
    pairs = sorted({(x["id"], x["key"]) for x in chosen})
    trend = await ctx.ch_rows(
        f"SELECT {bucket} AS b, toString(device_id) AS did, metric_key, "
        "sum(avg_value * sample_count) AS s, sum(sample_count) AS n FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(f)s AND timestamp < %(t)s AND toString(device_id) IN %(chosen)s "
        f"AND metric_key IN %(keys)s{dsql} GROUP BY b, did, metric_key ORDER BY b",
        {"f": ctx.frm, "t": ctx.to, "chosen": sorted({p[0] for p in pairs}) or ["__none__"],
         "keys": sorted({p[1] for p in pairs}) or ["__none__"], **dparams, **bparams})
    per: dict[str, dict] = defaultdict(dict)
    wanted = set(pairs)
    for r in trend:
        n = _num(r[4]) or 0.0
        if (str(r[1]), r[2]) in wanted and n > 0:
            per[str(r[1])][_aware(r[0])] = (_num(r[3]) or 0.0) / n
    stamps = sorted({ts for d in per.values() for ts in d if ts is not None})
    if stamps:
        series = [{"name": x["name"],
                   "points": _time_points(ctx, [(ts, per[x["id"]].get(ts)) for ts in stamps], step)}
                  for x in chosen]
        charts.append({"title": f"Concurrent sessions over time — top {len(chosen)} by peak",
                       "series": {"kind": "line", "unit": "sessions", "color": "info",
                                  "points": series[0]["points"], "series": series}})
    table_rows = [[x["name"], x["source"], _count(x["avg"]),
                   _count(x["peak"]) if x["peak"] is not None else "—",
                   ctx.fmt_short(x["peak_at"]) if x["peak"] is not None else "—",
                   _count(x["cap"]) if x["cap"] else "—", _pct(x["use"]),
                   _level(x["use"], warn, crit)] for x in listed]
    notes = ["Session counts are each device's own concurrent-session gauge (FortiGate sessions, PAN-OS "
             "active sessions, Juniper SRX flow sessions, Cisco ASA connections); their meaning varies "
             "by vendor. Averages are weighted by sample count from 5-minute rollups; the peak is the "
             "highest single poll.",
             "Capacity is reported only by PAN-OS and Juniper SRX; elsewhere use of capacity is not "
             f"measured. Use is graded on the peak (warning {warn:g}%, critical {crit:g}%)."]
    missing = [d for d in firewalls if d["id"] not in {x["id"] for x in stats}]
    if missing:
        notes.append(f"{len(missing):,} firewall(s) in scope reported no session count in this period "
                     "(not measured).")
    wn = ctx.window_note(ROLLUP_DAYS, SNMP_HISTORY)
    if wn:
        notes.append(wn)
    return _section(
        sid, title,
        description=(f"Concurrent sessions of {len(stats):,} firewall(s) over the period, busiest first."),
        kpis=kpis, charts=charts,
        tables=[{"title": _truncated("Firewalls by peak sessions", len(listed), len(stats), "firewalls"),
                 "headers": ["Device", "Source", "Average", "Peak", "Peak at", "Capacity", "Peak use",
                             "Status"],
                 "styles": ["text", "text", "num", "num", "text", "num", "pct-bar", "status"],
                 "rows": table_rows}],
        notes=notes)


# ─── SNMP monitoring coverage ───────────────────────────────────────────────

COVERAGE_LABELS = {"reporting": "SNMP reporting", "stale": "SNMP not responding",
                   "no data": "SNMP never responded", "ping only": "Ping only",
                   "not monitored": "Not monitored", "via controller": "Via controller"}
COVERAGE_ORDER = {"no data": 0, "stale": 1, "no template": 2, "ping only": 3, "not monitored": 4}


@register_section(
    "monitoring_coverage", title="SNMP Monitoring Coverage", category="Platform",
    description=("Which devices are polled by SNMP and responding, which have stopped responding or "
                 "have no monitoring template, and which are ping-only, as of report generation."),
    scope="device", requires=REQUIRES, semantics="snapshot",
    options=[{"key": "stale_hours", "label": "No SNMP response for at least (hours)", "type": "int",
              "default": 1, "min": 1, "max": 168},
             {"key": "include_ping_only", "label": "List ping-only and unmonitored devices", "type": "bool",
              "default": True},
             top_n_option(50, 500)],
)
async def monitoring_coverage(ctx: SectionCtx) -> dict:
    sid, title = "monitoring_coverage", "SNMP Monitoring Coverage"
    stale_h = int(ctx.opt("stale_hours"))
    top_n = int(ctx.opt("top_n"))
    devs = await _devices(ctx)
    if not devs:
        return _empty(sid, title, NO_DEVICES)
    now = _now()
    since = (now - timedelta(days=COVERAGE_LOOKBACK_DAYS)).replace(tzinfo=None)
    dsql, dparams = ctx.ch_device_clause("device_id")
    last_rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, max(timestamp) AS last_at FROM zenplus.snmp_metrics_5m "
        "WHERE timestamp >= %(since)s AND metric_key IN ('uptime', 'cpu', 'memory')"
        f"{dsql} GROUP BY did", {"since": since, **dparams})
    last = {str(r[0]): _aware(r[1]) for r in last_rows}
    clause, params = ctx.pg_device_clause("v.device_id")
    tpl_rows = (await ctx.db.execute(text(
        "SELECT v.device_id::text, max(v.updated_at) FROM device_template_values v "
        f"WHERE TRUE{clause} GROUP BY v.device_id"), params)).fetchall()
    tpl_last = {str(r[0]): _aware(r[1]) for r in tpl_rows}

    cutoff = now - timedelta(hours=stale_h)
    rows = []
    counts: Counter = Counter()
    no_template = 0
    for d in devs.values():
        issue = None
        if d["poll_mode"] == "via_controller":
            state = "via controller"
        elif not d["snmp"]:
            state = "ping only" if d["ping"] else "not monitored"
            issue = state
        else:
            seen = last.get(d["id"])
            state = "no data" if seen is None else "stale" if seen < cutoff else "reporting"
            if state != "reporting":
                issue = state
            if not d["profile_id"]:
                no_template += 1
                issue = issue or "no template"
        counts[state] += 1
        if issue:
            rows.append({"dev": d, "issue": issue, "state": state, "last": last.get(d["id"]),
                         "tpl": tpl_last.get(d["id"])})
    snmp_total = counts["reporting"] + counts["stale"] + counts["no data"]
    not_resp = counts["stale"] + counts["no data"]
    if not ctx.opt("include_ping_only"):
        rows = [r for r in rows if r["issue"] not in ("ping only", "not monitored")]
    rows.sort(key=lambda r: (COVERAGE_ORDER.get(r["issue"], 9), r["dev"]["name"].lower()))
    shown = rows[:top_n]
    issue_text = {"no data": f"No SNMP response in {COVERAGE_LOOKBACK_DAYS} days",
                  "stale": f"No SNMP response for {stale_h} h or more",
                  "no template": "SNMP responding, no monitoring template",
                  "ping only": "Ping only — no SNMP polling",
                  "not monitored": "Neither ping nor SNMP enabled"}
    state_word = {"no data": "no data", "stale": "stale", "reporting": "ok", "ping only": "n/a",
                  "not monitored": "disabled", "via controller": "n/a"}

    def polling(d: dict) -> str:
        parts = [p for p, on in (("SNMP", d["snmp"]), ("Ping", d["ping"])) if on]
        return " + ".join(parts) or "None"
    kpis = [
        _kpi("SNMP reporting", f"{counts['reporting']:,} of {snmp_total:,}",
             "success" if snmp_total and not not_resp else ("warning" if snmp_total else "neutral"),
             f"{counts['reporting'] / snmp_total * 100:.1f}% of SNMP-polled devices" if snmp_total else
             "No SNMP-polled devices"),
        _kpi("SNMP not responding", _count(not_resp),
             "danger" if not_resp else ("success" if snmp_total else "neutral"),
             f"No sample for {stale_h} h or more ({counts['no data']:,} never in {COVERAGE_LOOKBACK_DAYS} days)"),
        _kpi("No monitoring template", _count(no_template), "warning" if no_template else "neutral",
             "SNMP-polled, generic metrics only"),
        _kpi("Ping only", _count(counts["ping only"]), "info" if counts["ping only"] else "neutral",
             "Reachability only — no CPU, memory or interfaces"),
        _kpi("Not monitored", _count(counts["not monitored"]),
             "warning" if counts["not monitored"] else "neutral", "Neither ping nor SNMP"),
    ]
    order = ["reporting", "stale", "no data", "ping only", "not monitored", "via controller"]
    notes = ["Snapshot as of report generation. 'Last SNMP sample' is the newest 5-minute rollup of "
             f"the device's uptime, CPU or memory poll ({COVERAGE_LOOKBACK_DAYS}-day look-back); "
             "'Template data' is the newest monitoring-template value.",
             "Devices without a monitoring template still report uptime, CPU, memory and interfaces, "
             "but not vendor health (HA, fans and power supplies, sessions, VPN)."]
    if counts["via controller"]:
        notes.append(f"{counts['via controller']:,} device(s) report through their controller (thin APs, "
                     "FortiLink switches) and are not polled directly.")
    if not rows:
        notes.insert(0, "Every device in scope is polled by SNMP, responding and has a monitoring template.")
    return _section(
        sid, title,
        description=(f"SNMP and ping monitoring coverage of {len(devs):,} device(s) as of report generation."),
        kpis=kpis,
        charts=[{"title": "Devices by monitoring coverage",
                 "series": {"kind": "donut", "unit": "devices",
                            "points": [{"label": COVERAGE_LABELS[k], "value": counts[k]}
                                       for k in order if counts.get(k)]}}],
        tables=[{"title": _truncated("Devices needing attention", len(shown), len(rows), "devices"),
                 "headers": ["Device", "IP address", "Vendor / model", "Polling", "Template",
                             "Last SNMP sample", "Template data", "Issue", "State"],
                 "styles": ["text", "mono", "text", "text", "text", "text", "text", "text", "status"],
                 "rows": [[r["dev"]["name"], r["dev"]["ip"] or "—", _vendor(r["dev"]), polling(r["dev"]),
                           r["dev"]["profile"] or "—", ctx.fmt_dt(r["last"]), ctx.fmt_dt(r["tpl"]),
                           issue_text[r["issue"]], state_word.get(r["state"], "n/a")]
                          for r in shown]}] if shown else [],
        notes=notes)


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "top_devices_cpu_load", "Top Devices by CPU Load",
    "The 25 network devices with the highest 95th-percentile CPU over the period, their average and "
    "peak, the change against the previous period and the CPU trend of the busiest devices.",
    "hardware",
    [S("device_resource_utilization", "Top 25 Devices by CPU Load (95th Percentile)", metric="cpu",
       sort="p95", top_n=25),
     S("device_resource_trend", "CPU Load Trend — Busiest Devices", metric="cpu", breakdown="devices",
       top_n=5)],
    audience=("Network engineer", "Capacity planner", "IT manager"),
    tags=("cpu", "top n", "snmp", "utilization"), default_period="last_7d", frequency="weekly",
    icon="Cpu", featured=True, filterable=("devices",),
)

template(
    "top_devices_memory_utilization", "Top Devices by Memory Utilization",
    "The 25 network devices with the highest 95th-percentile memory use, with the F5 BIG-IP memory "
    "domains (system RAM, TMM, non-TMM and swap) kept separate, and the memory trend.",
    "hardware",
    [S("device_resource_utilization", "Top 25 Devices by Memory Utilization (95th Percentile)",
       metric="memory", sort="p95", top_n=25),
     S("device_resource_trend", "Memory Utilization Trend", metric="memory")],
    audience=("Network engineer", "Capacity planner"),
    tags=("memory", "top n", "snmp", "f5"), default_period="last_7d", frequency="weekly",
    icon="BarChart3", filterable=("devices",),
)

template(
    "device_cpu_memory_trend", "CPU and Memory Trend",
    "How network-device CPU and memory moved over the month: the scope average, 95th percentile and "
    "peak per interval, the busiest devices drawn individually, and the top devices by average load.",
    "performance",
    [S("device_resource_trend", "CPU and Memory Trend", metric="both"),
     S("device_resource_trend", "Busiest Devices — CPU", metric="cpu", breakdown="devices", top_n=5),
     S("device_resource_utilization", "Top 10 Devices by Average CPU", metric="cpu", sort="avg",
       top_n=10),
     S("device_resource_utilization", "Top 10 Devices by Average Memory", metric="memory",
       sort="avg", top_n=10)],
    audience=("Capacity planner", "Network engineer", "IT manager"),
    tags=("cpu", "memory", "trend", "capacity"), default_period="last_30d", frequency="monthly",
    icon="TrendingUp", filterable=("devices",),
)

template(
    "hardware_health_environment", "Hardware Health: Temperature, Fans and Power",
    "Chassis environment across the network: temperature sensors and their peaks, fan and power-"
    "supply faults, devices that return no hardware readings, and devices that rebooted.",
    "hardware",
    [S("hardware_environment"), S("device_reboots", "Device Reboots", top_n=25)],
    audience=("Network engineer", "NOC operator", "IT manager"),
    tags=("temperature", "fans", "power supply", "environment", "hardware"),
    default_period="last_7d", frequency="weekly", icon="Cpu", featured=True, filterable=("devices",),
)

template(
    "ha_redundancy_status", "High Availability and Redundancy Status",
    "HA cluster roles and sync, failover units, stack and VSF members, routing-engine redundancy and "
    "VRRP state, with the devices that rebooted in the last day.",
    "hardware",
    [S("ha_redundancy_state"), S("device_reboots", "Device Reboots", top_n=25)],
    audience=("Network engineer", "NOC operator"),
    tags=("ha", "failover", "cluster", "stack", "vrrp"), default_period="last_24h", frequency="daily",
    icon="Server", filterable=("devices",),
)

template(
    "vpn_tunnel_status", "VPN Tunnel Status",
    "IPsec tunnels that are up or down, SD-WAN link health and remote-access VPN usage on firewalls, "
    "problems first — the daily VPN check.",
    "security",
    [S("vpn_tunnel_state")],
    audience=("Network engineer", "NOC operator", "Security & audit"),
    tags=("vpn", "ipsec", "sd-wan", "remote access", "firewall"), default_period="last_24h",
    frequency="daily", icon="Waypoints", filterable=("devices",),
)

template(
    "bgp_peer_status", "BGP Peer Status",
    "Every BGP session with its state, remote AS and time in state; sessions that are not "
    "established are listed first, administratively stopped peers separately.",
    "availability",
    [S("bgp_peer_state")],
    audience=("Network engineer", "NOC operator"),
    tags=("bgp", "routing", "peers"), default_period="last_24h", frequency="daily", icon="Route",
    filterable=("devices",),
)

template(
    "f5_load_balancer_health", "F5 Load Balancer Health",
    "F5 BIG-IP virtual servers, pools, pool members and nodes that are not available — with "
    "administrative state kept separate — and the certificates expiring soonest.",
    "hardware",
    [S("f5_pool_health"), S("f5_certificate_inventory", "F5 Certificates Expiring Soonest", top_n=25)],
    audience=("Network engineer", "Application owner", "NOC operator"),
    tags=("f5", "big-ip", "load balancer", "pools", "virtual servers"), default_period="last_24h",
    frequency="daily", icon="Layers", featured=True, filterable=("devices",),
)

template(
    "f5_certificate_inventory_report", "F5 Certificate Inventory",
    "Every certificate installed on F5 BIG-IP devices with its expiry and days remaining, soonest "
    "first. An inventory: it does not prove a certificate is bound to live traffic.",
    "security",
    [S("f5_certificate_inventory", None, top_n=500, warn_days=60)],
    audience=("Security & audit", "Network engineer", "Application owner"),
    tags=("f5", "certificates", "tls", "expiry"), default_period="last_30d", frequency="monthly",
    icon="ShieldCheck", filterable=("devices",),
)

template(
    "optics_poe_health_report", "Optics and PoE Health",
    "Optical transceivers with weak receive power, high temperature or faults, and PoE budgets "
    "close to exhaustion or reporting faults.",
    "hardware",
    [S("optics_poe_health")],
    audience=("Network engineer", "Capacity planner"),
    tags=("optics", "sfp", "dom", "poe", "power"), default_period="last_7d", frequency="weekly",
    icon="Network", filterable=("devices",),
)

template(
    "firewall_session_capacity", "Firewall Session Capacity",
    "Concurrent sessions on every firewall over the month: averages, peaks and, where the device "
    "reports it, how close the session table came to capacity.",
    "capacity",
    [S("firewall_sessions", None, top_n=25)],
    audience=("Capacity planner", "Network engineer", "Security & audit"),
    tags=("firewall", "sessions", "capacity", "connections"), default_period="last_30d",
    frequency="monthly", icon="Gauge", filterable=("devices",),
)

template(
    "snmp_monitoring_coverage", "SNMP Monitoring Coverage",
    "Devices that stopped answering SNMP, never answered, have no monitoring template or are only "
    "pinged — the gaps in what the network monitor can see.",
    "platform",
    [S("monitoring_coverage"), S("device_status_now", "Devices Down or Degraded Now")],
    audience=("IT manager", "Network engineer", "NOC operator"),
    tags=("coverage", "snmp", "monitoring gaps", "templates"), default_period="last_7d",
    frequency="weekly", icon="Settings2", filterable=("devices",),
)
