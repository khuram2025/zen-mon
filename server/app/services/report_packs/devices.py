"""Device (node) sections: current state, availability by group/site,
latency/loss, stability, reboots and planned maintenance.

Canonical example of a section pack: see report_packs/__init__.py for the
authoring rules and tests/test_report_packs_contract.py for the contract.

Shared rules in this pack:
- Every section is device-scoped: each Postgres query carries
  ctx.pg_device_clause and each ClickHouse query ctx.ch_device_clause (or an
  id list derived from the scoped inventory).
- Planned maintenance is excluded the way the availability headline excludes
  it (report_service._fetch_device_maintenance_windows, inclusive bounds).
- Ping rollups store uptime_pct and avg_packet_loss as 0..1 fractions and
  record unanswered probes with rtt 0 / loss 1.0, so latency, jitter and loss
  are averaged over the probes a node answered (sample-weighted), and outage
  time is left to availability.
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _fmt_avail,
    _fmt_pct,
    _node_availability,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

logger = logging.getLogger("zenplus.reports")

STATUS_ORDER = ("down", "degraded", "maintenance", "unknown", "up")


@register_section(
    "device_status_now", title="Current Device Status", category="Availability",
    description="Up, down, degraded and unknown nodes right now, and the nodes that are down.",
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[top_n_option(25, 200)],
)
async def device_status_now(ctx: SectionCtx) -> dict:
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        "SELECT d.hostname, host(d.ip_address), COALESCE(NULLIF(d.location, ''), '—'), "
        "COALESCE(d.status, 'unknown'), d.last_seen "
        f"FROM devices d WHERE TRUE{clause} ORDER BY d.hostname"), params)).fetchall()
    if not rows:
        return _section("device_status_now", "Current Device Status",
                        notes=["No monitored devices in scope."])

    counts: dict[str, int] = {}
    for r in rows:
        counts[r[3]] = counts.get(r[3], 0) + 1
    down = [r for r in rows if r[3] in ("down", "degraded")]
    # last_seen is timestamptz (aware) and may be NULL (a controller child seen
    # down from the start): compare naive UTC, never-seen first.
    down.sort(key=lambda r: (r[3] != "down", _naive(r[4]) or datetime.min))
    top_n = int(ctx.opt("top_n"))
    total = len(rows)
    up = counts.get("up", 0)

    def share(n: int) -> str:
        return f"{n / total * 100:.1f}% of {total}" if total else "—"

    n_down, n_degraded = counts.get("down", 0), counts.get("degraded", 0)
    findings = []
    if n_down:
        findings.append({"severity": "danger", "weight": n_down,
                         "text": f"{n_down:,} network device(s) down at generation",
                         "action": "Review the down devices", "path": "/devices"})
    if n_degraded:
        findings.append({"severity": "warning", "weight": n_degraded,
                         "text": f"{n_degraded:,} network device(s) degraded at generation",
                         "action": "Review the degraded devices", "path": "/devices"})

    return _section(
        "device_status_now", "Current Device Status",
        description=f"State of {total} monitored node(s) when the report was generated.",
        findings=findings,
        kpis=[
            # Reachability of network devices only (the devices table), not
            # servers, services or alert incidents.
            {"label": "Network devices up", "value": f"{up:,}", "accent": "success",
             "subtitle": f"of {total:,}, at generation"},
            {"label": "Down", "value": f"{counts.get('down', 0):,}",
             "accent": "danger" if counts.get("down") else "success", "subtitle": share(counts.get("down", 0))},
            {"label": "Degraded", "value": f"{counts.get('degraded', 0):,}",
             "accent": "warning" if counts.get("degraded") else "neutral"},
            {"label": "Unknown", "value": f"{counts.get('unknown', 0):,}", "accent": "neutral",
             "subtitle": "Not measured"},
        ],
        charts=[{"title": "Nodes by state",
                 "series": {"kind": "donut", "unit": "nodes",
                            "points": [{"label": s.capitalize(), "value": counts[s]}
                                       for s in STATUS_ORDER if counts.get(s)]}}],
        tables=[{"title": "Nodes that are down or degraded",
                 "headers": ["Node", "IP address", "Location", "Status", "Last seen"],
                 "styles": ["text", "mono", "text", "status", "text"],
                 "rows": [[r[0] or r[1] or "—", r[1] or "—", r[2], r[3], ctx.fmt_short(r[4])]
                          for r in down[:top_n]]}] if down else [],
        notes=[] if down else ["Every node in scope is up or not yet measured."],
    )


# ─── Shared helpers ─────────────────────────────────────────────────────────

# sysUpTime is a 32-bit TimeTicks counter (1/100 s): it wraps after ~497 days.
SYSUPTIME_MAX_S = 4294967295 / 100.0
REBOOT_DROP_S = 60.0          # an uptime drop smaller than this is clock jitter
RAW_RETENTION_DAYS = 30       # ping_metrics / snmp_metrics raw TTL
ROLLUP_5M_RETENTION_DAYS = 90
PING_1H_RETENTION_DAYS = 365
STATUS_LOG_RETENTION_DAYS = 365
CONTROLLER_LABEL = "Controller"


def _naive(dt: Any) -> Optional[datetime]:
    """Naive UTC (what ClickHouse parameters and the maintenance windows use)."""
    if not isinstance(dt, datetime):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _ms(v: Optional[float]) -> str:
    """Latency with precision that suits its size ('0.42 ms', '12.3 ms', '245 ms')."""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    if v >= 100:
        return f"{v:.0f} ms"
    if v >= 10:
        return f"{v:.1f} ms"
    return f"{v:.2f} ms"


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


def _change(curr: Optional[float], prev: Optional[float], unit: str, digits: int = 2) -> str:
    """'▲ 2.10 ms vs previous period' (or why there is no comparison)."""
    if curr is None:
        return "Not measured"
    if prev is None:
        return "No data for the previous period"
    d = round(curr - prev, digits)
    if d == 0:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.{digits}f}{unit} vs previous period"


def _truncated(title: str, shown: int, total: int, noun: str) -> str:
    return f"{title} — top {shown} of {total} {noun}" if total > shown else title


def _kpi(label: str, value: str, accent: str, subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


async def _maintenance(ctx: SectionCtx, frm: Optional[datetime] = None,
                       to: Optional[datetime] = None) -> dict[str, list[tuple[datetime, datetime]]]:
    """device_id -> planned maintenance intervals (naive UTC, clamped to the
    window), limited to the device filter. Cached per render and window."""
    from app.services.report_service import _fetch_device_maintenance_windows
    frm, to = frm or ctx.frm, to or ctx.to
    key = f"devpack_maint:{frm.isoformat()}:{to.isoformat()}"
    if key not in ctx._cache:
        ctx._cache[key] = await _fetch_device_maintenance_windows(ctx.db, frm, to)
    wins = ctx._cache[key]
    ids = ctx.device_ids
    if ids:
        allowed = set(ids)
        wins = {k: v for k, v in wins.items() if k in allowed}
    return wins


def _maint_sql(windows: dict[str, list[tuple[datetime, datetime]]],
               prefix: str = "m") -> tuple[str, dict]:
    """`` AND NOT (...)`` dropping samples inside planned maintenance.

    Same semantics as report_data_service._maintenance_exclusion_sql (inclusive
    bounds), but devices sharing one interval (group/tag/all windows) share one
    clause so a fleet-wide window does not become thousands of OR terms."""
    by_interval: dict[tuple[datetime, datetime], set[str]] = defaultdict(set)
    for did, wins in windows.items():
        for s, e in wins:
            by_interval[(s, e)].add(str(did))
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, ((s, e), ids) in enumerate(sorted(by_interval.items())):
        clauses.append(f"(toString(device_id) IN %({prefix}ids{i})s "
                       f"AND timestamp >= %({prefix}s{i})s AND timestamp <= %({prefix}e{i})s)")
        params[f"{prefix}ids{i}"] = sorted(ids)
        params[f"{prefix}s{i}"] = s
        params[f"{prefix}e{i}"] = e
    if not clauses:
        return "", {}
    return " AND NOT (" + " OR ".join(clauses) + ")", params


async def _inventory(ctx: SectionCtx, extra: str = "") -> list[tuple]:
    """(id, name, ip[, extra columns…]) for every node in scope."""
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        "SELECT d.id::text, COALESCE(NULLIF(d.hostname, ''), host(d.ip_address), d.id::text), "
        f"COALESCE(host(d.ip_address), ''){extra} "
        f"FROM devices d WHERE TRUE{clause} ORDER BY d.hostname"), params)).fetchall()
    return [tuple(r) for r in rows]


def _coverage_note(ctx: SectionCtx, retention_days: int, what: str) -> list[str]:
    note = ctx.window_note(retention_days, what)
    return [note] if note else []


# ─── Availability by group / location / site / type ─────────────────────────

GROUP_BY_CHOICES = [["group", "Device group"], ["location", "Location"],
                    ["site", "Monitoring site"], ["type", "Device type"]]
GROUP_TITLES = {"group": "Availability by Device Group", "location": "Availability by Location",
                "site": "Availability by Monitoring Site", "type": "Availability by Device Type"}
GROUP_HEADERS = {"group": "Group", "location": "Location", "site": "Monitoring site",
                 "type": "Device type"}
GROUP_FALLBACK = {"group": "Ungrouped", "location": "No location", "site": "Unassigned",
                  "type": "other"}
GROUP_PLURAL = {"group": "groups", "location": "locations", "site": "monitoring sites",
                "type": "device types"}


async def _group_labels(ctx: SectionCtx, group_by: str) -> dict[str, str]:
    clause, params = ctx.pg_device_clause("d.id")
    if group_by == "group":
        sql = ("SELECT d.id::text, g.name FROM devices d "
               "LEFT JOIN device_groups g ON g.id = d.group_id WHERE TRUE")
    elif group_by == "type":
        sql = "SELECT d.id::text, NULLIF(d.device_type, '') FROM devices d WHERE TRUE"
    elif group_by == "site":
        # The site of the node's primary poller (device_polling_owner, the same
        # owner the device health surface uses).
        sql = ("SELECT d.id::text, CASE WHEN o.owner_kind = 'central' THEN 'Controller' "
               "ELSE COALESCE(si.name, s.name || ' (no site)') END "
               "FROM devices d LEFT JOIN device_polling_owner o ON o.device_id = d.id "
               "LEFT JOIN sensors s ON s.id = o.sensor_id "
               "LEFT JOIN sites si ON si.id = s.site_id WHERE TRUE")
    else:
        sql = "SELECT d.id::text, NULLIF(TRIM(d.location), '') FROM devices d WHERE TRUE"
    rows = (await ctx.db.execute(text(sql + clause), params)).fetchall()
    return {str(r[0]): (r[1] or GROUP_FALLBACK[group_by]) for r in rows}


@register_section(
    "availability_by_group", title="Availability by Group and Site", category="Availability",
    description=("Node availability rolled up by device group, location, monitoring site or "
                 "device type, weighted by ping samples rather than averaged."),
    scope="device", requires=("devices.view",),
    options=[{"key": "group_by", "label": "Group by", "type": "select", "default": "group",
              "choices": GROUP_BY_CHOICES},
             top_n_option(25, 200)],
)
async def availability_by_group(ctx: SectionCtx) -> dict:
    group_by = ctx.opt("group_by")
    title = GROUP_TITLES[group_by]
    head = GROUP_HEADERS[group_by]
    na = await _node_availability(ctx)
    nodes = na["nodes"]
    if not nodes:
        return _section("availability_by_group", title, notes=["No monitored devices in scope."])

    labels = ({n["id"]: (n.get("location") or "").strip() or GROUP_FALLBACK["location"]
               for n in nodes} if group_by == "location" else await _group_labels(ctx, group_by))
    target = na.get("sla_target") or 99.9

    groups: dict[str, dict[str, Any]] = {}
    for n in nodes:
        name = labels.get(n["id"]) or GROUP_FALLBACK[group_by]
        g = groups.setdefault(name, {"nodes": 0, "measured": 0, "up": 0.0, "total": 0.0,
                                     "down_min": 0.0, "outages": 0, "down_now": 0, "worst": None})
        g["nodes"] += 1
        g["outages"] += n.get("outages") or 0
        g["down_now"] += 1 if n.get("status") == "down" else 0
        if n.get("avail") is None:
            continue
        # Weighted by the samples behind each node's figure, from the same
        # table, window and maintenance exclusion as _node_availability.
        w = float(n.get("samples") or 0)
        if w <= 0:
            continue
        g["measured"] += 1
        g["up"] += float(n.get("up_samples") or 0)
        g["total"] += w
        g["down_min"] += n.get("downtime_min") or 0.0
        if g["worst"] is None or n["avail"] < g["worst"][1]:
            g["worst"] = (n["hostname"], n["avail"])
    for g in groups.values():
        g["avail"] = g["up"] / g["total"] * 100 if g["total"] > 0 else None

    ordered = sorted(groups.items(), key=lambda kv: (kv[1]["avail"] is None,
                                                     kv[1]["avail"] if kv[1]["avail"] is not None else 0,
                                                     kv[0].lower()))
    measured = [(k, g) for k, g in ordered if g["avail"] is not None]
    top_n = int(ctx.opt("top_n"))
    shown = ordered[:top_n]
    below = [k for k, g in measured if g["avail"] < target]
    unmeasured_nodes = sum(1 for n in nodes if n.get("avail") is None)

    kpis = [
        {"label": GROUP_PLURAL[group_by].capitalize(), "value": f"{len(groups):,}",
         "accent": "primary", "subtitle": f"{len(nodes):,} node(s)"},
        {"label": f"Lowest {head.lower()}", "value": _fmt_avail(measured[0][1]["avail"]) if measured else "—",
         "accent": ("neutral" if not measured else
                    "success" if measured[0][1]["avail"] >= target else "danger"),
         "subtitle": measured[0][0] if measured else "Not measured"},
        {"label": f"Best {head.lower()}", "value": _fmt_avail(measured[-1][1]["avail"]) if measured else "—",
         "accent": "success" if measured else "neutral",
         "subtitle": measured[-1][0] if measured else "Not measured"},
        {"label": f"Below target ({target:g}%)", "value": f"{len(below):,}",
         "accent": "danger" if below else ("success" if measured else "neutral"),
         "subtitle": f"of {len(measured):,} measured"},
        {"label": "Nodes not measured", "value": f"{unmeasured_nodes:,}",
         "accent": "warning" if unmeasured_nodes else "neutral",
         "subtitle": "No ping samples in the period"},
    ]
    episodes = not na.get("outages_failed")
    rows = [[name, f"{g['nodes']:,}", f"{g['measured']:,}", _fmt_avail(g["avail"]),
             f"{g['down_min']:,.0f} min" if g["measured"] else "—",
             f"{g['outages']:,}" if episodes else "—", f"{g['down_now']:,}",
             f"{g['worst'][0]} ({_fmt_avail(g['worst'][1])})" if g["worst"] else "—"]
            for name, g in shown]
    notes = ["Each row is weighted by ping samples (total up samples ÷ total samples), "
             "not a mean of node percentages; planned maintenance is excluded. Unreachable time "
             "counts every failed probe, isolated losses included; Outages are confirmed episodes."]
    if na.get("maint_excluded_min"):
        notes.append(f"Planned maintenance excluded: {na['maint_excluded_min']:,.0f} device-minutes.")
    if not episodes:
        notes.append("Outage episodes could not be loaded for this run; outage counts show —.")
    if group_by == "site":
        notes.append("Monitoring site is the site of each node's primary poller; "
                     "'Controller' means the central appliance polls it.")
    retention = {"ping_metrics": (RAW_RETENTION_DAYS, "Raw ping history"),
                 "ping_metrics_5m": (ROLLUP_5M_RETENTION_DAYS, "5-minute ping history"),
                 "ping_metrics_1h": (PING_1H_RETENTION_DAYS, "Hourly ping history")}.get(
        na.get("table") or "", (ROLLUP_5M_RETENTION_DAYS, "5-minute ping history"))
    notes += _coverage_note(ctx, *retention)
    return _section(
        "availability_by_group", title,
        description=(f"Availability of {len(nodes):,} node(s) by {head.lower()}, lowest first, "
                     "over the reporting period."),
        kpis=kpis,
        charts=[{"title": f"Lowest availability by {head.lower()}",
                 "series": {"kind": "rank", "unit": "%", "color": "success",
                            "points": [{"label": k, "value": round(g["avail"], 2)}
                                       for k, g in measured[:top_n]]}}] if measured else [],
        tables=[{"title": _truncated(f"By {head.lower()}", len(shown), len(ordered),
                                     GROUP_PLURAL[group_by]),
                 "headers": [head, "Nodes", "Measured", "Availability", "Unreachable", "Outages",
                             "Down now", "Lowest node"],
                 "styles": ["text", "num", "num", "pct-bar", "num", "num", "num", "text"],
                 "rows": rows}],
        notes=notes,
    )


# ─── Latency, packet loss and jitter ────────────────────────────────────────

LATENCY_METRICS = [["rtt", "Latency (p95)"], ["loss", "Packet loss"], ["jitter", "Jitter"]]
LATENCY_LABEL = {"rtt": "Latency (p95)", "loss": "Packet Loss", "jitter": "Jitter"}

_PING_SUMS_ROLLUP = ("sum(sample_count) AS samples, sum(uptime_pct * sample_count) AS up, "
                     "sum(avg_rtt_ms * sample_count) AS rtt_w, "
                     "sum(avg_jitter_ms * sample_count) AS jit_w, "
                     "sum(avg_packet_loss * sample_count) AS loss_w")
_PING_SUMS_RAW = ("count() AS samples, countIf(is_up = 1) AS up, "
                  "sumIf(rtt_ms, is_up = 1) AS rtt_w, sumIf(jitter_ms, is_up = 1) AS jit_w, "
                  "sumIf(packet_loss, is_up = 1) + countIf(is_up = 0) AS loss_w")
_PING_PEAK_ROLLUP = ("quantileExactIf(0.95)(avg_rtt_ms / uptime_pct, uptime_pct > 0 AND avg_rtt_ms > 0) "
                     "AS p95_rtt, max(max_rtt_ms) AS peak_rtt")
_PING_PEAK_RAW = ("quantileExactIf(0.95)(rtt_ms, is_up = 1 AND rtt_ms > 0) AS p95_rtt, "
                  "maxIf(max_rtt_ms, is_up = 1) AS peak_rtt")


def _quality(samples: Any, up: Any, rtt_w: Any, jit_w: Any, loss_w: Any) -> Optional[dict]:
    """Answered-probe averages from the weighted sums (None = no samples)."""
    samples, up = float(samples or 0), float(up or 0)
    if samples <= 0:
        return None
    out = {"samples": samples, "up": up, "rtt_w": float(rtt_w or 0), "jit_w": float(jit_w or 0),
           "loss_w": float(loss_w or 0), "avg_rtt": None, "jitter": None, "loss_pct": None}
    if up > 0:
        out["avg_rtt"] = out["rtt_w"] / up
        out["jitter"] = out["jit_w"] / up
        # Unanswered probes are stored with loss 1.0: remove them so loss
        # describes the probes the node answered (outages are availability).
        out["loss_pct"] = max(0.0, min(1.0, (out["loss_w"] - (samples - up)) / up)) * 100
    return out


async def _ping_quality(ctx: SectionCtx) -> dict:
    """Per-node and per-bucket latency/jitter/loss for the window plus the
    previous period's totals, from the table ping_rollups picks. Cached so the
    rtt/loss/jitter variants of the section share one set of queries."""
    from app.services.ping_rollups import is_rollup, ping_table_for_hours
    table = ping_table_for_hours(ctx.hours)
    key = f"devpack_ping_quality:{table}"
    if key in ctx._cache:
        return ctx._cache[key]
    rollup = is_rollup(table)
    sums = _PING_SUMS_ROLLUP if rollup else _PING_SUMS_RAW
    peak = _PING_PEAK_ROLLUP if rollup else _PING_PEAK_RAW
    dsql, dparams = ctx.ch_device_clause("device_id")
    msql, mparams = _maint_sql(await _maintenance(ctx))
    where = f"timestamp >= %(f)s AND timestamp < %(t)s{dsql}{msql}"
    params = {"f": ctx.frm, "t": ctx.to, **dparams, **mparams}

    per_node = {}
    for r in await ctx.ch_rows(
            f"SELECT toString(device_id) AS did, {sums}, {peak} FROM zenplus.{table} "
            f"WHERE {where} GROUP BY did", params):
        q = _quality(*r[1:6])
        if q is None:
            continue
        q["p95"] = float(r[6]) if q["up"] > 0 and r[6] is not None and float(r[6]) > 0 else None
        q["peak"] = float(r[7]) if q["up"] > 0 and r[7] is not None and float(r[7]) > 0 else None
        per_node[str(r[0])] = q

    bucket, bparams = ctx.ch_bucket("timestamp")
    trend = [(b, _quality(*rest)) for b, *rest in await ctx.ch_rows(
        f"SELECT {bucket} AS b, {sums} FROM zenplus.{table} WHERE {where} GROUP BY b ORDER BY b",
        {**params, **bparams})]

    pf, pt = ctx.prior_window()
    pmsql, pmparams = _maint_sql(await _maintenance(ctx, pf, pt), prefix="pm")
    prior = None
    rows = await ctx.ch_rows(
        f"SELECT {sums} FROM zenplus.{table} "
        f"WHERE timestamp >= %(f)s AND timestamp < %(t)s{dsql}{pmsql}",
        {"f": pf, "t": pt, **dparams, **pmparams})
    if rows:
        prior = _quality(*rows[0][:5])
    out = {"table": table, "nodes": per_node, "trend": trend, "prior": prior}
    ctx._cache[key] = out
    return out


def _fleet(nodes: list[dict]) -> Optional[dict]:
    if not nodes:
        return None
    return _quality(sum(n["samples"] for n in nodes), sum(n["up"] for n in nodes),
                    sum(n["rtt_w"] for n in nodes), sum(n["jit_w"] for n in nodes),
                    sum(n["loss_w"] for n in nodes))


def _metric_value(q: Optional[dict], metric: str) -> Optional[float]:
    if not q:
        return None
    if metric == "loss":
        return q.get("loss_pct")
    if metric == "jitter":
        return q.get("jitter")
    return q.get("p95") if q.get("p95") is not None else q.get("avg_rtt")


@register_section(
    "device_latency_loss", title="Latency, Packet Loss and Jitter", category="Performance",
    description=("Nodes ranked by 95th-percentile round-trip time, packet loss or jitter, with "
                 "average, peak and the change against the previous period."),
    scope="device", requires=("devices.view",),
    options=[{"key": "metric", "label": "Rank by", "type": "select", "default": "rtt",
              "choices": LATENCY_METRICS},
             top_n_option(10, 200),
             {"key": "loss_threshold", "label": "Packet loss threshold (%)", "type": "number",
              "default": 1.0, "min": 0.01, "max": 100,
              "help": "Nodes at or above this average loss are counted in the summary."}],
)
async def device_latency_loss(ctx: SectionCtx) -> dict:
    metric = ctx.opt("metric")
    top_n = int(ctx.opt("top_n"))
    threshold = float(ctx.opt("loss_threshold"))
    title = f"Top {top_n} Nodes by {LATENCY_LABEL[metric]}"
    inventory = await _inventory(ctx)
    if not inventory:
        return _section("device_latency_loss", title, notes=["No monitored devices in scope."])
    names = {r[0]: (r[1], r[2]) for r in inventory}

    pq = await _ping_quality(ctx)
    stats = {did: q for did, q in pq["nodes"].items() if did in names}
    answered = {did: q for did, q in stats.items() if q["up"] > 0}
    unmeasured = len(names) - len(stats)
    silent = len(stats) - len(answered)
    fleet = _fleet(list(stats.values()))
    prior = pq["prior"]

    ranked = sorted(((did, q) for did, q in answered.items() if _metric_value(q, metric) is not None),
                    key=lambda kv: (-_metric_value(kv[1], metric), names[kv[0]][0].lower()))
    shown = ranked[:top_n]
    worst_p95 = max(((q["p95"], did) for did, q in answered.items() if q.get("p95") is not None),
                    default=None)
    lossy = [did for did, q in answered.items() if (q.get("loss_pct") or 0) >= threshold]

    kpis = [
        {"label": "Average latency", "value": _ms(fleet and fleet["avg_rtt"]),
         "accent": "primary" if fleet and fleet["avg_rtt"] is not None else "neutral",
         "subtitle": _change(fleet and fleet["avg_rtt"], prior and prior["avg_rtt"], " ms")},
        {"label": "Worst p95 latency", "value": _ms(worst_p95[0]) if worst_p95 else "—",
         "accent": "warning" if worst_p95 else "neutral",
         "subtitle": names[worst_p95[1]][0] if worst_p95 else "Not measured"},
        {"label": "Average packet loss",
         "value": _fmt_pct(fleet and fleet["loss_pct"]),
         "accent": ("neutral" if not fleet or fleet["loss_pct"] is None else
                    "warning" if fleet["loss_pct"] >= threshold else "success"),
         "subtitle": _change(fleet and fleet["loss_pct"], prior and prior["loss_pct"], " pts")},
        {"label": f"Nodes with loss ≥ {threshold:g}%", "value": f"{len(lossy):,}",
         "accent": "danger" if lossy else ("success" if answered else "neutral"),
         "subtitle": f"of {len(answered):,} answering"},
        {"label": "Average jitter", "value": _ms(fleet and fleet["jitter"]),
         "accent": "info" if fleet and fleet["jitter"] is not None else "neutral",
         "subtitle": _change(fleet and fleet["jitter"], prior and prior["jitter"], " ms")},
        {"label": "Nodes measured", "value": f"{len(answered):,}",
         "accent": "neutral", "subtitle": f"of {len(names):,} in scope"},
    ]

    unit = "%" if metric == "loss" else "ms"
    color = {"rtt": "primary", "loss": "danger", "jitter": "info"}[metric]
    trend_key = {"rtt": "avg_rtt", "loss": "loss_pct", "jitter": "jitter"}[metric]
    trend_label = {"rtt": "Average latency", "loss": "Average packet loss",
                   "jitter": "Average jitter"}[metric]
    charts: list[dict] = []
    if shown:
        charts.append({"title": LATENCY_LABEL[metric],
                       "series": {"kind": "rank", "unit": unit, "color": color,
                                  "points": [{"label": names[did][0],
                                              "value": round(_metric_value(q, metric), 2)}
                                             for did, q in shown]}})
    points = [{"t": ctx.iso(b), "v": (round(q[trend_key], 2)
                                      if q and q.get(trend_key) is not None else None)}
              for b, q in pq["trend"]]
    if any(p["v"] is not None for p in points):
        charts.append({"title": f"{trend_label} over time",
                       "series": {"kind": "line", "unit": unit, "color": color, "points": points}})

    rows = [[names[did][0], names[did][1] or "—", _ms(q["avg_rtt"]), _ms(q.get("p95")),
             _ms(q.get("peak")), _ms(q["jitter"]), _fmt_pct(q["loss_pct"])]
            for did, q in shown]
    source = {"ping_metrics": "raw ping probes",
              "ping_metrics_5m": "5-minute rollups (p95 over 5-minute averages)",
              "ping_metrics_1h": "hourly rollups (p95 over hourly averages)"}[pq["table"]]
    notes = [f"Source: {source}. Latency, jitter and loss cover the probes each node answered; "
             "unanswered probes count against availability, not loss. Planned maintenance is excluded."]
    if unmeasured:
        notes.append(f"{unmeasured:,} node(s) in scope have no ping samples in this period (not measured).")
    if silent:
        notes.append(f"{silent:,} node(s) answered no probe at all; see the availability sections.")
    retention = {"ping_metrics": (RAW_RETENTION_DAYS, "Raw ping history"),
                 "ping_metrics_5m": (ROLLUP_5M_RETENTION_DAYS, "5-minute ping history"),
                 "ping_metrics_1h": (PING_1H_RETENTION_DAYS, "Hourly ping history")}[pq["table"]]
    notes += _coverage_note(ctx, *retention)
    if not answered:
        notes.append("No node answered a ping probe in this period.")
    return _section(
        "device_latency_loss", title,
        description=(f"Nodes ranked by {LATENCY_LABEL[metric].lower()} over the reporting period "
                     f"({len(answered):,} answering node(s))."),
        kpis=kpis, charts=charts,
        tables=[{"title": _truncated(f"Nodes by {LATENCY_LABEL[metric].lower()}", len(shown),
                                     len(ranked), "nodes"),
                 "headers": ["Node", "IP address", "Avg RTT", "p95 RTT", "Peak RTT", "Jitter",
                             "Packet loss"],
                 "styles": ["text", "mono", "num", "num", "num", "num", "pct"],
                 "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── Flapping / unstable devices ────────────────────────────────────────────

# A state change excludes entering/leaving planned maintenance (unless the node
# comes out of it down) and a newly added node's first result.
_CHANGE_COND = ("n != 'maintenance' AND NOT (o = 'maintenance' AND n NOT IN ('down', 'offline')) "
                "AND NOT (o IN ('', 'unknown') AND n = 'up')")


async def _flap_episodes(ctx: SectionCtx) -> dict[str, dict[str, Any]]:
    """device_id -> {episodes, flapping_s, ongoing} over the flap episodes (device_flap_episodes,
    one row per time the status writer flagged the node as flapping) that overlap the window.
    Time flapping is clipped to the window and, for an episode still open, to now."""
    clause, params = ctx.pg_device_clause("e.device_id")
    frm = ctx.frm if ctx.frm.tzinfo else ctx.frm.replace(tzinfo=timezone.utc)
    to = ctx.to if ctx.to.tzinfo else ctx.to.replace(tzinfo=timezone.utc)
    sql = text(
        "SELECT e.device_id::text, count(*), "
        "COALESCE(sum(GREATEST(EXTRACT(EPOCH FROM LEAST(COALESCE(e.ended_at, :now_ts), :to_ts) "
        "- GREATEST(e.started_at, :from_ts)), 0)), 0), bool_or(e.ended_at IS NULL) "
        "FROM device_flap_episodes e "
        f"WHERE e.started_at < :to_ts AND (e.ended_at IS NULL OR e.ended_at > :from_ts){clause} "
        "GROUP BY e.device_id")
    args = {"from_ts": frm, "to_ts": to, "now_ts": datetime.now(timezone.utc), **params}
    # A savepoint keeps a failure here from aborting the section's other reads.
    nested = getattr(ctx.db, "begin_nested", None)
    if nested is not None:
        async with nested():
            rows = (await ctx.db.execute(sql, args)).fetchall()
    else:
        rows = (await ctx.db.execute(sql, args)).fetchall()
    return {str(r[0]): {"episodes": int(r[1] or 0), "flapping_s": float(r[2] or 0), "ongoing": bool(r[3])}
            for r in rows}


@register_section(
    "device_flapping", title="Unstable Devices (Flapping)", category="Availability",
    description=("Nodes that changed state most often, their flapping episodes and time spent "
                 "flapping, with outage episodes, downtime, the longest outage and the median "
                 "time to recover."),
    scope="device", requires=("devices.view",),
    options=[top_n_option(15, 200),
             {"key": "threshold", "label": "Unstable threshold (state changes in the period)",
              "type": "int", "default": 4, "min": 1, "max": 1000}],
)
async def device_flapping(ctx: SectionCtx) -> dict:
    from app.services import report_data_service as rds
    title = "Unstable Devices (Flapping)"
    top_n = int(ctx.opt("top_n"))
    threshold = int(ctx.opt("threshold"))
    inventory = await _inventory(ctx)
    if not inventory:
        return _section("device_flapping", title, notes=["No monitored devices in scope."])
    names = {r[0]: r[1] for r in inventory}

    dsql, dparams = ctx.ch_device_clause("device_id")
    bucket, bparams = ctx.ch_bucket("timestamp")
    per_dev: dict[str, dict[str, Any]] = {}
    per_bucket: dict[Any, int] = defaultdict(int)
    for did, b, changes, downs, degraded, last in await ctx.ch_rows(
            "WITH lower(old_status) AS o, lower(new_status) AS n "
            f"SELECT toString(device_id) AS did, {bucket} AS b, countIf({_CHANGE_COND}) AS changes, "
            "countIf(n IN ('down', 'offline')) AS downs, countIf(n = 'degraded') AS degraded, "
            "max(timestamp) AS last_change "
            "FROM zenplus.device_status_log "
            f"WHERE timestamp >= %(f)s AND timestamp < %(t)s{dsql} GROUP BY did, b",
            {"f": ctx.frm, "t": ctx.to, **dparams, **bparams}):
        did = str(did)
        if did not in names:
            continue
        d = per_dev.setdefault(did, {"changes": 0, "downs": 0, "degraded": 0, "last": None})
        d["changes"] += int(changes or 0)
        d["downs"] += int(downs or 0)
        d["degraded"] += int(degraded or 0)
        if last is not None and (d["last"] is None or _naive(last) > d["last"]):
            d["last"] = _naive(last)
        per_bucket[b] += int(changes or 0)

    notes: list[str] = []
    episodes: list[dict] = []
    try:
        episodes = await asyncio.to_thread(rds.device_outage_episodes, ctx.frm, ctx.to,
                                           list(names), await _maintenance(ctx))
    except Exception:
        logger.exception("device_flapping: outage episodes unavailable")
        notes.append("Outage episodes could not be read; outage columns are not measured.")
    eps_by_dev: dict[str, list[dict]] = defaultdict(list)
    for e in episodes:
        if e["entity_id"] in names:
            eps_by_dev[e["entity_id"]].append(e)

    # Flap episodes: what the live Flapping badge showed (None = not measured).
    flaps: Optional[dict[str, dict[str, Any]]] = None
    try:
        flaps = {did: f for did, f in (await _flap_episodes(ctx)).items() if did in names}
    except Exception:
        logger.exception("device_flapping: flap episodes unavailable")
        notes.append("Flap episodes could not be read; the flap columns are not measured.")

    rows_data = []
    for did in set(per_dev) | set(eps_by_dev) | set(flaps or ()):
        d = per_dev.get(did, {"changes": 0, "downs": 0, "degraded": 0, "last": None})
        eps = eps_by_dev.get(did, [])
        completed = [e["duration_s"] for e in eps if e["completed_in_window"] and e["duration_s"] is not None]
        longest = max(eps, key=lambda e: e["duration_s"] if e["duration_s"] is not None else e["in_window_s"],
                      default=None)
        rows_data.append({
            "id": did, "name": names[did], **d, "outages": len(eps),
            "downtime_s": sum(e["in_window_s"] for e in eps),
            "longest": longest, "median_s": statistics.median(completed) if completed else None,
            "flap": None if flaps is None else flaps.get(did, {"episodes": 0, "flapping_s": 0.0, "ongoing": False}),
        })
    rows_data.sort(key=lambda r: (-r["changes"], -(r["flap"] or {}).get("episodes", 0), -r["outages"],
                                  -r["downtime_s"], r["name"].lower()))
    shown = rows_data[:top_n]
    unstable = [r for r in rows_data if r["changes"] >= threshold]
    flapping_nodes = None if flaps is None else [r for r in rows_data if r["flap"]["episodes"]]
    all_completed = [e["duration_s"] for e in episodes
                     if e["entity_id"] in names and e["completed_in_window"] and e["duration_s"] is not None]
    longest_all = max((r for r in rows_data if r["longest"]),
                      key=lambda r: (r["longest"]["duration_s"] if r["longest"]["duration_s"] is not None
                                     else r["longest"]["in_window_s"]), default=None)

    def _longest(e: Optional[dict]) -> str:
        if not e:
            return "—"
        out = _duration(e["duration_s"] if e["duration_s"] is not None else e["in_window_s"])
        return f"{out} (ongoing)" if e["ongoing"] else out

    def _flap_cells(f: Optional[dict]) -> list[str]:
        if f is None:
            return ["—", "—"]
        if not f["episodes"]:
            return ["0", "—"]
        spent = _duration(f["flapping_s"])
        return [f"{f['episodes']:,}", f"{spent} (ongoing)" if f["ongoing"] else spent]

    total_changes = sum(r["changes"] for r in rows_data)
    total_outages = sum(r["outages"] for r in rows_data)
    if flapping_nodes is None:
        flapping_kpi = {"label": "Flapping nodes (episodes)", "value": "—", "accent": "neutral",
                        "subtitle": "Not measured"}
    else:
        total_flaps = sum(r["flap"]["episodes"] for r in flapping_nodes)
        flapping_kpi = {"label": "Flapping nodes (episodes)", "value": f"{len(flapping_nodes):,}",
                        "accent": "warning" if flapping_nodes else "success",
                        "subtitle": f"{total_flaps:,} flap episode(s)" if flapping_nodes else "No flap episode"}
    kpis = [
        {"label": f"Unstable nodes (≥ {threshold} state changes)", "value": f"{len(unstable):,}",
         "accent": "warning" if unstable else "success", "subtitle": f"of {len(names):,} in scope"},
        flapping_kpi,
        {"label": "State changes", "value": f"{total_changes:,}", "accent": "info"},
        {"label": "Outage episodes", "value": f"{total_outages:,}",
         "accent": "danger" if total_outages else "success"},
        {"label": "Median time to recover",
         "value": _duration(statistics.median(all_completed)) if all_completed else "—",
         "accent": "primary" if all_completed else "neutral",
         "subtitle": f"{len(all_completed):,} recovered outage(s)" if all_completed else "No recovered outage"},
        {"label": "Longest outage", "value": _longest(longest_all["longest"]) if longest_all else "—",
         "accent": "warning" if longest_all else "neutral",
         "subtitle": longest_all["name"] if longest_all else "No outage"},
    ]
    charts: list[dict] = []
    if any(per_bucket.values()):
        charts.append({"title": "State changes over time",
                       "series": {"kind": "bars", "unit": "changes", "color": "warning",
                                  "points": [{"t": ctx.iso(b), "v": v}
                                             for b, v in sorted(per_bucket.items(),
                                                                key=lambda kv: ctx.iso(kv[0]) or "")]}})
    if all_completed:
        hist = rds._recovery_histogram([e for e in episodes if e["entity_id"] in names])
        charts.append({"title": "Time to recover",
                       "series": {"kind": "rank", "unit": "outages", "color": "primary",
                                  "points": [{"label": h["label"], "value": h["count"]} for h in hist]}})
    notes += ["State changes exclude entering and leaving planned maintenance and a new node's "
              "first result. Outages pair each down transition with the next recovery; "
              "maintenance time is excluded.",
              f"Unstable nodes changed state at least {threshold:,} times in the period. Flapping nodes "
              "were flagged as flapping by status monitoring (confirmed changes within the flap "
              "detection window) at least once; time flapping is clipped to the period."]
    notes += _coverage_note(ctx, STATUS_LOG_RETENTION_DAYS, "The device status log")
    if not rows_data:
        notes.insert(0, "No node changed state or had an outage in this period.")
    return _section(
        "device_flapping", title,
        description="Nodes that changed state, flapped or went down in the period, most changes first.",
        kpis=kpis, charts=charts,
        tables=[{"title": _truncated("Nodes with state changes or outages", len(shown),
                                     len(rows_data), "nodes"),
                 "headers": ["Node", "State changes", "Flap episodes", "Time flapping", "Outages",
                             "Degraded", "Downtime", "Longest outage", "Median recovery", "Last change"],
                 "styles": ["text", "num", "num", "num", "num", "num", "num", "num", "num", "text"],
                 "rows": [[r["name"], f"{r['changes']:,}", *_flap_cells(r["flap"]), f"{r['outages']:,}",
                           f"{r['degraded']:,}", _duration(r["downtime_s"]) if r["outages"] else "—",
                           _longest(r["longest"]), _duration(r["median_s"]),
                           ctx.fmt_short(r["last"])] for r in shown]}] if shown else [],
        notes=notes,
    )


# ─── Reboots (sysUpTime resets) ─────────────────────────────────────────────

def _is_wrap(prev: float, value: float, gap_s: float) -> bool:
    """A 32-bit sysUpTime wrap rather than a restart: the counter continued
    past its maximum (value ≈ prev + gap − max)."""
    return prev > SYSUPTIME_MAX_S - max(gap_s, 300.0) - 3600 and \
        abs(value - (prev + gap_s - SYSUPTIME_MAX_S)) < max(600.0, gap_s)


@register_section(
    "device_reboots", title="Device Reboots", category="Availability",
    description=("Nodes whose SNMP uptime reset during the period, with the number of reboots, "
                 "the estimated last boot time and the latest uptime."),
    scope="device", requires=("devices.view",),
    options=[top_n_option(25, 200)],
)
async def device_reboots(ctx: SectionCtx) -> dict:
    title = "Device Reboots"
    top_n = int(ctx.opt("top_n"))
    inventory = await _inventory(ctx, ", COALESCE(d.vendor, ''), COALESCE(d.model, '')")
    if not inventory:
        return _section("device_reboots", title, notes=["No monitored devices in scope."])
    names = {r[0]: r for r in inventory}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    frm, to = _naive(ctx.frm), _naive(ctx.to)
    raw = frm >= now - timedelta(days=RAW_RETENTION_DAYS) + timedelta(hours=1)
    table = "snmp_metrics" if raw else "snmp_metrics_5m"
    lo, hi = ("value", "value") if raw else ("min_value", "max_value")
    dsql, dparams = ctx.ch_device_clause("device_id")
    params = {"f": ctx.frm, "t": ctx.to, **dparams}

    # Pass 1 (cheap): a counter that never reset grows as fast as the clock.
    span: dict[str, dict[str, Any]] = {}
    for did, first_v, last_v, first_ts, last_ts in await ctx.ch_rows(
            f"SELECT toString(device_id) AS did, argMin({lo}, timestamp) AS first_v, "
            f"argMax({hi}, timestamp) AS last_v, min(timestamp) AS first_ts, max(timestamp) AS last_ts "
            f"FROM zenplus.{table} WHERE metric_key = 'uptime' "
            f"AND timestamp >= %(f)s AND timestamp < %(t)s{dsql} GROUP BY did", params):
        did = str(did)
        if did not in names:
            continue
        elapsed = ((_naive(last_ts) or to) - (_naive(first_ts) or frm)).total_seconds()
        grew = float(last_v or 0) - float(first_v or 0)
        span[did] = {"last_v": float(last_v or 0), "last_ts": _naive(last_ts),
                     "flagged": grew < elapsed * 0.999 - 300}
    flagged = sorted(did for did, s in span.items() if s["flagged"])

    # Pass 2 (precise, flagged nodes only): each drop of the counter.
    reboots: dict[str, list[datetime]] = defaultdict(list)
    wraps = 0
    if flagged:
        # One row per sample (raw) or per 5-minute bucket (rollup: several rows
        # can share a bucket, so reduce them to the bucket's min and max first).
        # Consecutive minima are compared, so a bucket that straddles a reboot
        # is counted once; the previous maximum feeds the counter-wrap check.
        if raw:
            src = ("SELECT device_id, timestamp, value AS lo_v, value AS hi_v FROM zenplus.snmp_metrics "
                   "WHERE metric_key = 'uptime' AND timestamp >= %(f)s AND timestamp < %(t)s "
                   "AND device_id IN %(flagged)s")
        else:
            src = ("SELECT device_id, timestamp, min(min_value) AS lo_v, max(max_value) AS hi_v "
                   "FROM zenplus.snmp_metrics_5m WHERE metric_key = 'uptime' "
                   "AND timestamp >= %(f)s AND timestamp < %(t)s AND device_id IN %(flagged)s "
                   "GROUP BY device_id, timestamp")
        for did, ts, value, prev_lo, prev_hi, prev_ts in await ctx.ch_rows(
                "SELECT did, ts, cur, prev_lo, prev_hi, prev_ts FROM ("
                "SELECT toString(device_id) AS did, timestamp AS ts, lo_v AS cur, "
                "lagInFrame(lo_v, 1, -1.0) OVER w AS prev_lo, "
                "lagInFrame(hi_v, 1, -1.0) OVER w AS prev_hi, "
                "lagInFrame(timestamp) OVER w AS prev_ts "
                f"FROM ({src}) "
                "WINDOW w AS (PARTITION BY device_id ORDER BY timestamp ASC "
                "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
                f") WHERE prev_lo >= 0 AND cur + {REBOOT_DROP_S:.0f} < prev_lo ORDER BY did, ts",
                {"f": ctx.frm, "t": ctx.to, "flagged": flagged}):
            ts_n, prev_n = _naive(ts), _naive(prev_ts) or _naive(ts)
            gap = max(0.0, (ts_n - prev_n).total_seconds()) if ts_n and prev_n else 0.0
            if _is_wrap(float(prev_hi), float(value), gap):
                wraps += 1
                continue
            # Boot time = sample time − uptime (a 5-minute bucket is placed at its middle).
            at = ts_n + (timedelta(seconds=150) if not raw else timedelta(0))
            boot = at - timedelta(seconds=max(0.0, float(value)))
            seen = reboots[str(did)]
            # Belt and braces: one boot seen in neighbouring buckets is one reboot.
            if not seen or abs((boot - seen[-1]).total_seconds()) > 300:
                seen.append(boot)

    rebooted = sorted(reboots, key=lambda d: (-len(reboots[d]), names[d][1].lower()))
    shown = rebooted[:top_n]
    total = sum(len(v) for v in reboots.values())
    kpis = [
        {"label": "Nodes rebooted", "value": f"{len(rebooted):,}",
         "accent": "warning" if rebooted else ("success" if span else "neutral"),
         "subtitle": f"of {len(span):,} reporting uptime"},
        {"label": "Reboots detected", "value": f"{total:,}",
         "accent": "warning" if total else ("success" if span else "neutral")},
        {"label": "Most reboots", "value": f"{len(reboots[rebooted[0]]):,}" if rebooted else "—",
         "accent": "danger" if rebooted and len(reboots[rebooted[0]]) > 1 else "neutral",
         "subtitle": names[rebooted[0]][1] if rebooted else "No reboot"},
        {"label": "Nodes measured", "value": f"{len(span):,}", "accent": "neutral",
         "subtitle": f"of {len(names):,} in scope report SNMP uptime"},
    ]
    charts: list[dict] = []
    if total and ctx.hours >= 48:
        try:
            tz = ZoneInfo(ctx.tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        per_day: dict[Any, int] = defaultdict(int)
        for boots in reboots.values():
            for b in boots:
                per_day[b.replace(tzinfo=timezone.utc).astimezone(tz).date()] += 1
        day = frm.replace(tzinfo=timezone.utc).astimezone(tz).date()
        last_day = to.replace(tzinfo=timezone.utc).astimezone(tz).date()
        points = []
        while day <= last_day:
            midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
            points.append({"t": ctx.iso(midnight), "v": per_day.get(day, 0)})
            day += timedelta(days=1)
        charts.append({"title": "Reboots per day",
                       "series": {"kind": "bars", "unit": "reboots", "color": "warning",
                                  "points": points}})

    def model(r: tuple) -> str:
        return " ".join(p for p in (r[3], r[4]) if p) or "—"

    rows = [[names[d][1], names[d][2] or "—", model(names[d]), f"{len(reboots[d]):,}",
             ctx.fmt_short(max(reboots[d])), _duration(span[d]["last_v"])] for d in shown]
    notes = ["Detected from SNMP sysUpTime resets; boot times are estimated from the uptime "
             "reported after the reset" + ("" if raw else " (5-minute resolution)") + ". "
             "sysUpTime counts the SNMP agent, so an agent restart without a reboot also appears here."]
    if wraps:
        notes.append(f"{wraps:,} 32-bit uptime counter wrap(s) (about every 497 days) were not "
                     "counted as reboots.")
    missing = len(names) - len(span)
    if missing:
        notes.append(f"{missing:,} node(s) reported no SNMP uptime in this period (not measured).")
    notes += _coverage_note(ctx, RAW_RETENTION_DAYS if raw else ROLLUP_5M_RETENTION_DAYS,
                            "Raw SNMP history" if raw else "5-minute SNMP history")
    if span and not rebooted:
        notes.insert(0, "No reboot was detected in this period.")
    return _section(
        "device_reboots", title,
        description="Nodes whose SNMP uptime reset during the reporting period, most reboots first.",
        kpis=kpis, charts=charts,
        tables=[{"title": _truncated("Rebooted nodes", len(shown), len(rebooted), "nodes"),
                 "headers": ["Node", "IP address", "Model", "Reboots", "Last reboot (est.)",
                             "Latest uptime"],
                 "styles": ["text", "mono", "text", "num", "text", "num"],
                 "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── Planned maintenance ────────────────────────────────────────────────────

# "Window m covers device d" — copied from app/api/v1/devices.py
# (MAINT_COVERS_DEVICE_SQL) so the report and the API agree on scope.
MAINT_COVERS_DEVICE_SQL = """(
       (m.scope_type = 'device' AND m.scope_device_id = d.id)
    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
    OR (m.scope_type = 'all')
)"""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _merged_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in sorted(intervals):
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += (cur_hi - cur_lo).total_seconds()
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        total += (cur_hi - cur_lo).total_seconds()
    return total


@register_section(
    "maintenance_summary", title="Planned Maintenance", category="Availability",
    description=("Device maintenance windows overlapping the period, their scope, the nodes they "
                 "covered and the device time excluded from availability."),
    scope="device", requires=("devices.view",),
    options=[top_n_option(50, 500)],
)
async def maintenance_summary(ctx: SectionCtx) -> dict:
    title = "Planned Maintenance"
    top_n = int(ctx.opt("top_n"))
    frm, to = _aware(ctx.frm), _aware(ctx.to)
    clause, dparams = ctx.pg_device_clause("d.id")
    params = {"from_ts": frm, "to_ts": to, **dparams}
    windows = (await ctx.db.execute(text(
        "SELECT m.id::text, m.scope_type, m.scope_tag, m.starts_at, m.ends_at, "
        "COALESCE(m.reason, ''), sd.hostname, g.name, COUNT(DISTINCT d.id) AS nodes "
        f"FROM device_maintenance m JOIN devices d ON {MAINT_COVERS_DEVICE_SQL} "
        "LEFT JOIN devices sd ON sd.id = m.scope_device_id "
        "LEFT JOIN device_groups g ON g.id = m.scope_group_id "
        f"WHERE m.starts_at < :to_ts AND m.ends_at > :from_ts{clause} "
        "GROUP BY m.id, m.scope_type, m.scope_tag, m.starts_at, m.ends_at, m.reason, sd.hostname, g.name "
        "ORDER BY m.starts_at"), params)).fetchall()
    if not windows:
        return _section("maintenance_summary", title,
                        description="Device maintenance windows overlapping the reporting period.",
                        kpis=[{"label": "Maintenance windows", "value": "0", "accent": "neutral"}],
                        notes=["No planned maintenance overlapped this period for the nodes in scope."])

    covered = (await ctx.db.execute(text(
        "SELECT d.id::text, GREATEST(m.starts_at, :from_ts), LEAST(m.ends_at, :to_ts) "
        f"FROM device_maintenance m JOIN devices d ON {MAINT_COVERS_DEVICE_SQL} "
        f"WHERE m.starts_at < :to_ts AND m.ends_at > :from_ts{clause}"), params)).fetchall()
    per_dev: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for did, s, e in covered:
        per_dev[str(did)].append((_aware(s), _aware(e)))
    excluded_s = sum(_merged_seconds(v) for v in per_dev.values())

    now = datetime.now(timezone.utc)

    def label(scope: str, tag: Optional[str], host: Optional[str], group: Optional[str]) -> str:
        if scope == "device":
            return f"Device: {host or '(deleted device)'}"
        if scope == "group":
            return f"Group: {group or '(deleted group)'}"
        if scope == "tag":
            return f"Tag: {tag or '—'}"
        return "All devices"

    rows, longest = [], None
    for _id, scope, tag, starts, ends, reason, host, group, nodes in windows:
        starts, ends = _aware(starts), _aware(ends)
        in_window = max(0.0, (min(ends, to) - max(starts, frm)).total_seconds())
        state = "active" if starts <= now <= ends else ("completed" if ends < now else "scheduled")
        length = (ends - starts).total_seconds()
        if longest is None or length > longest[0]:
            longest = (length, label(scope, tag, host, group))
        reason = (reason or "").strip()
        rows.append([label(scope, tag, host, group), ctx.fmt_dt(starts), ctx.fmt_dt(ends),
                     _duration(length), f"{int(nodes or 0):,}",
                     f"{in_window * int(nodes or 0) / 3600:,.1f}",
                     (reason[:77] + "…") if len(reason) > 80 else (reason or "—"), state])
    active = sum(1 for r in rows if r[-1] == "active")
    shown = rows[:top_n]
    return _section(
        "maintenance_summary", title,
        description=(f"{len(rows):,} device maintenance window(s) overlapping the reporting period "
                     f"({ctx.tz_label()})."),
        kpis=[
            _kpi("Maintenance windows", f"{len(rows):,}", "primary",
                 f"{active:,} active now" if active else None),
            _kpi("Nodes covered", f"{len(per_dev):,}", "info"),
            _kpi("Device-hours excluded", f"{excluded_s / 3600:,.1f} h", "neutral",
                 "From availability, within this period"),
            _kpi("Longest window", _duration(longest[0]) if longest else "—", "neutral",
                 longest[1] if longest else None),
        ],
        tables=[{"title": _truncated("Maintenance windows", len(shown), len(rows), "windows"),
                 "headers": ["Window", "Starts", "Ends", "Duration", "Nodes",
                             "Excluded (device-hours)", "Reason", "State"],
                 "styles": ["text", "text", "text", "num", "num", "num", "text", "status"],
                 "rows": shown}],
        notes=["Excluded time is clamped to the reporting period; overlapping windows on the same "
               "node count once in the total. Availability and outage figures in this report "
               "exclude planned maintenance."],
    )


# ─── Reachability by monitoring site (vantage point) ────────────────────────

@register_section(
    "site_vantage_reachability", title="Reachability by Monitoring Site", category="Availability",
    description=("Ping availability, latency and loss as measured from each monitoring site "
                 "(controller and remote sensors), and the nodes different sites see differently."),
    scope="device", requires=("devices.view",),
    options=[top_n_option(15, 200),
             {"key": "spread", "label": "Disagreement threshold (percentage points)",
              "type": "number", "default": 1.0, "min": 0.01, "max": 100}],
)
async def site_vantage_reachability(ctx: SectionCtx) -> dict:
    title = "Reachability by Monitoring Site"
    top_n = int(ctx.opt("top_n"))
    spread_min = float(ctx.opt("spread"))
    inventory = await _inventory(ctx)
    if not inventory:
        return _section("site_vantage_reachability", title, notes=["No monitored devices in scope."])
    names = {r[0]: r[1] for r in inventory}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    floor = now - timedelta(days=RAW_RETENTION_DAYS)
    frm, to = _naive(ctx.frm), _naive(ctx.to)
    if to <= floor:
        return _section("site_vantage_reachability", title,
                        notes=[f"Per-site ping results are kept for {RAW_RETENTION_DAYS} days; "
                               "this period is older, so there is nothing to compare."])
    dsql, dparams = ctx.ch_device_clause("device_id")
    msql, mparams = _maint_sql(await _maintenance(ctx))
    rows = await ctx.ch_rows(
        "SELECT toString(device_id) AS did, poller_id, count() AS samples, "
        "countIf(is_up = 1) AS up, sumIf(rtt_ms, is_up = 1 AND rtt_ms > 0) AS rtt_sum, "
        "countIf(is_up = 1 AND rtt_ms > 0) AS rtt_n, sumIf(packet_loss, is_up = 1) AS loss_sum, "
        "max(timestamp) AS last_ts FROM zenplus.ping_metrics "
        f"WHERE timestamp >= %(f)s AND timestamp < %(t)s{dsql}{msql} GROUP BY did, poller_id",
        {"f": max(frm, floor), "t": to, **dparams, **mparams})

    sensors = {str(r[0]): (r[1] or "Sensor", r[2]) for r in (await ctx.db.execute(text(
        "SELECT s.id::text, s.name, si.name FROM sensors s "
        "LEFT JOIN sites si ON si.id = s.site_id"))).fetchall()}
    controller_ids = {"central", os.getenv("POLLER_ID", "poller-01")}

    def vantage(pid: str) -> tuple[str, str]:
        if not pid or pid in controller_ids:
            return CONTROLLER_LABEL, "Central poller"
        if pid in sensors:
            name, site = sensors[pid]
            return site or "No site", name
        return "Unknown poller", pid[:12]

    sites: dict[str, dict[str, Any]] = {}
    by_node: dict[str, dict[str, dict]] = defaultdict(dict)
    for did, pid, samples, up, rtt_sum, rtt_n, loss_sum, last_ts in rows:
        did, pid = str(did), str(pid or "")
        if did not in names or not samples:
            continue
        site_label, sensor = vantage(pid)
        key = f"{site_label} · {sensor}" if site_label != CONTROLLER_LABEL else CONTROLLER_LABEL
        s = sites.setdefault(key, {"site": site_label, "sensor": sensor, "nodes": set(), "samples": 0.0,
                                   "up": 0.0, "rtt_sum": 0.0, "rtt_n": 0.0, "loss_sum": 0.0, "last": None})
        s["nodes"].add(did)
        s["samples"] += float(samples)
        s["up"] += float(up or 0)
        s["rtt_sum"] += float(rtt_sum or 0)
        s["rtt_n"] += float(rtt_n or 0)
        s["loss_sum"] += float(loss_sum or 0)
        last = _naive(last_ts)
        if last and (s["last"] is None or last > s["last"]):
            s["last"] = last
        v = by_node[did].setdefault(key, {"samples": 0.0, "up": 0.0, "rtt_sum": 0.0, "rtt_n": 0.0})
        v["samples"] += float(samples)
        v["up"] += float(up or 0)
        v["rtt_sum"] += float(rtt_sum or 0)
        v["rtt_n"] += float(rtt_n or 0)

    notes = ["Per-site figures come from raw ping probes grouped by the poller that sent them; "
             "planned maintenance is excluded. Elsewhere in this report a node's availability "
             "merges every site's probes."]
    notes += _coverage_note(ctx, RAW_RETENTION_DAYS, "Raw ping history (per monitoring site)")
    if not sites:
        return _section("site_vantage_reachability", title,
                        notes=["No ping results from any monitoring site in this period."] + notes)

    for s in sites.values():
        s["avail"] = s["up"] / s["samples"] * 100 if s["samples"] else None
        s["rtt"] = s["rtt_sum"] / s["rtt_n"] if s["rtt_n"] else None
        s["loss"] = s["loss_sum"] / s["up"] * 100 if s["up"] else None
    site_rows = sorted(sites.items(), key=lambda kv: (kv[1]["avail"] if kv[1]["avail"] is not None else 101,
                                                      kv[0].lower()))

    compared = []
    for did, vs in by_node.items():
        if len(vs) < 2:
            continue
        av = {k: v["up"] / v["samples"] * 100 for k, v in vs.items() if v["samples"]}
        rtts = [v["rtt_sum"] / v["rtt_n"] for v in vs.values() if v["rtt_n"]]
        best = max(av, key=lambda k: (av[k], k))
        worst = min(av, key=lambda k: (av[k], k))
        compared.append({"id": did, "best": best, "worst": worst, "best_v": av[best],
                         "worst_v": av[worst], "spread": av[best] - av[worst],
                         "rtt_lo": min(rtts) if rtts else None, "rtt_hi": max(rtts) if rtts else None})
    disagree = sorted((c for c in compared if c["spread"] >= spread_min),
                      key=lambda c: (-c["spread"], names[c["id"]].lower()))
    shown = disagree[:top_n]
    lowest = site_rows[0] if site_rows[0][1]["avail"] is not None else None

    tables = [{"title": "By monitoring site",
               "headers": ["Site", "Sensor", "Nodes", "Availability", "Avg RTT", "Packet loss",
                           "Last result"],
               "styles": ["text", "text", "num", "pct-bar", "num", "pct", "text"],
               "rows": [[s["site"], s["sensor"], f"{len(s['nodes']):,}", _fmt_avail(s["avail"]),
                         _ms(s["rtt"]), _fmt_pct(s["loss"]), ctx.fmt_short(s["last"])]
                        for _k, s in site_rows]}]
    if shown:
        tables.append({
            "title": _truncated(f"Nodes the sites disagree on (≥ {spread_min:g} pts)", len(shown),
                                len(disagree), "nodes"),
            "headers": ["Node", "Best site", "Availability", "Worst site", "Availability",
                        "Difference", "RTT by site"],
            "styles": ["text", "text", "pct", "text", "pct", "num", "num"],
            "rows": [[names[c["id"]], c["best"], _fmt_avail(c["best_v"]), c["worst"],
                      _fmt_avail(c["worst_v"]), f"{c['spread']:.2f} pts",
                      (f"{_ms(c['rtt_lo'])} – {_ms(c['rtt_hi'])}" if c["rtt_lo"] is not None else "—")]
                     for c in shown]})
    elif compared:
        notes.insert(0, f"All {len(compared):,} node(s) monitored from several sites agree within "
                        f"{spread_min:g} percentage points.")
    else:
        notes.insert(0, "Every node is monitored from a single site in this period; "
                        "there is nothing to compare.")
    return _section(
        "site_vantage_reachability", title,
        description=(f"Reachability of {len(by_node):,} node(s) as seen from {len(sites):,} "
                     "monitoring site(s) over the reporting period."),
        kpis=[
            {"label": "Monitoring sites", "value": f"{len(sites):,}", "accent": "primary",
             "subtitle": "With results in the period"},
            {"label": "Nodes seen from 2+ sites", "value": f"{len(compared):,}", "accent": "info",
             "subtitle": f"of {len(by_node):,} measured"},
            {"label": f"Sites disagree (≥ {spread_min:g} pts)", "value": f"{len(disagree):,}",
             "accent": "warning" if disagree else ("success" if compared else "neutral")},
            {"label": "Lowest site availability",
             "value": _fmt_avail(lowest[1]["avail"]) if lowest else "—",
             "accent": "neutral" if not lowest else ("success" if lowest[1]["avail"] >= 99 else "warning"),
             "subtitle": lowest[0] if lowest else "Not measured"},
        ],
        charts=[{"title": "Availability by monitoring site",
                 "series": {"kind": "rank", "unit": "%", "color": "success",
                            "points": [{"label": k, "value": round(s["avail"], 2)}
                                       for k, s in site_rows if s["avail"] is not None]}}],
        tables=tables, notes=notes,
    )


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "availability_by_site_group", "Availability by Site and Group",
    "Node availability rolled up by device group, location and monitoring site — weighted by ping "
    "samples, lowest first, with the configured target and the weakest node in each.",
    "availability",
    [S("availability_kpis"), S("availability_by_group", GROUP_TITLES["group"], group_by="group"),
     S("availability_by_group", GROUP_TITLES["location"], group_by="location"),
     S("availability_by_group", GROUP_TITLES["site"], group_by="site")],
    audience=("IT manager", "Executive", "Network engineer"),
    tags=("availability", "sla", "sites", "groups"), default_period="previous_month",
    frequency="monthly", icon="Layers", featured=True,
)

template(
    "node_availability_previous_month", "Node Availability – Previous Month",
    "Monthly node availability: the headline against target, the trend, every node's "
    "availability and downtime, the longest outages and the planned maintenance excluded.",
    "availability",
    ["availability_kpis", "availability_trend", "device_availability", "top_outages",
     "maintenance_summary"],
    audience=("IT manager", "NOC operator", "Service owner"),
    tags=("availability", "uptime", "monthly", "sla"), default_period="previous_month",
    frequency="monthly", icon="Activity", featured=True,
)

template(
    "down_degraded_nodes", "Down and Degraded Nodes",
    "What is down or degraded right now, the nodes that changed state in the last day and the "
    "longest outages — the daily NOC hand-over.",
    "availability",
    ["device_status_now", S("device_flapping", "Nodes That Changed State", threshold=2),
     "top_outages"],
    audience=("NOC operator", "Network engineer"), tags=("down nodes", "status", "daily"),
    default_period="last_24h", frequency="daily", icon="Server",
)

template(
    "latency_loss_jitter", "Latency, Packet Loss and Jitter",
    "Round-trip time, packet loss and jitter per node: the worst p95 latency, the lossiest and the "
    "most jittery nodes, with trends and the change against the previous period.",
    "performance",
    [S("device_latency_loss", "Top 10 Nodes by Latency (p95)", metric="rtt"),
     S("device_latency_loss", "Top 10 Nodes by Packet Loss", metric="loss"),
     S("device_latency_loss", "Top 10 Nodes by Jitter", metric="jitter")],
    audience=("Network engineer", "NOC operator"), tags=("latency", "packet loss", "jitter", "icmp"),
    default_period="last_7d", frequency="weekly", icon="Gauge", featured=True,
)

template(
    "top_nodes_response_time", "Top 25 Nodes by Response Time and Packet Loss",
    "The 25 nodes with the highest 95th-percentile response time and the 25 with the most packet "
    "loss over the previous month.",
    "performance",
    [S("device_latency_loss", "Top 25 Nodes by Response Time (p95)", metric="rtt", top_n=25),
     S("device_latency_loss", "Top 25 Nodes by Packet Loss", metric="loss", top_n=25)],
    audience=("Network engineer", "IT manager"), tags=("response time", "packet loss", "top n"),
    default_period="previous_month", frequency="monthly", icon="BarChart3",
)

template(
    "unstable_devices_flapping", "Unstable Devices (Flapping)",
    "Nodes that keep changing state, their flapping episodes, outage episodes and recovery times, "
    "devices that rebooted and the longest outages of the period.",
    "availability",
    ["device_flapping", "device_reboots", "top_outages"],
    audience=("Network engineer", "NOC operator"), tags=("flapping", "stability", "outages"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)

template(
    "device_reboot_report", "Device Reboots",
    "Nodes whose SNMP uptime reset during the period — how often, when they last booted and how "
    "long they have been up — with the state changes around them.",
    "availability",
    [S("device_reboots", None, top_n=50), S("device_flapping", "State Changes", top_n=25)],
    audience=("Network engineer", "Systems engineer"), tags=("reboots", "uptime", "stability"),
    default_period="last_30d", frequency="monthly", icon="Cpu",
)

template(
    "planned_maintenance_log", "Planned Maintenance Log",
    "Every device maintenance window that overlapped the period: scope, timing, reason, the "
    "nodes covered and the device time excluded from availability.",
    "availability",
    [S("maintenance_summary", None, top_n=200), "availability_kpis"],
    audience=("IT manager", "NOC operator", "Service owner"), tags=("maintenance", "change", "sla"),
    default_period="previous_month", frequency="monthly", icon="Wrench",
)

template(
    "reachability_by_monitoring_site", "Reachability by Monitoring Site",
    "Availability, latency and loss from each monitoring site, the nodes different sites see "
    "differently, and availability rolled up by each node's primary site.",
    "availability",
    ["site_vantage_reachability", S("availability_by_group", None, group_by="site")],
    audience=("Network engineer", "NOC operator"), tags=("sites", "sensors", "vantage", "reachability"),
    default_period="last_7d", frequency="weekly", icon="Globe",
)
