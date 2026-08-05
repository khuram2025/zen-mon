"""Section-based report engine.

A *section* is a self-contained unit of report content: it fetches its data
for a time window and emits a neutral ``SectionOutput`` dict::

    {
      "id": "availability_trend",
      "title": "Availability Trend",
      "description": "...",
      "kpis":   [{"label", "value", "accent", "subtitle"?}],
      "charts": [{"title", "png": bytes}],          # matplotlib PNG bytes
      "tables": [{"title"?, "headers": [...], "rows": [[...]], "widths"?}],
      "notes":  [str],
    }

Three renderers consume that model:

- :func:`sections_to_json` — for the dashboard's generic on-screen viewer
  (chart PNGs become base64 data URIs);
- :func:`render_html` — a self-contained, print-ready branded HTML document
  (also the artifact stored in ``report_runs`` for share links);
- :func:`render_pdf` — fpdf2 via the existing ``ZenPlusReport`` primitives.

Report *types* are named presets of section ids (``REPORT_PRESETS``); custom
reports are user-picked section lists stored in ``custom_reports``. Legacy
fpdf2 report types (executive_summary/device_health/service_health/
alert_analysis/full_report) keep their original pipeline in
report_service.py — this engine only serves the new types.

Sections reuse the heavy lifting already done for the on-screen report tabs
(report_data_service builders) through a per-render cache, so a render calls
each underlying builder at most once no matter how many sections consume it.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.report_service import (
    COLOR_FAINT,
    COLOR_HAIRLINE,
    COLOR_MUTED,
    COLOR_TEXT,
    CONTENT_W,
    HEX_INFO,
    HEX_PRIMARY,
    HEX_SUCCESS,
    HEX_WARNING,
    MARGIN_L,
    MARGIN_T,
    ZenPlusReport,
    _fetch_company_info,
    _make_donut,
    _make_line_chart,
    _make_time_bar_chart,
    _safe,
)
from app.services.report_data_service import (
    build_business,
    build_executive,
    build_inventory,
    build_technical,
)

logger = logging.getLogger("zenplus.reports")

ENTRY_KINDS = "('SERVER','CONSUMER')"

ACCENT_HEX = {
    "primary": "#4F6BF6",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "info": "#22D3EE",
}
ACCENT_RGB = {
    "primary": (79, 107, 246),
    "success": (34, 197, 94),
    "warning": (245, 158, 11),
    "danger": (239, 68, 68),
    "info": (34, 211, 238),
}


def _fmt_bps(v: float) -> str:
    for unit in ("bps", "Kbps", "Mbps", "Gbps", "Tbps"):
        if abs(v) < 1000:
            return f"{v:,.1f} {unit}"
        v /= 1000
    return f"{v:,.1f} Pbps"


def _fmt_bytes(v: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024:
            return f"{v:,.1f} {unit}"
        v /= 1024
    return f"{v:,.1f} PB"


def _fmt_ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def _iso_short(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ─── Section context with builder cache ─────────────────────────────────────

class SectionCtx:
    """Carries the window + db handle and memoises shared dataset builders."""

    def __init__(self, db: AsyncSession, frm: datetime, to: datetime,
                 filters: Optional[dict] = None):
        self.db = db
        self.frm = frm
        self.to = to
        self.filters = filters or {}
        self._cache: dict[str, Any] = {}

    @property
    def device_ids(self) -> Optional[list[str]]:
        ids = self.filters.get("device_ids")
        return [str(i) for i in ids] if ids else None

    async def dataset(self, name: str) -> dict:
        if name not in self._cache:
            if name == "executive":
                self._cache[name] = await build_executive(self.db, self.frm, self.to)
            elif name == "technical":
                self._cache[name] = await build_technical(self.db, self.frm, self.to)
            elif name == "business":
                self._cache[name] = await build_business(self.db, self.frm, self.to)
            elif name == "inventory":
                self._cache[name] = await build_inventory(self.db)
            else:
                raise KeyError(name)
        return self._cache[name]

    def ch(self):
        from app.core.database import get_clickhouse_client
        return get_clickhouse_client()

    @property
    def hours(self) -> float:
        return max((self.to - self.frm).total_seconds() / 3600.0, 0.01)

    def bucket(self) -> str:
        h = self.hours
        if h <= 6:
            return "5 MINUTE"
        if h <= 48:
            return "30 MINUTE"
        if h <= 21 * 24:
            return "2 HOUR"
        return "1 DAY"


def _section(id: str, title: str, **parts: Any) -> dict:
    out = {"id": id, "title": title, "kpis": [], "charts": [], "tables": [], "notes": []}
    out.update(parts)
    return out


# ─── Availability sections ──────────────────────────────────────────────────

SLA_TARGET_PCT = 99.9  # mirrors report_data_service.build_executive


async def _node_availability(ctx: SectionCtx) -> dict:
    """Per-node availability over the window, honouring the device filter.

    Weighted from ping_metrics_5m (uptime_pct x sample_count), outages from
    device_status_log, device identity/status from Postgres. Cached per render
    so KPIs, trend and the node table share one computation.
    """
    if "node_avail" in ctx._cache:
        return ctx._cache["node_avail"]

    ids = ctx.device_ids
    pg_sql = ("SELECT id::text, hostname, ip_address::text, COALESCE(location, ''), "
              "COALESCE(status, 'unknown') FROM devices")
    pg_params: dict[str, Any] = {}
    if ids:
        pg_sql += " WHERE id::text = ANY(:ids)"
        pg_params["ids"] = ids
    devices = [
        {"id": r[0], "hostname": r[1] or r[2] or r[0][:8],
         "ip": (r[2] or "").split("/")[0],
         "location": r[3], "status": r[4]}
        for r in (await ctx.db.execute(text(pg_sql + " ORDER BY hostname"), pg_params)).fetchall()
    ]

    id_cond = " AND device_id IN %(ids)s" if ids else ""
    ch_params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to}
    if ids:
        ch_params["ids"] = ids

    # Planned maintenance windows (device_id -> [(start, end)], naive UTC):
    # 5-minute rollup buckets whose midpoint falls inside a window are dropped
    # so planned downtime never dents availability or the trend.
    from app.services.report_service import _fetch_device_maintenance_windows
    try:
        maint = await _fetch_device_maintenance_windows(ctx.db, ctx.frm, ctx.to)
    except Exception:
        maint = {}

    def _in_maint(device_id: str, ts) -> bool:
        wins = maint.get(str(device_id))
        if not wins:
            return False
        t = ts.replace(tzinfo=None) if ts.tzinfo else ts
        return any(s <= t <= e for s, e in wins)

    stats: dict[str, dict] = {}
    trend: list[dict] = []
    bucket_secs = {"5 MINUTE": 300, "30 MINUTE": 1800, "2 HOUR": 7200, "1 DAY": 86400}
    try:
        rows = ctx.ch().query(
            f"""
            SELECT device_id,
                   toStartOfInterval(timestamp, INTERVAL {ctx.bucket()}) AS b,
                   -- uptime_pct is stored as a 0..1 fraction in the rollup
                   sum(uptime_pct * sample_count) AS up_weighted,
                   sum(sample_count) AS samples,
                   avg(avg_rtt_ms) AS rtt
            FROM zenplus.ping_metrics_5m
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s{id_cond}
            GROUP BY device_id, b ORDER BY b
            """, parameters=ch_params).result_rows
        half = timedelta(seconds=bucket_secs.get(ctx.bucket(), 300) / 2)
        per_dev: dict[str, list[float]] = {}
        per_bucket: dict[Any, list[float]] = {}
        for did, b, upw, sc, rtt in rows:
            if _in_maint(str(did), b + half):
                continue
            dv = per_dev.setdefault(str(did), [0.0, 0.0, 0.0, 0])
            dv[0] += float(upw or 0)
            dv[1] += float(sc or 0)
            dv[2] += float(rtt or 0)
            dv[3] += 1
            bv = per_bucket.setdefault(b, [0.0, 0.0])
            bv[0] += float(upw or 0)
            bv[1] += float(sc or 0)
        for did, (upw, sc, rtt_sum, nb) in per_dev.items():
            if sc > 0:
                stats[did] = {"avail": upw / sc * 100, "rtt": (rtt_sum / nb) if nb else 0}
        trend = [{"t": b.isoformat(), "v": round(upw / sc * 100, 2)}
                 for b, (upw, sc) in sorted(per_bucket.items()) if sc > 0]

        out_rows = ctx.ch().query(
            f"""
            SELECT device_id, timestamp, duration_sec
            FROM zenplus.device_status_log
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s{id_cond}
              AND lower(new_status) IN ('down', 'offline')
            """, parameters=ch_params).result_rows
        for did, ts, dur in out_rows:
            if _in_maint(str(did), ts):
                continue  # planned downtime
            s = stats.setdefault(str(did), {})
            s["outages"] = s.get("outages", 0) + 1
            s["logged_down_s"] = s.get("logged_down_s", 0) + int(dur or 0)
    except Exception:
        logger.exception("node availability query failed")

    window_min = ctx.hours * 60
    nodes = []
    for d in devices:
        s = stats.get(d["id"], {})
        avail = s.get("avail")
        # SLA-relevant minutes for this node exclude its own maintenance time.
        maint_min = sum(max(0.0, (e - st).total_seconds() / 60)
                        for st, e in maint.get(d["id"], []))
        sla_min = max(window_min - maint_min, 0.0)
        nodes.append({
            **d,
            "avail": round(avail, 3) if avail is not None else None,
            "downtime_min": round((100 - avail) / 100 * sla_min, 1) if avail is not None else None,
            "outages": s.get("outages", 0),
            "rtt": s.get("rtt"),
            "maintenance_min": round(maint_min, 1) if maint_min else 0,
        })
    # Problems first; devices with no data at the bottom.
    nodes.sort(key=lambda n: (n["avail"] is None, n["avail"] if n["avail"] is not None else 0))

    reporting = [n for n in nodes if n["avail"] is not None]
    overall = (sum(n["avail"] for n in reporting) / len(reporting)) if reporting else None
    result = {
        "nodes": nodes,
        "trend": trend,
        "overall": round(overall, 3) if overall is not None else None,
        "up": sum(1 for n in nodes if n["status"] == "up"),
        "below_sla": sum(1 for n in reporting if n["avail"] < SLA_TARGET_PCT),
        "downtime_min": round(sum(n["downtime_min"] or 0 for n in nodes), 1),
        "outage_events": sum(n["outages"] for n in nodes),
        "filtered": bool(ids),
        "maint_excluded_min": round(sum(n["maintenance_min"] for n in nodes), 1),
    }
    ctx._cache["node_avail"] = result
    return result


async def _sec_availability_kpis(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    avail = na["overall"]
    scope = f"{len(na['nodes'])} selected node(s)" if na["filtered"] else "all monitored nodes"
    maint_note = ([f"Planned maintenance excluded: {na['maint_excluded_min']:,.0f} "
                   "device-minutes fell inside maintenance windows and do not "
                   "count against availability."]
                  if na.get("maint_excluded_min") else [])
    return _section(
        "availability_kpis", "Availability Summary",
        notes=maint_note,
        description=f"Computed over {scope} for the reporting window.",
        kpis=[
            {"label": "Availability", "value": _fmt_pct(avail),
             "accent": "success" if (avail or 0) >= SLA_TARGET_PCT else "danger"},
            {"label": f"Nodes up (of {len(na['nodes'])})", "value": str(na["up"]),
             "accent": "success" if na["up"] == len(na["nodes"]) else "warning"},
            {"label": f"Below SLA target ({SLA_TARGET_PCT}%)", "value": str(na["below_sla"]),
             "accent": "danger" if na["below_sla"] else "success"},
            {"label": "Total downtime", "value": f"{na['downtime_min']:,.0f} min",
             "accent": "warning" if na["downtime_min"] else "success"},
            {"label": "Outage events", "value": str(na["outage_events"]), "accent": "info"},
        ],
    )


async def _sec_availability_trend(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    trend = na["trend"]
    ts = [_parse_ts(p["t"]) for p in trend]
    vals = [p["v"] for p in trend]
    png = _make_line_chart(ts, vals, ylabel="availability %", color=HEX_SUCCESS,
                           unit="%", y_domain=(80, 100), h_mm=46)
    scope = "selected nodes" if na["filtered"] else "all monitored nodes"
    return _section(
        "availability_trend", "Availability Trend",
        description=f"Ping availability across {scope} over the reporting window.",
        charts=[{"title": "", "png": png,
                 "series": {"kind": "area", "unit": "%", "color": "success",
                            "y_domain": [80, 100], "points": trend}}],
    )


async def _sec_device_availability(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    rows = [
        [n["hostname"], n["ip"], n["location"] or "—", n["status"],
         _fmt_pct(n["avail"], 2) if n["avail"] is not None else "—",
         f"{n['downtime_min']:,.0f} min" if n["downtime_min"] is not None else "—",
         str(n["outages"]),
         _fmt_ms(n["rtt"]) if n["rtt"] else "—"]
        for n in na["nodes"]
    ]
    scope = "the selected nodes" if na["filtered"] else "every monitored node"
    return _section(
        "device_availability", "Node Availability",
        description=f"Availability, downtime and outages for {scope} — worst first.",
        tables=[{"headers": ["Node", "IP address", "Location", "Status",
                             "Availability", "Downtime", "Outages", "Avg RTT"],
                 "styles": ["text", "mono", "text", "status", "pct-bar", "num", "num", "num"],
                 "rows": rows}],
    )


async def _sec_device_uptime(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    rows = [
        [w.get("hostname") or "—", w.get("ip") or "—", _fmt_pct(w.get("availability_pct")),
         str(w.get("outage_count") or 0), _fmt_ms(w.get("avg_rtt_ms")), _fmt_ms(w.get("p95_rtt_ms"))]
        for w in (d.get("worst_devices") or [])
    ]
    return _section(
        "device_uptime", "Lowest-Availability Devices",
        description="Devices with the poorest availability in the window — start remediation here.",
        tables=[{"headers": ["Device", "IP", "Availability", "Outages", "Avg RTT", "P95 RTT"],
                 "styles": ["text", "mono", "pct-bar", "num", "num", "num"], "rows": rows}],
    )


async def _sec_top_outages(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("executive")
    outages = sorted(d.get("outage_timeline") or [],
                     key=lambda o: -(o.get("duration_minutes") or 0))[:15]
    rows = [[o.get("hostname") or "—", _iso_short(o.get("started_at")),
             f"{o.get('duration_minutes') or 0:,.0f} min"] for o in outages]
    return _section(
        "top_outages", "Longest Outages", half=True,
        tables=[{"headers": ["Device", "Started", "Duration"],
                 "styles": ["text", "num", "num"], "rows": rows}],
    )


async def _sec_service_availability(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("business")
    rows = [
        [s.get("name") or "—", (s.get("type") or "—").upper(), s.get("status") or "—",
         _fmt_pct(s.get("availability_pct")),
         f"{s.get('checks_failed') or 0}/{s.get('checks_total') or 0}"]
        for s in (d.get("service_availability") or [])[:25]
    ]
    tls = [[w.get("name") or "—", str(w.get("days_remaining")), w.get("severity") or ""]
           for w in (d.get("tls_warnings") or [])]
    tables = [{"title": "Service check availability",
               "headers": ["Service", "Type", "Status", "Availability", "Failed checks"],
               "styles": ["text", "text", "status", "pct-bar", "num"], "rows": rows}]
    if tls:
        tables.append({"title": "TLS certificates expiring within 30 days",
                       "headers": ["Service", "Days remaining", "Severity"],
                       "styles": ["text", "num", "severity"], "rows": tls})
    return _section("service_availability", "Service Availability", tables=tables)


# ─── Performance sections ───────────────────────────────────────────────────

async def _sec_network_performance(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    worst = d.get("worst_devices") or []
    rtts = [w.get("p95_rtt_ms") for w in worst if w.get("p95_rtt_ms")]
    kpis = [
        {"label": "Devices analysed", "value": str(len(worst)), "accent": "primary"},
        {"label": "Worst p95 latency", "value": _fmt_ms(max(rtts)) if rtts else "—", "accent": "warning"},
    ]
    rows = [[w.get("hostname") or "—", _fmt_ms(w.get("avg_rtt_ms")), _fmt_ms(w.get("p95_rtt_ms")),
             _fmt_pct(w.get("availability_pct")), str(w.get("outage_count") or 0)]
            for w in worst]
    return _section(
        "network_performance", "Network Device Performance",
        description="Round-trip latency and stability for the most stressed devices.",
        kpis=kpis,
        tables=[{"headers": ["Device", "Avg RTT", "P95 RTT", "Availability", "Outages"],
                 "styles": ["text", "num", "num", "pct-bar", "num"], "rows": rows}],
    )


async def _sec_interface_utilization(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    rows = [
        [f"{i.get('hostname')} · {i.get('if_name')}", _fmt_bps(i.get("in_bps_avg") or 0),
         _fmt_bps(i.get("out_bps_avg") or 0),
         _fmt_pct(i.get("utilization_pct"), 1) if i.get("utilization_pct") is not None else "—"]
        for i in (d.get("top_bandwidth_interfaces") or [])
    ]
    return _section(
        "interface_utilization", "Busiest Interfaces",
        description="Top interfaces by average throughput over the window.",
        tables=[{"headers": ["Interface", "Avg In", "Avg Out", "Utilization"],
                 "styles": ["text", "num", "num", "pct-bar"], "rows": rows}],
    )


async def _sec_server_performance(ctx: SectionCtx) -> dict:
    frm, to = ctx.frm, ctx.to
    rows_out: list[list[str]] = []
    try:
        ch_rows = ctx.ch().query(
            """
            SELECT server_id,
                   avg(avg_total_pct)  AS avg_cpu,
                   max(max_total_pct)  AS max_cpu
            FROM zenplus.host_cpu_metrics_5m
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
            GROUP BY server_id ORDER BY avg_cpu DESC LIMIT 15
            """, parameters={"f": frm, "t": to}).result_rows
        mem_rows = ctx.ch().query(
            """
            SELECT server_id, avg(avg_used_pct) AS avg_mem, max(max_used_pct) AS max_mem
            FROM zenplus.host_memory_metrics_5m
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
            GROUP BY server_id
            """, parameters={"f": frm, "t": to}).result_rows
        mem_map = {str(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in mem_rows}
        ids = [str(r[0]) for r in ch_rows]
        names: dict[str, str] = {}
        if ids:
            pg = await ctx.db.execute(
                text("SELECT id::text, COALESCE(display_name, hostname) FROM servers WHERE id::text = ANY(:ids)"),
                {"ids": ids})
            names = {r[0]: r[1] for r in pg.fetchall()}
        for r in ch_rows:
            sid = str(r[0])
            mem = mem_map.get(sid, (None, None))
            rows_out.append([
                names.get(sid, sid[:8]), f"{float(r[1] or 0):.1f}%", f"{float(r[2] or 0):.1f}%",
                _fmt_pct(mem[0], 1) if mem[0] is not None else "—",
                _fmt_pct(mem[1], 1) if mem[1] is not None else "—",
            ])
    except Exception:
        logger.debug("server performance section: host metrics unavailable", exc_info=True)
    return _section(
        "server_performance", "Server Performance",
        description="Agent-monitored servers ranked by average CPU load.",
        tables=[{"headers": ["Server", "Avg CPU", "Peak CPU", "Avg Mem", "Peak Mem"],
                 "styles": ["text", "pct-bar", "num", "pct-bar", "num"], "rows": rows_out}],
        notes=[] if rows_out else ["No agent-monitored servers reported in this window."],
    )


# ─── Traffic (NetFlow) sections ─────────────────────────────────────────────

async def _sec_traffic_kpis(ctx: SectionCtx) -> dict:
    total_bytes = total_pkts = total_flows = 0
    try:
        r = ctx.ch().query(
            "SELECT sum(bytes), sum(packets), sum(flow_count) FROM zenplus.flow_traffic_5m "
            "WHERE timestamp >= %(f)s AND timestamp <= %(t)s",
            parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        if r:
            total_bytes, total_pkts, total_flows = (int(r[0][0] or 0), int(r[0][1] or 0), int(r[0][2] or 0))
    except Exception:
        logger.debug("traffic kpis: flow tables unavailable", exc_info=True)
    avg_bps = total_bytes * 8 / (ctx.hours * 3600)
    return _section(
        "traffic_kpis", "Traffic Summary",
        kpis=[
            {"label": "Total volume", "value": _fmt_bytes(total_bytes), "accent": "primary"},
            {"label": "Average rate", "value": _fmt_bps(avg_bps), "accent": "info"},
            {"label": "Packets", "value": f"{total_pkts:,}", "accent": "primary"},
            {"label": "Flows", "value": f"{total_flows:,}", "accent": "info"},
        ],
    )


async def _sec_traffic_trend(ctx: SectionCtx) -> dict:
    ts: list[datetime] = []
    vals: list[float] = []
    try:
        rows = ctx.ch().query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {ctx.bucket()}) AS b,
                   sum(bytes) * 8 / 300 AS bps
            FROM zenplus.flow_traffic_5m
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
            GROUP BY b ORDER BY b
            """, parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        secs = {"5 MINUTE": 300, "30 MINUTE": 1800, "2 HOUR": 7200, "1 DAY": 86400}[ctx.bucket()]
        for r in rows:
            ts.append(r[0])
            vals.append(float(r[1] or 0) * 300 / secs / 1e6)  # Mbps normalised per bucket
    except Exception:
        logger.debug("traffic trend unavailable", exc_info=True)
    png = _make_line_chart(ts, vals, ylabel="Mbps", color=HEX_INFO)
    return _section("traffic_trend", "Traffic Trend",
                    description="Network-wide NetFlow throughput.",
                    charts=[{"title": "", "png": png,
                             "series": {"kind": "area", "unit": "Mbps", "color": "info",
                                        "points": [{"t": t.isoformat(), "v": round(v, 2)}
                                                   for t, v in zip(ts, vals)]}}])


async def _sec_traffic_protocols(ctx: SectionCtx) -> dict:
    labels: list[str] = []
    values: list[float] = []
    try:
        proto_names = {6: "TCP", 17: "UDP", 1: "ICMP", 47: "GRE", 50: "ESP"}
        rows = ctx.ch().query(
            "SELECT protocol, sum(bytes) AS b FROM zenplus.flow_traffic_5m "
            "WHERE timestamp >= %(f)s AND timestamp <= %(t)s "
            "GROUP BY protocol ORDER BY b DESC LIMIT 6",
            parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        for r in rows:
            labels.append(proto_names.get(int(r[0]), f"proto {r[0]}"))
            values.append(float(r[1] or 0) / 1e9)
    except Exception:
        logger.debug("traffic protocols unavailable", exc_info=True)
    png = _make_donut(labels, [round(v, 2) for v in values],
                      center_text=_fmt_bytes(sum(values) * 1e9) if values else "",
                      center_sub="total", unit="GB")
    return _section("traffic_protocols", "Traffic by Protocol", half=True,
                    charts=[{"title": "Share of bytes (GB)", "png": png,
                             "series": {"kind": "donut", "unit": "GB",
                                        "points": [{"label": l, "value": round(v, 2)}
                                                   for l, v in zip(labels, values)]}}])


async def _sec_traffic_ports(ctx: SectionCtx) -> dict:
    port_names = {80: "HTTP", 443: "HTTPS", 53: "DNS", 22: "SSH", 25: "SMTP", 3389: "RDP",
                  445: "SMB", 123: "NTP", 161: "SNMP", 1194: "OpenVPN", 500: "IPsec IKE",
                  514: "Syslog", 1556: "NetBackup", 8123: "ClickHouse"}
    rows_out: list[list[Any]] = []
    try:
        rows = ctx.ch().query(
            "SELECT dst_port, sum(bytes) AS b, sum(flow_count) AS fc "
            "FROM zenplus.flow_traffic_5m WHERE timestamp >= %(f)s AND timestamp <= %(t)s "
            "GROUP BY dst_port ORDER BY b DESC LIMIT 12",
            parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        total = sum(float(r[1] or 0) for r in rows) or 1.0
        for r in rows:
            port = int(r[0])
            label = (f"{port} · {port_names[port]}" if port in port_names
                     else "non-TCP/UDP traffic" if port == 0 else str(port))
            share = float(r[1] or 0) / total * 100
            rows_out.append([label, _fmt_bytes(float(r[1] or 0)),
                             f"{share:.1f}%", f"{int(r[2] or 0):,}"])
    except Exception:
        logger.debug("traffic ports unavailable", exc_info=True)
    return _section("traffic_ports", "Top Applications / Ports", half=True,
                    tables=[{"headers": ["Application / port", "Volume", "Share", "Flows"],
                             "styles": ["text", "num", "pct-bar", "num"],
                             "rows": rows_out}])


async def _sec_traffic_talkers(ctx: SectionCtx) -> dict:
    """Top source/destination endpoints by bytes, from raw flow records."""
    src_rows: list[list[Any]] = []
    dst_rows: list[list[Any]] = []
    try:
        for col, out in (("src_addr", src_rows), ("dst_addr", dst_rows)):
            rows = ctx.ch().query(
                f"SELECT {col}, sum(bytes) AS b, count() AS fc "
                f"FROM zenplus.flow_records WHERE timestamp >= %(f)s AND timestamp <= %(t)s "
                f"GROUP BY {col} ORDER BY b DESC LIMIT 10",
                parameters={"f": ctx.frm, "t": ctx.to}).result_rows
            total = sum(float(r[1] or 0) for r in rows) or 1.0
            for r in rows:
                out.append([str(r[0]), _fmt_bytes(float(r[1] or 0)),
                            f"{float(r[1] or 0) / total * 100:.1f}%", f"{int(r[2]):,}"])
    except Exception:
        logger.debug("traffic talkers unavailable", exc_info=True)
    return _section(
        "traffic_talkers", "Top Talkers",
        description="Endpoints moving the most traffic (raw NetFlow records).",
        tables=[
            {"title": "Top sources", "headers": ["Source", "Volume", "Share", "Flows"],
             "styles": ["mono", "num", "pct-bar", "num"], "rows": src_rows},
            {"title": "Top destinations", "headers": ["Destination", "Volume", "Share", "Flows"],
             "styles": ["mono", "num", "pct-bar", "num"], "rows": dst_rows},
        ],
    )


# ─── APM sections ───────────────────────────────────────────────────────────

async def _sec_apm_services(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    healthy = degraded = 0
    try:
        rows = ctx.ch().query(
            f"""
            SELECT service_name,
                   sum(request_count) AS reqs,
                   sum(error_count)   AS errs,
                   arrayElement(quantilesTDigestMerge(0.5,0.95)(duration_state),1) AS p50,
                   arrayElement(quantilesTDigestMerge(0.5,0.95)(duration_state),2) AS p95,
                   sum(satisfied_count) AS sat, sum(tolerating_count) AS tol
            FROM zenplus.apm_span_metrics_5m
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND span_kind IN {ENTRY_KINDS}
            GROUP BY service_name ORDER BY reqs DESC LIMIT 25
            """, parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        for r in rows:
            reqs, errs = int(r[1] or 0), int(r[2] or 0)
            err_rate = errs / reqs if reqs else 0
            p95 = float(r[4] or 0)
            apdex = (int(r[5] or 0) + int(r[6] or 0) / 2) / reqs if reqs else 0
            state = "critical" if (err_rate >= 0.05 or p95 >= 1000) else \
                    "degraded" if (err_rate >= 0.01 or p95 >= 500) else "healthy"
            healthy += state == "healthy"
            degraded += state != "healthy"
            rows_out.append([r[0], f"{reqs:,}", _fmt_pct(err_rate * 100), _fmt_ms(float(r[3] or 0)),
                             _fmt_ms(p95), f"{apdex:.2f}", state])
    except Exception:
        logger.debug("apm services section unavailable", exc_info=True)
    return _section(
        "apm_services", "Application Service Health",
        description="Golden signals per instrumented service (entry spans).",
        kpis=[
            {"label": "Services reporting", "value": str(healthy + degraded), "accent": "primary"},
            {"label": "Healthy", "value": str(healthy), "accent": "success"},
            {"label": "Degraded / critical", "value": str(degraded),
             "accent": "danger" if degraded else "success"},
        ],
        tables=[{"headers": ["Service", "Requests", "Error rate", "P50", "P95", "Apdex", "Health"],
                 "styles": ["text", "num", "num", "num", "num", "num", "status"],
                 "rows": rows_out}],
    )


async def _sec_apm_errors(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = ctx.ch().query(
            """
            SELECT group_id, anyHeavy(exception_type), anyHeavy(exception_message),
                   anyHeavy(service_name), count() AS occurrences, max(timestamp)
            FROM zenplus.apm_exceptions
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
            GROUP BY group_id ORDER BY occurrences DESC LIMIT 12
            """, parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        for r in rows:
            rows_out.append([str(r[1] or "Error"), (str(r[2] or ""))[:60], str(r[3] or "—"),
                             f"{int(r[4]):,}", r[5].strftime("%m-%d %H:%M") if r[5] else "—"])
    except Exception:
        logger.debug("apm errors section unavailable", exc_info=True)
    return _section("apm_errors", "Top Application Errors",
                    tables=[{"headers": ["Type", "Message", "Service", "Count", "Last seen"],
                             "rows": rows_out}])


async def _sec_slo_attainment(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        from app.services.apm_slo_service import compute_slo_status, _load_slos
        slos = await _load_slos(ctx.db)
        for slo in slos:
            try:
                st = compute_slo_status(slo)
                remaining = max(0.0, 1.0 - float(st.get("budget_consumed") or 0)) * 100
                rows_out.append([
                    slo["name"], slo["service_name"], f"{slo['target']}% / {slo['window_days']}d",
                    f"{remaining:.0f}%",
                    "BREACHING" if (st.get("budget_consumed") or 0) >= 1 else
                    "AT RISK" if remaining < 25 else "ON TRACK",
                ])
            except Exception:
                rows_out.append([slo["name"], slo["service_name"],
                                 f"{slo['target']}% / {slo['window_days']}d", "—", "—"])
    except Exception:
        logger.debug("slo section unavailable", exc_info=True)
    return _section(
        "slo_attainment", "SLO Attainment",
        tables=[{"headers": ["SLO", "Service", "Objective", "Budget remaining", "Status"],
                 "styles": ["text", "text", "text", "pct-bar", "status"], "rows": rows_out}],
        notes=[] if rows_out else ["No SLOs defined."],
    )


async def _sec_synthetics(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        mons = (await ctx.db.execute(text(
            "SELECT id::text, name, status FROM apm_synthetic_monitors ORDER BY name"))).fetchall()
        if mons:
            import uuid as _uuid
            stats = ctx.ch().query(
                """
                SELECT monitor_id, count(), countIf(success = 1), avg(total_ms)
                FROM zenplus.apm_synthetic_results
                WHERE monitor_id IN %(ids)s AND timestamp >= %(f)s AND timestamp <= %(t)s
                GROUP BY monitor_id
                """,
                parameters={"ids": [_uuid.UUID(m[0]) for m in mons], "f": ctx.frm, "t": ctx.to},
            ).result_rows
            smap = {str(r[0]): r for r in stats}
            for m in mons:
                s = smap.get(m[0])
                uptime = f"{int(s[2]) / int(s[1]) * 100:.2f}%" if s and s[1] else "—"
                rows_out.append([m[1], (m[2] or "unknown").upper(), uptime,
                                 f"{int(s[1]):,}" if s else "0",
                                 _fmt_ms(float(s[3])) if s else "—"])
    except Exception:
        logger.debug("synthetics section unavailable", exc_info=True)
    return _section(
        "synthetics", "Synthetic Scenario Uptime",
        description="Scripted user-journey checks run from the appliance.",
        tables=[{"headers": ["Scenario", "Status", "Uptime", "Runs", "Avg duration"],
                 "styles": ["text", "status", "pct-bar", "num", "num"], "rows": rows_out}],
        notes=[] if rows_out else ["No synthetic scenarios configured."],
    )


# ─── Usage sections ─────────────────────────────────────────────────────────

USER_EXPR = ("if(attributes_string['enduser.id'] != '', "
             "attributes_string['enduser.id'], attributes_string['user.id'])")


async def _sec_usage_kpis(ctx: SectionCtx) -> dict:
    reqs = users = pages = errors = 0
    p95 = 0.0
    series_png = None
    try:
        # Raw spans carry a 7-day TTL; clamp so long windows stay honest.
        frm = max(ctx.frm, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)) \
            if ctx.frm.tzinfo is None else max(ctx.frm, datetime.now(timezone.utc) - timedelta(days=7))
        t = ctx.ch().query(
            f"""
            SELECT count(), uniqIf({USER_EXPR}, {USER_EXPR} != ''),
                   uniqIf(http_route, http_route != ''), countIf(has_error = 1),
                   quantile(0.95)(duration_nano) / 1e6
            FROM zenplus.apm_spans
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND span_kind_str IN {ENTRY_KINDS}
            """, parameters={"f": frm, "t": ctx.to}).result_rows[0]
        reqs, users, pages, errors, p95 = int(t[0]), int(t[1]), int(t[2]), int(t[3]), float(t[4] or 0)
        series = ctx.ch().query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {ctx.bucket()}) AS b, count()
            FROM zenplus.apm_spans
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND span_kind_str IN {ENTRY_KINDS}
            GROUP BY b ORDER BY b
            """, parameters={"f": frm, "t": ctx.to}).result_rows
        series_pts = [(r[0], float(r[1])) for r in series]
        series_png = _make_line_chart([t for t, _ in series_pts], [v for _, v in series_pts],
                                      ylabel="requests", color=HEX_PRIMARY)
    except Exception:
        logger.debug("usage kpis unavailable", exc_info=True)
    return _section(
        "usage_kpis", "Usage Summary",
        description="Application traffic and audience (usage data window is bounded to 7 days).",
        kpis=[
            {"label": "Requests", "value": f"{reqs:,}", "accent": "primary"},
            {"label": "Unique users", "value": f"{users:,}", "accent": "info"},
            {"label": "Pages", "value": f"{pages:,}", "accent": "primary"},
            {"label": "Error rate", "value": _fmt_pct(errors / reqs * 100 if reqs else 0),
             "accent": "danger" if reqs and errors / reqs > 0.02 else "success"},
            {"label": "P95 latency", "value": _fmt_ms(p95), "accent": "warning"},
        ],
        charts=([{"title": "Request volume", "png": series_png,
                  "series": {"kind": "area", "unit": "requests", "color": "primary",
                             "points": [{"t": t.isoformat(), "v": v}
                                        for t, v in series_pts]}}] if series_png else []),
    )


async def _sec_usage_pages(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = ctx.ch().query(
            f"""
            SELECT http_route, anyHeavy(service_name), count() AS hits,
                   uniqIf({USER_EXPR}, {USER_EXPR} != ''), countIf(has_error = 1),
                   quantile(0.95)(duration_nano) / 1e6
            FROM zenplus.apm_spans
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
              AND span_kind_str IN {ENTRY_KINDS} AND http_route != ''
            GROUP BY http_route ORDER BY hits DESC LIMIT 15
            """, parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        for r in rows:
            hits = int(r[2])
            rows_out.append([r[0], str(r[1]), f"{hits:,}", f"{int(r[3]):,}",
                             _fmt_pct(int(r[4]) / hits * 100 if hits else 0), _fmt_ms(float(r[5] or 0))])
    except Exception:
        logger.debug("usage pages unavailable", exc_info=True)
    return _section("usage_pages", "Top Pages",
                    tables=[{"headers": ["Route", "Service", "Hits", "Users", "Error rate", "P95"],
                             "styles": ["mono", "text", "num", "num", "num", "num"],
                             "rows": rows_out}])


async def _sec_usage_users(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = ctx.ch().query(
            f"""
            SELECT {USER_EXPR} AS uid, count() AS reqs, countIf(has_error = 1),
                   uniqIf(http_route, http_route != ''), max(timestamp)
            FROM zenplus.apm_spans
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
              AND span_kind_str IN {ENTRY_KINDS} AND {USER_EXPR} != ''
            GROUP BY uid ORDER BY reqs DESC LIMIT 15
            """, parameters={"f": ctx.frm, "t": ctx.to}).result_rows
        for r in rows:
            rows_out.append([r[0], f"{int(r[1]):,}", f"{int(r[2]):,}", f"{int(r[3]):,}",
                             r[4].strftime("%m-%d %H:%M") if r[4] else "—"])
    except Exception:
        logger.debug("usage users unavailable", exc_info=True)
    return _section(
        "usage_users", "Top Users",
        tables=[{"headers": ["User", "Requests", "Errors", "Pages", "Last seen"],
                 "styles": ["mono", "num", "num", "num", "num"], "rows": rows_out}],
        notes=[] if rows_out else
        ["No user attribution — set the enduser.id span attribute to unlock per-user analytics."],
    )


# ─── Capacity sections ──────────────────────────────────────────────────────

async def _sec_capacity_filesystems(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = (await ctx.db.execute(text(
            """
            SELECT COALESCE(s.display_name, s.hostname), f.mount, f.used_pct,
                   f.total_bytes, f.free_bytes
            FROM server_filesystem_inventory f
            JOIN servers s ON s.id = f.server_id
            WHERE f.used_pct IS NOT NULL
            ORDER BY f.used_pct DESC LIMIT 20
            """))).fetchall()
        for r in rows:
            rows_out.append([r[0] or "—", r[1] or "—", _fmt_pct(float(r[2]), 1),
                             _fmt_bytes(float(r[3] or 0)), _fmt_bytes(float(r[4] or 0))])
    except Exception:
        logger.debug("capacity filesystems unavailable", exc_info=True)
    critical = sum(1 for r in rows_out if float(r[2].rstrip("%") or 0) >= 90)
    return _section(
        "capacity_filesystems", "Filesystem Capacity",
        description="Fullest filesystems across agent-monitored servers.",
        kpis=[{"label": "Filesystems ≥90% full", "value": str(critical),
               "accent": "danger" if critical else "success"}],
        tables=[{"headers": ["Server", "Mount", "Used", "Size", "Free"],
                 "styles": ["text", "mono", "pct-bar", "num", "num"], "rows": rows_out}],
    )


async def _sec_capacity_interfaces(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    rows = [
        [f"{i.get('hostname')} · {i.get('if_name')}",
         _fmt_pct(i.get("utilization_pct"), 1) if i.get("utilization_pct") is not None else "—",
         _fmt_bps((i.get("in_bps_avg") or 0) + (i.get("out_bps_avg") or 0))]
        for i in (d.get("top_bandwidth_interfaces") or [])
        if (i.get("utilization_pct") or 0) > 0
    ]
    rows.sort(key=lambda r: -float(r[1].rstrip("%")) if r[1] != "—" else 0)
    return _section(
        "capacity_interfaces", "Link Capacity Headroom",
        description="Interfaces closest to saturation (average utilization vs configured speed).",
        tables=[{"headers": ["Interface", "Utilization", "Avg throughput"], "rows": rows[:15]}],
    )


# ─── Alerting sections ──────────────────────────────────────────────────────

async def _sec_alert_kpis(ctx: SectionCtx) -> dict:
    counts = {"critical": 0, "warning": 0, "info": 0}
    mttr_min = None
    total = 0
    try:
        rows = (await ctx.db.execute(text(
            "SELECT severity, COUNT(*) FROM alerts WHERE triggered_at >= :f AND triggered_at <= :t "
            "GROUP BY severity"), {"f": ctx.frm, "t": ctx.to})).fetchall()
        for sev, c in rows:
            counts[str(sev)] = int(c)
            total += int(c)
        m = (await ctx.db.execute(text(
            "SELECT AVG(EXTRACT(EPOCH FROM (resolved_at - triggered_at)))/60 FROM alerts "
            "WHERE resolved_at IS NOT NULL AND triggered_at >= :f AND triggered_at <= :t"),
            {"f": ctx.frm, "t": ctx.to})).scalar()
        mttr_min = float(m) if m is not None else None
    except Exception:
        logger.debug("alert kpis unavailable", exc_info=True)
    return _section(
        "alert_kpis", "Alert Summary",
        kpis=[
            {"label": "Total alerts", "value": f"{total:,}", "accent": "primary"},
            {"label": "Critical", "value": str(counts.get("critical", 0)),
             "accent": "danger" if counts.get("critical") else "success"},
            {"label": "Warning", "value": str(counts.get("warning", 0)), "accent": "warning"},
            {"label": "MTTR", "value": f"{mttr_min:.0f} min" if mttr_min is not None else "—",
             "accent": "info"},
        ],
    )


async def _sec_alert_trend(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    vol = d.get("alert_volume_by_severity") or []
    ts = [_parse_ts(p["ts"]) for p in vol]
    crit = [p.get("critical") or 0 for p in vol]
    totals = [(p.get("critical") or 0) + (p.get("warning") or 0) +
              (p.get("info") or 0) for p in vol]
    png = _make_time_bar_chart(ts, totals, color=HEX_WARNING, ylabel="alerts",
                               w_mm=85, h_mm=52)
    sec = _section("alert_trend", "Alert Volume Over Time", half=True,
                   charts=[{"title": "", "png": png,
                            "series": {"kind": "bars", "unit": "alerts", "color": "warning",
                                       "points": [{"t": t.isoformat(), "v": v}
                                                  for t, v in zip(ts, totals)]}}])
    if any(crit):
        sec["notes"].append(f"{sum(crit)} critical alert(s) in the window.")
    return sec


async def _sec_noisy_sources(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("technical")
    rows = [[n.get("hostname") or "—", (n.get("sample_message") or "")[:60],
             (n.get("severity") or "").upper(), str(n.get("alert_count") or 0)]
            for n in (d.get("noisy_alerts") or [])]
    return _section("noisy_sources", "Noisiest Alert Sources", half=True,
                    tables=[{"headers": ["Device", "Sample message", "Severity", "Count"],
                             "styles": ["text", "text", "severity", "num"], "rows": rows}])


async def _sec_recent_alerts(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = (await ctx.db.execute(text(
            """
            SELECT a.severity, a.message, COALESCE(d.hostname, a.metadata->>'service', '—'),
                   a.status, a.triggered_at
            FROM alerts a LEFT JOIN devices d ON d.id = a.device_id
            WHERE a.triggered_at >= :f AND a.triggered_at <= :t
            ORDER BY a.triggered_at DESC LIMIT 25
            """), {"f": ctx.frm, "t": ctx.to})).fetchall()
        for r in rows:
            rows_out.append([(r[0] or "").upper(), (r[1] or "")[:70], r[2], r[3] or "—",
                             r[4].strftime("%m-%d %H:%M") if r[4] else "—"])
    except Exception:
        logger.debug("recent alerts unavailable", exc_info=True)
    return _section("recent_alerts", "Recent Alerts",
                    tables=[{"headers": ["Severity", "Message", "Source", "Status", "Triggered"],
                             "styles": ["severity", "text", "text", "status", "num"],
                             "rows": rows_out}])


# ─── Inventory sections ─────────────────────────────────────────────────────

async def _sec_inventory_summary(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("inventory")
    totals = d.get("totals") or {}
    return _section(
        "inventory_summary", "Asset Summary",
        kpis=[
            {"label": "Network devices", "value": str(totals.get("devices") or 0), "accent": "primary"},
            {"label": "Monitored interfaces", "value": str((d.get("interface_totals") or {}).get("monitored") or 0),
             "accent": "info"},
            {"label": "Remote sensors", "value": str(totals.get("sensors") or 0), "accent": "primary"},
        ],
    )


async def _sec_inventory_breakdown(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("inventory")
    by_type = d.get("devices_by_type") or []
    total_dev = sum(t.get("count") or 0 for t in by_type)
    png = _make_donut([(t.get("type") or "unknown").replace("_", " ") for t in by_type[:6]],
                      [t.get("count") or 0 for t in by_type[:6]],
                      center_text=f"{total_dev:,}" if total_dev else "",
                      center_sub="devices")
    vendor_rows = [[v.get("vendor") or "unknown", str(v.get("count") or 0)]
                   for v in (d.get("devices_by_vendor") or [])[:10]]
    loc_rows = [[l.get("location") or "unknown", str(l.get("count") or 0)]
                for l in (d.get("devices_by_location") or [])[:10]]
    return _section(
        "inventory_breakdown", "Device Breakdown",
        charts=[{"title": "Devices by type", "png": png,
                 "series": {"kind": "donut", "unit": "devices",
                            "points": [{"label": t.get("type") or "unknown",
                                        "value": t.get("count") or 0}
                                       for t in by_type[:6]]}}],
        tables=[{"title": "By vendor", "headers": ["Vendor", "Devices"], "rows": vendor_rows},
                {"title": "By location", "headers": ["Location", "Devices"], "rows": loc_rows}],
    )


async def _sec_inventory_servers(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        rows = (await ctx.db.execute(text(
            """
            SELECT COALESCE(display_name, hostname), os_name, os_version, status,
                   environment, collection_mode
            FROM servers ORDER BY hostname LIMIT 50
            """))).fetchall()
        for r in rows:
            rows_out.append([r[0] or "—", f"{r[1] or '—'} {r[2] or ''}".strip(),
                             (r[3] or "—"), r[4] or "—", r[5] or "—"])
    except Exception:
        logger.debug("inventory servers unavailable", exc_info=True)
    return _section("inventory_servers", "Server Inventory",
                    tables=[{"headers": ["Server", "Operating system", "Status", "Environment", "Mode"],
                             "rows": rows_out}])


# ─── Registry + presets ─────────────────────────────────────────────────────

SectionFn = Callable[[SectionCtx], Awaitable[dict]]

SECTION_REGISTRY: dict[str, dict[str, Any]] = {
    # Availability
    "availability_kpis": {"fn": _sec_availability_kpis, "title": "Availability Summary",
                          "category": "Availability",
                          "description": "Uptime %, SLA attainment, incidents and MTTR."},
    "availability_trend": {"fn": _sec_availability_trend, "title": "Availability Trend",
                           "category": "Availability",
                           "description": "Network-wide availability chart over the window."},
    "device_availability": {"fn": _sec_device_availability, "title": "Node Availability",
                            "category": "Availability",
                            "description": "Per-node availability, downtime and outages "
                                           "(supports node selection)."},
    "device_uptime": {"fn": _sec_device_uptime, "title": "Lowest-Availability Devices",
                      "category": "Availability",
                      "description": "Devices with the worst uptime and their latency."},
    "top_outages": {"fn": _sec_top_outages, "title": "Longest Outages", "category": "Availability",
                    "description": "Outage episodes ranked by duration."},
    "service_availability": {"fn": _sec_service_availability, "title": "Service Availability",
                             "category": "Availability",
                             "description": "Service checks uptime and expiring TLS certificates."},
    # Performance
    "network_performance": {"fn": _sec_network_performance, "title": "Network Device Performance",
                            "category": "Performance",
                            "description": "Latency and stability of the most stressed devices."},
    "interface_utilization": {"fn": _sec_interface_utilization, "title": "Busiest Interfaces",
                              "category": "Performance",
                              "description": "Top interfaces by average throughput."},
    "server_performance": {"fn": _sec_server_performance, "title": "Server Performance",
                           "category": "Performance",
                           "description": "Agent-monitored servers ranked by CPU and memory load."},
    # Traffic
    "traffic_kpis": {"fn": _sec_traffic_kpis, "title": "Traffic Summary", "category": "Traffic",
                     "description": "Total NetFlow volume, rate, packets and flows."},
    "traffic_trend": {"fn": _sec_traffic_trend, "title": "Traffic Trend", "category": "Traffic",
                      "description": "Throughput over time from NetFlow."},
    "traffic_protocols": {"fn": _sec_traffic_protocols, "title": "Traffic by Protocol",
                          "category": "Traffic", "description": "Protocol share of total bytes."},
    "traffic_ports": {"fn": _sec_traffic_ports, "title": "Top Applications / Ports",
                      "category": "Traffic", "description": "Destination ports ranked by volume."},
    "traffic_talkers": {"fn": _sec_traffic_talkers, "title": "Top Talkers",
                        "category": "Traffic",
                        "description": "Source and destination endpoints by traffic volume."},
    # Applications
    "apm_services": {"fn": _sec_apm_services, "title": "Application Service Health",
                     "category": "Applications",
                     "description": "RED metrics and apdex per instrumented service."},
    "apm_errors": {"fn": _sec_apm_errors, "title": "Top Application Errors",
                   "category": "Applications", "description": "Most frequent exception groups."},
    "slo_attainment": {"fn": _sec_slo_attainment, "title": "SLO Attainment",
                       "category": "Applications",
                       "description": "Error-budget status for every SLO."},
    "synthetics": {"fn": _sec_synthetics, "title": "Synthetic Scenario Uptime",
                   "category": "Applications",
                   "description": "Scripted user-journey check results."},
    "usage_kpis": {"fn": _sec_usage_kpis, "title": "Usage Summary", "category": "Applications",
                   "description": "Requests, unique users, pages and error rate."},
    "usage_pages": {"fn": _sec_usage_pages, "title": "Top Pages", "category": "Applications",
                    "description": "Most visited routes with audience and latency."},
    "usage_users": {"fn": _sec_usage_users, "title": "Top Users", "category": "Applications",
                    "description": "Most active users by request volume."},
    # Capacity
    "capacity_filesystems": {"fn": _sec_capacity_filesystems, "title": "Filesystem Capacity",
                             "category": "Capacity",
                             "description": "Fullest filesystems across servers."},
    "capacity_interfaces": {"fn": _sec_capacity_interfaces, "title": "Link Capacity Headroom",
                            "category": "Capacity",
                            "description": "Interfaces closest to saturation."},
    # Alerting
    "alert_kpis": {"fn": _sec_alert_kpis, "title": "Alert Summary", "category": "Alerting",
                   "description": "Alert counts by severity and MTTR."},
    "alert_trend": {"fn": _sec_alert_trend, "title": "Alert Volume Over Time",
                    "category": "Alerting", "description": "Alert volume chart."},
    "noisy_sources": {"fn": _sec_noisy_sources, "title": "Noisiest Alert Sources",
                      "category": "Alerting", "description": "Devices/rules generating the most alerts."},
    "recent_alerts": {"fn": _sec_recent_alerts, "title": "Recent Alerts", "category": "Alerting",
                      "description": "Latest alerts in the window."},
    # Inventory
    "inventory_summary": {"fn": _sec_inventory_summary, "title": "Asset Summary",
                          "category": "Inventory", "description": "Fleet totals."},
    "inventory_breakdown": {"fn": _sec_inventory_breakdown, "title": "Device Breakdown",
                            "category": "Inventory",
                            "description": "Devices by type, vendor and location."},
    "inventory_servers": {"fn": _sec_inventory_servers, "title": "Server Inventory",
                          "category": "Inventory", "description": "Agent-monitored server list."},
}

REPORT_PRESETS: dict[str, dict[str, Any]] = {
    "availability": {
        "title": "Availability & SLA Report",
        "description": "Uptime, SLA attainment, outages and service availability.",
        "category": "Operations",
        "sections": ["availability_kpis", "availability_trend", "device_availability",
                     "top_outages", "service_availability"],
    },
    "performance": {
        "title": "Performance Report",
        "description": "Network latency, interface throughput and server load.",
        "category": "Operations",
        "sections": ["network_performance", "interface_utilization", "server_performance"],
    },
    "traffic": {
        "title": "Traffic Analysis Report",
        "description": "NetFlow volumes, protocols and top applications.",
        "category": "Operations",
        "sections": ["traffic_kpis", "traffic_trend", "traffic_protocols", "traffic_ports",
                     "traffic_talkers"],
    },
    "alerts": {
        "title": "Alert Insights Report",
        "description": "Alert volumes, noisiest sources and response times.",
        "category": "Operations",
        "sections": ["alert_kpis", "alert_trend", "noisy_sources", "recent_alerts"],
    },
    "capacity": {
        "title": "Capacity Planning Report",
        "description": "Filesystem and link capacity headroom.",
        "category": "Operations",
        "sections": ["capacity_filesystems", "capacity_interfaces", "server_performance"],
    },
    "apm_performance": {
        "title": "Application Performance Report",
        "description": "Service golden signals, SLOs, errors and synthetic checks.",
        "category": "Applications",
        "sections": ["apm_services", "slo_attainment", "apm_errors", "synthetics"],
    },
    "usage": {
        "title": "Usage Analytics Report",
        "description": "Application traffic, top pages and most active users.",
        "category": "Applications",
        "sections": ["usage_kpis", "usage_pages", "usage_users"],
    },
    "inventory": {
        "title": "Inventory Report",
        "description": "Devices, interfaces, sensors and servers on record.",
        "category": "Assets",
        "sections": ["inventory_summary", "inventory_breakdown", "inventory_servers"],
    },
}


async def build_sections(db: AsyncSession, section_ids: list[str],
                         frm: datetime, to: datetime,
                         filters: Optional[dict] = None) -> list[dict]:
    """Run the given sections; a failing section becomes an error note, never
    a failed report."""
    ctx = SectionCtx(db, frm, to, filters)
    out: list[dict] = []
    for sid in section_ids:
        entry = SECTION_REGISTRY.get(sid)
        if not entry:
            continue
        try:
            sec = await entry["fn"](ctx)
        except Exception as exc:
            logger.exception("report section %s failed", sid)
            sec = _section(sid, entry["title"],
                           notes=[f"Section could not be generated: {exc}"])
        sec.setdefault("category", entry.get("category"))
        out.append(sec)
    return out


# ─── Renderers ──────────────────────────────────────────────────────────────

def sections_to_json(sections: list[dict]) -> list[dict]:
    out = []
    for s in sections:
        j = dict(s)
        charts = []
        for c in s.get("charts") or []:
            entry: dict[str, Any] = {"title": c.get("title") or ""}
            if c.get("series"):
                # Native series render client-side; skip the heavy PNG payload.
                entry["series"] = c["series"]
            elif c.get("png"):
                entry["data_uri"] = ("data:image/png;base64,"
                                     + base64.b64encode(c["png"]).decode())
            charts.append(entry)
        j["charts"] = charts
        out.append(j)
    return out


_HTML_CSS = """
:root { --primary:#4F6BF6; --text:#1A2233; --muted:#6B7280; --line:#E5E7EB; --tint:#F5F7FE; }
* { box-sizing: border-box; margin: 0; }
body { font-family: 'Segoe UI', -apple-system, Helvetica, Arial, sans-serif;
       color: var(--text); background: #EDF0F7; padding: 24px; }
.page { max-width: 900px; margin: 0 auto; background: #fff; border-radius: 10px;
        box-shadow: 0 2px 14px rgba(16,24,40,.08); overflow: hidden; }
.cover { background: linear-gradient(135deg, #26324f 0%, #4F6BF6 120%); color: #fff; padding: 34px 40px; }
.cover .brand { display:flex; align-items:center; gap:12px; margin-bottom: 22px; }
.cover .brand img { height: 36px; border-radius: 6px; background:#fff; padding:3px; }
.cover .brand span { font-size: 14px; letter-spacing: .16em; text-transform: uppercase; opacity:.85; }
.cover h1 { font-size: 27px; font-weight: 700; margin-bottom: 8px; }
.cover .meta { font-size: 13px; opacity: .85; }
.content { padding: 8px 40px 36px; }
.section { margin-top: 30px; page-break-inside: avoid; }
.section > h2 { font-size: 17px; padding-left: 10px; border-left: 4px solid var(--primary);
                margin-bottom: 4px; }
.section > p.desc { color: var(--muted); font-size: 12.5px; margin: 2px 0 12px 14px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 10px; margin: 14px 0; }
.kpi { background: var(--tint); border-radius: 8px; padding: 12px 14px; border-top: 3px solid var(--primary); }
.kpi .v { font-size: 21px; font-weight: 700; }
.kpi .l { font-size: 11px; color: var(--muted); margin-top: 3px; text-transform: uppercase;
          letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 10px 0 4px; }
caption { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 6px 0; }
th { background: var(--primary); color: #fff; text-align: left; padding: 7px 10px; font-size: 11.5px; }
td { padding: 6px 10px; border-bottom: 1px solid var(--line); }
tr:nth-child(even) td { background: #FAFBFF; }
.chart { text-align: center; margin: 12px 0; }
.chart img { max-width: 100%; border: 1px solid var(--line); border-radius: 8px; }
.chart .cap { font-size: 11.5px; color: var(--muted); margin-top: 4px; }
.note { background: #FFFBEB; border: 1px solid #FDE68A; color: #92400E; font-size: 12.5px;
        border-radius: 8px; padding: 9px 12px; margin: 8px 0; }
.footer { border-top: 1px solid var(--line); color: var(--muted); font-size: 11.5px;
          padding: 16px 40px; display: flex; justify-content: space-between; }
@media print {
  body { background: #fff; padding: 0; }
  .page { box-shadow: none; border-radius: 0; max-width: none; }
  .section { page-break-inside: avoid; }
  .cover { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  th, .kpi { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


def _esc(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_html(meta: dict, sections: list[dict]) -> str:
    """Self-contained branded HTML document (charts inlined as data URIs)."""
    logo = meta.get("logo_data_uri")
    brand_img = f'<img src="{logo}" alt="logo">' if logo else ""
    parts: list[str] = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(meta.get('title') or 'Report')}</title>
<style>{_HTML_CSS}</style></head><body><div class="page">
<div class="cover">
  <div class="brand">{brand_img}<span>{_esc(meta.get('company_name') or 'ZenPlus')}</span></div>
  <h1>{_esc(meta.get('title') or 'Report')}</h1>
  <div class="meta">{_esc(meta.get('period_label') or '')} &nbsp;·&nbsp;
    Generated {_esc(meta.get('generated_label') or '')}</div>
</div>
<div class="content">"""]

    for s in sections:
        parts.append(f'<div class="section"><h2>{_esc(s["title"])}</h2>')
        if s.get("description"):
            parts.append(f'<p class="desc">{_esc(s["description"])}</p>')
        kpis = s.get("kpis") or []
        if kpis:
            parts.append('<div class="kpis">')
            for k in kpis:
                color = ACCENT_HEX.get(k.get("accent") or "primary", ACCENT_HEX["primary"])
                parts.append(
                    f'<div class="kpi" style="border-top-color:{color}">'
                    f'<div class="v" style="color:{color}">{_esc(k.get("value"))}</div>'
                    f'<div class="l">{_esc(k.get("label"))}</div></div>')
            parts.append("</div>")
        for c in s.get("charts") or []:
            uri = c.get("data_uri") or (
                "data:image/png;base64," + base64.b64encode(c["png"]).decode())
            cap = f'<div class="cap">{_esc(c["title"])}</div>' if c.get("title") else ""
            parts.append(f'<div class="chart"><img src="{uri}" alt="chart">{cap}</div>')
        for t in s.get("tables") or []:
            cap = f"<caption>{_esc(t['title'])}</caption>" if t.get("title") else ""
            head = "".join(f"<th>{_esc(h)}</th>" for h in t.get("headers") or [])
            body = "".join(
                "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
                for row in (t.get("rows") or [])[:60])
            if not t.get("rows"):
                body = f'<tr><td colspan="{max(len(t.get("headers") or []), 1)}" ' \
                       f'style="color:#6B7280">No data for this window</td></tr>'
            parts.append(f"<table>{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        for n in s.get("notes") or []:
            parts.append(f'<div class="note">{_esc(n)}</div>')
        parts.append("</div>")

    parts.append(f"""</div>
<div class="footer"><span>{_esc(meta.get('company_name') or 'ZenPlus')} · {_esc(meta.get('title') or '')}</span>
<span>Powered by ZenPlus</span></div>
</div></body></html>""")
    return "".join(parts)


_COL_GAP = 8


def _pdf_section_body(pdf: ZenPlusReport, s: dict, half: bool = False) -> None:
    """Render a section's KPIs, charts, tables and notes with the current
    margins (full page or a half-width column)."""
    kpis = s.get("kpis") or []
    charts = s.get("charts") or []
    tables = s.get("tables") or []
    notes = s.get("notes") or []
    if kpis:
        pdf.kpi_row(kpis, per_row=2 if half else None)
    for c in charts:
        if c.get("png"):
            pdf.add_chart(c["png"], title=c.get("title") or "")
    for t in tables:
        rows = [["" if c is None else str(c) for c in row] for row in (t.get("rows") or [])]
        if not rows and notes:
            continue  # the section's note already explains the absence of data
        if t.get("title"):
            pdf.sub_heading(t["title"])
        headers = t.get("headers") or []
        pdf.data_table(headers, rows, col_styles=t.get("styles"),
                       max_rows=30 if half else 100)
    for n in notes:
        pdf.note(n, kind="warning" if "could not be generated" in str(n) else "info")
    if not kpis and not charts and not tables and not notes:
        pdf.empty_state()


def _half_heading(pdf: ZenPlusReport, s: dict, number: int) -> None:
    """Compact heading used when two sections sit side by side."""
    x = pdf.l_margin
    y = pdf.get_y()
    if s.get("category"):
        pdf._eyebrow(x, y, s["category"], COLOR_FAINT, size=5.8)
        y += 4.2
    pdf._sans("B", 8.8)
    pdf.set_text_color(*pdf.accent)
    ns = f"{number:02d}"
    pdf.set_xy(x, y + 1.1)
    pdf.cell(pdf.get_string_width(ns) + 1, 5, ns)
    tx = x + pdf.get_string_width(ns) + 2.8
    pdf._sans("B", 11)
    pdf.set_text_color(*COLOR_TEXT)
    pdf.set_xy(tx, y)
    pdf.cell(pdf.content_w - (tx - x), 6.6,
             pdf._fit(_safe(s["title"]), pdf.content_w - (tx - x)))
    y2 = y + 7.8
    pdf.set_fill_color(*pdf.accent)
    pdf.rect(x, y2, 9, 0.8, "F")
    pdf._hairline(x + 11, y2 + 0.4, x + pdf.content_w)
    pdf.set_y(y2 + 3)
    if s.get("description"):
        pdf._sans("", 7.4)
        pdf.set_text_color(*COLOR_MUTED)
        pdf.set_x(x)
        pdf.multi_cell(pdf.content_w, 3.9, _safe(s["description"]))
        pdf.set_y(pdf.get_y() + 1.4)


def _measure_half(s: dict, col_w: float) -> float | None:
    """Height of a section rendered at column width, or None if it cannot fit
    on a single page."""
    try:
        probe = ZenPlusReport(title="probe")
        probe._decor = False
        probe._outline_on = False
        probe.add_page()
        probe.begin_column(MARGIN_L, col_w)
        probe.set_y(MARGIN_T)
        _half_heading(probe, s, 99)
        _pdf_section_body(probe, s, half=True)
        h = probe.get_y() - MARGIN_T
        probe.end_column()
        if probe.page_no() > 1:
            return None
        return h
    except Exception:
        logger.debug("half-section measurement failed", exc_info=True)
        return None


def render_pdf(meta: dict, sections: list[dict]) -> bytes:
    """Enterprise PDF: branded cover, contents (long reports), numbered
    sections with side-by-side placement of compact half-width sections."""
    pdf = ZenPlusReport(
        title=meta.get("title") or "Report",
        company_name=meta.get("company_name") or "ZenPlus",
        logo_bytes=meta.get("logo_bytes"),
        period_label=meta.get("period_label") or "",
        subtitle=meta.get("description") or "",
        category=meta.get("category") or "",
        scope_label=meta.get("scope_label") or "",
    )
    pdf.cover()
    if len(sections) >= 5:
        pdf.toc(expected_entries=len(sections))
    else:
        pdf.add_page()

    i = 0
    while i < len(sections):
        s = sections[i]
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        if s.get("half") and nxt is not None and nxt.get("half"):
            col_w = (CONTENT_W - _COL_GAP) / 2
            h1 = _measure_half(s, col_w)
            h2 = _measure_half(nxt, col_w)
            page_capacity = pdf.page_break_trigger - MARGIN_T
            if h1 is not None and h2 is not None and max(h1, h2) <= page_capacity:
                if max(h1, h2) > pdf.page_break_trigger - pdf.get_y() - 3:
                    pdf.add_page()
                n1 = pdf._section_n = pdf._section_n + 1
                n2 = pdf._section_n = pdf._section_n + 1
                if pdf._outline_on:
                    try:
                        pdf.start_section(_safe(s["title"]), level=0)
                        pdf.start_section(_safe(nxt["title"]), level=0)
                    except Exception:
                        pass
                y0 = pdf.get_y() + 2
                x_left = MARGIN_L
                x_right = MARGIN_L + col_w + _COL_GAP
                ends = []
                for sec, num, x in ((s, n1, x_left), (nxt, n2, x_right)):
                    pdf.begin_column(x, col_w)
                    pdf.set_y(y0)
                    _half_heading(pdf, sec, num)
                    _pdf_section_body(pdf, sec, half=True)
                    ends.append(pdf.get_y())
                    pdf.end_column()
                # vertical divider between the columns
                divider_x = MARGIN_L + col_w + _COL_GAP / 2
                pdf.set_draw_color(*COLOR_HAIRLINE)
                pdf.set_line_width(0.2)
                pdf.line(divider_x, y0 + 1, divider_x, max(ends) - 2)
                pdf.set_y(max(ends) + 7)
                i += 2
                continue
        pdf.section_title(s["title"], description=s.get("description"),
                          category=s.get("category"))
        _pdf_section_body(pdf, s, half=False)
        pdf.ln(4)
        i += 1
    return bytes(pdf.output())


# ─── Top-level: resolve + render ────────────────────────────────────────────

async def resolve_report(db: AsyncSession, key: str,
                         custom_id: Optional[str] = None) -> tuple[str, str, list[str]]:
    """Return (title, description, section_ids) for a preset key or custom id."""
    if key == "custom":
        row = (await db.execute(text(
            "SELECT name, description, sections FROM custom_reports WHERE id = :id"),
            {"id": custom_id})).first()
        if not row:
            raise KeyError("custom report not found")
        return row[0], row[1] or "", list(row[2] or [])
    preset = REPORT_PRESETS.get(key)
    if not preset:
        raise KeyError(key)
    return preset["title"], preset["description"], preset["sections"]


async def build_report_meta(db: AsyncSession, title: str, frm: datetime, to: datetime,
                            description: str | None = None,
                            category: str | None = None,
                            scope_label: str | None = None) -> dict:
    company = await _fetch_company_info(db)
    logo_bytes = company.get("logo_bytes")
    return {
        "title": title,
        "company_name": company.get("company_name") or "ZenPlus",
        "logo_bytes": logo_bytes,
        "logo_data_uri": ("data:image/png;base64," + base64.b64encode(logo_bytes).decode())
                         if logo_bytes else None,
        "period_label": f"{frm.strftime('%Y-%m-%d %H:%M')} — {to.strftime('%Y-%m-%d %H:%M')} UTC",
        "generated_label": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "description": description or "",
        "category": category or "",
        "scope_label": scope_label or "",
    }


def email_summary_html(meta: dict, sections: list[dict], view_url: Optional[str],
                       attached: bool) -> str:
    """Compact 600px email card: headline KPIs + view-report button."""
    kpis: list[dict] = []
    for s in sections:
        kpis.extend(s.get("kpis") or [])
        if len(kpis) >= 6:
            break
    cells = "".join(
        f'<td style="padding:10px 12px;background:#F5F7FE;border-radius:8px;">'
        f'<div style="font-size:19px;font-weight:700;color:{ACCENT_HEX.get(k.get("accent") or "primary", "#4F6BF6")}">'
        f'{_esc(k.get("value"))}</div>'
        f'<div style="font-size:11px;color:#6B7280;text-transform:uppercase;letter-spacing:.04em">'
        f'{_esc(k.get("label"))}</div></td><td style="width:8px"></td>'
        for k in kpis[:6])
    button = (f'<a href="{_esc(view_url)}" style="display:inline-block;background:#4F6BF6;color:#fff;'
              f'padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:600;'
              f'font-size:14px">View full report</a>') if view_url else ""
    note = ('<p style="font-size:12px;color:#6B7280;margin-top:14px">The full report is attached '
            'to this email.</p>' if attached else "")
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#EDF0F7;padding:24px;
font-family:'Segoe UI',Helvetica,Arial,sans-serif">
<table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0"
 style="background:#fff;border-radius:10px;overflow:hidden">
<tr><td style="background:linear-gradient(135deg,#26324f,#4F6BF6);padding:22px 28px">
  <div style="color:#fff;font-size:12px;letter-spacing:.14em;text-transform:uppercase;opacity:.85">
    {_esc(meta.get('company_name') or 'ZenPlus')}</div>
  <div style="color:#fff;font-size:21px;font-weight:700;margin-top:4px">{_esc(meta.get('title'))}</div>
  <div style="color:#fff;font-size:12px;opacity:.85;margin-top:4px">{_esc(meta.get('period_label') or '')}</div>
</td></tr>
<tr><td style="padding:22px 28px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>
  <div style="margin-top:20px">{button}</div>{note}
</td></tr>
<tr><td style="padding:14px 28px;border-top:1px solid #E5E7EB;color:#9CA3AF;font-size:11px">
  Generated {_esc(meta.get('generated_label') or '')} · Powered by ZenPlus</td></tr>
</table></body></html>"""


def email_summary_text(meta: dict, sections: list[dict], view_url: Optional[str]) -> str:
    lines = [meta.get("title") or "Report", meta.get("period_label") or "", ""]
    for s in sections:
        for k in s.get("kpis") or []:
            lines.append(f"- {k.get('label')}: {k.get('value')}")
    if view_url:
        lines += ["", f"View the full report: {view_url}"]
    return "\n".join(lines)
