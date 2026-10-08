"""Application performance (APM) sections: latency and throughput, slowest
operations, service dependencies, SLO error-budget burn, new and regressed
errors, release health, database statements, telemetry ingest quality,
instrumentation coverage and synthetic journey failures.

Data sources and their limits:
- ClickHouse ``apm_span_metrics_5m`` / ``_1h`` (AggregatingMergeTree RED
  rollups computed from every stored span; 90 / 395 days). Windows up to 6 h
  read the 5-minute rollup, longer ones the hourly rollup
  (``apm_rollup.table_for``), and the window start is snapped down to a bucket
  boundary (``apm_rollup.align_ms_window``) because rollup rows are labelled
  with their bucket start. RED is measured on entry spans (SERVER/CONSUMER),
  as the APM service pages do. The rollup has no service version.
- ``apm_service_graph`` (caller -> callee sums between instrumented services;
  90 days), ``apm_exceptions`` (30 days), raw ``apm_spans`` (14 days),
  ``apm_synthetic_results`` (90 days, one row per run after retries) and
  ``apm_ingest_stats`` (per-minute intake counters; 30 days).
- Postgres ``apm_services``, ``apm_slos``, ``apm_deployments``,
  ``apm_error_issues`` (triage) and ``apm_agent_processes``.

The APM API helpers (apm_services._query_services, apm_errors._query_groups,
apm_slo_service.compute_slo_series) call ClickHouse synchronously through their
own client; these sections reuse their thresholds and math but run every
query through ``ctx.ch_rows`` (off the event loop, honouring the report
window and the organisation timezone).

Every section is fleet-scoped (APM services are not network devices, so the
report's device filter does not apply) and requires apm.view; instrumentation
coverage also requires servers.view. Statement and error text never reaches a
report verbatim: literals, e-mail addresses, tokens and identifiers are
masked and the text is shortened to 120 characters.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from app.services.apm_rollup import BUCKET_5M, align_ms_window, bucket_for, table_for
from app.services.report_sections import (
    SectionCtx,
    _coerce_dt,
    _fmt_pct,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

REQUIRES = ("apm.view",)
CATEGORY = "Applications"
ENTRY_KINDS = "('SERVER','CONSUMER')"

ROLLUP_5M_DAYS = 90       # apm_span_metrics_5m TTL
ROLLUP_1H_DAYS = 395      # apm_span_metrics_1h TTL
GRAPH_DAYS = 90           # apm_service_graph TTL
EXCEPTION_DAYS = 30       # apm_exceptions TTL
SPAN_DAYS = 14            # apm_spans TTL (migrate-098)
SYNTHETIC_DAYS = 90       # apm_synthetic_results TTL
INGEST_DAYS = 30          # apm_ingest_stats TTL

# Service health thresholds of the APM service list (apm_services._health).
ERR_DEGRADED, ERR_CRITICAL = 1.0, 5.0          # error rate, %
LAT_DEGRADED, LAT_CRITICAL = 500.0, 1000.0     # latency, ms

TEXT_LIMIT = 120

INTERVAL_LABEL = {"5 MINUTE": "5-minute", "30 MINUTE": "30-minute", "1 HOUR": "hourly",
                  "2 HOUR": "2-hour", "1 DAY": "daily"}
INTERVAL_STEP = {"5 MINUTE": 300, "30 MINUTE": 1800, "1 HOUR": 3600, "2 HOUR": 7200,
                 "1 DAY": 86400}


# ─── Shared helpers ─────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: Any) -> Optional[datetime]:
    """datetime / ISO string / epoch -> naive UTC (ClickHouse parameters)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    dt = _coerce_dt(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _aware(value: Any) -> Optional[datetime]:
    """Aware UTC (Postgres timestamptz parameters and comparisons)."""
    dt = _naive(value)
    return dt.replace(tzinfo=timezone.utc) if dt else None


def _ts(value: Any) -> Optional[datetime]:
    """A ClickHouse timestamp cell -> naive UTC; the 1970 placeholder that
    minIf/maxIf return over no rows is 'not seen' (None)."""
    dt = _naive(value)
    if dt is None or dt.year < 1971:
        return None
    return dt


def _epoch_dt(epoch: Any) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)


def _int(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _num(v: Any) -> Optional[float]:
    """A measured number, or None (ClickHouse returns nan for empty quantiles)."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def _arr(v: Any) -> list:
    """An Array/Tuple cell as a list (named tuples come back as dicts)."""
    if isinstance(v, dict):
        return list(v.values())
    return list(v) if isinstance(v, (list, tuple)) else []


def _s(v: Any) -> str:
    """ClickHouse FixedString cells (group ids) come back as padded bytes."""
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    return str(v or "").rstrip("\x00")


def _count(n: Any) -> str:
    return f"{_int(n):,}"


def _ms(v: Any) -> str:
    """Latency with precision that suits its size ('4.21 ms', '42.1 ms', '1.24 s')."""
    x = _num(v)
    if x is None:
        return "—"
    if x >= 1000:
        return f"{x / 1000:.2f} s"
    if x >= 100:
        return f"{x:.0f} ms"
    if x >= 10:
        return f"{x:.1f} ms"
    return f"{x:.2f} ms"


def _total_time(ms: Any) -> str:
    """Accumulated time ('850 ms', '12.4 s', '3.5 min', '2.1 h', '3.2 d')."""
    x = _num(ms)
    if x is None:
        return "—"
    s = x / 1000.0
    if s < 1:
        return f"{x:.0f} ms"
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{s / 60:.1f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def _duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{s}s"


def _err_pct(errs: Any, reqs: Any) -> Optional[float]:
    r = _int(reqs)
    return _int(errs) / r * 100.0 if r else None


def _pts_delta(cur: Optional[float], prev: Optional[float]) -> Optional[str]:
    if cur is None or prev is None:
        return None
    d = cur - prev
    if abs(d) < 0.005:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.2f} pts vs previous period"


def _rel_delta(cur: Optional[float], prev: Optional[float]) -> Optional[str]:
    if cur is None or prev is None or prev <= 0:
        return None
    pct = (cur - prev) / prev * 100.0
    if abs(pct) < 0.05:
        return "No change vs previous period"
    return f"{'▲' if pct > 0 else '▼'} {abs(pct):.1f}% vs previous period"


def _err_accent(pct: Optional[float]) -> str:
    if pct is None:
        return "neutral"
    return "danger" if pct >= ERR_CRITICAL else "warning" if pct >= ERR_DEGRADED else "success"


def _lat_accent(ms: Optional[float]) -> str:
    if ms is None:
        return "neutral"
    return "danger" if ms >= LAT_CRITICAL else "warning" if ms >= LAT_DEGRADED else "success"


def _health(err: Optional[float], latency_ms: Optional[float]) -> str:
    if err is None:
        return "no data"
    if err >= ERR_CRITICAL or (latency_ms or 0) >= LAT_CRITICAL:
        return "critical"
    if err >= ERR_DEGRADED or (latency_ms or 0) >= LAT_DEGRADED:
        return "degraded"
    return "healthy"


def _rows_title(base: str, shown: int, total: int) -> str:
    return f"{base} (top {shown:,} of {total:,})" if total > shown else base


def _clip(value: Any, n: int = TEXT_LIMIT) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _tz(ctx: SectionCtx) -> ZoneInfo:
    try:
        return ZoneInfo(ctx.tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _end(ctx: SectionCtx) -> datetime:
    """The window end that can hold data: the report end, or now if earlier."""
    return min(_naive(ctx.to), _utcnow())


def _axis(ctx: SectionCtx, interval: str, frm: datetime, end: datetime) -> list[int]:
    """Epochs of the organisation-timezone buckets ClickHouse returns for
    ``toUnixTimestamp(toDateTime(ctx.ch_bucket(col, interval), tz))`` from the
    bucket holding ``frm`` up to ``end``."""
    tz = _tz(ctx)
    f, e = _aware(frm), _aware(end)
    out: list[int] = []
    if interval == "1 DAY":
        day = f.astimezone(tz).date()
        while len(out) < 1000:
            midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
            if midnight >= e:
                break
            out.append(int(midnight.timestamp()))
            day += timedelta(days=1)
        return out
    step = INTERVAL_STEP[interval]
    loc = f.astimezone(tz)
    minutes = (loc.hour * 60 + loc.minute) // (step // 60) * (step // 60)
    start = loc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
    epoch, stop = int(start.timestamp()), int(e.timestamp())
    while epoch < stop and len(out) < 3000:
        out.append(epoch)
        epoch += step
    return out


def _bucket_index(axis: list[int], when: datetime) -> Optional[int]:
    if not axis or when is None:
        return None
    i = bisect.bisect_right(axis, int(_aware(when).timestamp())) - 1
    return i if i >= 0 else None


def _gap_points(ctx: SectionCtx, pts: list[tuple[int, Optional[float]]], step_s: int,
                digits: int = 2) -> list[dict]:
    """Chart points with an explicit null where buckets are missing (a gap,
    never a zero)."""
    out: list[dict] = []
    prev: Optional[int] = None
    for epoch, v in pts:
        if prev is not None and epoch - prev > step_s * 1.5:
            out.append({"t": ctx.iso(_epoch_dt(prev + step_s)), "v": None})
        out.append({"t": ctx.iso(_epoch_dt(epoch)), "v": None if v is None else round(v, digits)})
        prev = epoch
    return out


def _axis_points(ctx: SectionCtx, axis: list[int], values: dict[int, Optional[float]],
                 digits: int = 2) -> list[dict]:
    return [{"t": ctx.iso(_epoch_dt(e)),
             "v": None if values.get(e) is None else round(values[e], digits)} for e in axis]


def _day_or_hour(ctx: SectionCtx) -> str:
    return "1 HOUR" if ctx.hours <= 48 else "1 DAY"


def _bucket_label(ctx: SectionCtx, interval: str, epoch: int) -> str:
    when = _epoch_dt(epoch)
    return ctx.fmt_date(when) if interval == "1 DAY" else ctx.fmt_short(when)


# --- text masking -----------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONG_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{16,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_SECRET_KV = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
                        r"authorization|session[_-]?id|sid)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
_URL_QUERY = re.compile(r"([?&][^=&\s#]+)=[^&\s#]*")
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")
_LONG_TOKEN = re.compile(r"\b(?=[\w+/=-]*\d)(?=[\w+/=-]*[A-Za-z])[\w+/=-]{32,}")

_SQL_STRING = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_SQL_OPEN_STRING = re.compile(r"'[^']*$")
_SQL_DOLLAR = re.compile(r"\$([A-Za-z_]*)\$.*?\$\1\$", re.S)
_SQL_DQUOTE = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:)?')
_SQL_HEX = re.compile(r"(?<![\w$.])0[xX][0-9a-fA-F]+\b")
_SQL_NUMBER = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b")
_SQL_LIST = re.compile(r"\?(?:\s*,\s*\?)+")
_IDENT = re.compile(r"[A-Za-z_][\w.$-]{0,62}")


def _mask_common(s: str) -> str:
    s = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}?", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} ?", s)
    s = _EMAIL.sub("?", s)
    s = _UUID.sub("?", s)
    s = _LONG_HEX.sub("?", s)
    s = _URL_QUERY.sub(lambda m: f"{m.group(1)}=?", s)
    s = _LONG_DIGITS.sub("?", s)
    return _LONG_TOKEN.sub("?", s)


def mask_statement(stmt: Any, limit: int = TEXT_LIMIT) -> str:
    """A database statement safe for a report: every literal value replaced
    by ?, value lists collapsed, at most ``limit`` characters."""
    s = " ".join(str(stmt or "").split())
    if not s:
        return "—"
    s = _SQL_DOLLAR.sub("?", s)
    s = _SQL_STRING.sub("?", s)
    s = _SQL_OPEN_STRING.sub("?", s)          # a statement cut off mid-literal
    json_like = s[:1] in "{["

    def dq(m: re.Match) -> str:
        inner, colon = m.group(1), m.group(2) or ""
        if colon or (not json_like and _IDENT.fullmatch(inner)):
            return m.group(0)                 # a JSON key or a quoted identifier
        return "?" + colon

    s = _SQL_DQUOTE.sub(dq, s)
    s = _mask_common(s)
    s = _SQL_HEX.sub("?", s)
    s = _SQL_NUMBER.sub("?", s)
    s = _SQL_LIST.sub("?, …", s)
    return _clip(s, limit)


def mask_message(msg: Any, limit: int = TEXT_LIMIT) -> str:
    """Error text safe for a report: credentials, e-mail addresses, tokens,
    long identifiers and URL query values masked; at most ``limit`` chars."""
    s = " ".join(str(msg or "").split())
    return _clip(_mask_common(s), limit) if s else "—"


# --- rollup windows ---------------------------------------------------------

@dataclass(frozen=True)
class RollupWin:
    start: datetime        # naive UTC, snapped down to a rollup bucket
    end: datetime          # naive UTC (exclusive)
    table: str
    bucket_s: int
    covered_s: float
    retention_days: int


def rollup_window(frm: Any, to: Any) -> Optional[RollupWin]:
    """The RED rollup and bucket-aligned bounds that serve ``[frm, to)``
    (``to`` capped at now). None when the window has not started."""
    f = _aware(frm)
    t = min(_aware(to), datetime.now(timezone.utc))
    if f is None or t is None or t <= f:
        return None
    f_ms, t_ms = int(f.timestamp() * 1000), int(t.timestamp() * 1000)
    a_from, a_to, covered = align_ms_window(f_ms, t_ms)
    span_s = int((t_ms - f_ms) / 1000)
    bucket = bucket_for(span_s)
    return RollupWin(start=_epoch_dt(a_from // 1000), end=_naive(t), table=table_for(span_s),
                     bucket_s=bucket, covered_s=covered,
                     retention_days=ROLLUP_5M_DAYS if bucket == BUCKET_5M else ROLLUP_1H_DAYS)


def _retained(start: Any, days: int) -> bool:
    return _aware(start) >= datetime.now(timezone.utc) - timedelta(days=days)


def _rollup_label(win: RollupWin) -> str:
    return "5-minute" if win.bucket_s == BUCKET_5M else "hourly"


async def _red_totals(ctx: SectionCtx, win: RollupWin, q: float) -> Optional[dict]:
    """Requests, errors, median and q-quantile latency, apdex and services
    over a rollup window (entry spans). None when nothing was measured."""
    rows = await ctx.ch_rows(
        "SELECT sum(request_count), sum(error_count), "
        f"quantilesTDigestMerge(0.5, {q:g})(duration_state), "
        "sum(satisfied_count), sum(tolerating_count), uniqExact(service_name) "
        f"FROM zenplus.{win.table} "
        f"WHERE timestamp >= %(f)s AND timestamp < %(t)s AND span_kind IN {ENTRY_KINDS}",
        {"f": win.start, "t": win.end})
    if not rows:
        return None
    r = rows[0]
    reqs = _int(r[0])
    if reqs <= 0:
        return None
    qs = _arr(r[2])
    return {"reqs": reqs, "errs": _int(r[1]), "err": _err_pct(r[1], reqs),
            "p50": _num(qs[0]) if qs else None, "pq": _num(qs[1]) if len(qs) > 1 else None,
            "apdex": (_int(r[3]) + _int(r[4]) / 2) / reqs, "services": _int(r[5])}


# ─── Latency and throughput trend ───────────────────────────────────────────

PERCENTILES = {"p90": (0.90, "90th percentile", "P90"),
               "p95": (0.95, "95th percentile", "P95"),
               "p99": (0.99, "99th percentile", "P99")}


@register_section(
    "apm_latency_trend", title="Application Latency and Throughput", category=CATEGORY,
    description="Median and high-percentile response time, throughput and error rate of "
                "inbound requests across every instrumented service over the period.",
    scope="fleet", requires=REQUIRES,
    options=[{"key": "percentile", "label": "Upper percentile", "type": "select", "default": "p95",
              "choices": [[k, v[1].capitalize()] for k, v in PERCENTILES.items()],
              "help": "The high percentile drawn next to the median."}],
)
async def apm_latency_trend(ctx: SectionCtx) -> dict:
    sid, title = "apm_latency_trend", "Application Latency and Throughput"
    q, q_label, q_short = PERCENTILES.get(ctx.opt("percentile"), PERCENTILES["p95"])
    desc = (f"Median and {q_label} response time, throughput and error rate of inbound "
            "requests across every instrumented service.")
    win = rollup_window(ctx.frm, ctx.to)
    if win is None:
        return _section(sid, title, description=desc, notes=["This period has not started yet."])

    interval = ctx.bucket()
    if win.bucket_s > BUCKET_5M and INTERVAL_STEP[interval] < 3600:
        interval = "1 HOUR"           # the hourly rollup cannot fill finer buckets
    step = INTERVAL_STEP[interval]
    bucket, bparams = ctx.ch_bucket("timestamp", interval)
    rows = await ctx.ch_rows(
        f"SELECT toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS b, "
        "sum(request_count), sum(error_count), "
        f"quantilesTDigestMerge(0.5, {q:g})(duration_state) "
        f"FROM zenplus.{win.table} "
        f"WHERE timestamp >= %(f)s AND timestamp < %(t)s AND span_kind IN {ENTRY_KINDS} "
        "GROUP BY b ORDER BY b", {"f": win.start, "t": win.end, **bparams})
    totals = await _red_totals(ctx, win, q)

    notes: list[str] = []
    if (wn := ctx.window_note(win.retention_days, f"The {_rollup_label(win)} APM rollup")):
        notes.append(wn)
    if totals is None:
        return _section(sid, title, description=desc,
                        notes=["No inbound requests were recorded by instrumented services in "
                               "this period."] + notes)

    start_e, end_e = int(_aware(win.start).timestamp()), int(_aware(win.end).timestamp())
    p50_pts, pq_pts, rpm_pts, err_pts = [], [], [], []
    for r in rows:
        b, reqs = _int(r[0]), _int(r[1])
        if reqs <= 0:
            continue
        qs = _arr(r[3])
        minutes = max((min(b + step, end_e) - max(b, start_e)) / 60.0, 1.0)
        p50_pts.append((b, _num(qs[0]) if qs else None))
        pq_pts.append((b, _num(qs[1]) if len(qs) > 1 else None))
        rpm_pts.append((b, reqs / minutes))
        err_pts.append((b, _err_pct(r[2], reqs)))

    prev = None
    pf, pt = ctx.prior_window()
    pwin = rollup_window(pf, pt)
    if pwin is not None and _retained(pwin.start, pwin.retention_days):
        prev = await _red_totals(ctx, pwin, q)

    minutes_total = max(win.covered_s / 60.0, 1.0)
    rpm = totals["reqs"] / minutes_total
    kpis = [
        {"label": "Requests", "value": _count(totals["reqs"]), "accent": "primary",
         "subtitle": _rel_delta(totals["reqs"], prev and prev["reqs"])
         or f"{rpm:,.1f} requests per minute"},
        {"label": "Error rate", "value": _fmt_pct(totals["err"]),
         "accent": _err_accent(totals["err"]),
         "subtitle": _pts_delta(totals["err"], prev and prev["err"])
         or f"{_count(totals['errs'])} failed requests"},
        {"label": "Median response time", "value": _ms(totals["p50"]), "accent": "info",
         "subtitle": _rel_delta(totals["p50"], prev and prev["p50"]) or "50th percentile"},
        {"label": f"{q_short} response time", "value": _ms(totals["pq"]),
         "accent": _lat_accent(totals["pq"]),
         "subtitle": _rel_delta(totals["pq"], prev and prev["pq"]) or q_label.capitalize()},
        {"label": "Apdex", "value": f"{totals['apdex']:.2f}",
         "accent": "success" if totals["apdex"] >= 0.94 else "warning" if totals["apdex"] >= 0.7
         else "danger", "subtitle": "Satisfied ≤ 500 ms, tolerating ≤ 2 s"},
        {"label": "Services reporting", "value": _count(totals["services"]), "accent": "neutral"},
    ]
    p50_series = _gap_points(ctx, p50_pts, step)
    pq_series = _gap_points(ctx, pq_pts, step)
    charts = [
        {"title": f"Response time — median and {q_label} (ms)",
         "series": {"kind": "line", "unit": "ms", "color": "primary", "points": pq_series,
                    "series": [{"name": "Median", "points": p50_series},
                               {"name": q_label.capitalize(), "points": pq_series}]}},
        {"title": "Throughput (requests per minute)",
         "series": {"kind": "area", "unit": "req/min", "color": "info",
                    "points": _gap_points(ctx, rpm_pts, step)}},
        {"title": "Error rate (%)",
         "series": {"kind": "line", "unit": "%", "color": "danger",
                    "points": _gap_points(ctx, err_pts, step)}},
    ]
    notes.insert(0, f"Inbound requests (server and consumer entry spans) from the "
                    f"{_rollup_label(win)} rollup, which counts every stored span. Each point is a "
                    f"{INTERVAL_LABEL[interval]} interval ({ctx.tz_label()}); intervals without "
                    "requests are gaps.")
    if prev is None:
        notes.append("No comparison with the previous period: it is not retained or had no "
                     "requests.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, notes=notes)


# ─── Slowest operations ─────────────────────────────────────────────────────

OPERATION_SORTS = {
    "total_time": ("total_ms", "Total Time"),
    "p95": ("arrayElement(q, 2)", "95th-Percentile Latency"),
    "calls": ("calls", "Calls"),
    "error_rate": ("errs / calls", "Error Rate"),
}


@register_section(
    "apm_slowest_operations", title="Slowest Operations", category=CATEGORY,
    description="Inbound operations ranked by total time, 95th-percentile latency, calls or error "
                "rate, with each operation's share of all server time.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 100),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "total_time",
         "choices": [[k, v[1].capitalize() if k != "p95" else v[1]]
                     for k, v in OPERATION_SORTS.items()]},
        {"key": "min_calls", "label": "Minimum calls", "type": "int", "default": 10, "min": 1,
         "max": 1000000, "help": "Operations with fewer calls are left out of the ranking."},
    ],
)
async def apm_slowest_operations(ctx: SectionCtx) -> dict:
    sid = "apm_slowest_operations"
    top_n, min_calls = int(ctx.opt("top_n")), int(ctx.opt("min_calls"))
    sort = ctx.opt("sort") if ctx.opt("sort") in OPERATION_SORTS else "total_time"
    order, sort_label = OPERATION_SORTS[sort]
    title = f"Top {top_n} Operations by {sort_label}"
    desc = (f"Top {top_n} inbound operations by {sort_label.lower()}, with calls, latency, share "
            "of all server time and error rate.")
    win = rollup_window(ctx.frm, ctx.to)
    if win is None:
        return _section(sid, title, description=desc, notes=["This period has not started yet."])
    where = (f"FROM zenplus.{win.table} WHERE timestamp >= %(f)s AND timestamp < %(t)s "
             f"AND span_kind IN {ENTRY_KINDS}")
    params = {"f": win.start, "t": win.end, "min_calls": min_calls, "lim": top_n}
    tot = await ctx.ch_rows(
        "SELECT count(), countIf(c >= %(min_calls)s), sum(c), sum(d) FROM ("
        f"SELECT sum(request_count) AS c, sum(duration_sum) AS d {where} "
        "GROUP BY service_name, operation)", params)
    rows = await ctx.ch_rows(
        "SELECT service_name, operation, sum(request_count) AS calls, sum(error_count) AS errs, "
        "quantilesTDigestMerge(0.5, 0.95, 0.99)(duration_state) AS q, "
        f"sum(duration_sum) AS total_ms {where} "
        "GROUP BY service_name, operation HAVING calls >= %(min_calls)s "
        f"ORDER BY {order} DESC, calls DESC LIMIT %(lim)s", params)

    notes: list[str] = []
    if (wn := ctx.window_note(win.retention_days, f"The {_rollup_label(win)} APM rollup")):
        notes.append(wn)
    t = tot[0] if tot else (0, 0, 0, 0)
    ops_all, ops_ranked = _int(t[0]), _int(t[1])
    calls_all, time_all = _int(t[2]), _num(t[3]) or 0.0
    if not rows or not calls_all:
        msg = ("No inbound operations were recorded in this period." if not calls_all else
               f"No operation reached {min_calls:,} calls in this period.")
        return _section(sid, title, description=desc, notes=[msg] + notes)

    table_rows, rank_pts, listed_time = [], [], 0.0
    for r in rows:
        calls, total_ms = _int(r[2]), _num(r[5]) or 0.0
        qs = _arr(r[4])
        p95 = _num(qs[1]) if len(qs) > 1 else None
        err = _err_pct(r[3], calls)
        share = total_ms / time_all * 100.0 if time_all else None
        listed_time += total_ms
        table_rows.append([str(r[0] or "—"), _clip(r[1] or "—", 80), _count(calls),
                           _ms(total_ms / calls if calls else None), _ms(p95),
                           _total_time(total_ms), _fmt_pct(share), _fmt_pct(err)])
        value = {"total_time": share, "p95": p95, "calls": calls, "error_rate": err}[sort]
        rank_pts.append({"label": _clip(f"{r[0]} · {r[1]}", 60),
                         "value": round(value, 2) if value is not None else 0})
    chart_unit = {"total_time": "%", "p95": "ms", "calls": "calls", "error_rate": "%"}[sort]
    chart_title = {"total_time": "Share of server time (%)",
                   "p95": "95th-percentile latency (ms)", "calls": "Calls",
                   "error_rate": "Error rate (%)"}[sort]
    listed_share = listed_time / time_all * 100.0 if time_all else None
    kpis = [
        {"label": "Operations", "value": _count(ops_all), "accent": "primary",
         "subtitle": f"{ops_ranked:,} with at least {min_calls:,} calls"},
        {"label": "Calls", "value": _count(calls_all), "accent": "info"},
        {"label": "Server time", "value": _total_time(time_all), "accent": "neutral",
         "subtitle": "Sum of inbound request durations"},
        {"label": f"Share of time in the listed {len(rows)}", "value": _fmt_pct(listed_share),
         "accent": "warning" if (listed_share or 0) >= 80 else "neutral"},
    ]
    notes.insert(0, "Inbound operations (server and consumer entry spans). Share of time is the "
                    "operation's total duration divided by the total for all inbound operations in "
                    f"the period; operations with fewer than {min_calls:,} calls are not ranked.")
    return _section(
        sid, title, description=desc, kpis=kpis,
        charts=[{"title": chart_title,
                 "series": {"kind": "rank", "unit": chart_unit, "color": "primary",
                            "points": rank_pts[:10]}}],
        tables=[{"title": _rows_title("Operations", len(rows), ops_ranked),
                 "headers": ["Service", "Operation", "Calls", "Average", "P95", "Total time",
                             "Share of time", "Error rate"],
                 "styles": ["text", "mono", "num", "num", "num", "num", "pct-bar", "num"],
                 "rows": table_rows}],
        notes=notes)


# ─── Service dependencies ───────────────────────────────────────────────────

DEPENDENCY_SORTS = {
    "calls": ("calls", "Calls"),
    "error_rate": ("errs / calls", "Error Rate"),
    "latency": ("dur / calls", "Average Latency"),
    "total_time": ("dur", "Total Time"),
}


@register_section(
    "apm_service_dependencies", title="Service Dependencies", category=CATEGORY,
    description="Calls between instrumented services (caller → callee) with error rate and "
                "average latency per dependency.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(20, 200),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "calls",
         "choices": [[k, v[1].capitalize()] for k, v in DEPENDENCY_SORTS.items()]},
    ],
)
async def apm_service_dependencies(ctx: SectionCtx) -> dict:
    sid = "apm_service_dependencies"
    top_n = int(ctx.opt("top_n"))
    sort = ctx.opt("sort") if ctx.opt("sort") in DEPENDENCY_SORTS else "calls"
    order, sort_label = DEPENDENCY_SORTS[sort]
    title = "Service Dependencies" if sort == "calls" else f"Service Dependencies by {sort_label}"
    desc = (f"Calls between instrumented services ranked by {sort_label.lower()}: volume, error "
            "rate and average latency of each caller → callee dependency.")
    end = _end(ctx)
    if end <= _naive(ctx.frm):
        return _section(sid, title, description=desc, notes=["This period has not started yet."])
    # Graph rows are labelled with their 5-minute bucket start.
    start = _epoch_dt(int(_aware(ctx.frm).timestamp()) // BUCKET_5M * BUCKET_5M)
    where = "FROM zenplus.apm_service_graph WHERE timestamp >= %(f)s AND timestamp < %(t)s"
    params = {"f": start, "t": end, "lim": top_n}
    tot = await ctx.ch_rows(
        "SELECT count(), sum(c), sum(e), sum(d), "
        "length(arrayDistinct(arrayConcat(groupArray(cs), groupArray(ss)))) FROM ("
        "SELECT client_service AS cs, server_service AS ss, sum(request_count) AS c, "
        f"sum(error_count) AS e, sum(duration_sum_ms) AS d {where} "
        "GROUP BY cs, ss HAVING c > 0)", params)
    rows = await ctx.ch_rows(
        "SELECT client_service, server_service, sum(request_count) AS calls, "
        f"sum(error_count) AS errs, sum(duration_sum_ms) AS dur {where} "
        "GROUP BY client_service, server_service HAVING calls > 0 "
        f"ORDER BY {order} DESC, calls DESC LIMIT %(lim)s", params)

    notes = ["Only calls between two instrumented services are recorded; calls to databases, "
             "caches and external APIs are not dependencies here. Latency is the mean per call "
             "(the dependency rollup keeps sums, not percentiles). Status: degraded at 1% errors "
             "or 500 ms average, critical at 5% or 1 s.",
             "Dependencies are aggregated per closed 5-minute interval, so the last few minutes "
             "before the report was generated may not be included."]
    if (wn := ctx.window_note(GRAPH_DAYS, "Service dependency data")):
        notes.append(wn)
    t = tot[0] if tot else (0, 0, 0, 0, 0)
    edges, calls_all = _int(t[0]), _int(t[1])
    if not rows or not calls_all:
        return _section(sid, title, description=desc,
                        notes=["No calls between instrumented services in this period."] + notes[2:])
    minutes = max((end - start).total_seconds() / 60.0, 1.0)
    err_all = _err_pct(t[2], calls_all)
    table_rows, rank_pts = [], []
    slowest: Optional[tuple[float, str]] = None
    for r in rows:
        calls, dur = _int(r[2]), _num(r[4]) or 0.0
        err = _err_pct(r[3], calls)
        avg = dur / calls if calls else None
        name = f"{r[0]} → {r[1]}"
        if avg is not None and (slowest is None or avg > slowest[0]):
            slowest = (avg, name)
        table_rows.append([str(r[0] or "—"), str(r[1] or "—"), _count(calls),
                           f"{calls / minutes:,.1f}", _fmt_pct(err), _ms(avg), _total_time(dur),
                           _health(err, avg)])
        value = {"calls": calls, "error_rate": err, "latency": avg, "total_time": dur / 1000.0}[sort]
        rank_pts.append({"label": _clip(name, 60), "value": round(value or 0, 2)})
    chart_unit = {"calls": "calls", "error_rate": "%", "latency": "ms", "total_time": "s"}[sort]
    chart_title = {"calls": "Calls by dependency", "error_rate": "Error rate by dependency (%)",
                   "latency": "Average latency by dependency (ms)",
                   "total_time": "Total time by dependency (s)"}[sort]
    kpis = [
        {"label": "Dependencies", "value": _count(edges), "accent": "primary"},
        {"label": "Services connected", "value": _count(t[4]), "accent": "neutral"},
        {"label": "Calls", "value": _count(calls_all), "accent": "info",
         "subtitle": f"{calls_all / minutes:,.1f} per minute"},
        {"label": "Error rate", "value": _fmt_pct(err_all), "accent": _err_accent(err_all),
         "subtitle": f"{_count(t[2])} failed calls"},
    ]
    if slowest:
        kpis.append({"label": "Slowest listed dependency", "value": _ms(slowest[0]),
                     "accent": _lat_accent(slowest[0]), "subtitle": _clip(slowest[1], 60)})
    return _section(
        sid, title, description=desc, kpis=kpis,
        charts=[{"title": chart_title,
                 "series": {"kind": "rank", "unit": chart_unit, "color": "primary",
                            "points": rank_pts[:10]}}],
        tables=[{"title": _rows_title("Dependencies", len(rows), edges),
                 "headers": ["Caller", "Callee", "Calls", "Calls / min", "Error rate",
                             "Avg latency", "Total time", "Status"],
                 "styles": ["text", "text", "num", "num", "num", "num", "num", "status"],
                 "rows": table_rows}],
        notes=notes)


# ─── SLO error-budget burn ──────────────────────────────────────────────────

MAX_SLOS = 100
SLO_CHART_SERIES = 6


def _target_fmt(v: float) -> str:
    s = f"{v:.3f}"
    return (s[:-1] if s.endswith("0") else s) + "%"


def _sli_fmt(v: Optional[float], target: float) -> str:
    if v is None:
        return "—"
    digits = max(2, len(_target_fmt(target).rstrip("%").partition(".")[2]))
    return f"{v:.{digits}f}%"


def _objective(slo: dict) -> str:
    target, kind = float(slo["target"]), slo.get("sli_type") or "availability"
    if kind == "latency":
        what = f"under {int(slo.get('latency_threshold_ms') or 0):,} ms"
    elif kind == "error_rate":
        what = "without errors"
    elif kind == "custom":
        what = "(custom SLI)"
    else:
        what = "available"
    return f"{_target_fmt(target)} {what} · {int(slo.get('window_days') or 30)}d"


def _slo_status(remaining: Optional[float]) -> str:
    if remaining is None:
        return "no data"
    if remaining <= 0:
        return "breaching"
    return "at risk" if remaining < 0.25 else "on track"


async def _slo_days(ctx: SectionCtx, slo: dict, start: datetime, end: datetime) -> list[tuple]:
    """[(local-day epoch, requests, bad events)] from the hourly rollup."""
    from app.services.apm_slo_service import _Q_GRID, _frac_above

    bucket, bparams = ctx.ch_bucket("timestamp", "1 DAY")
    conds = [f"timestamp >= %(f)s AND timestamp < %(t)s AND span_kind IN {ENTRY_KINDS}",
             "service_name = %(svc)s"]
    params: dict[str, Any] = {"f": start, "t": end, "svc": slo["service_name"], **bparams}
    if slo.get("env"):
        conds.append("env = %(env)s")
        params["env"] = slo["env"]
    if slo.get("operation"):
        conds.append("operation = %(op)s")
        params["op"] = slo["operation"]
    where = " AND ".join(conds)
    day = f"toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS d"
    if slo.get("sli_type") == "latency":
        grid = ", ".join(f"{q:g}" for q in _Q_GRID)
        rows = await ctx.ch_rows(
            f"SELECT {day}, sum(request_count), quantilesTDigestMerge({grid})(duration_state) "
            f"FROM zenplus.apm_span_metrics_1h WHERE {where} GROUP BY d ORDER BY d", params)
        threshold = float(slo.get("latency_threshold_ms") or 0)
        out = []
        for r in rows:
            reqs = _int(r[1])
            vals = [_num(v) for v in _arr(r[2])]
            if reqs > 0 and vals and all(v is not None for v in vals):
                out.append((_int(r[0]), reqs, _frac_above(threshold, vals) * reqs))
        return out
    rows = await ctx.ch_rows(
        f"SELECT {day}, sum(request_count), sum(error_count) "
        f"FROM zenplus.apm_span_metrics_1h WHERE {where} GROUP BY d ORDER BY d", params)
    return [(_int(r[0]), _int(r[1]), float(_int(r[2]))) for r in rows if _int(r[1]) > 0]


@register_section(
    "apm_slo_budget_trend", title="SLO Error Budget Burn", category=CATEGORY,
    description="Daily SLI against target and error budget remaining for each service level "
                "objective over its own trailing window (7, 30 or 90 days).",
    scope="fleet", requires=REQUIRES, options=[top_n_option(10, 50)],
)
async def apm_slo_budget_trend(ctx: SectionCtx) -> dict:
    from app.services.apm_slo_service import _load_slos

    sid, title = "apm_slo_budget_trend", "SLO Error Budget Burn"
    top_n = int(ctx.opt("top_n"))
    end = _end(ctx)
    desc = ("Error budget remaining and daily SLI against target for each service level objective, "
            f"measured over its own trailing window ending {ctx.fmt_dt(end)}.")
    slos = sorted(await _load_slos(ctx.db), key=lambda s: (str(s.get("name") or ""), str(s.get("id"))))
    if not slos:
        return _section(sid, title, description=desc,
                        notes=["No service level objectives are defined."])
    shown_slos = slos[:MAX_SLOS]

    results = []
    custom = 0
    for slo in shown_slos:
        target = float(slo["target"])
        budget = max(1.0 - target / 100.0, 1e-9)
        window_days = int(slo.get("window_days") or 30)
        start = (end - timedelta(days=window_days)).replace(minute=0, second=0, microsecond=0)
        days: list[tuple] = []
        if slo.get("sli_type") == "custom":
            custom += 1
        else:
            days = await _slo_days(ctx, slo, start, end)
        cum_req = cum_bad = 0.0
        daily = []
        for epoch, reqs, bad in days:
            cum_req += reqs
            cum_bad += bad
            daily.append((epoch, (1.0 - bad / reqs) * 100.0,
                          1.0 - (cum_bad / cum_req) / budget))
        consumed = (cum_bad / cum_req) / budget if cum_req else None
        results.append({
            "slo": slo, "target": target, "start": start, "daily": daily,
            "sli": (1.0 - cum_bad / cum_req) * 100.0 if cum_req else None,
            "consumed": consumed, "remaining": None if consumed is None else 1.0 - consumed,
            "requests": cum_req,
        })

    def rank(r: dict) -> tuple:
        return (r["remaining"] is None, r["remaining"] if r["remaining"] is not None else 0.0,
                str(r["slo"].get("name") or ""))

    results.sort(key=rank)
    statuses = [_slo_status(r["remaining"]) for r in results]
    n_on, n_risk = statuses.count("on track"), statuses.count("at risk")
    n_breach, n_none = statuses.count("breaching"), statuses.count("no data")
    kpis = [
        {"label": "SLOs", "value": _count(len(results)), "accent": "primary",
         "subtitle": f"of {len(slos):,} defined" if len(slos) > len(results) else None},
        {"label": "On track", "value": _count(n_on), "accent": "success"},
        {"label": "At risk", "value": _count(n_risk), "accent": "warning" if n_risk else "neutral",
         "subtitle": "Less than 25% of budget left"},
        {"label": "Breaching", "value": _count(n_breach), "accent": "danger" if n_breach else "success",
         "subtitle": "Budget exhausted"},
        {"label": "No data", "value": _count(n_none), "accent": "neutral",
         "subtitle": "Not measured"},
    ]
    kpis = [{k: v for k, v in kp.items() if v is not None} for kp in kpis]

    charts = []
    with_data = [r for r in results if r["daily"]]
    if with_data:
        series = []
        for r in with_data[:SLO_CHART_SERIES]:
            pts = [(e, max(rem, 0.0) * 100.0) for e, _sli, rem in r["daily"]]
            series.append({"name": _clip(r["slo"].get("name") or "SLO", 40),
                           "points": _gap_points(ctx, pts, 86400)})
        charts.append({"title": "Error budget remaining (%)",
                       "series": {"kind": "line", "unit": "%", "color": "primary",
                                  "y_domain": [0, 100], "points": series[0]["points"],
                                  "series": series}})
        worst = with_data[0]
        sli_pts = _gap_points(ctx, [(e, sli) for e, sli, _rem in worst["daily"]], 86400, 4)
        target_pts = [{"t": p["t"], "v": round(worst["target"], 4)} for p in sli_pts]
        charts.append({"title": f"Daily SLI vs target — {_clip(worst['slo'].get('name'), 50)} (%)",
                       "series": {"kind": "line", "unit": "%", "color": "primary",
                                  "points": sli_pts,
                                  "series": [{"name": "SLI", "points": sli_pts},
                                             {"name": "Target", "points": target_pts}]}})

    table_rows = []
    for r, status in list(zip(results, statuses))[:top_n]:
        slo = r["slo"]
        remaining = r["remaining"]
        table_rows.append([
            _clip(slo.get("name") or "—", 60), str(slo.get("service_name") or "—"),
            _objective(slo), _sli_fmt(r["sli"], r["target"]),
            _fmt_pct(None if r["consumed"] is None else r["consumed"] * 100.0),
            _fmt_pct(None if remaining is None else max(remaining, 0.0) * 100.0), status])
    notes = [f"Each SLO is measured over its own trailing window (7, 30 or 90 days) ending "
             f"{ctx.fmt_dt(end)} {ctx.tz_label()}, not over the report period. Budget remaining "
             "is cumulative from the start of that window; days without requests are gaps, and an "
             "SLO with no requests in its window shows no data, not a full budget.",
             "Latency SLIs are estimated from the latency distribution (about 0.1% resolution). "
             "The chart shows the SLOs with the least budget left and stops at 0%; the table shows "
             "any overspend as budget consumed above 100%."]
    if custom:
        notes.append(f"{custom} custom SLO(s) are not evaluated (no data).")
    if len(slos) > MAX_SLOS:
        notes.append(f"Only the first {MAX_SLOS} of {len(slos):,} SLOs (by name) are evaluated.")
    return _section(
        sid, title, description=desc, kpis=kpis, charts=charts,
        tables=[{"title": _rows_title("Service level objectives", len(table_rows), len(results)),
                 "headers": ["SLO", "Service", "Objective", "SLI", "Budget consumed",
                             "Budget remaining", "Status"],
                 "styles": ["text", "text", "text", "num", "num", "pct-bar", "status"],
                 "rows": table_rows}],
        notes=notes)


# ─── New and regressed errors ───────────────────────────────────────────────

def _version_key(v: str) -> Optional[tuple]:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:4]) if parts else None


def _counts_as_regression(version: str, fixed: Optional[str]) -> bool:
    """Whether an occurrence in ``version`` after an issue was resolved (in
    ``fixed``) is a regression: the fixed version or a later one, or a
    version that cannot be told apart (unknown)."""
    if not fixed or not version or version == fixed:
        return True
    a, b = _version_key(version), _version_key(fixed)
    if a is None or b is None:
        return False
    return a >= b


ERROR_INCLUDE = {"both": "New and regressed", "new": "New only", "regressed": "Regressed only"}


@register_section(
    "apm_new_errors", title="New and Regressed Errors", category=CATEGORY,
    description="Error groups first seen in the period and resolved errors that came back, "
                "excluding ignored issues.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 100),
        {"key": "include", "label": "Show", "type": "select", "default": "both",
         "choices": [[k, v] for k, v in ERROR_INCLUDE.items()]},
    ],
)
async def apm_new_errors(ctx: SectionCtx) -> dict:
    sid = "apm_new_errors"
    top_n = int(ctx.opt("top_n"))
    include = ctx.opt("include") if ctx.opt("include") in ERROR_INCLUDE else "both"
    title = {"both": "New and Regressed Errors", "new": "New Errors",
             "regressed": "Regressed Errors"}[include]
    desc = {"both": "Error groups first seen in the period and resolved errors that occurred again "
                    "after they were resolved; ignored issues are excluded.",
            "new": "Error groups first seen in the period; ignored issues are excluded.",
            "regressed": "Resolved error groups that occurred again after they were resolved."}[include]
    frm, end = _naive(ctx.frm), _end(ctx)
    notes: list[str] = []
    if (wn := ctx.window_note(EXCEPTION_DAYS, "Error occurrence data")):
        notes.append(wn)
    if end <= frm:
        return _section(sid, title, description=desc, notes=["This period has not started yet."])
    lookback = frm - timedelta(days=EXCEPTION_DAYS)
    rows = await ctx.ch_rows(
        "SELECT group_id, any(etype), argMax(msg, last_in), argMax(svc, last_in), "
        "min(first_any), sum(occ) AS total_occ, max(last_in), "
        "groupArrayIf((ver, occ, last_in), occ > 0) FROM ("
        "SELECT group_id, service_version AS ver, any(exception_type) AS etype, "
        "argMax(exception_message, timestamp) AS msg, argMax(service_name, timestamp) AS svc, "
        "min(timestamp) AS first_any, countIf(timestamp >= %(f)s) AS occ, "
        "maxIf(timestamp, timestamp >= %(f)s) AS last_in "
        "FROM zenplus.apm_exceptions WHERE timestamp >= %(lb)s AND timestamp < %(t)s "
        "GROUP BY group_id, ver) "
        "GROUP BY group_id HAVING total_occ > 0 ORDER BY total_occ DESC LIMIT 2000",
        {"f": frm, "t": end, "lb": lookback})
    groups = []
    for r in rows:
        versions = []
        for v in _arr(r[7]):
            v = _arr(v)
            if len(v) >= 3:
                versions.append((str(v[0] or ""), _int(v[1]), _ts(v[2])))
        groups.append({"gid": _s(r[0]), "type": str(r[1] or "Error"), "msg": r[2],
                       "service": str(r[3] or "—"), "first": _ts(r[4]), "occ": _int(r[5]),
                       "last": _ts(r[6]), "versions": versions})

    triage: dict[str, dict] = {}
    if groups:
        res = await ctx.db.execute(text(
            "SELECT DISTINCT ON (group_id) group_id, status, resolved_in_version, updated_at "
            "FROM apm_error_issues WHERE group_id = ANY(:gids) "
            "ORDER BY group_id, updated_at DESC NULLS LAST, last_seen_at DESC NULLS LAST"),
            {"gids": [g["gid"] for g in groups]})
        triage = {str(t["group_id"]): dict(t) for t in res.mappings().all()}

    new, regressed, ignored = [], [], 0
    for g in groups:
        t = triage.get(g["gid"]) or {}
        status = str(t.get("status") or "unresolved")
        if status == "ignored":
            ignored += 1
            continue
        g["status"] = status
        if status in ("resolved", "resolved_in_version"):
            resolved_at = _naive(t.get("updated_at"))
            fixed = t.get("resolved_in_version") if status == "resolved_in_version" else None
            again = [v for v in g["versions"]
                     if v[2] is not None and (resolved_at is None or v[2] > resolved_at)
                     and _counts_as_regression(v[0], fixed)]
            if again:
                g.update(resolved_at=resolved_at, fixed=fixed,
                         again_versions=sorted({v[0] or "unknown" for v in again}),
                         again_occ=sum(v[1] for v in again))
                regressed.append(g)
                continue
        if g["first"] is not None and g["first"] >= frm:
            new.append(g)

    show_new = include in ("both", "new")
    show_reg = include in ("both", "regressed")
    listed = (new if show_new else []) + (regressed if show_reg else [])
    reach = _utcnow() - timedelta(days=EXCEPTION_DAYS)
    history_days = (frm - reach).total_seconds() / 86400.0
    if show_new and history_days < 7:
        notes.append(f"Only {max(history_days, 0):.0f} day(s) of error history before this period "
                     "are retained, so some long-standing errors may be listed as new.")
    if ignored:
        notes.append(f"{ignored} ignored error group(s) are excluded.")
    if not listed:
        empty = {"both": "No new or regressed errors in this period.",
                 "new": "No new errors in this period.",
                 "regressed": "No resolved errors occurred again in this period."}[include]
        return _section(sid, title, description=desc, notes=[empty] + notes)

    occ_total = sum(g["occ"] for g in listed)
    services = {g["service"] for g in listed}
    kpis = []
    if show_new:
        kpis.append({"label": "New error groups", "value": _count(len(new)),
                     "accent": "warning" if new else "success"})
    if show_reg:
        kpis.append({"label": "Regressed", "value": _count(len(regressed)),
                     "accent": "danger" if regressed else "success",
                     "subtitle": "Resolved, then seen again"})
    kpis += [{"label": "Occurrences", "value": _count(occ_total), "accent": "info"},
             {"label": "Services affected", "value": _count(len(services)), "accent": "neutral"}]

    charts, tables = [], []
    if show_new and new:
        axis = _axis(ctx, "1 DAY", frm, end)
        counts = {e: 0.0 for e in axis}
        for g in new:
            i = _bucket_index(axis, g["first"])
            if i is not None:
                counts[axis[i]] += 1
        charts.append({"title": "New error groups by day of first occurrence",
                       "series": {"kind": "bars", "unit": "groups", "color": "warning",
                                  "points": _axis_points(ctx, axis, counts, 0)}})
        new.sort(key=lambda g: (-g["occ"], g["first"] or frm))
        tables.append({
            "title": _rows_title("New error groups", min(top_n, len(new)), len(new)),
            "headers": ["Error", "Message", "Service", "Occurrences", "First seen", "Last seen",
                        "Triage"],
            "styles": ["text", "text", "text", "num", "text", "text", "status"],
            "rows": [[_clip(g["type"], 60), mask_message(g["msg"]), g["service"], _count(g["occ"]),
                      ctx.fmt_short(g["first"]), ctx.fmt_short(g["last"]),
                      g["status"].replace("_", " ")] for g in new[:top_n]]})
    if show_reg and regressed:
        regressed.sort(key=lambda g: (-g["again_occ"], g["service"]))

        def resolved_cell(g: dict) -> str:
            when = ctx.fmt_short(g["resolved_at"])
            return f"{when} (in {_clip(g['fixed'], 30)})" if g.get("fixed") else when

        tables.append({
            "title": _rows_title("Regressed error groups", min(top_n, len(regressed)), len(regressed)),
            "headers": ["Error", "Message", "Service", "Resolved", "Seen again in", "Occurrences",
                        "Last seen"],
            "styles": ["text", "text", "text", "text", "mono", "num", "text"],
            "rows": [[_clip(g["type"], 60), mask_message(g["msg"]), g["service"], resolved_cell(g),
                      _clip(", ".join(g["again_versions"]), 60), _count(g["occ"]),
                      ctx.fmt_short(g["last"])] for g in regressed[:top_n]]})
    method = []
    if show_new:
        method.append("A group is new when it has no occurrence before the period in the retained "
                      "error history.")
    if show_reg:
        method.append("A group has regressed when it was marked resolved and occurred again after "
                      "that (for 'resolved in version', in that version or a later one).")
    notes.insert(0, " ".join(method) + " Messages are shortened and identifying values masked.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Release health ─────────────────────────────────────────────────────────

def _floor5(dt: datetime) -> datetime:
    return _epoch_dt(int(_aware(dt).timestamp()) // BUCKET_5M * BUCKET_5M)


def _release_verdict(before: Optional[dict], after: Optional[dict]) -> str:
    if not before or not after:
        return "no data"
    d_err = after["err"] - before["err"]
    if d_err >= ERR_CRITICAL or (after["err"] >= ERR_CRITICAL and d_err >= ERR_DEGRADED):
        return "critical"
    p95_b, p95_a = before.get("p95"), after.get("p95")
    slower = (p95_b is not None and p95_a is not None and p95_b > 0
              and p95_a >= p95_b * 1.25 and p95_a - p95_b >= 50)
    if d_err >= ERR_DEGRADED or slower:
        return "degraded"
    return "healthy"


@register_section(
    "apm_release_health", title="Release Health", category=CATEGORY,
    description="Each deployment in the period with error rate and 95th-percentile latency before "
                "and after it, and a verdict on whether the release made things worse.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 50),
        {"key": "compare_hours", "label": "Compare window either side (hours)", "type": "int",
         "default": 24, "min": 1, "max": 168},
        {"key": "detect_versions", "label": "Also detect new versions from traces", "type": "bool",
         "default": True},
    ],
)
async def apm_release_health(ctx: SectionCtx) -> dict:
    sid, title = "apm_release_health", "Release Health"
    top_n, hours = int(ctx.opt("top_n")), int(ctx.opt("compare_hours"))
    detect = bool(ctx.opt("detect_versions"))
    desc = (f"Deployments in the period with error rate and 95th-percentile latency in the {hours} "
            "hour(s) before and after each one.")
    frm, end = _naive(ctx.frm), _end(ctx)
    if end <= frm:
        return _section(sid, title, description=desc, notes=["This period has not started yet."])

    res = await ctx.db.execute(text(
        "SELECT d.id::text AS id, s.name AS service, e.name AS env, d.version, d.git_sha, "
        "d.deployed_at FROM apm_deployments d JOIN apm_services s ON s.id = d.service_id "
        "LEFT JOIN apm_environments e ON e.id = COALESCE(d.env_id, s.env_id) "
        "WHERE d.deployed_at >= :f AND d.deployed_at < :t ORDER BY d.deployed_at DESC LIMIT 1000"),
        {"f": _aware(frm), "t": _aware(end)})
    deps = [{"service": str(r["service"]), "env": r.get("env") or "", "version": str(r["version"] or ""),
             "sha": str(r.get("git_sha") or ""), "at": _naive(r["deployed_at"]), "source": "Recorded"}
            for r in res.mappings().all() if r.get("deployed_at") is not None]
    notes: list[str] = []
    if detect:
        reach = _utcnow() - timedelta(days=SPAN_DAYS)
        lb = max(frm - timedelta(days=1), reach)
        first_ok = max(frm, reach + timedelta(days=1))
        if first_ok < end:
            seen = await ctx.ch_rows(
                "SELECT service_name, env, service_version, min(timestamp) AS first_seen "
                "FROM zenplus.apm_spans WHERE timestamp >= %(lb)s AND timestamp < %(t)s "
                f"AND span_kind_str IN {ENTRY_KINDS} AND service_version != '' "
                "GROUP BY service_name, env, service_version HAVING first_seen >= %(f)s "
                "ORDER BY first_seen DESC LIMIT 1000", {"lb": lb, "t": end, "f": first_ok})
            recorded = {(d["service"], d["version"]) for d in deps}
            for r in seen:
                key = (str(r[0]), str(r[2]))
                at = _ts(r[3])
                if key in recorded or at is None:
                    continue
                deps.append({"service": key[0], "env": str(r[1] or ""), "version": key[1], "sha": "",
                             "at": at, "source": "Detected"})
        if frm < reach + timedelta(days=1):
            notes.append(f"Versions are detected from traces, which are kept for {SPAN_DAYS} days: "
                         "only versions first seen from "
                         f"{ctx.fmt_date(reach + timedelta(days=1))} onwards are detected.")
    if not deps:
        return _section(sid, title, description=desc,
                        notes=["No deployments were recorded or detected in this period."] + notes)

    # Neighbouring deployments of the same service bound each comparison.
    by_service: dict[tuple, list[datetime]] = {}
    for d in deps:
        by_service.setdefault((d["service"], d["env"]), []).append(_floor5(d["at"]))
    for v in by_service.values():
        v.sort()
    deps.sort(key=lambda d: d["at"], reverse=True)
    listed = deps[:top_n]
    now = _utcnow()
    span = timedelta(hours=hours)
    rows_out, verdicts = [], []
    for d in listed:
        d0 = _floor5(d["at"])
        times = by_service[(d["service"], d["env"])]
        prev = [t for t in times if t < d0]
        nxt = [t for t in times if t > d0]
        b0 = max(d0 - span, (prev[-1] + timedelta(minutes=5)) if prev else d0 - span)
        a1 = min(d0 + timedelta(minutes=5) + span, nxt[0] if nxt else now, now)
        params = {"svc": d["service"], "d0": d0, "b0": b0, "a1": a1}
        env_sql = ""
        if d["env"]:
            env_sql = " AND env = %(env)s"
            params["env"] = d["env"]
        phases = await ctx.ch_rows(
            "SELECT timestamp > %(d0)s AS is_after, sum(request_count), sum(error_count), "
            "quantilesTDigestMerge(0.95)(duration_state) FROM zenplus.apm_span_metrics_5m "
            f"WHERE service_name = %(svc)s{env_sql} AND span_kind IN {ENTRY_KINDS} "
            "AND timestamp >= %(b0)s AND timestamp < %(a1)s AND timestamp != %(d0)s "
            "GROUP BY is_after", params)
        side: dict[bool, dict] = {}
        for p in phases:
            reqs = _int(p[1])
            if reqs > 0:
                qs = _arr(p[3])
                side[bool(_int(p[0]))] = {"reqs": reqs, "err": _err_pct(p[2], reqs),
                                          "p95": _num(qs[0]) if qs else None}
        before, after = side.get(False), side.get(True)
        verdict = _release_verdict(before, after)
        verdicts.append(verdict)

        def arrow(fmt, key):
            b = fmt(before[key]) if before else "—"
            a = fmt(after[key]) if after else "—"
            return f"{b} → {a}"

        version = _clip(d["version"] or "(unset)", 40)
        if d["sha"]:
            version += f" ({d['sha'][:7]})"
        rows_out.append([d["service"], version, ctx.fmt_short(d["at"]), d["source"],
                         arrow(_fmt_pct, "err"), arrow(_ms, "p95"),
                         _count(after["reqs"]) if after else "—", verdict])

    n_bad = sum(1 for v in verdicts if v in ("degraded", "critical"))
    n_nodata = verdicts.count("no data")
    kpis = [
        {"label": "Deployments", "value": _count(len(deps)), "accent": "primary",
         "subtitle": f"{sum(1 for d in deps if d['source'] == 'Recorded'):,} recorded"},
        {"label": "Services deployed", "value": _count(len({d['service'] for d in deps})),
         "accent": "neutral"},
        {"label": "Releases that degraded", "value": _count(n_bad),
         "accent": "danger" if n_bad else "success",
         "subtitle": f"of {len(listed):,} compared"},
        {"label": "Not enough traffic to compare", "value": _count(n_nodata), "accent": "neutral",
         "subtitle": "Not measured"},
    ]
    axis = _axis(ctx, "1 DAY", frm, end)
    per_day = {e: 0.0 for e in axis}
    for d in deps:
        i = _bucket_index(axis, d["at"])
        if i is not None:
            per_day[axis[i]] += 1
    notes.insert(0, f"Each release is compared over up to {hours} hour(s) either side of the "
                    "deployment, cut short at the neighbouring deployment of the same service; the "
                    "5-minute interval containing the deployment is left out. Degraded: error rate "
                    "up 1 point or more, or P95 latency up 25% and at least 50 ms; critical: error "
                    "rate up 5 points or more. Recorded deployments come from deployment markers; "
                    "detected ones are versions first seen in traces.")
    if (wn := ctx.window_note(ROLLUP_5M_DAYS, "The 5-minute APM rollup")):
        notes.append(wn)
    return _section(
        sid, title, description=desc, kpis=kpis,
        charts=[{"title": "Deployments per day",
                 "series": {"kind": "bars", "unit": "deployments", "color": "primary",
                            "points": _axis_points(ctx, axis, per_day, 0)}}],
        tables=[{"title": _rows_title("Deployments, most recent first", len(listed), len(deps)),
                 "headers": ["Service", "Version", "Deployed", "Source", "Error rate",
                             "P95 latency", "Requests after", "Verdict"],
                 "styles": ["text", "mono", "text", "text", "num", "num", "num", "status"],
                 "rows": rows_out}],
        notes=notes)


# ─── Database statements ────────────────────────────────────────────────────

DB_SORTS = {
    "total_time": ("total_ms", "Total Time"),
    "calls": ("calls", "Calls"),
    "p95": ("p95", "95th-Percentile Latency"),
    "error_rate": ("errs / calls", "Error Rate"),
}


@register_section(
    "apm_db_queries", title="Top Database Queries", category=CATEGORY,
    description="Database statements issued by instrumented services, ranked by total time, "
                "calls, 95th-percentile latency or error rate (statement text normalised and "
                "masked).",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(15, 100),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "total_time",
         "choices": [[k, v[1].capitalize() if k != "p95" else v[1]] for k, v in DB_SORTS.items()]},
    ],
)
async def apm_db_queries(ctx: SectionCtx) -> dict:
    sid = "apm_db_queries"
    top_n = int(ctx.opt("top_n"))
    sort = ctx.opt("sort") if ctx.opt("sort") in DB_SORTS else "total_time"
    order, sort_label = DB_SORTS[sort]
    title = f"Top {top_n} Database Queries by {sort_label}"
    desc = (f"Top {top_n} database statements by {sort_label.lower()}, with calls, latency, share "
            "of database time and error rate.")
    end = _end(ctx)
    start = max(_naive(ctx.frm), _utcnow() - timedelta(days=SPAN_DAYS))
    notes = ["Statements are normalised (literal values replaced by ?), masked and shortened to "
             f"{TEXT_LIMIT} characters; full statement text is never included in reports. Measured "
             "from stored trace spans that carry a database statement. Share is the statement's "
             "part of all database time; errors are the share of its calls that failed."]
    if (wn := ctx.window_note(SPAN_DAYS, "Trace span data")):
        notes.append(wn)
    if end <= start:
        return _section(sid, title, description=desc,
                        notes=["No trace data is retained for this period."] + notes)
    where = ("FROM zenplus.apm_spans WHERE timestamp >= %(f)s AND timestamp < %(t)s "
             "AND db_statement != ''")
    params = {"f": start, "t": end, "lim": top_n}
    tot = await ctx.ch_rows(
        f"SELECT count(), countIf(has_error = 1), sum(duration_nano) / 1e6, "
        f"uniqExact(normalizedQueryHash(db_statement)) {where}", params)
    rows = await ctx.ch_rows(
        "SELECT normalizedQueryHash(db_statement) AS h, normalizeQuery(any(db_statement)), "
        "any(db_system), any(db_operation), groupUniqArray(3)(service_name), "
        "uniqExact(service_name), count() AS calls, countIf(has_error = 1) AS errs, "
        "quantileTDigest(0.95)(duration_nano / 1e6) AS p95, sum(duration_nano) / 1e6 AS total_ms "
        f"{where} GROUP BY h ORDER BY {order} DESC, calls DESC LIMIT %(lim)s", params)
    t = tot[0] if tot else (0, 0, 0, 0)
    calls_all, time_all, distinct = _int(t[0]), _num(t[2]) or 0.0, _int(t[3])
    if not rows or not calls_all:
        return _section(sid, title, description=desc,
                        notes=["No database calls were recorded in traces in this period."] + notes)
    err_all = _err_pct(t[1], calls_all)
    table_rows, rank_pts = [], []
    for r in rows:
        calls, total_ms = _int(r[6]), _num(r[9]) or 0.0
        err = _err_pct(r[7], calls)
        p95 = _num(r[8])
        share = total_ms / time_all * 100.0 if time_all else None
        stmt = mask_statement(r[1])
        system = str(r[2] or "").strip() or "database"
        svcs = [str(x) for x in _arr(r[4]) if x]
        more = _int(r[5]) - len(svcs)
        source = (f"{system} from {', '.join(svcs)}" + (f" +{more}" if more > 0 else "")
                  if svcs else system)
        table_rows.append([stmt, source, _count(calls), _ms(p95), _total_time(total_ms),
                           _fmt_pct(share), _fmt_pct(err)])
        value = {"total_time": share, "calls": calls, "p95": p95, "error_rate": err}[sort]
        rank_pts.append({"label": _clip(stmt, 48), "value": round(value or 0, 2)})
    top_share = (_num(rows[0][9]) or 0.0) / time_all * 100.0 if time_all and sort == "total_time" else None
    kpis = [
        {"label": "Database calls", "value": _count(calls_all), "accent": "primary"},
        {"label": "Distinct statements", "value": _count(distinct), "accent": "neutral"},
        {"label": "Database time", "value": _total_time(time_all), "accent": "info"},
        {"label": "Error rate", "value": _fmt_pct(err_all), "accent": _err_accent(err_all),
         "subtitle": f"{_count(t[1])} failed calls"},
    ]
    if top_share is not None:
        kpis.append({"label": "Top statement share", "value": _fmt_pct(top_share),
                     "accent": "warning" if top_share >= 50 else "neutral",
                     "subtitle": "Of all database time"})
    chart_unit = {"total_time": "%", "calls": "calls", "p95": "ms", "error_rate": "%"}[sort]
    chart_title = {"total_time": "Share of database time (%)", "calls": "Calls",
                   "p95": "95th-percentile latency (ms)", "error_rate": "Error rate (%)"}[sort]
    return _section(
        sid, title, description=desc, kpis=kpis,
        charts=[{"title": chart_title,
                 "series": {"kind": "rank", "unit": chart_unit, "color": "primary",
                            "points": rank_pts[:10]}}],
        tables=[{"title": _rows_title("Statements", len(rows), distinct),
                 "headers": ["Statement", "Source", "Calls", "P95", "Total time",
                             "Share", "Errors"],
                 "styles": ["mono", "text", "num", "num", "num", "pct-bar", "num"],
                 "rows": table_rows}],
        notes=notes)


# ─── Telemetry ingest quality ───────────────────────────────────────────────

DAILY_TABLE_ROWS = 31


@register_section(
    "apm_data_quality", title="Telemetry Ingest Quality", category=CATEGORY,
    description="Spans accepted, rejected (including clock skew) and dropped by the trace intake "
                "per day, and services that have stopped reporting as of report generation.",
    scope="fleet", requires=REQUIRES,
    options=[
        top_n_option(20, 200),
        {"key": "silent_hours", "label": "Silent for at least (hours)", "type": "int",
         "default": 24, "min": 1, "max": 720},
    ],
)
async def apm_data_quality(ctx: SectionCtx) -> dict:
    sid, title = "apm_data_quality", "Telemetry Ingest Quality"
    top_n, silent_h = int(ctx.opt("top_n")), int(ctx.opt("silent_hours"))
    interval = _day_or_hour(ctx)
    per = "day" if interval == "1 DAY" else "hour"
    desc = (f"Spans accepted, rejected and dropped by the trace intake per {per}, and services "
            f"silent for {silent_h} hour(s) or more as of report generation.")
    frm, end = _naive(ctx.frm), _end(ctx)
    notes = ["Accepted spans were written to storage; rejected spans failed validation, including "
             "spans whose timestamps were outside the accepted clock-skew range; dropped spans were "
             "accepted but lost when a storage write failed."]
    if (wn := ctx.window_note(INGEST_DAYS, "Ingest counter data")):
        notes.append(wn)

    rows: list = []
    if end > frm:
        bucket, bparams = ctx.ch_bucket("timestamp", interval)
        rows = await ctx.ch_rows(
            f"SELECT toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS b, sum(accepted), "
            "sum(rejected), sum(dropped), sum(skewed) FROM zenplus.apm_ingest_stats "
            "WHERE timestamp >= %(f)s AND timestamp < %(t)s GROUP BY b ORDER BY b",
            {"f": frm, "t": end, **bparams})
    res = await ctx.db.execute(text(
        "SELECT s.name, e.name AS env, s.health, s.last_seen_at FROM apm_services s "
        "LEFT JOIN apm_environments e ON e.id = s.env_id "
        "ORDER BY s.last_seen_at ASC NULLS FIRST, s.name"))
    services = [dict(r) for r in res.mappings().all()]

    by_bucket = {_int(r[0]): (_int(r[1]), _int(r[2]), _int(r[3]), _int(r[4])) for r in rows}
    acc = sum(v[0] for v in by_bucket.values())
    rej = sum(v[1] for v in by_bucket.values())
    drop = sum(v[2] for v in by_bucket.values())
    skew = sum(v[3] for v in by_bucket.values())
    received = acc + rej + drop
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=silent_h)
    silent = [s for s in services if _aware(s.get("last_seen_at")) is None
              or _aware(s.get("last_seen_at")) < cutoff]
    if not by_bucket and not services:
        return _section(sid, title, description=desc,
                        notes=["No spans were received and no instrumented services are "
                               "registered."] + notes[1:])

    rej_pct = rej / received * 100.0 if received else None
    kpis = [
        {"label": "Spans accepted", "value": _count(acc) if by_bucket else "—", "accent": "primary",
         "subtitle": None if by_bucket else "Not measured"},
        {"label": "Rejected", "value": _count(rej) if by_bucket else "—",
         "accent": "warning" if rej else "success",
         "subtitle": (f"{_fmt_pct(rej_pct)} of received · {skew:,} clock skew"
                      if by_bucket else "Not measured")},
        {"label": "Dropped on write", "value": _count(drop) if by_bucket else "—",
         "accent": "danger" if drop else "success"},
        {"label": "Silent services", "value": f"{len(silent):,} of {len(services):,}",
         "accent": "warning" if silent else "success",
         "subtitle": f"No spans for {silent_h} h or more"},
    ]
    kpis = [{k: v for k, v in kp.items() if v is not None} for kp in kpis]

    charts, tables = [], []
    if by_bucket:
        axis = _axis(ctx, interval, frm, end)
        axis_set = set(axis)
        extra = sorted(e for e in by_bucket if e not in axis_set)
        axis = sorted(axis + extra)
        accepted_vals = {e: (by_bucket[e][0] if e in by_bucket else None) for e in axis}
        charts.append({"title": f"Spans accepted per {per}",
                       "series": {"kind": "bars", "unit": "spans", "color": "primary",
                                  "points": _axis_points(ctx, axis, accepted_vals, 0)}})
        if rej or drop:
            def pts(fn):
                return _axis_points(ctx, axis, {e: (fn(by_bucket[e]) if e in by_bucket else None)
                                                for e in axis}, 0)
            skew_pts = pts(lambda v: v[3])
            charts.append({"title": f"Rejected and dropped spans per {per}",
                           "series": {"kind": "stacked", "unit": "spans", "points": skew_pts,
                                      "series": [{"name": "Rejected — clock skew", "points": skew_pts},
                                                 {"name": "Rejected — other",
                                                  "points": pts(lambda v: max(v[1] - v[3], 0))},
                                                 {"name": "Dropped", "points": pts(lambda v: v[2])}]}})
        shown = axis[-DAILY_TABLE_ROWS:]
        trows = []
        for e in reversed(shown):
            v = by_bucket.get(e)
            if v is None:
                trows.append([_bucket_label(ctx, interval, e), "—", "—", "—", "—", "—"])
                continue
            recv = v[0] + v[1] + v[2]
            trows.append([_bucket_label(ctx, interval, e), _count(v[0]), _count(v[1]), _count(v[3]),
                          _count(v[2]), _fmt_pct(v[1] / recv * 100.0 if recv else None)])
        label = "Day" if per == "day" else "Hour"
        tables.append({"title": (f"Ingest by {per}" if len(axis) <= DAILY_TABLE_ROWS else
                                 f"Ingest by {per} (most recent {len(shown)} of {len(axis)})"),
                       "headers": [label, "Accepted", "Rejected", "Clock skew", "Dropped",
                                   "Rejected %"],
                       "styles": ["text", "num", "num", "num", "num", "num"], "rows": trows})
        notes.append(f"A {per} with no counters received no spans (or the intake was not running) "
                     "and is shown as not measured.")
    else:
        notes.insert(0, "No ingest counters were recorded in this period (not measured).")
    if silent:
        trows = []
        for s in silent[:top_n]:
            seen = _aware(s.get("last_seen_at"))
            trows.append([str(s.get("name") or "—"), str(s.get("env") or "—"),
                          ctx.fmt_short(seen) if seen else "Never",
                          _duration((now - seen).total_seconds()) if seen else "—",
                          str(s.get("health") or "no_data").replace("_", " ")])
        tables.append({"title": _rows_title(f"Services silent for {silent_h} hour(s) or more",
                                            min(top_n, len(silent)), len(silent)),
                       "headers": ["Service", "Environment", "Last seen", "Silent for", "Health"],
                       "styles": ["text", "text", "text", "num", "status"], "rows": trows})
    elif services:
        notes.append(f"Every registered service sent spans within the last {silent_h} hour(s).")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Instrumentation coverage ───────────────────────────────────────────────

RUNTIME_LABELS = {"dotnet": ".NET", "dotnet_framework": ".NET Framework", "java": "Java",
                  "node": "Node.js", "python": "Python", "iis": "IIS", "other": "Other"}
STATE_LABELS = {"active": "Instrumented", "pending": "Pending", "failed": "Failed",
                "none": "Not instrumented", "unsupported": "Unsupported"}
STATE_ORDER = ("active", "pending", "failed", "none", "unsupported")


def _process_label(name: Any, exe_path: Any) -> str:
    n = str(name or "").strip()
    if n:
        return _clip(n, 60)
    exe = re.split(r"[\\/]", str(exe_path or "").strip())[-1]
    return _clip(exe, 60) or "—"


@register_section(
    "apm_instrumentation_coverage", title="Instrumentation Coverage", category=CATEGORY,
    description="Application processes discovered by server agents by runtime and instrumentation "
                "state, and the ones that failed or are not yet instrumented, as of report "
                "generation.",
    scope="fleet", requires=("apm.view", "servers.view"), semantics="snapshot",
    options=[
        top_n_option(20, 200),
        {"key": "active_days", "label": "Seen within (days)", "type": "int", "default": 7,
         "min": 1, "max": 90},
    ],
)
async def apm_instrumentation_coverage(ctx: SectionCtx) -> dict:
    sid, title = "apm_instrumentation_coverage", "Instrumentation Coverage"
    top_n, days = int(ctx.opt("top_n")), int(ctx.opt("active_days"))
    desc = (f"Application processes seen by server agents in the {days} day(s) before the report "
            "was generated, by runtime and instrumentation state.")
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = ("FROM apm_agent_processes p JOIN servers s ON s.id = p.server_id "
            "WHERE p.last_seen_at >= :since AND s.status <> 'disabled'")
    res = await ctx.db.execute(text(
        "SELECT p.runtime, p.instrumentation_state AS state, COUNT(*) AS n, "
        f"COUNT(*) FILTER (WHERE p.otel_detected) AS otel {base} "
        "GROUP BY p.runtime, p.instrumentation_state"), {"since": since})
    matrix = [dict(r) for r in res.mappings().all()]
    if not matrix:
        return _section(sid, title, description=desc,
                        notes=[f"No application processes were reported by server agents in the "
                               f"last {days} day(s)."])
    res = await ctx.db.execute(text(
        f"SELECT COUNT(DISTINCT p.server_id) AS servers {base}"), {"since": since})
    srv = res.mappings().first() or {}
    res = await ctx.db.execute(text(
        "SELECT COALESCE(NULLIF(s.display_name, ''), NULLIF(s.hostname, ''), s.id::text) AS server, "
        "p.runtime, p.runtime_version, COALESCE(NULLIF(p.service_name_guess, ''), "
        "NULLIF(p.iis_site, ''), NULLIF(p.windows_service, ''), '') AS name, p.exe_path, "
        f"p.instrumentation_state AS state, p.otel_detected, p.last_seen_at {base} "
        "AND p.instrumentation_state IN ('failed', 'pending', 'none') AND p.runtime <> 'other' "
        "ORDER BY CASE p.instrumentation_state WHEN 'failed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
        "p.last_seen_at DESC LIMIT :lim"), {"since": since, "lim": top_n})
    attention = [dict(r) for r in res.mappings().all()]

    per_runtime: dict[str, dict[str, int]] = {}
    otel_rt: dict[str, int] = {}
    by_state = {s: 0 for s in STATE_ORDER}
    for m in matrix:
        rt_, st = str(m.get("runtime") or "other"), str(m.get("state") or "none")
        n = _int(m.get("n"))
        per_runtime.setdefault(rt_, {s: 0 for s in STATE_ORDER})
        per_runtime[rt_][st] = per_runtime[rt_].get(st, 0) + n
        by_state[st] = by_state.get(st, 0) + n
        otel_rt[rt_] = otel_rt.get(rt_, 0) + _int(m.get("otel"))
    total = sum(by_state.values())

    def instrumentable(c: dict, rt_: str) -> int:
        """Processes whose runtime supports instrumentation ('other' counts
        only the processes that were instrumented anyway)."""
        if rt_ == "other":
            return c.get("active", 0)
        return sum(c.values()) - c.get("unsupported", 0)

    denom_all = sum(instrumentable(c, rt_) for rt_, c in per_runtime.items())
    coverage = by_state["active"] / denom_all * 100.0 if denom_all > 0 else None
    need = sum(c[s] for rt_, c in per_runtime.items() if rt_ != "other"
               for s in ("failed", "pending", "none"))

    def cov(c: dict, rt_: str) -> Optional[float]:
        denom = instrumentable(c, rt_)
        return c.get("active", 0) / denom * 100.0 if denom > 0 and rt_ != "other" else None

    order = sorted(per_runtime, key=lambda k: (k == "other", -sum(per_runtime[k].values()), k))
    matrix_rows = []
    for rt_ in order:
        c = per_runtime[rt_]
        matrix_rows.append([RUNTIME_LABELS.get(rt_, rt_), _count(sum(c.values())),
                            _count(c["active"]), _count(c["pending"]), _count(c["failed"]),
                            _count(c["none"]), _count(otel_rt.get(rt_, 0)),
                            _fmt_pct(cov(c, rt_), 1)])
    kpis = [
        {"label": "Processes", "value": _count(total), "accent": "primary",
         "subtitle": f"On {_count(srv.get('servers'))} server(s)"},
        {"label": "Instrumented", "value": _count(by_state["active"]), "accent": "success",
         "subtitle": f"{_fmt_pct(coverage, 1)} coverage" if coverage is not None else "Not measured"},
        {"label": "Failed", "value": _count(by_state["failed"]),
         "accent": "danger" if by_state["failed"] else "success"},
        {"label": "Pending", "value": _count(by_state["pending"]),
         "accent": "warning" if by_state["pending"] else "neutral"},
        {"label": "OTel present", "value": _count(sum(otel_rt.values())),
         "accent": "info", "subtitle": "Already load an OpenTelemetry SDK"},
    ]
    tables = [{"title": "Processes by runtime and instrumentation state",
               "headers": ["Runtime", "Processes", "Instrumented", "Pending", "Failed",
                           "Uninstrumented", "OTel present", "Coverage"],
               "styles": ["text", "num", "num", "num", "num", "num", "num", "pct-bar"],
               "rows": matrix_rows}]
    if attention:
        rows = []
        for a in attention:
            rt_ = RUNTIME_LABELS.get(str(a.get("runtime") or "other"), str(a.get("runtime")))
            ver = str(a.get("runtime_version") or "").strip()
            rows.append([str(a.get("server") or "—"), _process_label(a.get("name"), a.get("exe_path")),
                         f"{rt_} {_clip(ver, 20)}".strip(),
                         STATE_LABELS.get(str(a.get("state")), str(a.get("state"))).lower(),
                         "yes" if a.get("otel_detected") else "no",
                         ctx.fmt_short(a.get("last_seen_at"))])
        tables.append({"title": _rows_title("Processes that failed, are pending or are not "
                                            "instrumented", len(rows), need),
                       "headers": ["Server", "Process", "Runtime", "State", "OTel present",
                                   "Last seen"],
                       "styles": ["text", "text", "text", "status", "text", "text"],
                       "rows": rows})
    notes = [f"Processes seen by server agents in the last {days} day(s) as of report generation; "
             "processes on disabled servers are excluded. Coverage is instrumented processes "
             "divided by processes whose runtime supports instrumentation (runtime 'Other' and "
             "unsupported processes are not counted against it). Command lines are never "
             "included."]
    if by_state.get("unsupported"):
        notes.append(f"{by_state['unsupported']:,} process(es) run a runtime version that cannot "
                     "be instrumented (unsupported).")
    return _section(
        sid, title, description=desc, kpis=kpis,
        charts=[{"title": "Processes by instrumentation state",
                 "series": {"kind": "donut", "unit": "processes",
                            "points": [{"label": STATE_LABELS[s], "value": by_state[s]}
                                       for s in STATE_ORDER if by_state.get(s)]}}],
        tables=tables, notes=notes)


# ─── Synthetic journey failures ─────────────────────────────────────────────

@register_section(
    "synthetics_failures", title="Synthetic Journey Failures", category=CATEGORY,
    description="Success rate of scripted user journeys per day, the scenarios with the most "
                "failed runs, and the steps and errors behind them.",
    scope="fleet", requires=REQUIRES, options=[top_n_option(10, 100)],
)
async def synthetics_failures(ctx: SectionCtx) -> dict:
    import uuid as _uuid

    sid, title = "synthetics_failures", "Synthetic Journey Failures"
    top_n = int(ctx.opt("top_n"))
    interval = _day_or_hour(ctx)
    per = "day" if interval == "1 DAY" else "hour"
    desc = (f"Success rate of scripted user journeys per {per}, the scenarios with the most failed "
            "runs, and the failing steps and errors.")
    res = await ctx.db.execute(text(
        "SELECT id::text AS id, name, monitor_type, enabled FROM apm_synthetic_monitors "
        "ORDER BY name"))
    monitors = {str(m["id"]): dict(m) for m in res.mappings().all()}
    if not monitors:
        return _section(sid, title, description=desc, notes=["No synthetic scenarios are configured."])
    notes = ["Each run is counted after the scenario's configured retries, so a failed run is a "
             "confirmed failure. Error text is shortened and identifying values masked."]
    if (wn := ctx.window_note(SYNTHETIC_DAYS, "Synthetic run history")):
        notes.append(wn)
    frm, end = _naive(ctx.frm), _end(ctx)
    if end <= frm:
        return _section(sid, title, description=desc, notes=["This period has not started yet."])
    ids = [_uuid.UUID(i) for i in monitors]
    where = ("FROM zenplus.apm_synthetic_results WHERE monitor_id IN %(ids)s "
             "AND timestamp >= %(f)s AND timestamp < %(t)s")
    params = {"ids": ids, "f": frm, "t": end}
    bucket, bparams = ctx.ch_bucket("timestamp", interval)
    daily = await ctx.ch_rows(
        f"SELECT toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS b, count(), "
        f"countIf(success = 1) {where} GROUP BY b ORDER BY b", {**params, **bparams})
    per_mon = await ctx.ch_rows(
        "SELECT monitor_id, count() AS runs, countIf(success = 0) AS failed, avg(total_ms), "
        f"maxIf(timestamp, success = 0) {where} GROUP BY monitor_id ORDER BY failed DESC, runs DESC",
        params)
    runs = sum(_int(r[1]) for r in per_mon)
    if not runs:
        return _section(sid, title, description=desc,
                        notes=["No synthetic runs were recorded in this period."] + notes[1:])
    failed = sum(_int(r[2]) for r in per_mon)
    success = (runs - failed) / runs * 100.0
    failing = [r for r in per_mon if _int(r[2]) > 0]
    kpis = [
        {"label": "Runs", "value": _count(runs), "accent": "primary",
         "subtitle": f"{len(per_mon):,} of {len(monitors):,} scenario(s) ran"},
        {"label": "Success rate", "value": _fmt_pct(success),
         "accent": "success" if success >= 99 else "warning" if success >= 95 else "danger"},
        {"label": "Failed runs", "value": _count(failed), "accent": "danger" if failed else "success"},
        {"label": "Scenarios with failures", "value": f"{len(failing):,} of {len(per_mon):,}",
         "accent": "warning" if failing else "success"},
    ]
    axis = _axis(ctx, interval, frm, end)
    by_b = {_int(r[0]): (_int(r[1]), _int(r[2])) for r in daily}
    axis = sorted(set(axis) | set(by_b))
    rate = {e: (by_b[e][1] / by_b[e][0] * 100.0 if e in by_b and by_b[e][0] else None) for e in axis}
    charts = [{"title": f"Success rate per {per} (%)",
               "series": {"kind": "bars", "unit": "%", "color": "success", "y_domain": [0, 100],
                          "points": _axis_points(ctx, axis, rate)}}]
    if not failing:
        notes.insert(0, "Every synthetic run in this period succeeded.")
        return _section(sid, title, description=desc, kpis=kpis, charts=charts, notes=notes)

    def name_of(mid: Any) -> str:
        m = monitors.get(str(mid))
        return _clip(m["name"], 60) if m else "—"

    charts.append({"title": "Failed runs by scenario",
                   "series": {"kind": "rank", "unit": "runs", "color": "danger",
                              "points": [{"label": name_of(r[0]), "value": _int(r[2])}
                                         for r in failing[:10]]}})
    scen_rows = []
    for r in failing[:top_n]:
        m = monitors.get(str(r[0])) or {}
        n_runs, n_fail = _int(r[1]), _int(r[2])
        scen_rows.append([name_of(r[0]), str(m.get("monitor_type") or "api").upper(), _count(n_runs),
                          _count(n_fail), _fmt_pct((n_runs - n_fail) / n_runs * 100.0 if n_runs else None),
                          _ms(_num(r[3])), ctx.fmt_short(_ts(r[4]))])
    steps = await ctx.ch_rows(
        f"SELECT monitor_id, failed_step, count() AS n {where} AND success = 0 "
        "GROUP BY monitor_id, failed_step ORDER BY n DESC LIMIT %(lim)s", {**params, "lim": top_n})
    errors = await ctx.ch_rows(
        f"SELECT error, count() AS n, uniqExact(monitor_id), max(timestamp) {where} "
        "AND success = 0 GROUP BY error ORDER BY n DESC LIMIT 200", params)
    fails_by_mon = {str(r[0]): _int(r[2]) for r in failing}
    step_rows = [[name_of(r[0]), _clip(r[1] or "(scenario)", 60), _count(r[2]),
                  _fmt_pct(_int(r[2]) / fails_by_mon[str(r[0])] * 100.0
                           if fails_by_mon.get(str(r[0])) else None)] for r in steps]
    merged: dict[str, list] = {}
    for r in errors:
        key = mask_message(r[0]) if str(r[0] or "").strip() else "(no error text)"
        slot = merged.setdefault(key, [0, 0, None])
        slot[0] += _int(r[1])
        slot[1] = max(slot[1], _int(r[2]))
        last = _ts(r[3])
        if last and (slot[2] is None or last > slot[2]):
            slot[2] = last
    err_rows = [[k, _count(v[0]), _count(v[1]), ctx.fmt_short(v[2])]
                for k, v in sorted(merged.items(), key=lambda kv: -kv[1][0])[:top_n]]
    tables = [{"title": _rows_title("Scenarios with failed runs", len(scen_rows), len(failing)),
               "headers": ["Scenario", "Type", "Runs", "Failed", "Success rate", "Avg duration",
                           "Last failure"],
               "styles": ["text", "text", "num", "num", "pct-bar", "num", "text"],
               "rows": scen_rows}]
    if step_rows:
        tables.append({"title": "Most frequent failing steps",
                       "headers": ["Scenario", "Step", "Failures", "Share of scenario failures"],
                       "styles": ["text", "text", "num", "pct-bar"], "rows": step_rows})
    if err_rows:
        tables.append({"title": "Most frequent errors",
                       "headers": ["Error", "Failures", "Scenarios", "Last seen"],
                       "styles": ["text", "num", "num", "text"], "rows": err_rows})
    passing = len(per_mon) - len(failing)
    if passing:
        notes.append(f"{passing} scenario(s) ran without a failed run.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "apm_latency_throughput", "Application Latency and Throughput",
    "Response time (median and 95th percentile), throughput and error rate of every instrumented "
    "service, with the operations that consume the most server time.",
    "applications",
    [S("apm_latency_trend"), S("apm_services"),
     S("apm_slowest_operations", None, top_n=10, sort="total_time")],
    audience=("Application owner", "Systems engineer"), tags=("apm", "latency", "throughput"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)
template(
    "apm_slowest_operations_review", "Slowest Operations (95th Percentile)",
    "The inbound operations with the highest 95th-percentile latency and those that consume the "
    "most total server time, with their error rates.",
    "applications",
    [S("apm_slowest_operations", "Top 25 Operations by 95th-Percentile Latency",
       top_n=25, sort="p95", min_calls=20),
     S("apm_slowest_operations", "Top 25 Operations by Total Time", top_n=25, sort="total_time")],
    audience=("Application owner", "Systems engineer"), tags=("apm", "latency", "endpoints"),
    default_period="last_7d", frequency="weekly", icon="Gauge",
)
template(
    "apm_service_dependency_review", "Service Dependencies",
    "Calls between instrumented services: the busiest dependencies and those with the highest "
    "error rate and latency.",
    "applications",
    [S("apm_service_dependencies", "Busiest Service Dependencies", top_n=25, sort="calls"),
     S("apm_service_dependencies", "Service Dependencies by Error Rate", top_n=10,
       sort="error_rate"),
     S("apm_service_dependencies", "Slowest Service Dependencies", top_n=10, sort="latency")],
    audience=("Application owner", "Systems engineer"), tags=("apm", "service map", "dependencies"),
    default_period="last_7d", frequency="weekly", icon="Network",
)
template(
    "apm_slo_error_budget", "SLO Error Budget Burn",
    "Service level objectives against target: error budget remaining over each SLO's own window, "
    "daily SLI, and the latency and releases behind any burn.",
    "applications",
    [S("apm_slo_budget_trend"), S("apm_latency_trend"),
     S("apm_release_health", None, top_n=10)],
    audience=("Service owner", "Application owner", "IT manager"), tags=("slo", "error budget", "sla"),
    default_period="last_30d", frequency="weekly", icon="TrendingUp", featured=True,
)
template(
    "apm_new_regressed_errors", "New and Regressed Errors",
    "Error groups first seen in the period and resolved errors that came back, with the most "
    "frequent application errors.",
    "applications",
    [S("apm_new_errors"), S("apm_errors")],
    audience=("Application owner",), tags=("apm", "errors", "exceptions"),
    default_period="last_7d", frequency="weekly", icon="Bell",
)
template(
    "apm_release_health_review", "Release Health",
    "Every deployment in the period with error rate and latency before and after it, the new "
    "errors it introduced, and the overall latency trend.",
    "applications",
    [S("apm_release_health"), S("apm_new_errors", "New Errors", include="new"),
     S("apm_latency_trend")],
    audience=("Application owner", "Service owner"), tags=("apm", "deployments", "releases"),
    default_period="last_7d", frequency="weekly", icon="Layers",
)
template(
    "apm_top_database_queries", "Top Database Queries",
    "Database statements issued by instrumented services that consume the most time, and the "
    "slowest by 95th-percentile latency (statement text normalised and masked).",
    "applications",
    [S("apm_db_queries", "Top 25 Database Queries by Total Time", top_n=25, sort="total_time"),
     S("apm_db_queries", "Slowest Database Queries (95th Percentile)", top_n=10, sort="p95")],
    audience=("Application owner", "Systems engineer"), tags=("apm", "database", "sql"),
    default_period="last_7d", frequency="weekly", icon="HardDrive",
)
template(
    "apm_telemetry_ingest_quality", "Telemetry Ingest Quality",
    "Spans accepted, rejected and dropped by the trace intake, services that stopped reporting, "
    "and how many application processes are instrumented.",
    "applications",
    [S("apm_data_quality"), S("apm_instrumentation_coverage", None, top_n=10)],
    audience=("Systems engineer", "Application owner"), tags=("apm", "data quality", "telemetry"),
    default_period="last_7d", frequency="weekly", icon="FileCheck2",
)
template(
    "apm_instrumentation_coverage_report", "Instrumentation Coverage",
    "Application processes discovered by server agents by runtime and instrumentation state, the "
    "ones that failed or are not yet instrumented, the services reporting traces and the health "
    "of the agents that discover them.",
    "applications",
    [S("apm_instrumentation_coverage", None, top_n=50), S("apm_services"),
     S("agent_fleet_health")],
    audience=("Systems engineer", "IT manager"), tags=("apm", "agents", "coverage"),
    default_period="last_30d", frequency="monthly", icon="Cpu",
)
template(
    "apm_synthetic_journey_failures", "Synthetic Journey Failures",
    "Success rate of scripted user journeys, the scenarios with the most failed runs, their "
    "failing steps and errors, and every scenario's uptime.",
    "applications",
    [S("synthetics_failures"), S("synthetics")],
    audience=("Application owner", "NOC operator", "Service owner"),
    tags=("synthetics", "user journeys", "uptime"),
    default_period="last_7d", frequency="weekly", icon="Globe",
)
