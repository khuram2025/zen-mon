"""Server performance and capacity sections (agent-monitored servers).

Data sources and the rules every section here follows:

- CPU and memory utilization come from the 5-minute rollups
  (host_cpu_metrics_5m / host_memory_metrics_5m, kept 90 days by default).
  A rollup holds several rows per bucket (one per agent insert), so averages
  are weighted by ``sample_count`` and percentiles are taken over each
  server's 5-minute averages.
- Swap and committed memory, disk I/O and processes exist only in raw samples
  (host_memory_metrics / host_disk_io_metrics 30 days, host_process_metrics
  14 days); filesystem growth uses host_filesystem_metrics_5m (365 days).
- host_network_metrics_5m.avg_*_bps hold BYTES per second (the materialized
  view reads rx_bytes_ps), so throughput is multiplied by 8; sum_*_errors add
  up per-second error rates, so errors are reported as an average rate.
- Disabled servers are excluded. A server without samples is "not measured"
  (—), never 0 %. Thresholds are the server health defaults
  (server_health_service), the capacity filter is filesystem_monitoring's.

Every section reads all enabled servers (scope "fleet") and needs
servers.view.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.services.filesystem_monitoring import pg_capacity_filter
from app.services.host_metric_detail import days_until_full
from app.services.report_sections import (
    SectionCtx,
    _fmt_bps,
    _fmt_bytes,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template
from app.services.server_health_service import (
    CPU_CRIT,
    CPU_WARN,
    DISK_CRIT,
    DISK_WARN,
    MEM_CRIT,
    MEM_WARN,
)

REQUIRES = ("servers.view",)
ROLLUP_DAYS = 90        # host_cpu/memory/network_metrics_5m default TTL
FS_ROLLUP_DAYS = 365    # host_filesystem_metrics_5m default TTL
RAW_DAYS = 30           # host_memory_metrics / host_disk_io_metrics default TTL
PROCESS_DAYS = 14       # host_process_metrics default TTL
STALE_INVENTORY = timedelta(days=7)
MAX_DISK_ROWS = 5000    # safety cap on (server, disk) groups pulled into Python
MIN_FIT_POINTS = 10     # least-squares growth needs this many rollup points …
MIN_FIT_SPAN_S = 86400  # … spread over at least a day (as on the server page)
NO_SERVERS = "No enabled agent-monitored servers."

METRICS: dict[str, dict[str, Any]] = {
    "cpu": {"label": "CPU", "noun": "CPU", "table": "zenplus.host_cpu_metrics_5m",
            "avg": "avg_total_pct", "max": "max_total_pct",
            "warn": CPU_WARN, "crit": CPU_CRIT, "color": "primary"},
    "memory": {"label": "Memory", "noun": "memory", "table": "zenplus.host_memory_metrics_5m",
               "avg": "avg_used_pct", "max": "max_used_pct",
               "warn": MEM_WARN, "crit": MEM_CRIT, "color": "info"},
}
METRIC_CHOICES = [["cpu", "CPU"], ["memory", "Memory"]]
UTIL_SORTS = {"avg": ("avg", "Average"), "p95": ("p95", "95th Percentile"),
              "peak": ("peak", "Peak")}

_WEIGHTING_NOTE = ("Averages are weighted by sample count from the 5-minute rollups; P95 is the "
                   "95th percentile of each server's 5-minute averages and Peak is the highest "
                   "single sample.")


# ─── helpers ────────────────────────────────────────────────────────────────

def _utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ch_ts(dt: datetime) -> str:
    return _utc_naive(dt).strftime("%Y-%m-%d %H:%M:%S")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _num(v: Any) -> Optional[float]:
    """float, or None for NULL / NaN / ±inf (ClickHouse returns nan for an
    empty quantile or avg)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def _pts(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.2f} pts"


def _delta(now: Optional[float], before: Optional[float]) -> str:
    if now is None:
        return "Not measured"
    if before is None:
        return "No previous-period data"
    d = now - before
    if abs(d) < 0.005:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.2f} pts vs previous period"


def _duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "—"
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h} h {mm:02d} min" if mm else f"{h} h"
    d, hh = divmod(h, 24)
    return f"{d} d {hh} h" if hh else f"{d} d"


def _ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.2f} ms" if v < 10 else f"{v:,.1f} ms"


def _bytes(v: Optional[float]) -> str:
    return "—" if v is None else _fmt_bytes(v)


def _bytes_rate(v: Optional[float]) -> str:
    return "—" if v is None else f"{_fmt_bytes(v)}/s"


def _bits(bytes_ps: Optional[float]) -> str:
    return "—" if bytes_ps is None else _fmt_bps(bytes_ps * 8)


def _growth(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if abs(v) < 1:
        return "0 B/day"
    return f"{'+' if v > 0 else '-'}{_fmt_bytes(abs(v))}/day"


def _count(n: int) -> str:
    return f"{n:,}"


def _level(v: Optional[float], warn: float, crit: float) -> str:
    if v is None:
        return "unknown"
    return "critical" if v >= crit else "warning" if v >= warn else "ok"


def _accent(level: str) -> str:
    return {"critical": "danger", "warning": "warning", "ok": "success"}.get(level, "neutral")


def _top_title(shown: int, total: int, noun: str) -> str:
    return f"Top {shown} of {total:,} {noun}" if total > shown else f"{noun[:1].upper()}{noun[1:]}"


def _to_dt(v: Any) -> Optional[datetime]:
    """A ClickHouse bucket (epoch seconds, datetime or date) as aware UTC."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(v), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _step_seconds(interval: str) -> int:
    n, unit = interval.split()
    return int(n) * {"MINUTE": 60, "HOUR": 3600, "DAY": 86400}.get(unit.upper(), 60)


def _interval_label(interval: str) -> str:
    """'2 HOUR' -> '2-hour'."""
    n, unit = interval.split()
    return f"{n}-{unit.lower()}"


def _time_points(ctx: SectionCtx, pts: list[tuple[datetime, Optional[float]]],
                 step: int) -> list[dict]:
    """Chart points with a null inserted where buckets are missing (gaps stay
    gaps, never zero)."""
    out: list[dict] = []
    prev: Optional[datetime] = None
    for ts, v in pts:
        if prev is not None and (ts - prev).total_seconds() > step * 1.5:
            out.append({"t": ctx.iso(prev + timedelta(seconds=step)), "v": None})
        out.append({"t": ctx.iso(ts), "v": None if v is None else round(v, 2)})
        prev = ts
    return out


def _reach_note(ctx: SectionCtx, frm: datetime, days: int, what: str) -> Optional[str]:
    """Retention disclosure for an arbitrary window start (ctx.window_note
    covers the report window itself)."""
    reach = _now() - timedelta(days=days)
    if _utc_naive(frm) < reach:
        return (f"{what} is kept for {days} days; the comparison period only has data from "
                f"{ctx.fmt_date(reach)} onwards.")
    return None


async def _servers(ctx: SectionCtx) -> dict[str, dict]:
    """Enabled servers {id: {name, cores, mem_total}} (cached for the render)."""
    key = "servers_performance:servers"
    cached = ctx._cache.get(key)
    if cached is None:
        rows = (await ctx.db.execute(text(
            "SELECT s.id::text, COALESCE(NULLIF(btrim(s.display_name), ''), "
            "NULLIF(btrim(s.hostname), ''), s.id::text), s.cpu_cores, s.memory_total_bytes "
            "FROM servers s WHERE s.status <> 'disabled' ORDER BY 2"))).fetchall()
        cached = {str(r[0]): {"name": r[1] or str(r[0])[:8], "cores": r[2], "mem_total": r[3]}
                  for r in rows}
        ctx._cache[key] = cached
    return cached


def _name(servers: dict[str, dict], sid: Any) -> str:
    s = servers.get(str(sid))
    return s["name"] if s else str(sid)[:8]


def _params(ctx: SectionCtx, servers: dict, frm: Optional[datetime] = None,
            to: Optional[datetime] = None, **extra: Any) -> dict:
    p: dict[str, Any] = {"sids": tuple(servers), "f": _ch_ts(frm or ctx.frm),
                         "t": _ch_ts(to or ctx.to)}
    p.setdefault("lo", p["f"])
    p.update(extra)
    return p


def _bucket_sql(m: dict) -> str:
    """Per server and 5-minute bucket: weighted sum/count, the bucket average
    and the highest sample, over [lo, t) for the enabled servers."""
    return (
        "SELECT server_id, timestamp AS b, "
        f"sum({m['avg']} * sample_count) AS s, sum(sample_count) AS n, s / n AS v, "
        f"max({m['max']}) AS pk "
        f"FROM {m['table']} "
        "WHERE server_id IN %(sids)s AND timestamp >= %(lo)s AND timestamp < %(t)s "
        "AND sample_count > 0 "
        "GROUP BY server_id, b")


def _empty(sid: str, title: str, description: str, note: str) -> dict:
    return _section(sid, title, description=description, notes=[note])


# ─── Biggest CPU increases ──────────────────────────────────────────────────

@register_section(
    "server_cpu_change", title="Biggest Server CPU Increases", category="Servers",
    description="Servers whose average CPU rose the most against the previous period of equal "
                "length, with 95th percentiles for both periods.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(10, 100),
             {"key": "min_change", "label": "Minimum increase (points)", "type": "number",
              "default": 1.0, "min": 0, "max": 100,
              "help": "List servers whose average CPU rose by at least this many percentage "
                      "points."}],
)
async def server_cpu_change(ctx: SectionCtx) -> dict:
    sid, title = "server_cpu_change", "Biggest Server CPU Increases"
    top_n = int(ctx.opt("top_n"))
    min_change = float(ctx.opt("min_change"))
    desc = (f"Top {top_n} servers by increase in average CPU against the previous period of "
            f"equal length (increases of at least {min_change:.2f} pts).")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    pf, pt = ctx.prior_window()
    rows = await ctx.ch_rows(
        "SELECT server_id, "
        "sumIf(s, b >= %(f)s), sumIf(n, b >= %(f)s), sumIf(s, b < %(f)s), sumIf(n, b < %(f)s), "
        "quantileIf(0.95)(v, b >= %(f)s), quantileIf(0.95)(v, b < %(f)s), maxIf(pk, b >= %(f)s) "
        f"FROM ({_bucket_sql(METRICS['cpu'])}) GROUP BY server_id",
        _params(ctx, servers, lo=_ch_ts(pf)))

    stats: list[dict] = []
    fleet = [0.0, 0.0, 0.0, 0.0]  # s_now, n_now, s_before, n_before
    for r in rows:
        s_now, n_now, s_b, n_b = (_num(x) or 0.0 for x in r[1:5])
        fleet[0] += s_now
        fleet[1] += n_now
        fleet[2] += s_b
        fleet[3] += n_b
        avg_now = s_now / n_now if n_now > 0 else None
        avg_b = s_b / n_b if n_b > 0 else None
        stats.append({
            "name": _name(servers, r[0]), "avg_now": avg_now, "avg_b": avg_b,
            "p95_now": _num(r[5]) if n_now > 0 else None,
            "p95_b": _num(r[6]) if n_b > 0 else None,
            "peak": _num(r[7]) if n_now > 0 else None,
            "change": (avg_now - avg_b) if avg_now is not None and avg_b is not None else None,
        })
    measured = [x for x in stats if x["avg_now"] is not None]
    risers = sorted((x for x in measured if x["change"] is not None and x["change"] >= min_change),
                    key=lambda x: (-x["change"], -x["avg_now"], x["name"]))
    no_baseline = sorted((x for x in measured if x["avg_b"] is None),
                         key=lambda x: (-x["avg_now"], x["name"]))
    candidates = risers + no_baseline
    listed = candidates[:top_n]
    fleet_now = fleet[0] / fleet[1] if fleet[1] > 0 else None
    fleet_before = fleet[2] / fleet[3] if fleet[3] > 0 else None

    notes = [f"Previous period: {ctx.fmt_dt(pf)} – {ctx.fmt_dt(pt)} ({ctx.tz_label()}). "
             "Change is in percentage points of average CPU; the fleet average includes every "
             "server that reported in each period. " + _WEIGHTING_NOTE]
    if not measured:
        notes.insert(0, "No server CPU samples in this period.")
    elif not candidates:
        notes.insert(0, f"No server's average CPU rose by {min_change:.2f} pts or more against "
                        "the previous period.")
    if no_baseline:
        notes.append(f"{len(no_baseline)} server(s) have no CPU samples in the previous period, so "
                     "their change is not measured (—); they are listed after the ranked servers.")
    missing = len(servers) - len(measured)
    if missing > 0:
        notes.append(f"{missing} enabled server(s) sent no CPU samples in this period "
                     "(not measured).")
    for n in (ctx.window_note(ROLLUP_DAYS, "5-minute server CPU data"),
              _reach_note(ctx, pf, ROLLUP_DAYS, "5-minute server CPU data")):
        if n and n not in notes:
            notes.append(n)

    top = risers[0] if risers else None
    kpis = [
        {"label": "Fleet average CPU", "value": _pct(fleet_now), "accent": "primary",
         "subtitle": _delta(fleet_now, fleet_before)},
        {"label": "Servers with higher CPU", "value": _count(len(risers)),
         "accent": "warning" if risers else "success",
         "subtitle": f"Rose by at least {min_change:.2f} pts"},
        {"label": "Biggest increase", "value": _pts(top["change"]) if top else "—",
         "accent": ("danger" if top and (top["avg_now"] or 0) >= CPU_WARN else
                    "warning" if top else "neutral"),
         "subtitle": top["name"] if top else "No increase"},
        {"label": "Servers measured", "value": f"{len(measured):,} of {len(servers):,}",
         "accent": "neutral",
         "subtitle": (f"{len(no_baseline)} without previous-period data" if no_baseline
                      else "All have a previous-period baseline" if measured else "Not measured")},
    ]
    charts = [{"title": "Increase in average CPU (percentage points)",
               "series": {"kind": "rank", "unit": "pts", "color": "warning",
                          "points": [{"label": x["name"], "value": round(x["change"], 2)}
                                     for x in risers[:top_n]]}}] if risers else []
    tables = [{
        "title": _top_title(len(listed), len(candidates), "servers by CPU increase"),
        "headers": ["Server", "Avg CPU", "Previous avg", "Change", "P95 CPU", "Previous P95",
                    "Peak CPU"],
        "styles": ["text", "pct-bar", "pct", "num", "pct", "pct", "pct"],
        "rows": [[x["name"], _pct(x["avg_now"]), _pct(x["avg_b"]), _pts(x["change"]),
                  _pct(x["p95_now"]), _pct(x["p95_b"]), _pct(x["peak"])] for x in listed],
    }] if listed else []
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Top servers by CPU / memory ────────────────────────────────────────────

@register_section(
    "server_resource_utilization", title="Top Servers by CPU or Memory", category="Servers",
    description="Servers ranked by average, 95th-percentile or peak CPU or memory utilization "
                "over the period.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(10, 100),
        {"key": "metric", "label": "Metric", "type": "select", "default": "cpu",
         "choices": METRIC_CHOICES},
        {"key": "sort", "label": "Rank by", "type": "select", "default": "p95",
         "choices": [["avg", "Average"], ["p95", "95th percentile"], ["peak", "Peak"]]},
    ],
)
async def server_resource_utilization(ctx: SectionCtx) -> dict:
    sid = "server_resource_utilization"
    metric = ctx.opt("metric") if ctx.opt("metric") in METRICS else "cpu"
    sort = ctx.opt("sort") if ctx.opt("sort") in UTIL_SORTS else "p95"
    m = METRICS[metric]
    top_n = int(ctx.opt("top_n"))
    key, sort_label = UTIL_SORTS[sort]
    title = f"Top {top_n} Servers by {m['label']} ({sort_label})"
    desc = (f"Top {top_n} servers by {sort_label.lower()} {m['noun']} utilization over the "
            "period, with average, 95th percentile and peak.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    rows = await ctx.ch_rows(
        "SELECT server_id, sum(s), sum(n), quantile(0.95)(v), max(pk) "
        f"FROM ({_bucket_sql(m)}) GROUP BY server_id",
        _params(ctx, servers))
    stats: list[dict] = []
    fleet_s = fleet_n = 0.0
    for r in rows:
        s, n = _num(r[1]) or 0.0, _num(r[2]) or 0.0
        if n <= 0:
            continue
        fleet_s += s
        fleet_n += n
        info = servers.get(str(r[0])) or {}
        stats.append({"name": _name(servers, r[0]), "avg": s / n, "p95": _num(r[3]),
                      "peak": _num(r[4]), "cores": info.get("cores"),
                      "mem_total": info.get("mem_total")})
    stats.sort(key=lambda x: (-(x[key] if x[key] is not None else -1.0), x["name"]))
    listed = stats[:top_n]
    warn = [x for x in stats if x["p95"] is not None and x["p95"] >= m["warn"]]
    crit = [x for x in warn if x["p95"] >= m["crit"]]
    fleet = fleet_s / fleet_n if fleet_n > 0 else None

    notes = [_WEIGHTING_NOTE + f" Load is graded on P95 against the server health thresholds "
             f"(warning {m['warn']:g}%, critical {m['crit']:g}%)."]
    if not stats:
        notes.insert(0, f"No server {m['noun']} samples in this period.")
    missing = len(servers) - len(stats)
    if missing > 0:
        notes.append(f"{missing} enabled server(s) sent no {m['noun']} samples in this period "
                     "(not measured).")
    wn = ctx.window_note(ROLLUP_DAYS, f"5-minute server {m['noun']} data")
    if wn:
        notes.append(wn)

    top = listed[0] if listed else None
    kpis = [
        {"label": f"Fleet average {m['noun']}", "value": _pct(fleet), "accent": m["color"],
         "subtitle": "Weighted by samples"},
        {"label": f"Highest {sort_label.lower()}", "value": _pct(top[key]) if top else "—",
         "accent": _accent(_level(top[key], m["warn"], m["crit"])) if top else "neutral",
         "subtitle": top["name"] if top else "Not measured"},
        {"label": f"Servers at or above {m['warn']:g}% (P95)", "value": _count(len(warn)),
         "accent": "danger" if crit else "warning" if warn else "success",
         "subtitle": f"{len(crit)} at or above {m['crit']:g}%"},
        {"label": "Servers measured", "value": f"{len(stats):,} of {len(servers):,}",
         "accent": "neutral"},
    ]
    size_header = "Cores" if metric == "cpu" else "RAM"
    styles = ["text", "pct", "pct", "pct", "num", "status"]
    styles[1 + ("avg", "p95", "peak").index(key)] = "pct-bar"
    rows_out = []
    for x in listed:
        if metric == "cpu":
            size = f"{int(x['cores']):,}" if x["cores"] else "—"
        else:
            size = _bytes(float(x["mem_total"])) if x["mem_total"] else "—"
        rows_out.append([x["name"], _pct(x["avg"]), _pct(x["p95"]), _pct(x["peak"]), size,
                         _level(x["p95"], m["warn"], m["crit"])])
    charts = [{"title": f"{sort_label} {m['noun']} by server",
               "series": {"kind": "rank", "unit": "%", "color": m["color"],
                          "points": [{"label": x["name"], "value": round(x[key], 2)}
                                     for x in listed if x[key] is not None]}}] if listed else []
    tables = [{
        "title": _top_title(len(listed), len(stats),
                            f"servers by {sort_label.lower()} {m['noun']}"),
        "headers": ["Server", f"Avg {m['noun']}", f"P95 {m['noun']}", f"Peak {m['noun']}",
                    size_header, "Load"],
        "styles": styles, "rows": rows_out,
    }] if listed else []
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Fleet resource trend ───────────────────────────────────────────────────

@register_section(
    "server_resource_trend", title="Server Resource Trend", category="Servers",
    description="Fleet average and 95th-percentile CPU and memory utilization over time.",
    scope="fleet", requires=REQUIRES,
)
async def server_resource_trend(ctx: SectionCtx) -> dict:
    sid, title = "server_resource_trend", "Server Resource Trend"
    interval = ctx.bucket()
    desc = (f"Fleet average and 95th-percentile CPU and memory per {_interval_label(interval)} "
            "interval across enabled agent-monitored servers.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    bucket_expr, bparams = ctx.ch_bucket("b", interval)
    step = _step_seconds(interval)
    series: dict[str, dict] = {}
    for metric, m in METRICS.items():
        rows = await ctx.ch_rows(
            f"SELECT toUnixTimestamp(toDateTime({bucket_expr}, %(org_tz)s)) AS ts, "
            "sum(s), sum(n), quantile(0.95)(v), uniqExact(server_id) "
            f"FROM ({_bucket_sql(m)}) GROUP BY ts ORDER BY ts",
            _params(ctx, servers, **bparams))
        avg_pts: list[tuple[datetime, Optional[float]]] = []
        p95_pts: list[tuple[datetime, Optional[float]]] = []
        tot_s = tot_n = 0.0
        worst: Optional[tuple[float, datetime]] = None
        for r in rows:
            ts = _to_dt(r[0])
            s, n = _num(r[1]) or 0.0, _num(r[2]) or 0.0
            if ts is None or n <= 0:
                continue
            p95 = _num(r[3])
            tot_s += s
            tot_n += n
            avg_pts.append((ts, s / n))
            p95_pts.append((ts, p95))
            if p95 is not None and (worst is None or p95 > worst[0]):
                worst = (p95, ts)
        series[metric] = {"avg": avg_pts, "p95": p95_pts,
                          "fleet": tot_s / tot_n if tot_n > 0 else None, "worst": worst}

    reporting_rows = await ctx.ch_rows(
        "SELECT uniqExact(server_id) FROM zenplus.host_cpu_metrics_5m "
        "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
        "AND sample_count > 0", _params(ctx, servers))
    reporting = int(_num(reporting_rows[0][0]) or 0) if reporting_rows else 0

    if not any(series[k]["avg"] for k in series):
        return _empty(sid, title, desc, "No server CPU or memory samples in this period.")

    kpis = []
    for metric, m in METRICS.items():
        d = series[metric]
        kpis.append({"label": f"Fleet average {m['noun']}", "value": _pct(d["fleet"]),
                     "accent": m["color"], "subtitle": "Weighted by samples"})
        w = d["worst"]
        kpis.append({"label": f"Highest interval P95 {m['noun']}",
                     "value": _pct(w[0]) if w else "—",
                     "accent": _accent(_level(w[0], m["warn"], m["crit"])) if w else "neutral",
                     "subtitle": ctx.fmt_short(w[1]) if w else "Not measured"})
    kpis.append({"label": "Servers reporting", "value": f"{reporting:,} of {len(servers):,}",
                 "accent": "neutral"})

    charts = []
    for metric, m in METRICS.items():
        d = series[metric]
        if not d["avg"]:
            continue
        avg_points = _time_points(ctx, d["avg"], step)
        p95_points = _time_points(ctx, d["p95"], step)
        charts.append({"title": f"{m['label']} utilization — fleet average and 95th percentile (%)",
                       "series": {"kind": "line", "unit": "%", "color": m["color"],
                                  "y_domain": [0, 100], "points": avg_points,
                                  "series": [{"name": "Average", "points": avg_points},
                                             {"name": "95th percentile", "points": p95_points}]}})
    notes = [f"Each point is a {_interval_label(interval)} interval ({ctx.tz_label()}). The "
             "average is weighted by sample count; the 95th percentile is taken across every "
             "server's 5-minute averages in the interval. Intervals without samples are left as "
             "gaps."]
    missing = len(servers) - reporting
    if missing > 0:
        notes.append(f"{missing} enabled server(s) sent no CPU samples in this period "
                     "(not measured).")
    wn = ctx.window_note(ROLLUP_DAYS, "5-minute server CPU and memory data")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, notes=notes)


# ─── Sustained saturation ───────────────────────────────────────────────────

@register_section(
    "server_saturation_hotspots", title="Sustained Saturation Hotspots", category="Servers",
    description="Servers that spent the most time with CPU or memory at or above a threshold, "
                "and their longest unbroken run.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(10, 100),
        {"key": "threshold", "label": "Saturation threshold (%)", "type": "number",
         "default": CPU_WARN, "min": 50, "max": 100,
         "help": "A 5-minute interval counts when the server's average is at or above this."},
    ],
)
async def server_saturation_hotspots(ctx: SectionCtx) -> dict:
    sid = "server_saturation_hotspots"
    thr = float(ctx.opt("threshold"))
    top_n = int(ctx.opt("top_n"))
    title = f"Sustained Saturation Hotspots (≥{thr:g}%)"
    desc = (f"Top {top_n} servers by time spent with 5-minute average CPU or memory at or above "
            f"{thr:g}%, with the longest unbroken run.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    per: dict[str, dict] = {}
    measured: dict[str, set] = {"cpu": set(), "memory": set()}
    for metric, m in METRICS.items():
        rows = await ctx.ch_rows(
            "SELECT server_id, hot, measured, last_hot, "
            "if(hot = 0, 0, arrayMax(arrayDifference(arrayPushBack("
            "arrayFilter((i, d) -> d != 1, arrayEnumerate(idx), arrayDifference(idx)), "
            "length(idx) + 1)))) AS longest "
            "FROM (SELECT server_id, countIf(v >= %(thr)s) AS hot, count() AS measured, "
            "maxIf(b, v >= %(thr)s) AS last_hot, "
            "arraySort(groupArrayIf(intDiv(toUnixTimestamp(b), 300), v >= %(thr)s)) AS idx "
            f"FROM ({_bucket_sql(m)}) GROUP BY server_id)",
            _params(ctx, servers, thr=thr))
        for r in rows:
            key = str(r[0])
            if int(_num(r[2]) or 0) > 0:
                measured[metric].add(key)
            hot = int(_num(r[1]) or 0)
            if hot <= 0:
                continue
            entry = per.setdefault(key, {"sid": key, "name": _name(servers, key), "cpu": None,
                                         "memory": None, "cpu_run": None, "memory_run": None,
                                         "last": None})
            entry[metric] = hot * 5
            entry[f"{metric}_run"] = int(_num(r[4]) or 0) * 5
            last = _to_dt(r[3])
            if last is not None and (entry["last"] is None or last > entry["last"]):
                entry["last"] = last

    hot_rows = sorted(per.values(), key=lambda x: (-((x["cpu"] or 0) + (x["memory"] or 0)),
                                                    -max(x["cpu_run"] or 0, x["memory_run"] or 0),
                                                    x["name"]))
    listed = hot_rows[:top_n]

    def cell(entry: dict, metric: str, run: bool = False) -> str:
        v = entry[f"{metric}_run" if run else metric]
        if v is not None:
            return _duration(v)
        # Measured but never saturated is 0; no samples at all is not measured.
        return "0 min" if entry["sid"] in measured[metric] else "—"

    cpu_hot = [x for x in hot_rows if x["cpu"]]
    mem_hot = [x for x in hot_rows if x["memory"]]
    longest = max(((x["cpu_run"] or 0, x["name"], "CPU") for x in hot_rows),
                  default=None)
    longest_mem = max(((x["memory_run"] or 0, x["name"], "memory") for x in hot_rows),
                      default=None)
    best = max((v for v in (longest, longest_mem) if v), default=None)
    kpis = [
        {"label": f"Servers with CPU ≥{thr:g}%", "value": _count(len(cpu_hot)),
         "accent": "warning" if cpu_hot else "success",
         "subtitle": f"of {len(measured['cpu']):,} measured"},
        {"label": f"Servers with memory ≥{thr:g}%", "value": _count(len(mem_hot)),
         "accent": "warning" if mem_hot else "success",
         "subtitle": f"of {len(measured['memory']):,} measured"},
        {"label": "CPU-saturated time", "value": _duration(sum(x["cpu"] or 0 for x in hot_rows)),
         "accent": "warning" if cpu_hot else "neutral", "subtitle": "All servers combined"},
        {"label": "Longest run", "value": _duration(best[0]) if best and best[0] else "—",
         "accent": ("danger" if best and best[0] >= 60 else
                    "warning" if best and best[0] else "neutral"),
         "subtitle": f"{best[1]} · {best[2]}" if best and best[0] else "No saturation"},
    ]
    charts = [{"title": f"Time at or above {thr:g}% (minutes, CPU + memory)",
               "series": {"kind": "rank", "unit": "min", "color": "danger",
                          "points": [{"label": x["name"],
                                      "value": (x["cpu"] or 0) + (x["memory"] or 0)}
                                     for x in listed]}}] if listed else []
    tables = [{
        "title": _top_title(len(listed), len(hot_rows), "saturated servers"),
        "headers": ["Server", f"CPU ≥{thr:g}%", "Longest CPU run", f"Memory ≥{thr:g}%",
                    "Longest memory run", "Last saturated"],
        "styles": ["text", "num", "num", "num", "num", "text"],
        "rows": [[x["name"], cell(x, "cpu"), cell(x, "cpu", True), cell(x, "memory"),
                  cell(x, "memory", True), ctx.fmt_short(x["last"])] for x in listed],
    }] if listed else []
    notes = []
    if not (measured["cpu"] or measured["memory"]):
        notes.append("No server CPU or memory samples in this period.")
    elif not hot_rows:
        notes.append(f"No server averaged {thr:g}% CPU or memory over any 5-minute interval in "
                     "this period.")
    notes.append("A 5-minute interval counts when the server's sample-weighted average is at or "
                 "above the threshold, so brief spikes inside an interval do not; a run is "
                 "consecutive saturated intervals. '—' means the metric was not measured.")
    wn = ctx.window_note(ROLLUP_DAYS, "5-minute server CPU and memory data")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Memory and swap pressure ───────────────────────────────────────────────

MEMORY_SORTS = {"avg": "Average used", "peak": "Peak used", "swap": "Swap used",
                "commit": "Committed"}


@register_section(
    "server_memory_pressure", title="Memory and Swap Pressure", category="Servers",
    description="Average and peak memory use, swap use and committed memory against physical "
                "RAM per server (raw samples).",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(10, 100),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "avg",
         "choices": [[k, v] for k, v in MEMORY_SORTS.items()]},
    ],
)
async def server_memory_pressure(ctx: SectionCtx) -> dict:
    sid, title = "server_memory_pressure", "Memory and Swap Pressure"
    sort = ctx.opt("sort") if ctx.opt("sort") in MEMORY_SORTS else "avg"
    top_n = int(ctx.opt("top_n"))
    desc = (f"Top {top_n} servers by {MEMORY_SORTS[sort].lower()} memory: average and peak use, "
            "swap use and committed memory as a share of physical RAM.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    rows = await ctx.ch_rows(
        "SELECT server_id, avg(used_pct), max(used_pct), "
        "avgIf(swap_used_bytes / swap_total_bytes * 100, swap_total_bytes > 0), "
        "maxIf(swap_used_bytes / swap_total_bytes * 100, swap_total_bytes > 0), "
        "countIf(swap_total_bytes > 0), "
        "avgIf(committed_bytes / total_bytes * 100, total_bytes > 0 AND committed_bytes > 0), "
        "countIf(total_bytes > 0 AND committed_bytes > 0), "
        "argMax(total_bytes, timestamp), count() "
        "FROM zenplus.host_memory_metrics "
        "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
        "GROUP BY server_id",
        _params(ctx, servers))
    stats: list[dict] = []
    fleet_s = fleet_n = 0.0
    for r in rows:
        samples = int(_num(r[9]) or 0)
        avg_used = _num(r[1])
        if samples <= 0 or avg_used is None:
            continue
        fleet_s += avg_used * samples
        fleet_n += samples
        swap_n, commit_n = int(_num(r[5]) or 0), int(_num(r[7]) or 0)
        total = _num(r[8]) or _num((servers.get(str(r[0])) or {}).get("mem_total"))
        stats.append({
            "name": _name(servers, r[0]), "avg": avg_used, "peak": _num(r[2]),
            "swap": _num(r[3]) if swap_n else None, "swap_peak": _num(r[4]) if swap_n else None,
            "commit": _num(r[6]) if commit_n else None, "total": total or None,
        })
    for x in stats:
        x["status"] = (
            "critical" if x["avg"] >= MEM_CRIT
            or ((x["peak"] or 0) >= MEM_CRIT and (x["swap"] or 0) >= 50) else
            "warning" if (x["peak"] or 0) >= MEM_WARN or (x["swap"] or 0) >= 50
            or (x["commit"] or 0) > 100 else "ok")
    stats.sort(key=lambda x: (-(x[sort] if x[sort] is not None else -1.0), x["name"]))
    listed = stats[:top_n]
    peaking = [x for x in stats if (x["peak"] or 0) >= MEM_WARN]
    over = [x for x in stats if (x["commit"] or 0) > 100]
    swappers = sorted((x for x in stats if x["swap"] is not None), key=lambda x: -x["swap"])
    fleet = fleet_s / fleet_n if fleet_n > 0 else None

    kpis = [
        {"label": "Fleet average memory used", "value": _pct(fleet), "accent": "info",
         "subtitle": "Weighted by samples"},
        {"label": f"Servers peaking ≥{MEM_WARN:g}%", "value": _count(len(peaking)),
         "accent": "warning" if peaking else "success",
         "subtitle": f"of {len(stats):,} measured"},
        {"label": "Highest average swap use",
         "value": _pct(swappers[0]["swap"]) if swappers else "—",
         "accent": ("danger" if swappers and swappers[0]["swap"] >= 80 else
                    "warning" if swappers and swappers[0]["swap"] >= 50 else "neutral"),
         "subtitle": swappers[0]["name"] if swappers else "No swap measured"},
        {"label": "Over-committed servers", "value": _count(len(over)),
         "accent": "warning" if over else "success",
         "subtitle": "Committed memory above physical RAM"},
    ]
    charts = [{"title": f"{MEMORY_SORTS[sort]} memory by server (%)",
               "series": {"kind": "rank", "unit": "%", "color": "info",
                          "points": [{"label": x["name"], "value": round(x[sort], 2)}
                                     for x in listed if x[sort] is not None]}}] if listed else []
    tables = [{
        "title": _top_title(len(listed), len(stats), f"servers by {MEMORY_SORTS[sort].lower()}"),
        "headers": ["Server", "RAM", "Avg used", "Peak used", "Avg swap used", "Peak swap used",
                    "Committed (% of RAM)", "Pressure"],
        "styles": ["text", "num", "pct-bar", "pct", "pct", "pct", "pct", "status"],
        "rows": [[x["name"], _bytes(x["total"]), _pct(x["avg"]), _pct(x["peak"]),
                  _pct(x["swap"]), _pct(x["swap_peak"]), _pct(x["commit"]), x["status"]]
                 for x in listed],
    }] if listed else []
    notes = []
    if not stats:
        notes.append("No server memory samples in this period.")
    notes.append(f"Pressure is critical when average use is at least {MEM_CRIT:g}% (or peak use "
                 f"is at least {MEM_CRIT:g}% while swap is more than half used); warning when "
                 f"peak use reaches {MEM_WARN:g}%, swap is more than half used or committed "
                 "memory exceeds physical RAM. '—' means not measured (no swap configured or no "
                 "commit counter).")
    missing = len(servers) - len(stats)
    if missing > 0:
        notes.append(f"{missing} enabled server(s) sent no memory samples in this period "
                     "(not measured).")
    wn = ctx.window_note(RAW_DAYS, "Raw server memory data (swap and committed memory)")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Disk I/O ───────────────────────────────────────────────────────────────

DISK_SORTS = {
    "util": ("Utilization", "avg_util"),
    "latency": ("Latency", "greatest(ifNull(read_ms, 0), ifNull(write_ms, 0))"),
    "iops": ("IOPS", "iops"),
    "queue": ("Queue Length", "avg_queue"),
}


@register_section(
    "server_disk_io", title="Disk Latency and Utilization", category="Servers",
    description="Per-disk utilization, read and write latency, queue length, IOPS and "
                "throughput across servers (raw samples).",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 200),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "util",
         "choices": [[k, v[0]] for k, v in DISK_SORTS.items()]},
    ],
)
async def server_disk_io(ctx: SectionCtx) -> dict:
    sid = "server_disk_io"
    sort = ctx.opt("sort") if ctx.opt("sort") in DISK_SORTS else "util"
    sort_label, order = DISK_SORTS[sort]
    top_n = int(ctx.opt("top_n"))
    title = f"Top {top_n} Disks by {sort_label}"
    desc = (f"Top {top_n} server disks by {sort_label.lower()}: utilization, read and write "
            "latency, queue length, IOPS and throughput over the period.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    rows = await ctx.ch_rows(
        "SELECT server_id, device, avg(util_pct) AS avg_util, "
        "quantile(0.95)(util_pct) AS p95_util, "
        "if(sum(read_iops) > 0, sum(avg_read_ms * read_iops) / sum(read_iops), NULL) AS read_ms, "
        "if(sum(write_iops) > 0, sum(avg_write_ms * write_iops) / sum(write_iops), NULL) "
        "AS write_ms, "
        "avg(queue_length) AS avg_queue, avg(read_iops + write_iops) AS iops, "
        "avg(read_bytes_ps + write_bytes_ps) AS bytes_ps, count() AS samples "
        "FROM zenplus.host_disk_io_metrics "
        "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
        "AND NOT match(device, '^(loop|ram)[0-9]') "
        "GROUP BY server_id, device "
        f"ORDER BY {order} DESC, avg_util DESC "
        "LIMIT %(cap)s",
        _params(ctx, servers, cap=MAX_DISK_ROWS))
    disks = []
    for r in rows:
        if int(_num(r[9]) or 0) <= 0:
            continue
        read_ms, write_ms = _num(r[4]), _num(r[5])
        lat = [v for v in (read_ms, write_ms) if v is not None]
        disks.append({"name": _name(servers, r[0]), "device": str(r[1] or "—"),
                      "util": _num(r[2]), "p95": _num(r[3]), "read_ms": read_ms,
                      "write_ms": write_ms, "latency": max(lat) if lat else None,
                      "queue": _num(r[6]), "iops": _num(r[7]), "bps": _num(r[8])})
    listed = disks[:top_n]
    busiest = max((d for d in disks if d["util"] is not None), key=lambda d: d["util"],
                  default=None)
    slowest = max((d for d in disks if d["latency"] is not None), key=lambda d: d["latency"],
                  default=None)
    queued = max((d for d in disks if d["queue"] is not None), key=lambda d: d["queue"],
                 default=None)
    n_servers = len({d["name"] for d in disks})

    kpis = [
        {"label": "Disks measured", "value": _count(len(disks)), "accent": "neutral",
         "subtitle": f"on {n_servers:,} server(s)"},
        {"label": "Busiest disk (avg utilization)",
         "value": _pct(busiest["util"]) if busiest else "—",
         "accent": ("danger" if busiest and busiest["util"] >= DISK_CRIT else
                    "warning" if busiest and busiest["util"] >= DISK_WARN else "primary"),
         "subtitle": f"{busiest['name']} · {busiest['device']}" if busiest else "Not measured"},
        {"label": "Slowest disk (avg latency)",
         "value": _ms(slowest["latency"]) if slowest else "—",
         "accent": ("danger" if slowest and slowest["latency"] >= 50 else
                    "warning" if slowest and slowest["latency"] >= 20 else "primary"),
         "subtitle": f"{slowest['name']} · {slowest['device']}" if slowest else "Not measured"},
        {"label": "Longest average queue", "value": f"{queued['queue']:.2f}" if queued else "—",
         "accent": "neutral",
         "subtitle": f"{queued['name']} · {queued['device']}" if queued else "Not measured"},
    ]
    value_key = {"util": "util", "latency": "latency", "iops": "iops", "queue": "queue"}[sort]
    unit = {"util": "%", "latency": "ms", "iops": "IOPS", "queue": "requests"}[sort]
    charts = [{"title": f"{sort_label} by disk",
               "series": {"kind": "rank", "unit": unit, "color": "primary",
                          "points": [{"label": f"{d['name']} · {d['device']}",
                                      "value": round(d[value_key], 2)}
                                     for d in listed if d[value_key] is not None]}}
              ] if listed else []
    tables = [{
        "title": _top_title(len(listed), len(disks), f"disks by {sort_label.lower()}"),
        "headers": ["Server", "Disk", "Avg util", "P95 util", "Read latency", "Write latency",
                    "Avg queue", "IOPS", "Throughput"],
        "styles": ["text", "mono", "pct-bar", "pct", "num", "num", "num", "num", "num"],
        "rows": [[d["name"], d["device"], _pct(d["util"]), _pct(d["p95"]), _ms(d["read_ms"]),
                  _ms(d["write_ms"]), "—" if d["queue"] is None else f"{d['queue']:.2f}",
                  "—" if d["iops"] is None else f"{d['iops']:,.1f}", _bytes_rate(d["bps"])]
                 for d in listed],
    }] if listed else []
    notes = []
    if not disks:
        notes.append("No server disk I/O samples in this period.")
    notes.append("Latency is weighted by I/O operations ('—' when the disk did no reads or "
                 "writes); utilization is the share of time the disk was busy. Loop and RAM "
                 "devices are excluded; Linux agents may report partitions next to whole disks.")
    if len(rows) >= MAX_DISK_ROWS:
        notes.append(f"Only the first {MAX_DISK_ROWS:,} disks by {sort_label.lower()} were "
                     "evaluated.")
    wn = ctx.window_note(RAW_DAYS, "Raw server disk I/O data")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Network throughput ─────────────────────────────────────────────────────

NETWORK_SORTS = {"avg": "Average Throughput", "peak": "Peak Throughput",
                 "errors": "Interface Errors"}


@register_section(
    "server_network_throughput", title="Server Network Throughput", category="Servers",
    description="Average and peak receive plus transmit throughput per server, with interface "
                "error rates.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(10, 100),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "avg",
         "choices": [["avg", "Average throughput"], ["peak", "Peak throughput"],
                     ["errors", "Interface errors"]]},
    ],
)
async def server_network_throughput(ctx: SectionCtx) -> dict:
    sid = "server_network_throughput"
    sort = ctx.opt("sort") if ctx.opt("sort") in NETWORK_SORTS else "avg"
    top_n = int(ctx.opt("top_n"))
    title = f"Top {top_n} Servers by {NETWORK_SORTS[sort]}"
    desc = (f"Top {top_n} servers by {NETWORK_SORTS[sort].lower()}: average and peak receive and "
            "transmit across every non-loopback interface, and interface error rates.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    rows = await ctx.ch_rows(
        "SELECT server_id, avg(srv_rx), avg(srv_tx), max(srv_rx + srv_tx), avg(srv_err), "
        "countIf(srv_err > 0), max(ifs), count() "
        "FROM (SELECT server_id, b, sum(rx) AS srv_rx, sum(tx) AS srv_tx, sum(err) AS srv_err, "
        "uniqExact(if_name) AS ifs "
        "FROM (SELECT server_id, timestamp AS b, if_name, "
        "sum(avg_rx_bps * sample_count) / sum(sample_count) AS rx, "
        "sum(avg_tx_bps * sample_count) / sum(sample_count) AS tx, "
        "sum(sum_rx_errors + sum_tx_errors) / sum(sample_count) AS err "
        "FROM zenplus.host_network_metrics_5m "
        "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
        "AND sample_count > 0 AND lower(if_name) != 'lo' "
        "AND NOT startsWith(lower(if_name), 'loopback') "
        "GROUP BY server_id, b, if_name) "
        "GROUP BY server_id, b) "
        "GROUP BY server_id",
        _params(ctx, servers))
    stats = []
    for r in rows:
        if int(_num(r[7]) or 0) <= 0:
            continue
        rx, tx = _num(r[1]), _num(r[2])
        stats.append({"name": _name(servers, r[0]), "rx": rx, "tx": tx,
                      "avg": None if rx is None and tx is None else (rx or 0.0) + (tx or 0.0),
                      "peak": _num(r[3]), "errors": _num(r[4]),
                      "err_intervals": int(_num(r[5]) or 0), "ifs": int(_num(r[6]) or 0)})
    stats.sort(key=lambda x: (-(x[sort] if x[sort] is not None else -1.0), x["name"]))
    listed = stats[:top_n]
    total_avg = sum(x["avg"] or 0.0 for x in stats) if stats else None
    peak = max((x for x in stats if x["peak"] is not None), key=lambda x: x["peak"], default=None)
    erroring = [x for x in stats if (x["errors"] or 0) > 0]
    top = max((x for x in stats if x["avg"] is not None), key=lambda x: x["avg"], default=None)

    kpis = [
        {"label": "Fleet average throughput", "value": _bits(total_avg), "accent": "primary",
         "subtitle": f"{len(stats):,} of {len(servers):,} servers measured"},
        {"label": "Busiest server (average)", "value": _bits(top["avg"]) if top else "—",
         "accent": "info", "subtitle": top["name"] if top else "Not measured"},
        {"label": "Highest 5-minute peak", "value": _bits(peak["peak"]) if peak else "—",
         "accent": "info", "subtitle": peak["name"] if peak else "Not measured"},
        {"label": "Servers with interface errors", "value": _count(len(erroring)),
         "accent": "warning" if erroring else "success",
         "subtitle": "Any receive or transmit errors"},
    ]
    if sort == "errors":
        chart = {"title": "Average interface errors per second",
                 "series": {"kind": "rank", "unit": "errors/s", "color": "warning",
                            "points": [{"label": x["name"], "value": round(x["errors"], 3)}
                                       for x in listed if x["errors"]]}}
    else:
        chart = {"title": f"{NETWORK_SORTS[sort]} by server (bits per second)",
                 "series": {"kind": "rank", "unit": "bps", "color": "primary",
                            "points": [{"label": x["name"], "value": round(x[sort] * 8, 1)}
                                       for x in listed if x[sort] is not None]}}
    tables = [{
        "title": _top_title(len(listed), len(stats), f"servers by {NETWORK_SORTS[sort].lower()}"),
        "headers": ["Server", "Interfaces", "Avg receive", "Avg transmit", "Avg total",
                    "Peak (5-min)", "Errors/s (avg)", "Intervals with errors"],
        "styles": ["text", "num", "num", "num", "num", "num", "num", "num"],
        "rows": [[x["name"], _count(x["ifs"]) if x["ifs"] else "—", _bits(x["rx"]),
                  _bits(x["tx"]), _bits(x["avg"]), _bits(x["peak"]),
                  "—" if x["errors"] is None else f"{x['errors']:,.3f}",
                  _count(x["err_intervals"])]
                 for x in listed],
    }] if listed else []
    notes = []
    if not stats:
        notes.append("No server network samples in this period.")
    notes.append("Throughput adds up every non-loopback interface; the agent reports bytes per "
                 "second, shown here as bits per second. Peak is the busiest 5-minute average. "
                 "Errors are the average receive plus transmit error rate per second.")
    missing = len(servers) - len(stats)
    if missing > 0:
        notes.append(f"{missing} enabled server(s) sent no network samples in this period "
                     "(not measured).")
    wn = ctx.window_note(ROLLUP_DAYS, "5-minute server network data")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=[chart] if listed else [],
                    tables=tables, notes=notes)


# ─── Top processes ──────────────────────────────────────────────────────────

PROCESS_SORTS = {"cpu": ("CPU", "avg_cpu"), "memory": ("Memory", "peak_mem")}


@register_section(
    "server_top_processes", title="Top Processes", category="Servers",
    description="Processes using the most CPU or memory across servers, grouped by process name "
                "per server (raw samples, at most the last 14 days of the period).",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 200),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "cpu",
         "choices": [["cpu", "Average CPU"], ["memory", "Peak memory"]]},
    ],
)
async def server_top_processes(ctx: SectionCtx) -> dict:
    sid = "server_top_processes"
    sort = ctx.opt("sort") if ctx.opt("sort") in PROCESS_SORTS else "cpu"
    sort_label, order = PROCESS_SORTS[sort]
    top_n = int(ctx.opt("top_n"))
    title = f"Top {top_n} Processes by {sort_label}"
    desc = (f"Top {top_n} processes by {'average CPU' if sort == 'cpu' else 'peak memory'} across "
            "servers; instances of a process on one server are added together.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    frm = max(_utc_naive(ctx.frm), _utc_naive(ctx.to) - timedelta(days=PROCESS_DAYS))
    rows = await ctx.ch_rows(
        "SELECT server_id, process_name, avg_cpu, peak_cpu, avg_mem, peak_mem, instances, "
        "samples, count() OVER () AS total "
        "FROM (SELECT server_id, process_name, avg(cpu) AS avg_cpu, max(cpu) AS peak_cpu, "
        "avg(mem) AS avg_mem, max(mem) AS peak_mem, max(inst) AS instances, count() AS samples "
        "FROM (SELECT server_id, process_name, timestamp, sum(cpu_pct) AS cpu, "
        "sum(memory_bytes) AS mem, count() AS inst "
        "FROM zenplus.host_process_metrics "
        "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
        "AND pid > 0 AND notEmpty(process_name) "
        "GROUP BY server_id, process_name, timestamp) "
        "GROUP BY server_id, process_name) "
        f"ORDER BY {order} DESC, avg_cpu DESC "
        "LIMIT %(n)s",
        _params(ctx, servers, frm=frm, n=top_n))
    procs = [{"server": _name(servers, r[0]), "name": str(r[1] or "—"), "avg_cpu": _num(r[2]),
              "peak_cpu": _num(r[3]), "avg_mem": _num(r[4]), "peak_mem": _num(r[5]),
              "instances": int(_num(r[6]) or 0), "samples": int(_num(r[7]) or 0)}
             for r in rows]
    total = int(_num(rows[0][8]) or len(rows)) if rows else 0
    value_key = "avg_cpu" if sort == "cpu" else "peak_mem"
    top = procs[0] if procs else None
    kpis = [
        {"label": "Process groups observed", "value": _count(total), "accent": "neutral",
         "subtitle": "Process name per server"},
        {"label": "Highest average CPU" if sort == "cpu" else "Largest peak memory",
         "value": ((_pct(top["avg_cpu"]) if sort == "cpu" else _bytes(top["peak_mem"]))
                   if top else "—"),
         "accent": "primary" if sort == "cpu" else "info",
         "subtitle": f"{top['name']} · {top['server']}" if top else "Not measured"},
        {"label": "Servers in this list", "value": _count(len({p["server"] for p in procs})),
         "accent": "neutral"},
    ]
    charts = [{"title": ("Average CPU by process (%)" if sort == "cpu"
                         else "Peak memory by process (bytes)"),
               "series": {"kind": "rank", "unit": "%" if sort == "cpu" else "B",
                          "color": "primary" if sort == "cpu" else "info",
                          "points": [{"label": f"{p['name']} · {p['server']}",
                                      "value": round(p[value_key], 2)}
                                     for p in procs if p[value_key] is not None]}}] if procs else []
    tables = [{
        "title": _top_title(len(procs), total, "processes by " + (
            "average CPU" if sort == "cpu" else "peak memory")),
        "headers": ["Process", "Server", "Avg CPU", "Peak CPU", "Avg memory", "Peak memory",
                    "Max instances", "Samples"],
        "styles": ["mono", "text", "pct-bar" if sort == "cpu" else "pct", "pct", "num", "num",
                   "num", "num"],
        "rows": [[p["name"], p["server"], _pct(p["avg_cpu"]), _pct(p["peak_cpu"]),
                  _bytes(p["avg_mem"]), _bytes(p["peak_mem"]), _count(p["instances"]),
                  _count(p["samples"])] for p in procs],
    }] if procs else []
    notes = []
    if not procs:
        notes.append("No process samples in this period.")
    notes.append("CPU is the share of the whole server (all cores); averages cover only the "
                 "samples in which the process appeared.")
    if frm > _utc_naive(ctx.frm):
        notes.append(f"Process samples are kept for {PROCESS_DAYS} days, so this section covers "
                     f"the last {PROCESS_DAYS} days of the period ({ctx.fmt_dt(frm)} onwards).")
    wn = ctx.window_note(PROCESS_DAYS, "Process data")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Disk capacity forecast ─────────────────────────────────────────────────

@register_section(
    "capacity_filesystem_forecast", title="Disk Capacity Forecast", category="Capacity",
    description="Filesystem usage as of report generation, growth per day fitted over the "
                "period and the projected days until each volume is full.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(20, 200),
        {"key": "horizon", "label": "Forecast horizon (days)", "type": "int", "default": 30,
         "min": 1, "max": 365,
         "help": "Volumes projected to fill within this many days are flagged."},
    ],
)
async def capacity_filesystem_forecast(ctx: SectionCtx) -> dict:
    sid, title = "capacity_filesystem_forecast", "Disk Capacity Forecast"
    horizon = int(ctx.opt("horizon"))
    top_n = int(ctx.opt("top_n"))
    desc = (f"Top {top_n} volumes closest to full: usage as of report generation, growth per day "
            f"fitted over the period, and volumes projected to fill within {horizon} days.")
    servers = await _servers(ctx)
    if not servers:
        return _empty(sid, title, desc, NO_SERVERS)

    inv = (await ctx.db.execute(text(
        "SELECT f.server_id::text, f.mount, f.total_bytes, f.used_bytes, f.free_bytes, "
        "f.used_pct, f.updated_at "
        "FROM server_filesystem_inventory f JOIN servers s ON s.id = f.server_id "
        f"WHERE s.status <> 'disabled' AND {pg_capacity_filter('f.fs_type')} "
        "ORDER BY f.used_pct DESC NULLS LAST"))).fetchall()
    fits: dict[tuple[str, str], tuple[Optional[float], int, float]] = {}
    if inv:
        epoch = int(_utc_naive(ctx.frm).replace(tzinfo=timezone.utc).timestamp())
        rows = await ctx.ch_rows(
            "SELECT server_id, mount, "
            "tupleElement(simpleLinearRegression("
            "toFloat64(toUnixTimestamp(timestamp)) - %(epoch)s, avg_used_bytes), 1), "
            "count(), dateDiff('second', min(timestamp), max(timestamp)) "
            "FROM zenplus.host_filesystem_metrics_5m "
            "WHERE server_id IN %(sids)s AND timestamp >= %(f)s AND timestamp < %(t)s "
            "AND sample_count > 0 "
            "GROUP BY server_id, mount",
            _params(ctx, servers, epoch=epoch))
        for r in rows:
            fits[(str(r[0]), str(r[1]))] = (_num(r[2]), int(_num(r[3]) or 0), _num(r[4]) or 0.0)

    now = datetime.now(timezone.utc)
    vols: list[dict] = []
    stale = 0
    for r in inv:
        key = (str(r[0]), str(r[1]))
        if key[0] not in servers:
            continue
        updated = r[6]
        if isinstance(updated, datetime):
            upd = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
            if now - upd > STALE_INVENTORY:
                stale += 1
                continue
        total, used, free, used_pct = _num(r[2]), _num(r[3]), _num(r[4]), _num(r[5])
        if free is None and total is not None and used is not None:
            free = max(total - used, 0.0)
        if used_pct is None and total and used is not None:
            used_pct = used / total * 100
        slope, points, span = fits.get(key, (None, 0, 0.0))
        growth = (slope * 86400 if slope is not None and points >= MIN_FIT_POINTS
                  and span >= MIN_FIT_SPAN_S else None)
        days = (days_until_full(total, used, growth)
                if growth is not None and total is not None and used is not None else None)
        status = ("critical" if (used_pct or 0) >= DISK_CRIT or (days is not None and days <= 7)
                  else "warning" if (used_pct or 0) >= DISK_WARN
                  or (days is not None and days <= horizon)
                  else "ok" if used_pct is not None else "unknown")
        vols.append({"server": _name(servers, key[0]), "mount": key[1], "total": total,
                     "free": free, "pct": used_pct, "growth": growth, "days": days,
                     "status": status})
    vols.sort(key=lambda v: (v["days"] is None, v["days"] if v["days"] is not None else 0.0,
                             -(v["pct"] or 0.0), v["server"], v["mount"]))
    listed = vols[:top_n]
    soon = [v for v in vols if v["days"] is not None and v["days"] <= horizon]
    full_ish = [v for v in vols if (v["pct"] or 0) >= DISK_WARN]
    critical = [v for v in full_ish if (v["pct"] or 0) >= DISK_CRIT]
    fastest = max((v for v in vols if v["growth"] is not None and v["growth"] > 0),
                  key=lambda v: v["growth"], default=None)
    no_trend = sum(1 for v in vols if v["growth"] is None)

    def days_cell(v: dict) -> str:
        d = v["days"]
        if d is None:
            return "—"
        if d < 1:
            return "< 1"
        return "> 3,650" if d > 3650 else f"{d:,.0f}"

    def full_by(v: dict) -> str:
        d = v["days"]
        if d is None or d > 3650:
            return "—"
        return ctx.fmt_date(now + timedelta(days=d))

    kpis = [
        {"label": f"Full within {horizon} days", "value": _count(len(soon)),
         "accent": "danger" if soon else "success", "subtitle": "At the current growth rate"},
        {"label": f"Volumes ≥{DISK_WARN:g}% used", "value": _count(len(full_ish)),
         "accent": "danger" if critical else "warning" if full_ish else "success",
         "subtitle": f"{len(critical)} at or above {DISK_CRIT:g}%"},
        {"label": "Fastest growth", "value": _growth(fastest["growth"]) if fastest else "—",
         "accent": "info",
         "subtitle": f"{fastest['server']} · {fastest['mount']}" if fastest
         else "No volume is growing"},
        {"label": "Volumes tracked", "value": _count(len(vols)), "accent": "neutral",
         "subtitle": (f"{no_trend} without enough history for a trend" if no_trend
                      else f"on {len({v['server'] for v in vols}):,} server(s)")},
    ]
    charts = [{"title": "Used capacity of the most urgent volumes (%)",
               "series": {"kind": "rank", "unit": "%", "color": "warning",
                          "points": [{"label": f"{v['server']} · {v['mount']}",
                                      "value": round(v["pct"], 2)}
                                     for v in listed if v["pct"] is not None]}}] if listed else []
    tables = [{
        "title": _top_title(len(listed), len(vols), "volumes by time to full"),
        "headers": ["Server", "Mount", "Used", "Size", "Free", "Growth / day", "Days to full",
                    "Full by", "Status"],
        "styles": ["text", "mono", "pct-bar", "num", "num", "num", "num", "text", "status"],
        "rows": [[v["server"], v["mount"], _pct(v["pct"]), _bytes(v["total"]), _bytes(v["free"]),
                  _growth(v["growth"]), days_cell(v), full_by(v), v["status"]] for v in listed],
    }] if listed else []
    notes = []
    if not vols:
        notes.append("No filesystem inventory for enabled servers.")
    notes.append("Usage is the latest agent inventory; growth is a least-squares fit of used space "
                 "over the reporting period from 5-minute rollups and needs at least a day of "
                 "history. Shrinking or flat volumes have no forecast (—). Optical media, ISO and "
                 "virtual filesystems (udf, iso9660, tmpfs, …) are excluded.")
    if stale:
        notes.append(f"{stale} volume(s) not reported by their agent for more than "
                     f"{STALE_INVENTORY.days} days were left out.")
    wn = ctx.window_note(FS_ROLLUP_DAYS, "5-minute filesystem history")
    if wn:
        notes.append(wn)
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "server_cpu_increases", "Server CPU – Biggest Increases",
    "Which servers are working harder than before: the largest rises in average CPU against the "
    "previous period, the fleet CPU and memory trend, sustained saturation and the processes "
    "using the most CPU.",
    "servers",
    [S("server_cpu_change", "Biggest Server CPU Increases", top_n=25),
     S("server_resource_trend"),
     S("server_saturation_hotspots", None, top_n=10),
     S("server_top_processes", "Top 15 Processes by CPU", sort="cpu", top_n=15)],
    audience=("Systems engineer", "IT manager"), tags=("cpu", "change", "servers", "trend"),
    default_period="last_7d", frequency="weekly", icon="TrendingUp", featured=True,
)

template(
    "server_top_cpu", "Top Servers by CPU",
    "Servers ranked by 95th-percentile CPU with average and peak load, time spent saturated and "
    "the processes behind it.",
    "servers",
    [S("server_resource_utilization", "Top 25 Servers by CPU (95th Percentile)", metric="cpu",
       sort="p95", top_n=25),
     S("server_saturation_hotspots", None, top_n=10),
     S("server_top_processes", "Top 15 Processes by CPU", sort="cpu", top_n=15)],
    audience=("Systems engineer", "NOC operator"), tags=("cpu", "top n", "servers"),
    default_period="last_7d", frequency="weekly", icon="Cpu",
)

template(
    "server_top_memory", "Top Servers by Memory",
    "Servers ranked by 95th-percentile memory use, with swap and committed-memory pressure and "
    "the processes holding the most memory.",
    "servers",
    [S("server_resource_utilization", "Top 25 Servers by Memory (95th Percentile)",
       metric="memory", sort="p95", top_n=25),
     S("server_memory_pressure", None, top_n=10),
     S("server_top_processes", "Top 15 Processes by Memory", sort="memory", top_n=15)],
    audience=("Systems engineer", "NOC operator"), tags=("memory", "top n", "servers"),
    default_period="last_7d", frequency="weekly", icon="Layers",
)

template(
    "server_resource_trend_review", "Server Resource Trend",
    "Monthly view of fleet CPU and memory: average and 95th-percentile trend, the busiest servers "
    "on average and the biggest CPU increases against the previous month.",
    "servers",
    [S("server_resource_trend"),
     S("server_resource_utilization", "Top 10 Servers by CPU (Average)", metric="cpu",
       sort="avg"),
     S("server_resource_utilization", "Top 10 Servers by Memory (Average)", metric="memory",
       sort="avg"),
     S("server_cpu_change", "Biggest Server CPU Increases")],
    audience=("IT manager", "Capacity planner", "Systems engineer"),
    tags=("trend", "cpu", "memory", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="Activity",
)

template(
    "server_saturation_review", "Sustained Saturation Hotspots",
    "Servers that ran at 90% CPU or memory or more for whole 5-minute intervals: total saturated "
    "time, the longest unbroken runs and the highest peaks.",
    "servers",
    [S("server_saturation_hotspots", None, top_n=25, threshold=CPU_WARN),
     S("server_resource_utilization", "Top 10 Servers by CPU (Peak)", metric="cpu", sort="peak"),
     S("server_resource_utilization", "Top 10 Servers by Memory (Peak)", metric="memory",
       sort="peak")],
    audience=("Systems engineer", "NOC operator"), tags=("saturation", "cpu", "memory"),
    default_period="last_7d", frequency="weekly", icon="Gauge",
)

template(
    "server_memory_swap_pressure", "Memory and Swap Pressure",
    "Average and peak memory use, swap activity and committed memory against physical RAM, with "
    "the servers leaning hardest on swap.",
    "servers",
    [S("server_memory_pressure", "Memory and Swap Pressure", top_n=25),
     S("server_memory_pressure", "Servers Using the Most Swap", sort="swap", top_n=10),
     S("server_resource_utilization", "Top 10 Servers by Memory (95th Percentile)",
       metric="memory", sort="p95")],
    audience=("Systems engineer",), tags=("memory", "swap", "servers"),
    default_period="last_7d", frequency="weekly", icon="Layers",
)

template(
    "server_disk_latency_utilization", "Disk Latency and Utilization",
    "Server disks ranked by utilization and by latency, with queue length, IOPS and throughput.",
    "servers",
    [S("server_disk_io", "Top 25 Disks by Utilization", sort="util", top_n=25),
     S("server_disk_io", "Top 25 Disks by Latency", sort="latency", top_n=25),
     S("server_disk_io", "Top 10 Disks by IOPS", sort="iops", top_n=10)],
    audience=("Systems engineer",), tags=("disk", "latency", "iops", "storage"),
    default_period="last_7d", frequency="weekly", icon="HardDrive",
)

template(
    "server_network_throughput_review", "Server Network Throughput",
    "Servers ranked by average and peak network throughput in bits per second, and the servers "
    "reporting interface errors.",
    "servers",
    [S("server_network_throughput", "Top 25 Servers by Average Throughput", sort="avg",
       top_n=25),
     S("server_network_throughput", "Top 10 Servers by Peak Throughput", sort="peak"),
     S("server_network_throughput", "Servers with Interface Errors", sort="errors")],
    audience=("Systems engineer", "Network engineer"), tags=("network", "throughput", "errors"),
    default_period="last_7d", frequency="weekly", icon="Network",
)

template(
    "server_top_processes_review", "Top Processes",
    "The processes consuming the most CPU and memory across all servers, with peaks and instance "
    "counts.",
    "servers",
    [S("server_top_processes", "Top 25 Processes by CPU", sort="cpu", top_n=25),
     S("server_top_processes", "Top 25 Processes by Memory", sort="memory", top_n=25)],
    audience=("Systems engineer", "Application owner"), tags=("processes", "cpu", "memory"),
    default_period="last_7d", frequency="weekly", icon="Server",
)

template(
    "server_disk_capacity_forecast", "Disk Capacity Forecast",
    "Volumes closest to full: current usage, growth per day from a least-squares fit and the "
    "projected date each volume fills, flagging those due within 30 days.",
    "capacity",
    [S("capacity_filesystem_forecast", "Disk Capacity Forecast", top_n=50, horizon=30)],
    audience=("Capacity planner", "Systems engineer", "IT manager"),
    tags=("capacity", "disk", "forecast", "storage"),
    default_period="last_30d", frequency="monthly", icon="HardDrive", featured=True,
)

