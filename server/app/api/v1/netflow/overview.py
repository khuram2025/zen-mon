"""NetFlow overview endpoints (WP B1): headline totals, the throughput series and
its previous-period comparison, the exporter-health card data (successor of the
old synthetic "device status"), the day-of-week x hour heatmap and the exporter
list.

Every endpoint takes the shared window (``window_params``), the full filter
(``flow_filter``) and the caller's visibility (``flow_visibility``), plans its
tables through ``sql.plan_source`` (hourly rollup for complete interior hours,
raw records for partial edges, all half-open) and runs through ``run_query``.
Without ``envelope=1`` the legacy response shapes are unchanged for the
dashboards, device pages and security dashboard that read them.

Rules this module follows (review findings in brackets):

* totals and the chart come from the same plan, so the page total equals the
  sum of the chart buckets (NF-17);
* unknown is not zero: a bucket without matching flows is "idle" only when the
  exporters in scope delivered records in it; otherwise it is a collection gap
  (null) (X-35, FE-03);
* the previous period is resolved on the server from the same window, with the
  identical filter, and its buckets are paired by index (NF-02, X-23);
* flow duration is the true mean of records whose start/end times are valid,
  with the share that had valid times (NF-18);
* the TCP RST ratio divides by TCP records whose flags were exported, ignoring
  the page's protocol/flag filters (NF-19, X-39);
* exporter liveness never comes from filtered traffic: /exporters reports the
  unfiltered receipt time separately from the last matching flow (X-04).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

from .access import FlowVisibility, canonical_ip, flow_visibility
from .common import (
    CONVERSATIONS_1H,
    EXPORTER_MINUTES,
    QOS_1H,
    RAW,
    Window,
    _protocol_name,
    _to_iso_utc,
    current_meta,
    get_retention,
    invalid_filter,
    invalid_window,
    now_utc,
    run_query,
    table_ref,
    window_params,
)
from .filters import FlowFilter, compile_filter, flow_filter, require_filter_exporters_visible
from .meta import (
    _MINUTE_COUNTS,
    covered_minutes_sql,
    expected_intervals,
    full_minutes,
    measured_span,
    minute_total,
)
from .sql import DST, EXPORTER, SRC, SourcePlan, _whole_hour_tz, display, floor_to, plan_source, table_caps

router = APIRouter()


# ── Definitions ───────────────────────────────────────────────────────────────

# NF-18: a flow's duration is trusted only when both switched times are set,
# ordered, in the same time domain (absolute epoch ms vs. sysUptime-relative ms)
# and at most a day apart (contract §3.4). Durations are signed Int64 so a
# wrapped or reversed pair can never become ~1.8e19 ms.
DUR_EXPR = "(toInt64(last_switched_ms) - toInt64(first_switched_ms))"
DUR_VALID = (
    "(first_switched_ms > 0 AND last_switched_ms >= first_switched_ms"
    " AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)"
    " AND last_switched_ms - first_switched_ms <= 86400000)"
)

# NF-07/NF-19: TCP flags are evidence only when the exporter sent the field.
# Tracked records say so in fields_present bit 0; for legacy records (written
# before field tracking) a non-zero flag byte proves the field was exported,
# while a zero proves nothing. The QoS rollup keeps the same three cases in
# flags_known (1 exported, 2 legacy, 0 tracked and absent).
RAW_FLAGS_KNOWN = "(bitTest(fields_present, 0) OR (fields_present = 0 AND tcp_flags != 0))"
QOS_FLAGS_KNOWN = "(flags_known = 1 OR (flags_known = 2 AND tcp_flags != 0))"

# Below this many TCP records with exported flags the RST share is not stated
# as a ratio (a handful of aborted probes would read as a high percentage).
RST_MIN_SAMPLE = 200
# A partial flag export (some templates carry flags, some do not) is disclosed.
FLAG_COVERAGE_OK_PCT = 95.0

# The "current rate" is the rate over the last minutes of the window.
RATE_WINDOW_S = 300

# Previous-period baselines (NF-02, CR-15).
BASELINES = ("previous_equal", "same_time_last_week")
WEEK = timedelta(days=7)

# A bucket (or heatmap cell) counts as covered when records arrived in at
# least this share of its minutes; below it the value is marked partial.
COVERED_SHARE = 0.9


# ── Small value helpers ───────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    """A finite float, or None (NaN/inf/None/non-numbers are unknown)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int:
    f = _num(v)
    return int(f) if f is not None else 0


def _iso(v: Any) -> str | None:
    """RFC3339 for a datetime (or datetime text); None for anything else."""
    if isinstance(v, (datetime, str)):
        return _to_iso_utc(v)
    return None


def _latest(values: Iterable[Any]) -> datetime | None:
    out: datetime | None = None
    for v in values:
        if isinstance(v, datetime) and v.year >= 2000:
            v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            out = v if out is None or v > out else out
    return out


def _pct(part: float, whole: float, digits: int = 2) -> float | None:
    return round(part / whole * 100, digits) if whole else None


def _first(res, width: int) -> tuple:
    rows = getattr(res, "result_rows", None) or []
    row = tuple(rows[0]) if rows else ()
    return row + (None,) * max(0, width - len(row))


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ── Plans ─────────────────────────────────────────────────────────────────────

def uses_rollup(plan: SourcePlan) -> bool:
    return any(seg.table != RAW for seg in plan.segments)


def per_table(plan: SourcePlan) -> list[SourcePlan]:
    """One sub-plan per table of ``plan`` (the whole plan when it is empty).

    Aggregates that only add up (sums, counts, max) run once per table so each
    query carries only its own table's predicates; the results are combined in
    Python."""
    tables = plan.tables
    if not tables:
        return [plan]
    return [replace(plan, segments=tuple(s for s in plan.segments if s.table == t)) for t in tables]


async def aggregate_per_table(
    plan: SourcePlan,
    columns: Mapping[str, tuple[str, str]],
    f: FlowFilter,
    vis: FlowVisibility,
    *,
    label: str,
) -> dict[str, list[Any]]:
    """Run ``columns`` (alias -> (rollup aggregate, raw aggregate)) over each
    table of the plan; returns alias -> list of per-table values."""

    async def one(sub: SourcePlan):
        sql, params = sub.from_sql(columns, f, vis)
        return _first(await run_query(f"SELECT * FROM {sql} AS part", params, label=label), len(columns))

    rows = await asyncio.gather(*(one(sub) for sub in per_table(plan)))
    return {alias: [row[i] for row in rows] for i, alias in enumerate(columns)}


def _sum(values: Iterable[Any]) -> int:
    return sum(_int(v) for v in values)


# ── Delivery coverage (flow_exporter_minutes) ─────────────────────────────────

def coverage_filter(f: FlowFilter) -> FlowFilter:
    """The part of ``f`` that describes *which exporters* are in scope.

    Coverage is about what exporters delivered (contract §11 /coverage), so
    traffic dimensions never narrow it; only the exporter and the counting
    mode's exporter roles do. The request's filter metadata is untouched."""
    base = FlowFilter(
        exporter=f.exporter or (f.iface.exporter if f.iface else None),
        count=f.count, counting=f.counting, tz=f.tz,
    )
    missing = table_caps(EXPORTER_MINUTES).unsupported(base)
    return base.without(*missing) if missing else base


async def tracking_from() -> datetime | None:
    """When the appliance started recording deliveries (None = no data yet)."""
    retention = await get_retention()
    earliest = retention.earliest.get(EXPORTER_MINUTES)
    if earliest is None:
        return None
    return floor_to(earliest, 60)


async def receipt_minutes(
    start: datetime,
    end: datetime,
    f: FlowFilter,
    vis: FlowVisibility,
    *,
    group_by: str | None = None,
    label: str,
    intervals: Mapping[str, int] | None = None,
    full_to: datetime | None = None,
) -> list[tuple]:
    """Minutes (by the appliance's receive clock) of ``[start, end)`` covered
    by deliveries of the exporters in scope, optionally grouped by
    ``group_by`` (an expression over ``minute``). A delivery covers its
    exporter's expected export interval (``intervals``, from
    ``meta.expected_intervals``). Rows: (group, minutes_with_data,
    minutes_with_receipts, running_with_data); a single row with group None
    when ungrouped. ``running_with_data`` is 1 when a minute at or after
    ``full_to`` (the minute still filling) has records."""
    where, params = compile_filter(coverage_filter(f), EXPORTER_MINUTES, vis)
    cov_sql, cov_params = covered_minutes_sql(where, params, start, end, intervals, full_to=full_to)
    key = group_by or "NULL"
    sql = f"SELECT {key} AS g, {_MINUTE_COUNTS} FROM {cov_sql} AS cov"
    if group_by:
        sql += " GROUP BY g"
    res = await run_query(sql, cov_params, label=label)
    return [tuple(r) for r in (res.result_rows or [])]


async def window_coverage(win_start: datetime, win_end: datetime, f: FlowFilter, vis: FlowVisibility,
                          *, label: str, intervals: Mapping[str, int] | None = None) -> dict:
    """Delivery coverage of one window: covered minutes over tracked minutes.

    The minute still running (or the partial last minute of a fixed window)
    counts once records arrived in it, and is not held against the window
    while it fills."""
    tracked = await tracking_from()
    if tracked is None:
        return {"minutes_with_data": None, "minutes_tracked": 0, "coverage_pct": None, "tracking_from": None}
    measured_from, full_to, measured_to = measured_span(win_start, win_end, tracked, now_utc())
    unknown = {"minutes_with_data": None, "minutes_tracked": 0, "coverage_pct": None, "tracking_from": _iso(tracked)}
    if measured_to <= measured_from:
        return unknown
    rows = await receipt_minutes(measured_from, measured_to, f, vis, label=label, intervals=intervals,
                                 full_to=full_to)
    row = rows[0] if rows else (None, 0, 0, 0)
    minutes = minute_total(full_minutes(measured_from, full_to), _int(row[3]))
    if minutes == 0:
        return unknown
    with_data = min(_int(row[1]), minutes)
    return {"minutes_with_data": with_data, "minutes_tracked": minutes,
            "coverage_pct": _pct(with_data, minutes, 1), "tracking_from": _iso(tracked)}


# ── /overview ─────────────────────────────────────────────────────────────────

async def rate_at_end(win: Window, f: FlowFilter, vis: FlowVisibility,
                      intervals: Mapping[str, int] | None = None) -> tuple[float | None, int, str]:
    """(bps, seconds, status) over the last minutes of the window, from raw
    records. For a relative window this is the current rate; for a fixed one,
    the rate at the end of the selected window. ``status``:

    * ``ok`` -- a rate (0 is a real zero: deliveries arrived, nothing matched);
    * ``beyond_retention`` -- raw records no longer cover that moment;
    * ``no_receipts`` -- nothing was delivered by the exporters in scope over
      those minutes (allowing for each exporter's expected export interval):
      the rate is unknown, not zero (a silent exporter, a collector outage).
    """
    seconds = min(RATE_WINDOW_S, win.seconds)
    rate_win = Window(start=win.end - timedelta(seconds=seconds), end=win.end, mode="fixed", tz=win.tz)
    plan = await plan_source(rate_win, f, rollup=None, record=False)
    if plan.effective_from > rate_win.start:
        return None, seconds, "beyond_retention"
    sql, params = plan.from_sql({"b": "bytes"}, f, vis)
    tracked = await tracking_from()

    async def receipts() -> list[tuple] | None:
        # Deliveries in the rate window (None: deliveries were not tracked then).
        if tracked is None or tracked >= rate_win.end:
            return None
        return await receipt_minutes(max(rate_win.start, tracked), rate_win.end, f, vis,
                                     label="overview-rate-receipts", intervals=intervals)

    res, rx = await asyncio.gather(
        run_query(f"SELECT sum(b) FROM {sql} AS src", params, label="overview-rate"), receipts(),
    )
    total = _int(_first(res, 1)[0])
    if total == 0 and rx is not None and not any(_int(r[2]) for r in rx):
        return None, seconds, "no_receipts"
    return total * 8 / seconds, seconds, "ok"


@router.get("/overview")
async def netflow_overview(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Headline totals for the window and filter.

    ``bytes``/``packets``/``flows`` use the same plan as /timeseries (their sum
    equals the chart). ``src_hosts``/``dst_hosts`` are separate distinct counts
    and ``hosts`` is the union. ``current_bps`` is the rate over the last
    ``rate_window_s`` seconds of the window (null -- with ``rate_status`` --
    when raw retention no longer covers them or nothing was delivered then).
    ``last_seen`` is the receipt time of the last *matching* flow -- exporter
    liveness comes from /exporters/status, never from this.

    When nothing retained can answer the window for this filter (e.g. a
    raw-only filter such as DSCP on a range older than raw retention), the
    totals are null with ``totals_status`` ``beyond_retention``: unknown, not
    zero. ``raw_only_filters`` names the filter keys the rollups cannot serve.
    """
    require_filter_exporters_visible(f, vis)
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    sql, params = plan.from_sql(
        {
            "b": "bytes",
            "p": "packets",
            "n": ("flows", "toUInt64(1)"),
            "e": EXPORTER,
            "s": SRC,
            "d": DST,
            "r": "received_at",
            "proto": "protocol",
        },
        f, vis,
    )
    summary_q = run_query(
        f"""
        SELECT sum(b), sum(p), sum(n), uniqExact(e), uniqExact(s), uniqExact(d),
               uniqExactArray([s, d]), max(r)
        FROM {sql} AS src
        """,
        params, label="overview",
    )
    proto_q = run_query(
        f"SELECT proto, sum(b) AS pb FROM {sql} AS src GROUP BY proto ORDER BY pb DESC LIMIT 1",
        params, label="overview-protocol",
    )
    intervals = await expected_intervals(db, vis)
    summary_res, proto_res, (rate, rate_s, rate_status) = await asyncio.gather(
        summary_q, proto_q, rate_at_end(win, f, vis, intervals),
    )
    s = _first(summary_res, 8)
    top = (proto_res.result_rows or [None])[0]
    raw_only = sorted(table_caps(CONVERSATIONS_1H).unsupported(f))
    expired = plan.empty and plan.retention_limited

    def total(v: Any) -> int | None:
        return None if expired else _int(v)

    return {
        "bytes": total(s[0]),
        "packets": total(s[1]),
        "flows": total(s[2]),
        "exporters": total(s[3]),
        "src_hosts": total(s[4]),
        "dst_hosts": total(s[5]),
        "hosts": total(s[6]),
        "last_seen": _iso(s[7]),
        "current_bps": rate,
        "rate_window_s": rate_s,
        "rate_status": rate_status,
        "totals_status": "beyond_retention" if expired else "ok",
        "raw_only_filters": raw_only,
        "top_protocol": (
            {"protocol": int(top[0]), "name": _protocol_name(top[0]), "bytes": _int(top[1])}
            if top and not expired else None
        ),
    }


# ── Time series ───────────────────────────────────────────────────────────────

def bucket_seconds(span_s: int, rollup: bool) -> int:
    """Chart bucket for a window span. Hour-rollup data needs whole hours."""
    if span_s <= 6 * 3600:
        b = 60
    elif span_s <= 48 * 3600:
        b = 300
    elif span_s <= 72 * 3600:
        b = 1800
    elif span_s <= 8 * 86400:
        b = 3600
    elif span_s <= 31 * 86400:
        b = 7200
    else:
        b = 21600
    if rollup and b < 3600:
        b = 3600
    return b


def bucket_grid(start: datetime, end: datetime, bucket: int) -> list[tuple[int, int]]:
    """[(bucket_start_epoch, seconds of the bucket inside [start, end))]."""
    out: list[tuple[int, int]] = []
    s, e = _epoch(start), _epoch(end)
    b = s // bucket * bucket
    while b < e:
        out.append((b, min(e, b + bucket) - max(s, b)))
        b += bucket
    return out


def build_points(
    grid: list[tuple[int, int]],
    rows: Mapping[int, tuple[int, int, int]],
    receipts: Mapping[int, int],
    presence: Mapping[int, int],
    *,
    bucket: int,
    effective_from: datetime,
    effective_to: datetime,
    tracked_from: datetime | None,
    open_end: bool,
    fill: bool,
    origin: int,
) -> list[dict]:
    """Chart points for a bucket grid.

    ``rows``: bucket -> (bytes, packets, flows) of matching flows.
    ``receipts``: bucket -> minutes in which the exporters in scope delivered
    records (appliance receive clock). ``presence``: bucket -> in-scope flows of
    any kind (traffic filters removed, flow clock). A bucket with matching flows
    is ``data``; without them it is ``idle`` (a real zero) only when there is
    evidence the exporters were delivering (receipts or other flows), ``gap``
    (null) when there is none, ``unknown`` before delivery tracking started and
    ``expired`` outside the retained range. Rates divide by the bucket's seconds
    inside the window, so partial edge buckets do not dip. ``i`` is the index
    from the window's first bucket (``origin``), which pairs comparison series.
    """
    ef, et = _epoch(effective_from), _epoch(effective_to)
    tf = _epoch(tracked_from) if tracked_from is not None else None
    last = grid[-1][0] if grid else None
    points: list[dict] = []
    for b, _ in grid:
        lo, hi = max(b, ef), min(b + bucket, et)
        inside = max(0, hi - lo)
        row = rows.get(b)
        if not fill and row is None:
            continue
        point: dict[str, Any] = {
            "i": (b - origin) // bucket,
            "ts": b * 1000,
            "covered_s": inside,
            "partial": inside < bucket,
            "open": bool(open_end and b == last),
            "receipt_minutes": receipts.get(b) if tf is not None and b + bucket > tf else None,
        }
        evidence = bool(receipts.get(b) or presence.get(b))
        if row is not None and inside > 0:
            by, pk, fl = row
            point.update(bps=by * 8 / inside, bytes=by, packets=pk, flows=fl, state="data")
        elif row is not None:
            # Records stamped outside the retained part (should not happen): keep them visible.
            point.update(bps=None, bytes=row[0], packets=row[1], flows=row[2], state="data")
        elif inside == 0:
            point.update(bps=None, bytes=None, packets=None, flows=None, state="expired")
        elif evidence:
            point.update(bps=0.0, bytes=0, packets=0, flows=0, state="idle")
        elif tf is None or b + bucket <= tf:
            point.update(bps=None, bytes=None, packets=None, flows=None, state="unknown")
        else:
            point.update(bps=None, bytes=None, packets=None, flows=None, state="gap")
        points.append(point)
    return points


def series_coverage(points: list[dict], grid_len: int, rows: int) -> dict:
    """Share of the window's retained buckets with evidence of delivery."""
    known = [p for p in points if p.get("state") != "expired"]
    covered = sum(1 for p in known if p.get("state") in ("data", "idle"))
    return {
        "buckets_total": grid_len,
        "buckets_with_data": rows,
        "buckets_covered": covered,
        "buckets_gap": sum(1 for p in points if p.get("state") == "gap"),
        "buckets_unknown": sum(1 for p in points if p.get("state") in ("unknown", "expired")),
        "coverage_pct": _pct(covered, len(known), 1) if known else None,
    }


def has_traffic_filter(f: FlowFilter) -> bool:
    """True when ``f`` narrows traffic beyond which exporters are in scope."""
    return bool(f.active_keys() - coverage_filter(f).active_keys())


async def presence_by_bucket(win: Window, f: FlowFilter, vis: FlowVisibility, bucket: int, *, label: str) -> dict[int, int]:
    """In-scope flows per bucket with the traffic filters removed (evidence that
    the exporters were delivering while nothing matched the filter)."""
    cf = coverage_filter(f)
    plan = await plan_source(win, cf, rollup=CONVERSATIONS_1H, record=False)
    sql, params = plan.from_sql({"t": "toDateTime(timestamp, 'UTC')", "n": ("flows", "toUInt64(1)")}, cf, vis)
    res = await run_query(
        f"SELECT intDiv(toUnixTimestamp(t), {bucket}) * {bucket} AS ts, sum(n) FROM {sql} AS src GROUP BY ts",
        params, label=label,
    )
    return {int(r[0]): _int(r[1]) for r in (res.result_rows or [])}


async def series_for_plan(
    win: Window,
    plan: SourcePlan,
    f: FlowFilter,
    vis: FlowVisibility,
    *,
    bucket: int,
    fill: bool,
    label: str,
    intervals: Mapping[str, int] | None = None,
) -> dict:
    """Bucketed flows of one window plus per-bucket delivery evidence."""
    sql, params = plan.from_sql(
        {
            "t": "toDateTime(timestamp, 'UTC')",
            "b": "bytes",
            "p": "packets",
            "n": ("flows", "toUInt64(1)"),
        },
        f, vis,
    )
    flows_q = run_query(
        f"""
        SELECT intDiv(toUnixTimestamp(t), {bucket}) * {bucket} AS ts, sum(b), sum(p), sum(n)
        FROM {sql} AS src
        GROUP BY ts
        ORDER BY ts
        """,
        params, label=label,
    )
    tracked = await tracking_from()

    async def nothing():
        return []

    async def no_presence():
        return {}

    cov_q = receipt_minutes(
        win.start, win.end, f, vis,
        group_by=f"intDiv(toUnixTimestamp(minute), {bucket}) * {bucket}", label=f"{label}-receipts",
        intervals=intervals,
    ) if fill and tracked is not None else nothing()
    presence_q = presence_by_bucket(win, f, vis, bucket, label=f"{label}-presence") \
        if fill and has_traffic_filter(f) else no_presence()
    flows_res, cov_rows, presence = await asyncio.gather(flows_q, cov_q, presence_q)
    rows = {int(r[0]): (_int(r[1]), _int(r[2]), _int(r[3])) for r in (flows_res.result_rows or [])}
    receipts = {int(r[0]): _int(r[1]) for r in cov_rows if r[0] is not None}
    grid = bucket_grid(win.start, win.end, bucket)
    origin = grid[0][0] if grid else _epoch(win.start)
    points = build_points(
        grid, rows, receipts, presence, bucket=bucket,
        effective_from=plan.effective_from, effective_to=plan.effective_to,
        tracked_from=tracked, open_end=win.mode == "relative", fill=fill, origin=origin,
    )
    coverage = series_coverage(points, len(grid), len(rows)) if fill else None
    return {
        "points": points,
        "bytes": sum(r[0] for r in rows.values()),
        "packets": sum(r[1] for r in rows.values()),
        "flows": sum(r[2] for r in rows.values()),
        "coverage": coverage,
        "effective_from": plan.effective_from,
        "effective_to": plan.effective_to,
        "retention_limited": plan.retention_limited,
        "source": plan.source,
    }


@router.get("/timeseries")
async def netflow_timeseries(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    fill: bool = Query(default=False, description="Return every bucket of the window, with gaps as null"),
    bucket: int | None = Query(default=None, description="Bucket seconds (60..86400); default by span"),
    points: int | None = Query(default=None, description="Accepted for older callers; ignored"),
):
    """Throughput buckets ``[{ts, bps, bytes, packets, flows, ...}]``.

    Buckets are epoch-aligned; ``bps`` divides by the bucket's seconds inside
    the window (edge buckets are ``partial``, the running bucket of a relative
    window is ``open``). With ``fill=1`` every bucket is returned and ``state``
    tells data / idle (delivered, nothing matched) / gap (nothing delivered:
    null, not zero) / unknown / expired apart.
    """
    require_filter_exporters_visible(f, vis)
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    b = _checked_bucket(bucket, uses_rollup(plan), win.seconds) or bucket_seconds(win.seconds, uses_rollup(plan))
    current_meta().granularity_s = b
    intervals = await expected_intervals(db, vis) if fill else None
    out = await series_for_plan(win, plan, f, vis, bucket=b, fill=fill, label="timeseries", intervals=intervals)
    return out["points"]


# At most this many buckets per series: a caller-chosen bucket that would
# exceed it over the window is raised to the next step that fits (and said
# so), like the span-based default, so one request cannot ask for a 93-day
# per-minute series (134 000 points, 22 MB of JSON).
MAX_BUCKETS = 2000
BUCKET_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def _checked_bucket(bucket: int | None, rollup: bool, span_s: int | None = None) -> int | None:
    if bucket is None:
        return None
    if not 60 <= bucket <= 86400 or bucket % 60:
        raise invalid_filter("bucket must be a whole number of minutes between 60 and 86400 seconds",
                             {"bucket": "Invalid bucket"})
    if rollup and bucket % 3600:
        bucket = max(3600, bucket // 3600 * 3600)
    if span_s and math.ceil(span_s / bucket) > MAX_BUCKETS:
        need = span_s / MAX_BUCKETS
        raised = next((st for st in BUCKET_STEPS if st >= need and st >= bucket and (not rollup or st % 3600 == 0)),
                      BUCKET_STEPS[-1])
        current_meta().warn(
            "bucket_raised",
            f"A {bucket // 60}-minute bucket would give more than {MAX_BUCKETS} points over this window; "
            f"{raised // 60}-minute buckets are used instead",
        )
        bucket = raised
    return bucket


def baseline_window(win: Window, baseline: str) -> Window:
    """The comparison window for ``win``: the immediately preceding window of
    the same length, or the same span one week earlier. Always fixed bounds
    resolved from the one server-side window (NF-02)."""
    if baseline not in BASELINES:
        raise invalid_filter(f"Unknown baseline '{baseline[:40]}' (expected one of: {', '.join(BASELINES)})",
                             {"baseline": "Unknown baseline"})
    span = win.end - win.start
    if baseline == "previous_equal":
        shift = span
    else:
        if span > WEEK:
            raise invalid_window("same_time_last_week compares windows of at most 7 days", "baseline")
        shift = WEEK
    return Window(start=win.start - shift, end=win.end - shift, mode="fixed", tz=win.tz)


def compare_delta(
    cur_bytes: int,
    prior_bytes: int,
    *,
    prior_limited: bool,
    prior_coverage_pct: float | None,
    cur_coverage_pct: float | None,
) -> tuple[float | None, str | None]:
    """(delta %, reason when null). A comparison is only stated when the prior
    period is fully retained, mostly covered and carried traffic."""
    if prior_limited:
        return None, "prior_beyond_retention"
    if prior_coverage_pct is not None and prior_coverage_pct < 50:
        return None, "prior_low_coverage"
    if cur_coverage_pct is not None and cur_coverage_pct < 50:
        return None, "current_low_coverage"
    if prior_bytes <= 0:
        return None, "no_prior_traffic"
    return round((cur_bytes - prior_bytes) / prior_bytes * 100, 2), None


def _series_payload(win: Window, s: dict) -> dict:
    return {
        "from": _to_iso_utc(win.start),
        "to": _to_iso_utc(win.end),
        "effective_from": _to_iso_utc(s["effective_from"]),
        "effective_to": _to_iso_utc(s["effective_to"]),
        "retention_limited": s["retention_limited"],
        "source": s["source"],
    }


@router.get("/timeseries/compare")
async def netflow_timeseries_compare(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    baseline: str = Query(default="previous_equal", description="previous_equal | same_time_last_week"),
):
    """The window's throughput next to a baseline period (NF-02).

    Both periods come from the one server-resolved window, carry the identical
    filter (hour/day-of-week filters select the same local hours in each) and
    use the same bucket size; ``points[k].i`` pairs with ``prior_points[k].i``.
    ``totals.delta_pct`` is null — with ``delta_reason`` — when the baseline is
    beyond retention, poorly covered or empty.
    """
    require_filter_exporters_visible(f, vis)
    prior = baseline_window(win, baseline)
    cur_plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    prior_plan = await plan_source(prior, f, rollup=CONVERSATIONS_1H, record=False)
    b = bucket_seconds(win.seconds, uses_rollup(cur_plan) or uses_rollup(prior_plan))
    meta = current_meta()
    meta.granularity_s = b
    intervals = await expected_intervals(db, vis)
    cur, old = await asyncio.gather(
        series_for_plan(win, cur_plan, f, vis, bucket=b, fill=True, label="compare-current", intervals=intervals),
        series_for_plan(prior, prior_plan, f, vis, bucket=b, fill=True, label="compare-prior", intervals=intervals),
    )
    meta.tables.update(prior_plan.tables)
    prior_limited = prior_plan.retention_limited or prior_plan.effective_from > prior.start
    if prior_limited:
        meta.warn("prior_retention", "Part of the comparison period is older than the data kept; "
                                     "no change percentage is stated")
    delta, reason = compare_delta(
        cur["bytes"], old["bytes"], prior_limited=prior_limited,
        prior_coverage_pct=old["coverage"]["coverage_pct"], cur_coverage_pct=cur["coverage"]["coverage_pct"],
    )
    notes = []
    if f.hour is not None or f.dow is not None:
        notes.append(f"Hour/day filters select the same local hours ({win.tz}) in both periods")
    return {
        "baseline": baseline,
        "bucket_s": b,
        "window": _series_payload(win, cur),
        "prior_window": _series_payload(prior, old),
        "points": cur["points"],
        "prior_points": old["points"],
        "totals": {
            "bytes": cur["bytes"],
            "prior_bytes": old["bytes"],
            "flows": cur["flows"],
            "prior_flows": old["flows"],
            "delta_bytes": cur["bytes"] - old["bytes"],
            "delta_pct": delta,
            "delta_reason": reason,
        },
        "coverage": cur["coverage"],
        "prior_coverage": {**old["coverage"], "retention_limited": prior_limited},
        "filters": f.canonical(),
        "notes": notes,
    }


# ── TCP flags (shared by /device-status and the TCP-flags card) ────────────────

def rst_payload(tcp_flows: int, with_flags: int, rst: int, ignored: Iterable[str] = ()) -> dict:
    """The one RST ratio (NF-19): RST records over TCP records whose flags were
    exported. ``pct`` is null — with a ``status`` reason — when there is no
    TCP, the exporter sends no flags, or the sample is too small."""
    coverage = _pct(with_flags, tcp_flows, 1)
    if tcp_flows <= 0:
        status, pct = "no_tcp", None
    elif with_flags <= 0:
        status, pct = "flags_unavailable", None
    elif with_flags < RST_MIN_SAMPLE:
        status, pct = "low_sample", None
    else:
        pct = round(rst / with_flags * 100, 2)
        status = "partial" if coverage is not None and coverage < FLAG_COVERAGE_OK_PCT else "ok"
    return {
        "pct": pct,
        "status": status,
        "rst_flows": rst,
        "tcp_flows": tcp_flows,
        "tcp_flows_with_flags": with_flags,
        "flag_coverage_pct": coverage,
        "min_sample": RST_MIN_SAMPLE,
        "ignored_filters": sorted(ignored),
    }


_FLAG_CONDS = {
    "rst": "bitAnd(tcp_flags, 4) != 0",
    "syn_only": "bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0",
    "ack_only": "bitAnd(tcp_flags, 16) != 0 AND bitAnd(tcp_flags, 2) = 0",
    "fin": "bitAnd(tcp_flags, 1) != 0",
    "psh": "bitAnd(tcp_flags, 8) != 0",
    "urg": "bitAnd(tcp_flags, 32) != 0",
    "no_flags": "tcp_flags = 0",
}


async def tcp_flag_stats(win: Window, f: FlowFilter, vis: FlowVisibility, *, label: str = "tcp-flag-stats") -> dict:
    """TCP flag counts over the window, ignoring the page's ``protocol`` and
    ``tcpflag`` filters (the statistic is about all TCP in scope; filtering on
    the RST tile must not turn the ratio into 100%).

    Returns ``{total_tcp, tcp_with_flags, flag_coverage_pct, counts: {syn_only,
    ack_only, rst, fin, psh, urg, no_flags}, rst_ratio_pct, rst: rst_payload,
    source}`` (the legacy /tcp-flags shape is ``{total_tcp, **counts}``); every flag count is over TCP
    records whose flags were exported, so ``no_flags`` is an observed zero,
    never a missing field. For reuse by the TCP-flags breakdown."""
    base = f.without("tcpflag", "protocol")
    plan = await plan_source(win, base, rollup=QOS_1H, record=False)
    columns: dict[str, tuple[str, str]] = {
        "tcp": ("sumIf(flows, protocol = 6)", "countIf(protocol = 6)"),
        "flagged": (f"sumIf(flows, protocol = 6 AND {QOS_FLAGS_KNOWN})",
                    f"countIf(protocol = 6 AND {RAW_FLAGS_KNOWN})"),
    }
    for name, cond in _FLAG_CONDS.items():
        columns[f"f_{name}"] = (f"sumIf(flows, protocol = 6 AND {QOS_FLAGS_KNOWN} AND {cond})",
                                f"countIf(protocol = 6 AND {RAW_FLAGS_KNOWN} AND {cond})")
    agg = await aggregate_per_table(plan, columns, base, vis, label=label)
    tcp, flagged = _sum(agg["tcp"]), _sum(agg["flagged"])
    counts = {name: _sum(agg[f"f_{name}"]) for name in _FLAG_CONDS}
    ignored = [k for k in ("protocol", "tcpflag") if k in f.active_keys()]
    rst = rst_payload(tcp, flagged, counts["rst"], ignored)
    return {
        "total_tcp": tcp,
        "tcp_with_flags": flagged,
        "flag_coverage_pct": rst["flag_coverage_pct"],
        "counts": counts,
        "rst_ratio_pct": rst["pct"],
        "rst": rst,
        "source": plan.source,
    }


# ── /device-status (exporter-health card data) ────────────────────────────────

def duration_payload(flows: int, valid_flows: int, valid_ms: int) -> dict:
    """True mean flow duration over records with valid start/end times (NF-18).
    Bounded by the exporters' active/inactive timeouts; not a latency."""
    valid_flows = min(max(valid_flows, 0), max(flows, 0))
    avg = round(valid_ms / valid_flows, 1) if valid_flows > 0 and valid_ms >= 0 else None
    coverage = _pct(valid_flows, flows, 1)
    if flows <= 0:
        status = "no_flows"
    elif avg is None:
        status = "unavailable"
    elif coverage is not None and coverage < FLAG_COVERAGE_OK_PCT:
        status = "partial"
    else:
        status = "ok"
    return {"avg_ms": avg, "valid_flows": valid_flows, "flows": max(flows, 0), "coverage_pct": coverage,
            "status": status}


@router.get("/device-status")
async def netflow_device_status(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Flow-derived exporter-health signals for the window.

    * ``flow_duration_ms`` — true mean duration (ms) of flows with valid
      switched times, null when none had them; ``flow_duration`` says how many.
    * ``rst_ratio_pct`` — RST share of TCP records with exported flags over all
      TCP in scope (protocol/flag filters ignored); ``rst`` gives the counts and
      why it is null (no TCP, flags not exported, too few records).
    * ``flow_continuity_pct`` — share of the window's minutes in which the
      exporters in scope delivered records (``continuity``), read from the
      delivery table instead of rescanning raw flows.

    These are heuristics derived from flow records, not ICMP/SNMP probes.
    """
    require_filter_exporters_visible(f, vis)
    plan = await plan_source(win, f, rollup=QOS_1H)
    # Aliases never equal a column name: ClickHouse resolves SELECT aliases in
    # WHERE, and a filter predicate naming `bytes` must see the column.
    columns = {
        "a_flows": ("sum(flows)", "count()"),
        "a_packets": ("sum(packets)", "sum(packets)"),
        "a_bytes": ("sum(bytes)", "sum(bytes)"),
        "a_dvf": ("sum(duration_valid_flows)", f"countIf({DUR_VALID})"),
        "a_dvms": ("sum(duration_valid_ms)", f"sumIf({DUR_EXPR}, {DUR_VALID})"),
        "a_last": ("max(received_at)", "max(received_at)"),
        "a_exps": (f"groupUniqArray(1000)({EXPORTER})", f"groupUniqArray(1000)({EXPORTER})"),
    }
    intervals = await expected_intervals(db, vis)
    agg, flags, continuity = await asyncio.gather(
        aggregate_per_table(plan, columns, f, vis, label="device-status"),
        tcp_flag_stats(win, f, vis, label="device-status-flags"),
        window_coverage(win.start, win.end, f, vis, label="device-status-continuity", intervals=intervals),
    )
    flows = _sum(agg["a_flows"])
    duration = duration_payload(flows, _sum(agg["a_dvf"]), _sum(agg["a_dvms"]))
    exporters: set[str] = set()
    for value in agg["a_exps"]:
        if isinstance(value, (list, tuple)):
            exporters.update(str(canonical_ip(v) or v) for v in value)
    return {
        "flow_derived": True,
        "flow_duration_ms": duration["avg_ms"],
        "flow_duration": duration,
        "rst_ratio_pct": flags["rst_ratio_pct"],
        "rst": flags["rst"],
        "tcp_flags": {"total_tcp": flags["total_tcp"], "tcp_with_flags": flags["tcp_with_flags"],
                      "flag_coverage_pct": flags["flag_coverage_pct"], **flags["counts"]},
        "flow_continuity_pct": continuity["coverage_pct"],
        "continuity": {**continuity, "basis": "minutes with delivered records, each delivery covering its "
                                              "exporter's expected export interval (appliance clock)"},
        "flows": flows,
        "packets": _sum(agg["a_packets"]),
        "bytes": _sum(agg["a_bytes"]),
        "exporters": len(exporters),
        "last_seen": _iso(_latest(agg["a_last"])),
    }


# ── /heatmap ──────────────────────────────────────────────────────────────────

HEATMAP_STEP_S = 900   # every UTC offset is a multiple of 15 minutes


def heatmap_occurrences(
    start: datetime, end: datetime, tz: str, tracked_from: datetime | None = None,
) -> dict[tuple[int, int], dict]:
    """Per local (ISO weekday, hour): seconds of ``[start, end)`` falling in it,
    the seconds after delivery tracking began, and how many distinct local
    date-hours ("occurrences") it spans. DST-aware (zoneinfo)."""
    zone = ZoneInfo(tz)
    out: dict[tuple[int, int], dict] = {}
    s, e = _epoch(start), _epoch(end)
    tf = _epoch(tracked_from) if tracked_from is not None else None
    t = s // HEATMAP_STEP_S * HEATMAP_STEP_S
    while t < e:
        lo, hi = max(t, s), min(t + HEATMAP_STEP_S, e)
        if hi > lo:
            local = datetime.fromtimestamp(t, tz=zone)
            key = (local.isoweekday(), local.hour)
            cell = out.setdefault(key, {"seconds": 0, "tracked": 0, "slots": set()})
            cell["seconds"] += hi - lo
            if tf is not None:
                cell["tracked"] += max(0, hi - max(lo, tf))
            cell["slots"].add((local.date(), local.hour))
        t += HEATMAP_STEP_S
    return out


def heatmap_cell(
    dow: int, hour: int, occ: Mapping | None, bytes_: int, flows: int, dates: int,
    covered_slots: int, basis: str,
) -> dict:
    """One heatmap cell with its value and state (NF-28).

    ``occ`` is the cell's entry from ``heatmap_occurrences``: how many local
    date-hours ("occurrences") of this weekday/hour the window contains.
    ``covered_slots`` is how many of them show delivery evidence (records
    received, or any in-scope flow). States:

    * ``outside_window`` — the window contains no such hour;
    * ``unknown`` — no traffic, and deliveries were not tracked then;
    * ``no_coverage`` — no traffic and nothing delivered (a collection gap,
      not idle traffic);
    * ``zero`` — deliveries arrived but nothing matched;
    * ``data`` / ``partial`` — traffic, in every occurrence / with gaps.

    ``value`` (basis ``avg``) is bytes per covered occurrence — the average hour
    — so a window holding three Tuesdays and two Mondays compares like with
    like; basis ``sum`` is the byte total."""
    seconds = int(occ["seconds"]) if occ else 0
    tracked = int(occ["tracked"]) if occ else 0
    slots = len(occ["slots"]) if occ else 0
    cs = max(0, min(covered_slots, slots))
    cell = {
        "dow": dow, "hour": hour, "bytes": bytes_, "flows": flows, "dates_with_data": dates,
        "occurrences": slots, "covered_occurrences": cs, "occurrence_hours": round(seconds / 3600, 3),
    }
    if seconds <= 0:
        return {**cell, "value": None, "state": "outside_window"}
    if bytes_ > 0:
        value = bytes_ if basis == "sum" else bytes_ / max(cs, 1)
        return {**cell, "value": value, "state": "data" if cs >= slots else "partial"}
    if cs > 0:
        return {**cell, "value": 0, "state": "zero"}
    if tracked == 0:
        return {**cell, "value": None, "state": "unknown"}
    return {**cell, "value": None, "state": "no_coverage"}


def _tz_literal(tz: str) -> str:
    # Validated IANA names hold only [A-Za-z0-9_+-/]; quotes are stripped anyway.
    return "'" + tz.replace("'", "").replace("\\", "") + "'"


@router.get("/heatmap")
async def netflow_heatmap(
    win: Window = Depends(window_params(168)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    basis: str = Query(default="avg", description="avg (bytes per covered hour) | sum (byte total)"),
):
    """Day-of-week x hour traffic over the window, in the request's ``tz``.

    The page's hour/day filters are not applied (the heatmap is their picker;
    ``meta.filters`` says so). Each cell carries its basis, the number of local
    date-hours it covers and how many of them show delivery evidence, so a
    collection gap never reads as idle traffic.

    In time zones whose offsets are whole hours (UTC and most zones) the
    complete hours come from the hourly rollup and only partial edge hours
    from raw records (the same planner as the totals), so a 7-day heatmap is
    not a 7-day raw scan; half-hour zones (e.g. Asia/Kolkata) need raw
    records, whose hours do not line up with the rollup's UTC hours. A filter
    the rollup cannot evaluate also reads raw. The part of a window older than
    the data kept is left out and disclosed.
    """
    if basis not in ("avg", "sum"):
        raise invalid_filter("basis must be avg or sum", {"basis": "Unknown basis"})
    require_filter_exporters_visible(f, vis)
    tz = win.tz
    tzl = _tz_literal(tz)
    cell_key = f"toDayOfWeek(%s, 0, {tzl}) * 100 + toHour(%s, {tzl})"
    drop = frozenset({"hour", "dow"})
    rollup = CONVERSATIONS_1H if _whole_hour_tz(tz) else None
    plan = await plan_source(win, f, rollup=rollup, drop=drop)
    sql, params = plan.from_sql({"t": "timestamp", "b": "bytes", "n": ("flows", "toUInt64(1)")}, f, vis, drop)
    flows_q = run_query(
        f"""
        SELECT {cell_key % ('t', 't')} AS cell, sum(b), sum(n), uniqExact(toDate(t, {tzl})),
               uniqExact(toStartOfHour(t, {tzl}))
        FROM {sql} AS src
        GROUP BY cell
        ORDER BY cell
        """,
        params, label="heatmap",
    )
    tracked = await tracking_from()
    eff_from, eff_to = plan.effective_from, plan.effective_to
    intervals = await expected_intervals(db, vis)

    async def receipt_slots():
        # Covered occurrences by receipts: distinct local hours with delivered records.
        if tracked is None or eff_from >= eff_to:
            return []
        where, rparams = compile_filter(coverage_filter(f), EXPORTER_MINUTES, vis)
        cov_sql, cov_params = covered_minutes_sql(where, rparams, eff_from, eff_to, intervals)
        res = await run_query(
            f"""
            SELECT {cell_key % ('minute', 'minute')} AS cell, uniqExactIf(toStartOfHour(minute, {tzl}), has_data)
            FROM {cov_sql} AS cov
            GROUP BY cell
            """,
            cov_params, label="heatmap-receipt-slots",
        )
        return res.result_rows or []

    async def presence_slots():
        # Covered occurrences by any in-scope flow (only when traffic filters narrow the view).
        if not has_traffic_filter(f.without("hour", "dow")):
            return None
        cf = coverage_filter(f)
        cplan = await plan_source(win, cf, rollup=rollup, record=False)
        csql, cparams = cplan.from_sql({"t": "timestamp"}, cf, vis)
        res = await run_query(
            f"SELECT {cell_key % ('t', 't')} AS cell, uniqExact(toStartOfHour(t, {tzl})) "
            f"FROM {csql} AS src GROUP BY cell",
            cparams, label="heatmap-presence",
        )
        return res.result_rows or []

    res, rslots, pslots = await asyncio.gather(flows_q, receipt_slots(), presence_slots())
    by_cell = {int(r[0]): (_int(r[1]), _int(r[2]), _int(r[3]), _int(r[4])) for r in (res.result_rows or [])}
    receipts = {int(r[0]): _int(r[1]) for r in rslots if r[0] is not None}
    if pslots is None:
        presence = {k: v[3] for k, v in by_cell.items()}
    else:
        presence = {int(r[0]): _int(r[1]) for r in pslots if r[0] is not None}
    occ = heatmap_occurrences(eff_from, eff_to, tz, tracked) if eff_from < eff_to else {}
    cells = []
    for dow in range(1, 8):
        for hour in range(24):
            key = dow * 100 + hour
            by, fl, dates, _slots = by_cell.get(key, (0, 0, 0, 0))
            covered = max(receipts.get(key, 0), presence.get(key, 0))
            cells.append(heatmap_cell(dow, hour, occ.get((dow, hour)), by, fl, dates, covered, basis))
    values = [c["value"] for c in cells if isinstance(c["value"], (int, float))]
    slots_total = sum(c["occurrences"] for c in cells)
    slots_covered = sum(c["covered_occurrences"] for c in cells)
    return {
        "max_bytes": max((c["bytes"] for c in cells), default=0),
        "cells": cells,
        "from": _to_iso_utc(win.start),
        "to": _to_iso_utc(win.end),
        "effective_from": _to_iso_utc(eff_from),
        "effective_to": _to_iso_utc(eff_to),
        "tz": tz,
        "basis": basis,
        "value_unit": "bytes" if basis == "sum" else "bytes_per_hour",
        "max_value": max(values, default=0),
        "coverage": {
            "tracking_from": _iso(tracked),
            "hours_in_window": slots_total,
            "hours_covered": slots_covered,
            "coverage_pct": _pct(slots_covered, slots_total, 1),
        },
    }


# ── /exporters ────────────────────────────────────────────────────────────────

RECEIPT_LOOKBACK = timedelta(days=1)


@router.get("/exporters")
async def netflow_exporters(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """Exporters with matching traffic in the window (every exporter, whatever
    exporter is selected): bytes, packets, flows and ``last_flow_in_scope``
    (``last_seen``, kept for older callers). ``last_received_at`` is the last
    delivery from that exporter in the past day, independent of the window
    and filters: liveness must never depend on what the page filters (X-04).
    Receipt *state* comes from /exporters/status."""
    require_filter_exporters_visible(f, vis)
    drop = frozenset({"exporter"})
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H, drop=drop)
    sql, params = plan.from_sql(
        {"e": EXPORTER, "b": "bytes", "p": "packets", "n": ("flows", "toUInt64(1)"), "r": "received_at"},
        f, vis, drop,
    )
    flows_q = run_query(
        f"""
        SELECT {display('e')} AS exporter, sum(b) AS total, sum(p), sum(n), max(r)
        FROM {sql} AS src
        GROUP BY e
        ORDER BY total DESC
        LIMIT 100
        """,
        params, label="exporters",
    )
    where, rparams = compile_filter(coverage_filter(FlowFilter(count=f.count, counting=f.counting)),
                                    EXPORTER_MINUTES, vis)
    rparams = {**rparams, "rcv_from": now_utc() - RECEIPT_LOOKBACK}
    receipts_q = run_query(
        f"""
        SELECT {display(EXPORTER)} AS exporter, max(last_received_at)
        FROM {table_ref(EXPORTER_MINUTES)}
        WHERE minute >= %(rcv_from)s AND {where}
        GROUP BY exporter
        """,
        rparams, label="exporters-receipts",
    )
    res, receipts = await asyncio.gather(flows_q, receipts_q)
    last_rx = {canonical_ip(r[0]) or str(r[0]): r[1] for r in (receipts.result_rows or [])}
    out = []
    for r in res.result_rows or []:
        ip = canonical_ip(r[0]) or str(r[0])
        last_flow = _iso(r[4])
        out.append({
            "exporter_ip": ip,
            "bytes": _int(r[1]),
            "packets": _int(r[2]),
            "flows": _int(r[3]),
            "last_seen": last_flow,
            "last_flow_in_scope": last_flow,
            "last_received_at": _iso(last_rx.get(ip)),
        })
    return out


__all__ = [
    "router", "DUR_EXPR", "DUR_VALID", "RAW_FLAGS_KNOWN", "QOS_FLAGS_KNOWN", "RST_MIN_SAMPLE",
    "BASELINES", "baseline_window", "bucket_seconds", "bucket_grid", "build_points", "compare_delta",
    "rst_payload", "duration_payload", "tcp_flag_stats", "heatmap_occurrences", "heatmap_cell",
    "coverage_filter", "receipt_minutes", "window_coverage", "tracking_from", "aggregate_per_table",
    "per_table", "uses_rollup", "series_for_plan", "rate_at_end",
]
