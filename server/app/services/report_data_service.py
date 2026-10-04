"""
Live JSON data builders for the on-screen Reports section.

The PDF/Excel/CSV exporters live in `report_service.py` and `export_service.py`.
This module is the live-UI counterpart: each function returns a plain dict
suitable for JSON serialization to the dashboard.

Reuses the data fetchers and aggregation helpers from `report_service.py`
to stay consistent with the exported reports.

Semantics shared by the builders (see the Availability contract):
- `None` means not measured, never 0.
- Incidents are outage episodes (a down transition paired with the next
  non-down one), not alert records. The status logs' `duration_sec` column is
  always 0, so durations only exist as the gap between paired transitions.
- Service availability uses the confirmed-incident basis shared with the
  service list and detail pages (app.services.service_availability).
- A failed sub-query is reported in `data_errors` rather than rendered as an
  empty (healthy-looking) result.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_ch_client
from app.services.availability_policy import load_default_target, resolve_target
from app.services.ping_rollups import ping_table_for_hours, rtt_agg_sql, uptime_agg_sql
from app.services.report_service import (
    _fetch_devices,
    _fetch_service_checks,
    _fetch_alerts,
    _fetch_service_metrics,
    _fetch_device_maintenance_windows,
    _to_naive_utc,
    _mttr_seconds,
)
from app.services.service_availability import read_fleet_segments, summarize

logger = logging.getLogger(__name__)

MaintWindows = dict[str, list[tuple[datetime, datetime]]]

DOWN_STATES = frozenset({"down", "offline"})
AT_RISK_BUDGET_PCT = 75.0


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return _aware(ts).isoformat() if ts is not None else None


def _in_maintenance(windows: MaintWindows, device_id: str, ts: datetime) -> bool:
    wins = windows.get(str(device_id))
    if not wins:
        return False
    t = _to_naive_utc(ts) if ts.tzinfo else ts
    return any(s <= t <= e for s, e in wins)


def _maintenance_minutes(windows: MaintWindows, device_ids: Optional[set[str]] = None) -> float:
    """Total device-minutes of planned maintenance inside the query window."""
    total = 0.0
    for did, wins in windows.items():
        if device_ids is not None and did not in device_ids:
            continue
        for s, e in wins:
            total += max(0.0, (e - s).total_seconds() / 60)
    return round(total, 1)


class DataErrors(list):
    """Sub-query failures surfaced to the UI as `data_errors` (partial data)."""

    def add(self, source: str, exc: BaseException) -> None:
        logger.warning("report sub-query %s failed", source, exc_info=exc)
        # The exception class only: driver messages can carry SQL and hostnames.
        self.append({"source": source,
                     "message": f"{source.replace('_', ' ')} unavailable ({type(exc).__name__})"})

    def run(self, source: str, fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as exc:
            self.add(source, exc)
            return default


# ---------------------------------------------------------------------------
# Time-range helpers
# ---------------------------------------------------------------------------

def _normalise_window(
    from_time: Optional[datetime],
    to_time: Optional[datetime],
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = to_time or now
    start = from_time or (end - timedelta(hours=24))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _prior_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    delta = end - start
    return start - delta, start


_BUCKET_SECONDS = {"5 MINUTE": 300, "30 MINUTE": 1800, "1 HOUR": 3600,
                   "6 HOUR": 21600, "1 DAY": 86400}


def _trend_bucket(start: datetime, end: datetime) -> tuple[str, str]:
    """
    Return (clickhouse_interval, label) appropriate for the window length.
    Used for availability trend bucketing.
    """
    span = end - start
    if span <= timedelta(hours=6):
        return "5 MINUTE", "5m"
    if span <= timedelta(days=2):
        return "30 MINUTE", "30m"
    if span <= timedelta(days=14):
        return "1 HOUR", "1h"
    if span <= timedelta(days=60):
        return "6 HOUR", "6h"
    return "1 DAY", "1d"


def _delta(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    if curr is None or prev is None:
        return None
    return round(curr - prev, 2)


def _minutes(seconds: Optional[float]) -> Optional[float]:
    return round(seconds / 60, 2) if seconds is not None else None


# ---------------------------------------------------------------------------
# Internal aggregation helpers
# ---------------------------------------------------------------------------

def _group_rows(rows: list[dict], key: str) -> dict[str, list[dict]]:
    """Bucket rows by a string key in one pass.

    The per-entity loops below used to re-scan the whole sample list once per
    device/service, which is O(entities x samples) — 30+ seconds on a 7-day
    window. Bucketing first makes the same work O(samples)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[str(r[key])].append(r)
    return out


def _maintenance_exclusion_sql(windows: MaintWindows) -> tuple[str, dict]:
    """`AND NOT (...)` clause dropping samples inside planned maintenance.

    Mirrors the sample-level exclusion in `report_service` exactly (inclusive
    on both ends, naive-UTC timestamps) so the aggregated figures here agree
    with the exported reports."""
    clauses: list[str] = []
    params: dict = {}
    i = 0
    for device_id, wins in windows.items():
        for start, end in wins:
            clauses.append(
                f"(toString(device_id) = %(mdev{i})s "
                f"AND timestamp >= %(mstart{i})s AND timestamp <= %(mend{i})s)"
            )
            params[f"mdev{i}"] = str(device_id)
            params[f"mstart{i}"] = start
            params[f"mend{i}"] = end
            i += 1
    if not clauses:
        return "", {}
    return "AND NOT (" + " OR ".join(clauses) + ")", params


def _ping_table(start: datetime, end: datetime) -> str:
    """Source table for this window — see app.services.ping_rollups.

    Raw `ping_metrics` only survives a fraction of the longest windows the UI
    offers, so a 30-day report read from raw was really a report on however
    much raw data still existed.
    """
    return ping_table_for_hours((end - start).total_seconds() / 3600)


def _device_sample_counts(start: datetime, end: datetime,
                          windows: MaintWindows | None = None,
                          device_ids: Optional[list[str]] = None) -> dict[str, tuple[float, float]]:
    """device_id -> (up_samples, total_samples) for the window, aggregated in
    ClickHouse rather than by pulling every raw ping row into Python.

    Rollup rows carry a whole bucket, so "samples" are weighted by
    `sample_count` there and come back fractional; callers only ever take
    ratios of them. Only `device_ids` (current, visible inventory) are counted
    when given; history of deleted devices must not move the headline.
    """
    if device_ids is not None and not device_ids:
        return {}
    maint_sql, maint_params = _maintenance_exclusion_sql(windows or {})
    table = _ping_table(start, end)
    id_sql = "AND toString(device_id) IN %(ids)s" if device_ids is not None else ""
    q = f"""
        SELECT toString(device_id) AS did, {uptime_agg_sql(table)}
        FROM zenplus.{table}
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {id_sql}
        {maint_sql}
        GROUP BY did
    """
    params = {"start": start, "end": end, **maint_params}
    if device_ids is not None:
        params["ids"] = list(device_ids)
    rs = get_ch_client().query(q, parameters=params)
    return {str(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in rs.result_rows}


def _device_ping_stats(start: datetime, end: datetime,
                       windows: MaintWindows | None = None,
                       device_ids: Optional[list[str]] = None,
                       outage_counts: Optional[dict[str, int]] = None) -> dict[str, dict]:
    """Per-device uptime %, RTT stats and outage-episode count for the window.

    Aggregated in ClickHouse. The Python equivalent had to stream every raw
    ping sample of the window into the API process and then re-scan that list
    three times per device; on a 7-day window that was ~33s of pure CPU.

    Outage episodes cannot come from the rollups — a bucket only knows what
    fraction of it was up, not where the transitions were. They come from
    the paired `device_status_log` episodes the outage timeline also uses.
    """
    if device_ids is not None and not device_ids:
        return {}
    maint_sql, maint_params = _maintenance_exclusion_sql(windows or {})
    table = _ping_table(start, end)
    id_sql = "AND toString(device_id) IN %(ids)s" if device_ids is not None else ""
    q = f"""
        SELECT toString(device_id) AS did,
               {uptime_agg_sql(table)},
               {rtt_agg_sql(table)}
        FROM zenplus.{table}
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {id_sql}
        {maint_sql}
        GROUP BY did
    """
    params = {"start": start, "end": end, **maint_params}
    if device_ids is not None:
        params["ids"] = list(device_ids)
    rs = get_ch_client().query(q, parameters=params)
    episodes = outage_counts or {}
    out: dict[str, dict] = {}
    for did, up, total, avg_rtt, p95_rtt, rtt_samples in rs.result_rows:
        total = float(total or 0)
        if total <= 0:
            continue
        has_rtt = float(rtt_samples or 0) > 0
        out[str(did)] = {
            "uptime_pct": float(up or 0) / total * 100,
            "avg_rtt": float(avg_rtt) if has_rtt and avg_rtt is not None else None,
            "p95_rtt": float(p95_rtt) if has_rtt and p95_rtt is not None else None,
            "outage_count": episodes.get(str(did), 0),
        }
    return out


def _measured_coverage(start: datetime, end: datetime,
                       device_ids: Optional[list[str]] = None) -> dict:
    """Where the ping samples backing this window actually begin and end.

    A window is only as long as the data under it. Ask for 30 days on an
    appliance that has been collecting for eight and every figure on the page
    describes eight days while the header says a month. First-to-last span is
    not continuous coverage: internal gaps are not detected here.
    """
    table = _ping_table(start, end)
    if device_ids is not None and not device_ids:
        return {"source_table": table, "from": None, "to": None}
    id_sql = "AND toString(device_id) IN %(ids)s" if device_ids is not None else ""
    q = f"""
        SELECT min(timestamp), max(timestamp), count()
        FROM zenplus.{table}
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        {id_sql}
    """
    params = {"start": start, "end": end}
    if device_ids is not None:
        params["ids"] = list(device_ids)
    rs = get_ch_client().query(q, parameters=params)
    if not rs.result_rows or not rs.result_rows[0][2]:
        return {"source_table": table, "from": None, "to": None}
    first, last, _n = rs.result_rows[0]
    return {"source_table": table, "from": _iso(first), "to": _iso(last)}


def _coverage_block(coverage: Optional[dict], start: datetime, end: datetime,
                    measured: int, expected: int, table: str) -> dict:
    out = dict(coverage or {"source_table": table, "from": None, "to": None})
    first, last = out.get("from"), out.get("to")
    span = (end - start).total_seconds()
    covered = None
    if first and last and span > 0:
        lo = datetime.fromisoformat(first)
        hi = datetime.fromisoformat(last)
        covered = max(0.0, min(100.0, (hi - lo).total_seconds() / span * 100))
    out.update({
        "measured_entities": measured,
        "expected_entities": expected,
        "covered_pct": covered,
        "last_observation": last,
    })
    return out


def _pct_from_counts(counts: dict[str, tuple[float, float]],
                     device_ids: Optional[set[str]] = None) -> Optional[float]:
    """Sample-weighted availability over the given devices (all when None)."""
    up = total = 0.0
    for did, (u, t) in counts.items():
        if device_ids is not None and did not in device_ids:
            continue
        up += u
        total += t
    return up / total * 100 if total else None


def _measured_ids(counts: dict[str, tuple[float, float]]) -> set[str]:
    return {did for did, (_u, t) in counts.items() if t > 0}


def _availability_trend(start: datetime, end: datetime,
                        windows: MaintWindows | None = None,
                        device_ids: Optional[list[str]] = None) -> list[dict]:
    """Fleet availability bucketed over time; buckets without samples are null.

    Maintenance is excluded with the same sample-level clause the headline KPI
    uses, so a bucket straddling a window edge agrees with the KPI. Every
    bucket of the window is returned so a collection gap reads as a gap, not
    as an unbroken healthy line.
    """
    interval, _ = _trend_bucket(start, end)
    step = _BUCKET_SECONDS[interval]
    by_bucket: dict[int, Optional[float]] = {}
    if device_ids is None or device_ids:
        table = _ping_table(start, end)
        maint_sql, maint_params = _maintenance_exclusion_sql(windows or {})
        id_sql = "AND toString(device_id) IN %(ids)s" if device_ids is not None else ""
        q = f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval}) AS ts,
                   {uptime_agg_sql(table)}
            FROM zenplus.{table}
            WHERE timestamp >= %(start)s AND timestamp <= %(end)s
            {id_sql}
            {maint_sql}
            GROUP BY ts
            ORDER BY ts
        """
        params = {"start": start, "end": end, **maint_params}
        if device_ids is not None:
            params["ids"] = list(device_ids)
        rs = get_ch_client().query(q, parameters=params)
        for ts, up, total in rs.result_rows:
            by_bucket[int(_aware(ts).timestamp())] = (
                float(up or 0) / float(total) * 100 if total else None)
    first = math.floor(start.timestamp() / step) * step
    keys = set(range(first, int(math.ceil(end.timestamp())), step)) | set(by_bucket)
    return [
        {"ts": datetime.fromtimestamp(k, tz=timezone.utc).isoformat(),
         "availability_pct": by_bucket.get(k)}
        for k in sorted(keys)
    ]


def _alert_volume_by_severity(start: datetime, end: datetime, alerts: list[dict]) -> list[dict]:
    """Bucket alert counts per (day, severity)."""
    if not alerts:
        return []
    span = end - start
    fmt = "%Y-%m-%dT%H:00:00" if span <= timedelta(days=2) else "%Y-%m-%d"
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"critical": 0, "warning": 0, "info": 0})
    for a in alerts:
        t = a.get("triggered_at")
        if not t:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        key = t.strftime(fmt)
        sev = (a.get("severity") or "info").lower()
        if sev not in buckets[key]:
            buckets[key][sev] = 0
        buckets[key][sev] += 1
    return [
        {"ts": k + ("Z" if "T" in k else "T00:00:00Z"), **v}
        for k, v in sorted(buckets.items())
    ]


# ---------------------------------------------------------------------------
# Outage episodes
# ---------------------------------------------------------------------------

def _is_down(status) -> bool:
    return (status or "").lower() in DOWN_STATES


def pair_outage_episodes(
    transitions: list[tuple[datetime, Optional[str], Optional[str]]],
    recovered_at: Optional[datetime] = None,
) -> list[tuple[Optional[datetime], Optional[datetime]]]:
    """Down→recovery episodes from chronological (ts, old_status, new_status).

    The first row may be the last transition before the fetched span. When
    the log opens on a recovery (old status down, nothing earlier) the start
    is unknown and returned as None. `recovered_at` closes an episode still
    open at the end of the rows (first non-down transition after the span);
    an episode without any recovery has ended_at None (ongoing).
    """
    out: list[tuple[Optional[datetime], Optional[datetime]]] = []
    open_at: Optional[datetime] = None
    is_open = False
    for i, (ts, old, new) in enumerate(transitions):
        ts = _aware(ts)
        if _is_down(new):
            if not is_open:
                open_at, is_open = ts, True
        elif is_open:
            out.append((open_at, ts))
            is_open = False
        elif i == 0 and _is_down(old):
            out.append((None, ts))
    if is_open:
        out.append((open_at, _aware(recovered_at) if recovered_at else None))
    return out


def _overlap_seconds(lo: datetime, hi: datetime, wins) -> float:
    total = 0.0
    for s, e in wins or ():
        s, e = _aware(s), _aware(e)
        a, b = max(lo, s), min(hi, e)
        if b > a:
            total += (b - a).total_seconds()
    return total


def window_episodes(
    episodes: dict[str, list[tuple[Optional[datetime], Optional[datetime]]]],
    start: datetime,
    end: datetime,
    floor: datetime,
    maintenance: Optional[MaintWindows] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Episodes intersecting [start, end), clipped to it.

    `floor` is where the fetched log begins: an episode with an unknown start
    is only counted from there. Episodes that begin inside a planned
    maintenance window are excluded, and maintenance time is subtracted from
    the in-window downtime, matching the headline's sample exclusion.
    """
    now = now or datetime.now(timezone.utc)
    maintenance = maintenance or {}
    out: list[dict] = []
    for eid, eps in episodes.items():
        wins = maintenance.get(str(eid))
        for started, ended in eps:
            lo = max(start, started or floor)
            hi = min(end, ended or end)
            if hi <= lo:
                continue
            # Maintenance windows arrive clamped to the report range, so judge
            # the in-window start: planned work covering it excludes the episode.
            if wins and _in_maintenance(maintenance, eid, lo):
                continue
            in_window = max(0.0, (hi - lo).total_seconds() - _overlap_seconds(lo, hi, wins))
            duration = (max(0.0, ((ended or now) - started).total_seconds())
                        if started is not None else None)
            out.append({
                "entity_id": str(eid),
                "started_at": started,
                "clipped_start": lo,
                "ended_at": ended,
                "ongoing": ended is None,
                "duration_s": duration,
                "in_window_s": in_window,
                "completed_in_window": (ended is not None and started is not None
                                        and start <= ended <= end),
            })
    return out


def _episode_stats(eps: list[dict]) -> dict:
    completed = [e["duration_s"] for e in eps if e["completed_in_window"]]
    return {
        "count": len(eps),
        "ongoing": sum(1 for e in eps if e["ongoing"]),
        "downtime_s": sum(e["in_window_s"] for e in eps),
        "mttr_s": sum(completed) / len(completed) if completed else None,
    }


# Completed-episode duration classes for the time-to-recover distribution.
_RECOVERY_CLASSES = (
    (5 * 60, "lt_5m", "< 5 min"),
    (30 * 60, "5m_30m", "5–30 min"),
    (2 * 3600, "30m_2h", "30 min–2 h"),
    (8 * 3600, "2h_8h", "2–8 h"),
    (24 * 3600, "8h_1d", "8–24 h"),
    (None, "gte_1d", "≥ 1 day"),
)


def _recovery_histogram(eps: list[dict]) -> list[dict]:
    """Episodes that recovered inside the window, by full outage duration —
    the distribution behind the MTTR headline."""
    counts = {k: 0 for _, k, _ in _RECOVERY_CLASSES}
    for e in eps:
        if not e["completed_in_window"] or e["duration_s"] is None:
            continue
        for limit, k, _ in _RECOVERY_CLASSES:
            if limit is None or e["duration_s"] < limit:
                counts[k] += 1
                break
    return [{"key": k, "label": label, "count": counts[k]}
            for _, k, label in _RECOVERY_CLASSES]


def _outage_activity(eps: list[dict], start: datetime, end: datetime,
                     maintenance: Optional[MaintWindows] = None) -> list[dict]:
    """Per trend bucket: outage episodes that started in it, distinct entities
    down at any point in it, and down-minutes inside it (maintenance time
    subtracted like the headline). Buckets align with the availability trend."""
    interval, _ = _trend_bucket(start, end)
    step = _BUCKET_SECONDS[interval]
    first = math.floor(start.timestamp() / step) * step
    last = int(math.ceil(end.timestamp()))
    keys = list(range(first, last, step))
    if not keys:
        return []
    started = [0] * len(keys)
    affected: list[set[str]] = [set() for _ in keys]
    down_s = [0.0] * len(keys)
    maintenance = maintenance or {}
    for e in eps:
        lo = e["clipped_start"]
        hi = min(end, e["ended_at"] or end)
        if hi <= lo:
            continue
        if e["started_at"] is not None and start <= e["started_at"] < end:
            i = int((e["started_at"].timestamp() - first) // step)
            if 0 <= i < len(keys):
                started[i] += 1
        wins = maintenance.get(e["entity_id"])
        i0 = max(0, int((lo.timestamp() - first) // step))
        i1 = min(len(keys) - 1, int((hi.timestamp() - first) // step))
        for i in range(i0, i1 + 1):
            b_lo = datetime.fromtimestamp(keys[i], tz=timezone.utc)
            b_hi = b_lo + timedelta(seconds=step)
            a, b = max(lo, b_lo), min(hi, b_hi)
            if b <= a:
                continue
            secs = (b - a).total_seconds() - _overlap_seconds(a, b, wins)
            if secs > 0:
                down_s[i] += secs
                affected[i].add(e["entity_id"])
    return [
        {"ts": datetime.fromtimestamp(k, tz=timezone.utc).isoformat(),
         "outages_started": started[i],
         "entities_down": len(affected[i]),
         "downtime_minutes": round(down_s[i] / 60, 2)}
        for i, k in enumerate(keys)
    ]



def _device_transitions(span_start: datetime, end: datetime,
                        device_ids: list[str]) -> dict[str, list[tuple[datetime, datetime]]]:
    """device_id -> paired outage episodes over [span_start, end].

    Includes the last transition before the span (an outage already running
    when the window opens) and the first recovery after it (so an outage that
    outlived a historical window is not reported as ongoing).
    """
    if not device_ids:
        return {}
    ch = get_ch_client()
    params = {"f": span_start, "t": end, "ids": list(device_ids)}
    prior = ch.query("""
        SELECT toString(device_id) AS did, max(timestamp),
               argMax(old_status, timestamp), argMax(new_status, timestamp)
        FROM zenplus.device_status_log
        WHERE timestamp < %(f)s AND toString(device_id) IN %(ids)s
        GROUP BY did
    """, parameters=params).result_rows
    rows = ch.query("""
        SELECT toString(device_id) AS did, timestamp, old_status, new_status
        FROM zenplus.device_status_log
        WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND toString(device_id) IN %(ids)s
        ORDER BY did, timestamp
    """, parameters=params).result_rows
    by_dev: dict[str, list[tuple]] = defaultdict(list)
    for did, ts, old, new in prior:
        by_dev[str(did)].append((ts, old, new))
    for did, ts, old, new in rows:
        by_dev[str(did)].append((ts, old, new))
    still_down = [did for did, tr in by_dev.items() if tr and _is_down(tr[-1][2])]
    recovered: dict[str, datetime] = {}
    if still_down:
        recovered = {str(r[0]): r[1] for r in ch.query("""
            SELECT toString(device_id) AS did, min(timestamp)
            FROM zenplus.device_status_log
            WHERE timestamp > %(t)s AND toString(device_id) IN %(ids)s
              AND lower(new_status) NOT IN ('down', 'offline')
            GROUP BY did
        """, parameters={"t": end, "ids": still_down}).result_rows}
    return {did: pair_outage_episodes(tr, recovered.get(did)) for did, tr in by_dev.items()}


def device_outage_episodes(start: datetime, end: datetime, device_ids: list[str],
                           maintenance: Optional[MaintWindows] = None) -> list[dict]:
    """Maintenance-aware device outage episodes for one window (report engines)."""
    start, end = _aware(start), _aware(end)
    raw = _device_transitions(start, end, device_ids)
    return window_episodes(raw, start, end, start, maintenance)


def confirmed_down_episodes(segments, end: datetime, excluded=None) -> list[tuple[datetime, Optional[datetime]]]:
    """Contiguous confirmed-down runs, split exactly where summarize() splits
    incidents, so `outages` equals the detail page's incident count. A run
    that reaches the window end is ongoing (ended_at None). Maintenance
    (`excluded`) time is removed; a run interrupted only by it stays one."""
    from app.services.service_availability import _bridged, merge_intervals, subtract_intervals
    excluded = merge_intervals(excluded) if excluded else []
    if excluded:
        segments = subtract_intervals(segments, excluded)
    out: list[list] = []
    previous_end = None
    for lo, hi, fraction in segments:
        if hi <= lo:
            continue
        if fraction < 1:
            if out and out[-1][2] and _bridged(previous_end, lo, excluded):
                out[-1][1] = hi
            else:
                out.append([lo, hi, True])
        elif out:
            out[-1][2] = False
        previous_end = hi
    return [(lo, None if hi >= end else hi) for lo, hi, _open in out]


def _merged_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    cur_lo = cur_hi = None
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


async def _active_critical_now(db: AsyncSession, visible_tags: Optional[list[str]]) -> int:
    """Critical alerts active right now, whatever report window is selected."""
    where = "status = 'active' AND lower(severity) = 'critical'"
    params: dict = {}
    if visible_tags is not None:
        where += f" AND {scoping.alert_visible('alerts')}"
        params[scoping.SCOPE_PARAM] = visible_tags
    row = (await db.execute(text(f"SELECT COUNT(*) FROM alerts WHERE {where}"), params)).first()
    return int(row[0] or 0) if row else 0


def _device_state_now(status: Optional[str], in_maintenance: bool) -> str:
    if in_maintenance:
        return "maintenance"
    s = (status or "").lower()
    if s in ("up", "online"):
        return "up"
    if s in DOWN_STATES:
        return "down"
    if s in ("degraded", "warning"):
        return "degraded"
    return "unknown"


def _status_now(states: list[str]) -> dict:
    out = {"up": 0, "down": 0, "degraded": 0, "maintenance": 0, "unknown": 0}
    for s in states:
        out[s] += 1
    out["total"] = len(states)
    return out


def _breakdown(devices: list[dict], key, counts: dict[str, tuple[float, float]],
               states: dict[str, str],
               downtime_s: Optional[dict[str, float]] = None,
               outages: Optional[dict[str, int]] = None) -> list[dict]:
    """{name, devices, up_now, down_now, availability_pct, measured,
    downtime_minutes, outages} per bucket, over the same counts (bounds,
    maintenance, inventory) and outage episodes as the headline."""
    buckets: dict[str, set[str]] = defaultdict(set)
    for d in devices:
        buckets[key(d)].add(str(d["id"]))
    measured = _measured_ids(counts)
    downtime_s = downtime_s or {}
    outages = outages or {}
    rows = [
        {
            "name": name,
            "devices": len(ids),
            "up_now": sum(1 for i in ids if states.get(i) == "up"),
            "down_now": sum(1 for i in ids if states.get(i) == "down"),
            "availability_pct": _pct_from_counts(counts, ids),
            "measured": len(ids & measured),
            "downtime_minutes": round(sum(downtime_s.get(i, 0.0) for i in ids) / 60, 2),
            "outages": sum(outages.get(i, 0) for i in ids),
        }
        for name, ids in buckets.items()
    ]
    rows.sort(key=lambda r: (-r["devices"], r["name"]))
    return rows


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

async def build_executive(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    visible_tags: Optional[list[str]] = None,
) -> dict:
    start, end = _normalise_window(from_time, to_time)
    prev_start, prev_end = _prior_window(start, end)
    errors = DataErrors()

    devices = await _fetch_devices(db, visible_tags=visible_tags)
    device_map = {str(d["id"]): d for d in devices}
    ids = list(device_map)
    id_set = set(ids)
    alerts = await _fetch_alerts(db, start, end, visible_tags=visible_tags)
    prev_alerts = await _fetch_alerts(db, prev_start, prev_end, visible_tags=visible_tags)
    active_critical = await _active_critical_now(db, visible_tags)
    target, target_source = await load_default_target(db)
    # Planned maintenance is excluded from every availability figure so SLA
    # numbers only reflect unplanned downtime.
    maint = await _fetch_device_maintenance_windows(db, start, end)
    prev_maint = await _fetch_device_maintenance_windows(db, prev_start, prev_end)
    now = datetime.now(timezone.utc)
    maint_now = await _fetch_device_maintenance_windows(db, now, now + timedelta(seconds=1))
    # Blocking ClickHouse driver calls — keep them off the event loop.
    counts, prev_counts, transitions, trend, coverage = await asyncio.gather(
        asyncio.to_thread(errors.run, "device_availability", _device_sample_counts,
                          start, end, maint, ids, default={}),
        asyncio.to_thread(errors.run, "prior_device_availability", _device_sample_counts,
                          prev_start, prev_end, prev_maint, ids, default={}),
        asyncio.to_thread(errors.run, "device_status_log", _device_transitions,
                          prev_start, end, ids, default={}),
        asyncio.to_thread(errors.run, "availability_trend", _availability_trend,
                          start, end, maint, ids, default=[]),
        asyncio.to_thread(errors.run, "coverage", _measured_coverage,
                          start, end, ids, default=None),
    )
    prev_failed = any(e["source"] == "prior_device_availability" for e in errors)

    avail = _pct_from_counts(counts, id_set)
    prev_avail = _pct_from_counts(prev_counts, id_set)
    measured = _measured_ids(counts) & id_set

    episodes = window_episodes(transitions, start, end, prev_start, maint, now)
    prev_episodes = window_episodes(transitions, prev_start, prev_end, prev_start, prev_maint, now)
    cur, prev = _episode_stats(episodes), _episode_stats(prev_episodes)
    comparable = bool(prev_counts) and not prev_failed
    mttr_min = _minutes(cur["mttr_s"])
    prev_mttr_min = _minutes(prev["mttr_s"]) if comparable else None

    alert_mttr_s = _mttr_seconds(alerts)
    critical_raised = sum(1 for a in alerts if (a.get("severity") or "").lower() == "critical")

    # Top issues — real downtime first, then alert volume.
    alert_count_by_dev: dict[str, int] = defaultdict(int)
    for a in alerts:
        if a.get("device_id"):
            alert_count_by_dev[str(a["device_id"])] += 1
    downtime_by_dev: dict[str, float] = defaultdict(float)
    outages_by_dev: dict[str, int] = defaultdict(int)
    ongoing_by_dev: dict[str, bool] = defaultdict(bool)
    for e in episodes:
        downtime_by_dev[e["entity_id"]] += e["in_window_s"]
        outages_by_dev[e["entity_id"]] += 1
        ongoing_by_dev[e["entity_id"]] |= e["ongoing"]

    candidates = []
    for did in device_map:
        dev_avail = _pct_from_counts(counts, {did})
        degraded = dev_avail is not None and dev_avail < 100
        if not (outages_by_dev[did] or alert_count_by_dev[did] or degraded):
            continue
        candidates.append((did, dev_avail, degraded))
    candidates.sort(key=lambda c: (-downtime_by_dev[c[0]], -outages_by_dev[c[0]],
                                   -alert_count_by_dev[c[0]],
                                   c[1] if c[1] is not None else 100.0))
    top_issues = []
    for did, dev_avail, degraded in candidates[:5]:
        downtime_s = downtime_by_dev[did]
        category = ("outage" if outages_by_dev[did] else
                    "degraded" if degraded else "alert_noise")
        sev = ("critical" if downtime_s >= 300 or ongoing_by_dev[did] else
               "warning" if category != "alert_noise" else "info")
        top_issues.append({
            "device_id": did,
            "hostname": device_map[did].get("hostname", "Unknown"),
            "issue": {"outage": "down", "degraded": "degraded"}.get(category, "noisy"),
            "category": category,
            "duration_minutes": _minutes(downtime_s),
            "downtime_minutes": _minutes(downtime_s),
            "outages": outages_by_dev[did],
            "availability_pct": dev_avail,
            "alert_count": alert_count_by_dev[did],
            "severity": sev,
        })

    states = {did: _device_state_now(d.get("status"), did in maint_now)
              for did, d in device_map.items()}

    # Location summary. `devices`/`down` are live counts; `availability_pct` is
    # measured over the selected window (maintenance excluded) so it matches the
    # range label the UI puts on the table.
    location_summary = [
        {"location": r["name"], "devices": r["devices"], "down": r["down_now"],
         "availability_pct": r["availability_pct"], "up_now": r["up_now"],
         "measured": r["measured"], "downtime_minutes": r["downtime_minutes"],
         "outages": r["outages"]}
        for r in _breakdown(devices, lambda d: d.get("location") or "Unknown", counts, states,
                            downtime_by_dev, outages_by_dev)
    ]

    # Every device with confirmed downtime in the window, worst first.
    downtime_by_device = []
    for did in sorted((d for d in outages_by_dev if d in device_map),
                      key=lambda d: (-downtime_by_dev[d], -outages_by_dev[d]))[:25]:
        d = device_map[did]
        downtime_by_device.append({
            "device_id": did,
            "hostname": d.get("hostname", "Unknown"),
            "location": d.get("location"),
            "group_name": d.get("group_name"),
            "device_type": d.get("device_type"),
            "downtime_minutes": _minutes(downtime_by_dev[did]),
            "outages": outages_by_dev[did],
            "ongoing": ongoing_by_dev[did],
            "availability_pct": _pct_from_counts(counts, {did}),
        })

    outage_timeline = []
    for e in sorted(episodes, key=lambda e: e["clipped_start"], reverse=True)[:50]:
        d = device_map.get(e["entity_id"], {})
        outage_timeline.append({
            "device_id": e["entity_id"],
            "hostname": d.get("hostname", "Unknown"),
            "started_at": _iso(e["started_at"] or e["clipped_start"]),
            "start_known": e["started_at"] is not None,
            "ended_at": _iso(e["ended_at"]),
            "ongoing": e["ongoing"],
            "duration_minutes": _minutes(e["duration_s"]),
            "in_window_minutes": _minutes(e["in_window_s"]),
            "kind": "device",
        })

    delta_pp = (avail - prev_avail) if avail is not None and prev_avail is not None else None

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "generated_at": now.isoformat(),
        "data_errors": list(errors),
        # Span the figures below were actually measured over, which is shorter
        # than [from, to] whenever retention or a collection gap bites.
        "coverage": _coverage_block(coverage, start, end, len(measured), len(devices),
                                    _ping_table(start, end)),
        "kpis": {
            "availability_pct": avail,
            "availability_delta_pct": delta_pp,
            "availability_delta_pp": delta_pp,
            "active_critical_count": active_critical,
            "active_critical_now": active_critical,
            "critical_raised_in_period": critical_raised,
            "mttr_minutes": mttr_min,
            "mttr_delta_minutes": _delta(mttr_min, prev_mttr_min),
            "alerts_count": len(alerts),
            "alert_mttr_minutes": _minutes(alert_mttr_s),
            "devices_monitored": len(devices),
            "devices_expected": len(devices),
            "devices_measured": len(measured),
            "sla_target_pct": target,
            "configured_target_pct": target,
            "target_source": target_source,
            "sla_attained_pct": avail,
            "sla_met": (avail >= target) if avail is not None else None,
            "incidents_count": cur["count"],
            "incidents_delta": cur["count"] - prev["count"] if comparable else None,
            "outage_episodes": cur["count"],
            "ongoing_outages": cur["ongoing"],
            "outage_downtime_minutes": round(cur["downtime_s"] / 60, 2),
            # Device-minutes of planned maintenance excluded from the figures
            # above (0 when no windows overlap the range).
            "maintenance_minutes": _maintenance_minutes(maint, id_set),
        },
        "status_now": _status_now(list(states.values())),
        "availability_trend": trend,
        "top_issues": top_issues,
        "location_summary": location_summary,
        "group_summary": _breakdown(devices, lambda d: d.get("group_name") or "Ungrouped",
                                    counts, states, downtime_by_dev, outages_by_dev),
        "type_summary": _breakdown(devices, lambda d: d.get("device_type") or "other",
                                   counts, states, downtime_by_dev, outages_by_dev),
        "outage_timeline": outage_timeline,
        # Visual breakdowns over EVERY episode (outage_timeline is capped).
        "outage_activity": _outage_activity(episodes, start, end, maint),
        "downtime_by_device": downtime_by_device,
        "recovery_histogram": _recovery_histogram(episodes),
    }


async def build_technical(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    visible_tags: Optional[list[str]] = None,
    *,
    redact_netflow: bool = False,
) -> dict:
    """``redact_netflow``: the requester lacks netflow.view, so a NetFlow
    alert's message never reaches ``noisy_alerts`` (sample or grouping key)."""
    start, end = _normalise_window(from_time, to_time)
    errors = DataErrors()

    devices = await _fetch_devices(db, visible_tags=visible_tags)
    device_map = {str(d["id"]): d for d in devices}
    ids = list(device_map)
    alerts = await _fetch_alerts(db, start, end, visible_tags=visible_tags, redact_netflow=redact_netflow)
    maint = await _fetch_device_maintenance_windows(db, start, end)
    transitions = await asyncio.to_thread(
        errors.run, "device_status_log", _device_transitions, start, end, ids, default={})
    episodes = window_episodes(transitions, start, end, start, maint)
    outage_counts: dict[str, int] = defaultdict(int)
    for e in episodes:
        outage_counts[e["entity_id"]] += 1
    ping_stats = await asyncio.to_thread(
        errors.run, "device_availability", _device_ping_stats,
        start, end, maint, ids, dict(outage_counts), default={})

    # Top-10 worst availability devices (maintenance samples already excluded)
    per_device_uptime = sorted(
        ((did, ping_stats[did]) for did in device_map if did in ping_stats),
        key=lambda x: x[1]["uptime_pct"],
    )
    worst_devices = []
    for did, st in per_device_uptime[:10]:
        d = device_map.get(did, {})
        worst_devices.append({
            "device_id": did,
            "hostname": d.get("hostname", "Unknown"),
            "ip": str(d.get("ip_address") or ""),
            "availability_pct": round(st["uptime_pct"], 2),
            "outage_count": st["outage_count"],
            "avg_rtt_ms": round(st["avg_rtt"], 1) if st["avg_rtt"] else None,
            "p95_rtt_ms": round(st["p95_rtt"], 1) if st["p95_rtt"] else None,
        })

    # Top-10 noisy alerts
    per_rule_count: dict[tuple, dict] = defaultdict(lambda: {"alert_count": 0, "device_id": None, "hostname": None})
    for a in alerts:
        rule_id = str(a.get("rule_id") or "")
        key = rule_id or f"msg:{(a.get('message') or '')[:40]}"
        per_rule_count[key]["alert_count"] += 1
        if a.get("device_id") and not per_rule_count[key]["device_id"]:
            per_rule_count[key]["device_id"] = str(a["device_id"])
            per_rule_count[key]["hostname"] = a.get("hostname")
            per_rule_count[key]["sample_message"] = a.get("message")
            per_rule_count[key]["severity"] = a.get("severity")

    noisy_alerts = sorted(per_rule_count.items(), key=lambda x: -x[1]["alert_count"])[:10]
    noisy_alerts_out = [
        {
            "rule_key": k,
            "alert_count": v["alert_count"],
            "device_id": v["device_id"],
            "hostname": v["hostname"],
            "sample_message": v.get("sample_message"),
            "severity": v.get("severity"),
        }
        for k, v in noisy_alerts
    ]

    # Top bandwidth interfaces — query snmp_if_metrics (in_bps/out_bps already in bits/sec)
    top_bw = []
    try:
        client = get_ch_client()
        id_sql = "AND toString(device_id) IN %(ids)s" if visible_tags is not None else ""
        q = f"""
            SELECT device_id, if_index,
                   avg(in_bps) AS in_bps_avg,
                   avg(out_bps) AS out_bps_avg
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(start)s AND timestamp <= %(end)s
            {id_sql}
            GROUP BY device_id, if_index
            ORDER BY (in_bps_avg + out_bps_avg) DESC
            LIMIT 10
        """
        rows = []
        if visible_tags is None or ids:
            params = {"start": start, "end": end}
            if visible_tags is not None:
                params["ids"] = ids
            rs = await asyncio.to_thread(client.query, q, parameters=params)
            rows = rs.result_rows
        # Lookup interface names
        if_names: dict[tuple, dict] = {}
        if rows:
            id_pairs = [(str(r[0]), int(r[1])) for r in rows]
            placeholders = ",".join(f"(:d{i}, :i{i})" for i in range(len(id_pairs)))
            params = {}
            for i, (did, idx) in enumerate(id_pairs):
                params[f"d{i}"] = did
                params[f"i{i}"] = idx
            sql = text(f"""
                SELECT device_id, if_index, if_name, if_speed
                FROM device_interfaces
                WHERE (device_id, if_index) IN ({placeholders})
            """)
            res = await db.execute(sql, params)
            for row in res.fetchall():
                if_names[(str(row[0]), int(row[1]))] = {"if_name": row[2], "if_speed": row[3]}

        for r in rows:
            did, idx = str(r[0]), int(r[1])
            in_bps, out_bps = float(r[2] or 0), float(r[3] or 0)
            meta = if_names.get((did, idx), {})
            speed = meta.get("if_speed") or 0
            util = round(max(in_bps, out_bps) / speed * 100, 1) if speed else None
            d = device_map.get(did, {})
            top_bw.append({
                "device_id": did,
                "hostname": d.get("hostname", "Unknown"),
                "if_index": idx,
                "if_name": meta.get("if_name", f"#{idx}"),
                "in_bps_avg": in_bps,
                "out_bps_avg": out_bps,
                "utilization_pct": util,
            })
    except Exception as exc:
        errors.add("interface_bandwidth", exc)
        top_bw = []

    # Interface table — best-effort
    iface_errors = []
    try:
        scope_sql, scope_params = "", {}
        if visible_tags is not None:
            scope_sql = f"AND {scoping.jsonb_tags_visible('d.tags')}"
            scope_params[scoping.SCOPE_PARAM] = visible_tags
        sql = text(f"""
            SELECT d.id::text AS device_id, d.hostname, di.if_index, di.if_name,
                   di.oper_status
            FROM device_interfaces di
            JOIN devices d ON d.id = di.device_id
            WHERE di.monitored = TRUE {scope_sql}
            ORDER BY d.hostname, di.if_index
            LIMIT 50
        """)
        res = await db.execute(sql, scope_params)
        for row in res.fetchall():
            iface_errors.append({
                "device_id": row[0],
                "hostname": row[1],
                "if_index": row[2],
                "if_name": row[3],
                "oper_status": row[4],
            })
    except Exception as exc:
        errors.add("interfaces", exc)
        iface_errors = []

    outage_history = []
    for e in sorted(episodes, key=lambda e: e["clipped_start"]):
        d = device_map.get(e["entity_id"], {})
        outage_history.append({
            "device_id": e["entity_id"],
            "hostname": d.get("hostname", "Unknown"),
            "started_at": _iso(e["started_at"] or e["clipped_start"]),
            "start_known": e["started_at"] is not None,
            "ended_at": _iso(e["ended_at"]),
            "ongoing": e["ongoing"],
            "duration_minutes": _minutes(e["duration_s"]),
            "in_window_minutes": _minutes(e["in_window_s"]),
            "kind": "device",
        })

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_errors": list(errors),
        "worst_devices": worst_devices,
        "noisy_alerts": noisy_alerts_out,
        "top_bandwidth_interfaces": top_bw,
        "alert_volume_by_severity": _alert_volume_by_severity(start, end, alerts),
        "interface_errors": iface_errors,
        "outage_history": outage_history,
    }


def _service_state_now(status: Optional[str], in_maintenance: bool) -> str:
    if in_maintenance:
        return "maintenance"
    s = (status or "").lower()
    if s in ("up", "online"):
        return "up"
    if s in DOWN_STATES:
        return "down"
    if s in ("warning", "degraded"):
        return "degraded"
    return "unknown"


def _tls_eligible(s: dict) -> bool:
    kind = (s.get("check_type") or "").lower()
    return kind == "tls" or (kind == "http" and (s.get("target_url") or "").lower().startswith("https://"))


def _compliance(avail: Optional[float], target: float,
                downtime_s: Optional[float], covered_s: float) -> tuple[Optional[bool], str, Optional[float]]:
    """(meets_target, compliance, error_budget_used_pct)."""
    if avail is None:
        return None, "not_assessed", None
    budget = (100.0 - target) / 100.0 * covered_s
    downtime_s = downtime_s or 0.0
    used = downtime_s / budget * 100 if budget > 0 else (0.0 if downtime_s <= 0 else None)
    if avail < target:
        return False, "breached", used
    if used is not None and used > AT_RISK_BUDGET_PCT:
        return True, "at_risk", used
    return True, "met", used


def _confirmed_service_evidence(services: list[dict], start: datetime, end: datetime,
                                maintenance: Optional[dict] = None) -> dict:
    """service_check_id -> (summary, episodes, observed_start) on the confirmed basis.

    `maintenance` maps check id -> merged maintenance intervals; that time is
    excluded from both availability and outage episodes (B03)."""
    maintenance = maintenance or {}
    checks = {str(s["id"]): (s.get("created_at"), s.get("check_interval") or 60) for s in services}
    out = {}
    for cid, segments, _count, observed_start in read_fleet_segments(
            get_ch_client(), checks, start, end, basis="confirmed"):
        excluded = maintenance.get(cid)
        out[cid] = (summarize(segments, observed_start, end, excluded),
                    confirmed_down_episodes(segments, end, excluded), observed_start)
    return out


async def _service_maintenance_intervals(db, services: list[dict], start: datetime, end: datetime) -> dict:
    from app.services import service_maintenance
    return await service_maintenance.intervals_for_checks(db, services, start, end)


async def build_business(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
    visible_tags: Optional[list[str]] = None,
) -> dict:
    start, end = _normalise_window(from_time, to_time)
    errors = DataErrors()

    services = await _fetch_service_checks(db, visible_tags=visible_tags)
    svc_ids = [str(s["id"]) for s in services]
    policy_target, policy_source = await load_default_target(db)
    try:
        from app.services.service_check_service import active_maintenance_check_ids
        maint_now = {str(i) for i in await active_maintenance_check_ids(db)}
    except Exception as exc:
        errors.add("service_maintenance", exc)
        maint_now = set()

    svc_maintenance: dict = {}
    if svc_ids:
        try:
            svc_maintenance = await _service_maintenance_intervals(db, services, start, end)
        except Exception as exc:
            errors.add("service_maintenance", exc)

    svc_rows, confirmed = [], {}
    if svc_ids:
        svc_rows, confirmed = await asyncio.gather(
            asyncio.to_thread(errors.run, "service_probes", _fetch_service_metrics,
                              start, end, svc_ids, default=[]),
            asyncio.to_thread(errors.run, "service_availability", _confirmed_service_evidence,
                              services, start, end, svc_maintenance, default={}),
        )
    rows_by_service = _group_rows(svc_rows, "service_check_id")

    service_availability = []
    response_quantiles = []
    service_outages = []
    compliance_summary = {"met": 0, "at_risk": 0, "breached": 0, "not_assessed": 0}
    all_down: list[tuple[datetime, datetime]] = []
    impact_down: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    impact_assessed = 0
    impact_measured = any_measured = False

    for s in services:
        sid = str(s["id"])
        rows = rows_by_service.get(sid, [])
        total = len(rows)
        up = sum(1 for r in rows if r.get("is_up"))
        failed = total - up

        resp = sorted(r["response_ms"] for r in rows if r.get("response_ms") and r.get("response_ms") > 0)
        if resp:
            n = len(resp)
            p50 = resp[int(n * 0.5)]
            p95 = resp[min(int(n * 0.95), n - 1)]
            p99 = resp[min(int(n * 0.99), n - 1)]
        else:
            p50 = p95 = p99 = None

        summary, episodes, _observed = confirmed.get(sid, (None, [], start))
        avail = summary["uptime_pct"] if summary else None
        downtime_s = summary["total_downtime_sec"] if summary and avail is not None else None
        covered_s = summary["covered_sec"] if summary else 0.0
        target, target_source = resolve_target(s.get("sla_target_pct"), policy_target, policy_source)
        meets, compliance, budget_used = _compliance(avail, target, downtime_s, covered_s)
        compliance_summary[compliance] += 1

        grouped = s.get("group_id") is not None
        gname = s.get("group_name") or "Ungrouped"
        criticality = s.get("criticality")
        mapped = grouped and criticality is not None
        if avail is not None:
            any_measured = True
        intervals = [(lo, hi or end) for lo, hi in episodes]
        all_down.extend(intervals)
        if mapped:
            impact_assessed += 1
            impact_measured |= avail is not None
            impact_down[gname].extend(intervals)

        service_availability.append({
            "service_check_id": sid,
            "name": s.get("name", "Unknown"),
            "type": (s.get("check_type") or "").lower(),
            "group_name": gname,
            "status": (s.get("status") or "").lower(),
            "availability_pct": avail,
            "availability_basis": "confirmed",
            "probe_availability_pct": up / total * 100 if total else None,
            "coverage_pct": summary["coverage_pct"] if summary else None,
            "maintenance_seconds": summary.get("maintenance_sec", 0.0) if summary else 0.0,
            "downtime_seconds": downtime_s,
            "outages": len(episodes),
            "checks_total": total,
            "checks_failed": failed,
            "target_pct": target,
            "target_source": target_source,
            "meets_target": meets,
            "compliance": compliance,
            "error_budget_used_pct": budget_used,
            "criticality": criticality,
            "business_owner": s.get("business_owner"),
            "grouped": grouped,
        })
        response_quantiles.append({
            "service_check_id": sid,
            "name": s.get("name", "Unknown"),
            "p50_ms": round(p50, 1) if p50 else None,
            "p95_ms": round(p95, 1) if p95 else None,
            "p99_ms": round(p99, 1) if p99 else None,
        })
        for lo, hi in episodes:
            service_outages.append({
                "service_check_id": sid,
                "name": s.get("name") or "Unknown",
                "group_name": gname,
                "started_at": _iso(lo),
                "ended_at": _iso(hi),
                "ongoing": hi is None,
                "duration_minutes": _minutes(((hi or end) - lo).total_seconds()),
            })
    service_outages.sort(key=lambda o: o["started_at"] or "", reverse=True)
    compliance_summary["eligible"] = (compliance_summary["met"] + compliance_summary["at_risk"]
                                      + compliance_summary["breached"])

    # TLS expiry warnings, on each check's own thresholds (as the poller uses them).
    tls_warnings = []
    tls_eligible = [s for s in services if _tls_eligible(s)]
    for s in services:
        days = s.get("tls_days_remaining")
        if days is None:
            continue
        warn_days = s.get("tls_warn_days") or 30
        critical_days = s.get("tls_critical_days") or 7
        if days <= warn_days:
            expiry = s.get("tls_expiry_date")
            tls_warnings.append({
                "service_check_id": str(s["id"]),
                "name": s.get("name", "Unknown"),
                "tls_expiry_date": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
                "days_remaining": days,
                "warn_days": warn_days,
                "critical_days": critical_days,
                "severity": "critical" if days <= critical_days else "warning",
            })
    tls_warnings.sort(key=lambda x: x["days_remaining"])

    impact_minutes = None
    if impact_measured:
        impact_minutes = round(sum(_merged_seconds(iv) for iv in impact_down.values()) / 60, 2)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_errors": list(errors),
        "availability_basis": "confirmed",
        "service_availability": service_availability,
        "compliance_summary": compliance_summary,
        "response_time_quantiles": response_quantiles,
        "tls_warnings": tls_warnings,
        "tls_evidence": {
            "eligible": len(tls_eligible),
            "with_expiry_data": sum(1 for s in tls_eligible if s.get("tls_days_remaining") is not None),
        },
        "customer_impact_minutes": impact_minutes,
        "impact_assessed_services": impact_assessed,
        "impact_unassessed_services": len(services) - impact_assessed,
        "impact_basis": "confirmed_downtime_merged" if impact_assessed else None,
        "confirmed_downtime_minutes": (round(_merged_seconds(all_down) / 60, 2)
                                       if any_measured else None),
        "service_outages": service_outages,
        "status_now": _status_now([_service_state_now(s.get("status"), str(s["id"]) in maint_now)
                                   for s in services]),
    }


async def build_inventory(db: AsyncSession) -> dict:
    devices = await _fetch_devices(db)

    # Devices by type
    by_type: dict[str, int] = defaultdict(int)
    for d in devices:
        by_type[d.get("device_type") or "other"] += 1

    # Devices by vendor (from discovery)
    by_vendor: dict[str, int] = defaultdict(int)
    sql = text("SELECT COALESCE(vendor, 'Unknown') AS vendor, COUNT(*) FROM devices GROUP BY vendor ORDER BY 2 DESC")
    res = await db.execute(sql)
    for row in res.fetchall():
        by_vendor[row[0]] = int(row[1])

    # Devices by location
    by_location: dict[str, int] = defaultdict(int)
    for d in devices:
        by_location[d.get("location") or "Unknown"] += 1

    # Interface totals
    interface_totals = {"total": 0, "monitored": 0, "down": 0}
    try:
        sql = text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE monitored) AS monitored,
                COUNT(*) FILTER (WHERE oper_status = 'down') AS down
            FROM device_interfaces
        """)
        res = await db.execute(sql)
        row = res.fetchone()
        if row:
            interface_totals = {
                "total": int(row[0] or 0),
                "monitored": int(row[1] or 0),
                "down": int(row[2] or 0),
            }
    except Exception:
        pass

    # Sensors fleet
    sensors_out = []
    try:
        sql = text("""
            SELECT s.id::text, s.name, s.status, s.last_heartbeat_at, s.queue_depth,
                   s.queue_dropped_count, s.version, s.hostname,
                   si.name AS site_name
            FROM sensors s
            LEFT JOIN sites si ON si.id = s.site_id
            ORDER BY s.name
        """)
        res = await db.execute(sql)
        for row in res.fetchall():
            hb = row[3]
            if hb and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            sensors_out.append({
                "sensor_id": row[0],
                "name": row[1],
                "status": row[2],
                "last_heartbeat": hb.isoformat() if hb else None,
                "queue_depth": int(row[4] or 0),
                "queue_dropped_count": int(row[5] or 0),
                "version": row[6],
                "hostname": row[7],
                "site": row[8],
            })
    except Exception:
        sensors_out = []

    # Recently added devices
    recently_added = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    sql = text("""
        SELECT id::text, hostname, ip_address::text, device_type, location, vendor, model, created_at
        FROM devices
        WHERE created_at >= :cutoff
        ORDER BY created_at DESC
        LIMIT 20
    """)
    try:
        res = await db.execute(sql, {"cutoff": cutoff})
        for row in res.fetchall():
            ca = row[7]
            if ca and ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            recently_added.append({
                "device_id": row[0],
                "hostname": row[1],
                "ip": row[2],
                "device_type": row[3],
                "location": row[4],
                "vendor": row[5],
                "model": row[6],
                "added_at": ca.isoformat() if ca else None,
            })
    except Exception:
        recently_added = []

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "devices_by_type": [{"type": k, "count": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "devices_by_vendor": [{"vendor": k, "count": v} for k, v in sorted(by_vendor.items(), key=lambda x: -x[1])],
        "devices_by_location": [{"location": k, "count": v} for k, v in sorted(by_location.items(), key=lambda x: -x[1])],
        "interface_totals": interface_totals,
        "sensors": sensors_out,
        "recently_added_devices": recently_added,
        "totals": {
            "devices": len(devices),
            "sensors": len(sensors_out),
            "interfaces": interface_totals["total"],
        },
    }
