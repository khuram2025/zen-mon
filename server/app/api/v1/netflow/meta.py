"""NetFlow data-quality metadata (contract §11): ``/coverage`` and ``/retention``.

``/retention`` says how far back each flow table reaches — the TTL applied by
Settings -> Storage and the oldest data actually present — so the UI can explain
why a long range is answered from rollups or is partly unavailable (FE-11). It
also carries the retention configured in Settings -> Storage and the state of
the NetFlow v5 sampling repair (X-02).

``/coverage`` says, per exporter, how much of a window is backed by delivered
records and what those records can support: TCP-flag availability, IPv6 share,
sampling and legacy (pre-v2, field presence unknown) records (FE-03). It reads
``flow_exporter_minutes``, which is keyed by the minute the appliance received
the records, so an exporter with a skewed clock is not mistaken for a silent
one. Coverage of the window is approximated by minutes:

* ``minutes_with_data`` -- minutes in which the exporter delivered countable
  records (``records > 0``: the table's counters apply the volume guard);
* ``minutes_with_receipts`` -- minutes in which anything at all arrived, which
  also counts minutes carrying only NSEL create/deny events or corrupt records.

Minutes before the table started recording (an upgrade) are not counted as
missing: the measured span starts at its first row.

A delivery covers the minute it arrived in *and the exporter's expected export
interval after it* (configured in the exporter settings, else learned from its
cadence, else 60 s: the interval ``/exporters/status`` judges lateness by), so
an exporter that healthily exports every eight minutes is not reported as
80 % collection gaps. The minute still running at the end of the window (or the
partial last minute of a fixed window) is measured too: it counts when records
already arrived in it, and is not held against the window while it is filling.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Mapping

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import storage_service

from .access import FlowVisibility, canonical_ip, flow_visibility
from .common import (
    BUDGET_BACKGROUND,
    BUDGET_INTERACTIVE,
    CONVERSATIONS_1H,
    EXPORTER_MINUTES,
    INTERFACE_5M,
    QOS_1H,
    RAW,
    Retention,
    NetflowError,
    Window,
    _to_iso_utc,
    current_meta,
    get_retention,
    get_sampling_repair,
    new_error_id,
    now_utc,
    run_query,
    table_ref,
    window_params,
)
from .filters import FlowFilter, compile_filter, flow_filter, require_filter_exporters_visible
from .sql import EXPORTER, floor_to, ip_param, table_caps

logger = logging.getLogger(__name__)

router = APIRouter()

# The flow tables /retention reports, as (payload key, table).
_RETENTION_KEYS = (
    ("raw_days", RAW),
    ("rollup_days", CONVERSATIONS_1H),
    ("qos_days", QOS_1H),
    ("interface_days", INTERFACE_5M),
    ("exporter_minutes_days", EXPORTER_MINUTES),
)


def _retention_payload(r: Retention) -> dict:
    return {
        "raw_days": r.raw_days,
        "rollup_days": r.rollup_days,
        "qos_days": r.qos_days,
        "interface_days": r.interface_days,
        "exporter_minutes_days": r.exporter_minutes_days,
        "earliest_raw": _to_iso_utc(r.earliest.get(RAW)),
        "earliest_rollup": _to_iso_utc(r.earliest.get(CONVERSATIONS_1H)),
        "earliest_qos": _to_iso_utc(r.earliest.get(QOS_1H)),
        "earliest_interface": _to_iso_utc(r.earliest.get(INTERFACE_5M)),
        "earliest_exporter_minutes": _to_iso_utc(r.earliest.get(EXPORTER_MINUTES)),
        "known": r.known,
    }


async def _configured_days(db: AsyncSession) -> dict[str, int | None] | None:
    """Retention configured in Settings -> Storage per flow table (table
    override, else its category), None per key when nothing is configured
    (the table keeps its install-time TTL). The TTLs above are what is in
    force; a configured value that differs means applying it failed. None
    overall when the settings cannot be read."""
    try:
        settings = await storage_service.get_retention_settings(db)
    except Exception:
        logger.warning("netflow: storage retention settings unavailable", exc_info=True)
        return None
    categories = settings["categories"]
    overrides = settings["table_overrides"]
    out: dict[str, int | None] = {}
    for key, table in _RETENTION_KEYS:
        days = overrides.get(table, categories.get(storage_service.classify_table(table)))
        out[key] = int(days) if days else None
    return out


@router.get("/retention")
async def netflow_retention(db: AsyncSession = Depends(get_db)):
    """How long each flow table keeps data and the oldest data present, what
    Settings -> Storage has configured, and the state of the NetFlow v5
    sampling repair (X-02): rollup hours before ``verified_from`` could not be
    re-checked, ``unrecoverable_hours`` could not be corrected. ``sampling_repair``
    is null when its state cannot be read."""
    return {
        **_retention_payload(await get_retention()),
        "configured": await _configured_days(db),
        "sampling_repair": await get_sampling_repair(),
    }


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _counts(row) -> dict:
    records, tcp, flagged = int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
    sampled = int(row[5] or 0)
    return {
        "records": records,
        "tcp_records": tcp,
        "tcp_flagged_records": flagged,
        "v6_records": int(row[4] or 0),
        "sampled_records": sampled,
        "max_sampling": int(row[6] or 0) or None,
        "legacy_records": int(row[7] or 0),
        "first_received_at": _to_iso_utc(row[8]),
        "last_received_at": _to_iso_utc(row[9]),
        "flag_coverage_pct": _pct(flagged, tcp),
        "sampled_share": _share(sampled, records),
    }


# ── Delivery coverage primitives (shared with the overview) ───────────────────

EXPECTED_TTL_S = 60.0
_EXPECTED_CACHE_MAX = 64
_expected_cache: dict = {}   # scope (None = every exporter) -> (monotonic time, intervals)


async def expected_intervals(db: AsyncSession | None, vis: FlowVisibility | None = None) -> dict[str, int]:
    """{canonical exporter: expected export interval in seconds}, for the
    exporters ``vis`` may see whose interval is longer than a minute.

    The same rule ``/exporters/status`` uses for lateness: the interval set in
    the exporter's settings, else the learned cadence (p95 gap between receipt
    minutes over the last day, at least 60 s), else 60 s. Cached per scope for
    a minute; when settings or the learned cadence cannot be read the
    exporters fall back to one minute per delivery (the strictest reading,
    never a looser one)."""
    scope = None if vis is None or vis.exporters is None else vis.exporters
    if scope is not None and not scope:
        return {}
    hit = _expected_cache.get(scope)
    if hit is not None and time.monotonic() - hit[0] < EXPECTED_TTL_S:
        return hit[1]
    from app.services import netflow_health as health

    settings: dict[str, dict] = {}
    if db is not None:
        try:
            settings = await health.load_exporter_settings(db)
        except Exception:  # settings are optional here; never fail a read for them
            logger.warning("netflow: exporter settings unavailable for coverage", exc_info=True)
    try:
        # Interactive budget: this runs inside /overview and /coverage
        # requests, which must answer within their own budget (NF-31).
        learned = await health._learned_gaps(now_utc() - health.LEARN_WINDOW, budget=BUDGET_INTERACTIVE,
                                             exporters=sorted(scope) if scope is not None else None)
    except NetflowError:
        learned = {}
    out: dict[str, int] = {}
    for ip in set(settings) | set(learned):
        if scope is not None and ip not in scope:
            continue
        configured = (settings.get(ip) or {}).get("expected_interval_s")
        expected = health.thresholds_for(configured, learned.get(ip)).expected_s
        if expected > 60:
            out[ip] = int(expected)
    if len(_expected_cache) >= _EXPECTED_CACHE_MAX:
        _expected_cache.clear()
    _expected_cache[scope] = (time.monotonic(), out)
    return out


def invalidate_expected_intervals() -> None:
    _expected_cache.clear()


def covered_minutes_sql(
    where: str,
    params: Mapping,
    start: datetime,
    end: datetime,
    intervals: Mapping[str, int] | None,
    *,
    full_to: datetime | None = None,
) -> tuple[str, dict]:
    """``(SELECT e, minute, has_data, running ...)``: the minutes of ``[start,
    end)`` covered by deliveries of the exporters ``where`` selects.

    A delivery in minute m covers m and the minutes after it up to the
    exporter's expected interval (``intervals``: seconds per canonical IP;
    unlisted exporters cover just their minute), so deliveries shortly before
    ``start`` cover its first minutes. ``has_data``: the delivery carried
    countable records (the table's counters apply the volume guard), as
    opposed to anything at all. ``running``: the minute is at or after
    ``full_to`` (the minute still filling). Rows are not merged: aggregate with
    ``uniqExact(minute)`` / ``uniqExactIf(minute, has_data)``."""
    out = dict(params)
    out.update(nf_cv_from=floor_to(start, 60), nf_cv_to=end, nf_cv_full=full_to or end)
    spans = {ip_param(ip): math.ceil(s / 60) for ip, s in (intervals or {}).items() if s > 60}
    running = "minute >= %(nf_cv_full)s"
    if not spans:
        return (
            f"(SELECT {EXPORTER} AS e, minute, records > 0 AS has_data, {running} AS running "
            f"FROM {table_ref(EXPORTER_MINUTES)} "
            f"WHERE minute >= %(nf_cv_from)s AND minute < %(nf_cv_to)s AND {where})",
            out,
        )
    out.update(
        nf_cv_lookback=floor_to(start, 60) - timedelta(minutes=max(spans.values()) - 1),
        nf_cv_ips=list(spans),
        nf_cv_spans=list(spans.values()),
    )
    span = (f"transform({EXPORTER}, CAST(%(nf_cv_ips)s AS Array(IPv6)), "
            "CAST(%(nf_cv_spans)s AS Array(UInt32)), toUInt32(1))")
    # The covered minute is aliased cm inside: ClickHouse resolves SELECT
    # aliases in WHERE, and the WHERE below must see the stored column.
    inner = (
        f"SELECT {EXPORTER} AS e, minute + toIntervalMinute(nf_cv_off) AS cm, records > 0 AS has_data "
        f"FROM {table_ref(EXPORTER_MINUTES)} ARRAY JOIN range({span}) AS nf_cv_off "
        f"WHERE minute >= %(nf_cv_lookback)s AND minute < %(nf_cv_to)s AND {where}"
    )
    return (
        f"(SELECT e, cm AS minute, has_data, cm >= %(nf_cv_full)s AS running FROM ({inner}) "
        f"WHERE cm >= %(nf_cv_from)s AND cm < %(nf_cv_to)s)",
        out,
    )


def measured_span(win_start: datetime, win_end: datetime, tracked: datetime | None,
                  now: datetime) -> tuple[datetime, datetime, datetime]:
    """(measured_from, full_to, measured_to) of a window's delivery coverage.

    Minutes before delivery tracking began are not measured. ``full_to`` ends
    the last complete minute; ``measured_to`` also takes in the minute still
    running at ``now`` (or the partial last minute of a fixed window), which
    counts only when something already arrived in it (see ``minute_total``)."""
    measured_from = floor_to(win_start, 60)
    if tracked is not None:
        measured_from = max(measured_from, floor_to(tracked, 60))
    stop = min(win_end, now)
    full_to = floor_to(stop, 60)
    measured_to = full_to + timedelta(seconds=60) if stop > full_to else full_to
    if measured_from >= measured_to:
        return measured_to, measured_to, measured_to
    return measured_from, max(full_to, measured_from), measured_to


def full_minutes(measured_from: datetime, full_to: datetime) -> int:
    return max(0, int((full_to - measured_from).total_seconds() // 60))


# Columns 1..9 feed _counts. Rows are not merged (no FINAL): the sums are
# exact over parts.
_SUMS = """
    sum(records), sum(tcp_records), sum(tcp_flagged_records), sum(v6_records),
    sum(sampled_records), max(max_sampling), sum(legacy_records),
    min(first_received_at), max(last_received_at)
"""
# Over covered_minutes_sql: minutes with countable records, minutes with any
# receipt, and whether the running minute already has records (0/1).
_MINUTE_COUNTS = "uniqExactIf(minute, has_data), uniqExact(minute), uniqExactIf(minute, has_data AND running)"


def minute_total(full: int, running_with_data: int) -> int:
    """Minutes a window is measured over: the complete ones, plus the running
    minute once records arrived in it."""
    return full + (1 if running_with_data else 0)


def _minutes(row, full: int) -> dict:
    """``row``: (with_data, with_receipts, running_with_data)."""
    with_data = int(row[0] or 0)
    minutes = minute_total(full, int(row[2] or 0))
    return {
        "minutes_with_data": with_data,
        "minutes_with_receipts": int(row[1] or 0),
        "minutes_in_window": minutes,
        "coverage_pct": _pct(min(with_data, minutes), minutes),
    }


@router.get("/coverage")
async def netflow_coverage(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Per-exporter delivery coverage and field availability over the window.

    Only the exporter dimension (and count=edge's exporter roles) applies:
    coverage describes what exporters delivered, not a traffic slice. A
    delivery covers its exporter's expected export interval; the running
    minute counts once records arrived in it (module docstring).
    """
    require_filter_exporters_visible(f, vis)
    caps = table_caps(EXPORTER_MINUTES)
    drop = f.active_keys() - caps.supports
    drop |= caps.unsupported(f, drop)
    where, params = compile_filter(f, EXPORTER_MINUTES, vis, drop)

    retention = await get_retention()
    if not retention.known:
        # ClickHouse could not be read: an error, never "no delivery data
        # recorded yet" (NF-31) -- the page must not report a collection gap.
        error_id = new_error_id()
        logger.warning("netflow coverage: flow retention unreadable [%s]", error_id)
        raise NetflowError(503, "Flow data is temporarily unavailable", code="unavailable", error_id=error_id)
    now = now_utc()
    earliest = retention.earliest.get(EXPORTER_MINUTES)
    measured_from, full_to, measured_to = measured_span(win.start, win.end, earliest, now)
    full = full_minutes(measured_from, full_to)
    meta = current_meta()
    meta.set_source("rollup", measured_from, measured_to, 60, retention_checked=True)
    if earliest is None:
        meta.warn("coverage_unavailable", "No exporter delivery data has been recorded yet")
    elif measured_from > floor_to(win.start, 60):
        meta.warn("coverage_partial", "Delivery tracking started after the start of the range; "
                                      "coverage is measured from its first record")

    exporters: list[dict] = []
    # Nothing measured is unknown, not zero.
    totals: dict = dict.fromkeys([*_minutes((None,) * 3, 0), *_counts((None,) * 10)])
    if earliest is not None and measured_to > measured_from:
        intervals = await expected_intervals(db, vis)
        counts_params = {**params, "nf_cov_from": measured_from, "nf_cov_to": measured_to}
        base = (f"FROM {table_ref(EXPORTER_MINUTES)} "
                f"WHERE minute >= %(nf_cov_from)s AND minute < %(nf_cov_to)s AND {where}")
        cov_sql, cov_params = covered_minutes_sql(where, params, measured_from, measured_to, intervals,
                                                  full_to=full_to)
        per_exporter, overall, per_minutes, all_minutes = await asyncio.gather(
            run_query(f"SELECT {EXPORTER} AS e, {_SUMS} {base} GROUP BY e ORDER BY 2 DESC",
                      counts_params, label="coverage"),
            run_query(f"SELECT NULL, {_SUMS} {base}", counts_params, label="coverage-total"),
            run_query(f"SELECT e, {_MINUTE_COUNTS} FROM {cov_sql} AS cov GROUP BY e", cov_params,
                      label="coverage-minutes"),
            run_query(f"SELECT {_MINUTE_COUNTS} FROM {cov_sql} AS cov", cov_params,
                      label="coverage-minutes-total"),
        )
        minutes_by = {canonical_ip(r[0]): tuple(r[1:]) for r in per_minutes.result_rows or []}
        for row in per_exporter.result_rows or []:
            ip = canonical_ip(row[0])
            exporters.append({"exporter": ip, **_minutes(minutes_by.get(ip, (0, 0, 0)), full), **_counts(row)})
        total_row = overall.result_rows[0] if overall.result_rows else None
        minutes_row = all_minutes.result_rows[0] if all_minutes.result_rows else (0, 0, 0)
        if total_row is not None:
            totals = {**_minutes(minutes_row, full), **_counts(total_row)}
    meta.set_sampling(totals["sampled_share"], totals["max_sampling"])
    return {
        "window": {
            **win.as_meta(),
            "measured_from": _to_iso_utc(measured_from),
            "measured_to": _to_iso_utc(measured_to),
            "minutes_in_window": (totals["minutes_in_window"] if totals["minutes_in_window"] is not None
                                  else full if earliest is not None else 0),
        },
        "exporters": exporters,
        "totals": totals,
        "retention": _retention_payload(retention),
    }
