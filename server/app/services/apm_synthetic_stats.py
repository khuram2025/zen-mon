"""Read models for synthetic scenario history (``zenplus.apm_synthetic_results``).

Everything the Synthetics pages show for a time range is aggregated here:

* ``scenario_stats`` — one row per scenario: runs, failures, uptime, average,
  p95 and the last failure, plus a small availability sparkline;
* ``summary`` — one scenario: totals, a bucketed trend (runs, failures, average
  and p95 duration), per-step statistics and trends, grouped errors and outages
  (consecutive failed runs);
* ``runs`` / ``run_detail`` — paged run list and one run's full step detail.

Timestamps are stored as naive UTC ``DateTime`` and always returned with an
explicit UTC offset (``iso``), so browsers never read them as local time.
Pure helpers (bucket choice, outage folding, secret masking) are tested
without a database.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

MAX_RANGE = timedelta(days=90)          # results TTL (migrate-058)
TARGET_BUCKETS = 120
_NICE_BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
SPARK_BUCKETS = 48
MASK = "••••••"
_SENSITIVE_HEADER = re.compile(r"authorization|cookie|token|secret|password|api[-_]?key|x-auth", re.I)


def iso(ts) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    return str(ts)


def resolve_range(hours: Optional[int], from_time: Optional[datetime],
                  to_time: Optional[datetime], *, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """(since, until) as naive UTC. ``from``/``to`` win over ``hours``; the
    span is capped at the 90-day retention. Raises ValueError on a bad range."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = to_time.astimezone(timezone.utc) if to_time else now
    if from_time:
        since = from_time.astimezone(timezone.utc)
    else:
        since = until - timedelta(hours=hours or 24)
    if since >= until:
        raise ValueError("'from' must be before 'to'")
    if until - since > MAX_RANGE:
        since = until - MAX_RANGE
    return since.replace(tzinfo=None), until.replace(tzinfo=None)


def bucket_seconds(since: datetime, until: datetime, *, interval: int = 60,
                   target: int = TARGET_BUCKETS) -> int:
    """A round bucket size giving about ``target`` buckets, never finer than
    the scenario's run interval (a bucket should normally hold a run)."""
    span = max(1.0, (until - since).total_seconds())
    want = max(span / target, float(interval or 60))
    for b in _NICE_BUCKETS:
        if b >= want:
            return b
    return _NICE_BUCKETS[-1]


def outages(points: Iterable[tuple[datetime, bool, str, str]], *, until: datetime) -> list[dict]:
    """Fold ordered (timestamp, success, failed_step, error) runs into outages:
    each maximal run of consecutive failures. ``end`` is the first passing run
    after it (None while still failing)."""
    out: list[dict] = []
    cur: Optional[dict] = None
    for ts, ok, step, err in points:
        if not ok:
            if cur is None:
                cur = {"start": ts, "end": None, "failed_runs": 0, "failed_step": step, "error": err}
            cur["failed_runs"] += 1
            cur["last_failure"] = ts
        elif cur is not None:
            cur["end"] = ts
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    result = []
    for o in out:
        end = o["end"] or until
        result.append({
            "start": iso(o["start"]), "end": iso(o["end"]), "ongoing": o["end"] is None,
            "duration_s": max(0, int((end - o["start"]).total_seconds())),
            "failed_runs": o["failed_runs"], "failed_step": o["failed_step"], "error": o["error"],
        })
    return list(reversed(result))          # newest first


def mask_scenario(monitor: dict) -> dict:
    """Scenario as a read-only user sees it: variable values and sensitive
    header values hidden (they hold credentials for the monitored app)."""
    out = dict(monitor)
    out["variables"] = {k: MASK for k in (monitor.get("variables") or {})}
    steps = []
    for s in monitor.get("steps") or []:
        s = dict(s)
        s["headers"] = {k: (MASK if _SENSITIVE_HEADER.search(k) else v)
                        for k, v in (s.get("headers") or {}).items()}
        if s.get("body"):
            s["body"] = MASK
        steps.append(s)
    out["steps"] = steps
    out["secrets_masked"] = True
    return out


def _pct(n, d) -> Optional[float]:
    return round(n / d * 100, 2) if d else None


def _ms(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 1)


# ── ClickHouse queries (sync; call through asyncio.to_thread) ────────────────

_T = "zenplus.apm_synthetic_results"


def _uuid_list(ids):
    return [uuid.UUID(str(i)) for i in ids]


def scenario_stats(ch, monitor_ids: list[str], since: datetime, until: datetime) -> dict[str, dict]:
    if not monitor_ids:
        return {}
    p = {"ids": _uuid_list(monitor_ids), "since": since, "until": until}
    rows = ch.query(
        f"""SELECT monitor_id, count(), countIf(success = 1), avg(total_ms),
                   quantile(0.95)(total_ms), max(timestamp),
                   maxIf(timestamp, success = 0), argMaxIf(error, timestamp, success = 0),
                   argMaxIf(failed_step, timestamp, success = 0)
            FROM {_T} WHERE monitor_id IN %(ids)s AND timestamp >= %(since)s AND timestamp < %(until)s
            GROUP BY monitor_id""", parameters=p).result_rows
    span = max(60, int((until - since).total_seconds() / SPARK_BUCKETS))
    spark_rows = ch.query(
        f"""SELECT monitor_id, intDiv(toUInt32(timestamp) - toUInt32(toDateTime(%(since)s)), %(b)s) AS i,
                   count(), countIf(success = 1)
            FROM {_T} WHERE monitor_id IN %(ids)s AND timestamp >= %(since)s AND timestamp < %(until)s
            GROUP BY monitor_id, i ORDER BY monitor_id, i""", parameters={**p, "b": span}).result_rows
    sparks: dict[str, list] = {}
    for mid, i, n, ok in spark_rows:
        arr = sparks.setdefault(str(mid), [None] * SPARK_BUCKETS)
        if 0 <= int(i) < SPARK_BUCKETS:
            arr[int(i)] = _pct(int(ok), int(n))
    out = {}
    for r in rows:
        mid, runs, ok = str(r[0]), int(r[1]), int(r[2])
        last_fail = r[6] if isinstance(r[6], datetime) and r[6].year > 1971 else None
        out[mid] = {
            "runs": runs, "failures": runs - ok, "uptime_pct": _pct(ok, runs),
            "avg_ms": _ms(r[3]), "p95_ms": _ms(r[4]), "last_run_at": iso(r[5]),
            "last_failure_at": iso(last_fail), "last_error": (r[7] or None) if last_fail else None,
            "last_failed_step": (r[8] or None) if last_fail else None,
            "spark": sparks.get(mid) or [None] * SPARK_BUCKETS,
        }
    return out


_STEPS_JOIN = """ARRAY JOIN JSONExtractArrayRaw(steps_json) AS s, arrayEnumerate(JSONExtractArrayRaw(steps_json)) AS idx"""


def summary(ch, monitor_id: str, since: datetime, until: datetime, *, interval: int = 60) -> dict:
    p = {"id": uuid.UUID(str(monitor_id)), "since": since, "until": until}
    cond = "WHERE monitor_id = %(id)s AND timestamp >= %(since)s AND timestamp < %(until)s"
    where = f"FROM {_T} {cond}"
    stepped = f"FROM {_T} {_STEPS_JOIN} {cond}"
    t = ch.query(f"""SELECT count(), countIf(success = 1), avg(total_ms), quantile(0.5)(total_ms),
                            quantile(0.95)(total_ms), max(total_ms), min(timestamp), max(timestamp) {where}""",
                 parameters=p).result_rows[0]
    runs, ok = int(t[0] or 0), int(t[1] or 0)
    b = bucket_seconds(since, until, interval=interval)
    pb = {**p, "b": b}
    series = [{"t": iso(r[0]), "runs": int(r[1]), "failed": int(r[1]) - int(r[2]),
               "uptime_pct": _pct(int(r[2]), int(r[1])), "avg_ms": _ms(r[3]), "p95_ms": _ms(r[4])}
              for r in ch.query(f"""SELECT toStartOfInterval(timestamp, INTERVAL %(b)s second) AS t, count(),
                                           countIf(success = 1), avg(total_ms), quantile(0.95)(total_ms)
                                    {where} GROUP BY t ORDER BY t""", parameters=pb).result_rows]
    step_rows = ch.query(
        f"""SELECT idx, any(JSONExtractString(s, 'name')) AS name, any(JSONExtractString(s, 'method')),
                   any(JSONExtractString(s, 'url')), count(), countIf(JSONExtractBool(s, 'ok')),
                   avg(JSONExtractFloat(s, 'ms')), quantile(0.95)(JSONExtractFloat(s, 'ms')),
                   argMax(JSONExtractUInt(s, 'status_code'), timestamp),
                   topK(5)(JSONExtractUInt(s, 'status_code')),
                   avg(JSONExtractFloat(s, 'timing', 'connect_ms')), avg(JSONExtractFloat(s, 'timing', 'tls_ms')),
                   avg(JSONExtractFloat(s, 'timing', 'ttfb_ms')), avg(JSONExtractFloat(s, 'timing', 'download_ms')),
                   countIf(JSONHas(s, 'timing'))
            {stepped} GROUP BY idx ORDER BY idx""", parameters=p).result_rows
    errors_by_step = {}
    for r in ch.query(f"""SELECT failed_step, error, count(), min(timestamp), max(timestamp)
                          {where} AND success = 0 GROUP BY failed_step, error ORDER BY count() DESC LIMIT 50""",
                      parameters=p).result_rows:
        errors_by_step.setdefault(r[0], []).append(r)
    steps = []
    for r in step_rows:
        executed, passed = int(r[4]), int(r[5])
        timed = int(r[14] or 0)
        steps.append({
            "index": int(r[0]) - 1, "name": r[1], "method": r[2], "url": r[3],
            "executed": executed, "passed": passed, "failed": executed - passed,
            "skipped": max(0, runs - executed), "pass_pct": _pct(passed, executed),
            "avg_ms": _ms(r[6]), "p95_ms": _ms(r[7]), "last_status": int(r[8] or 0),
            "status_codes": [int(c) for c in (r[9] or []) if int(c)],
            "timing": ({"connect_ms": _ms(r[10]), "tls_ms": _ms(r[11]), "ttfb_ms": _ms(r[12]),
                        "download_ms": _ms(r[13])} if timed else None),
            "top_errors": [{"error": e[1], "count": int(e[2]), "last_seen": iso(e[4])}
                           for e in errors_by_step.get(r[1], [])[:5]],
        })
    step_series: dict[int, list] = {}
    for r in ch.query(f"""SELECT toStartOfInterval(timestamp, INTERVAL %(b)s second) AS t, idx,
                                 avg(JSONExtractFloat(s, 'ms')), countIf(NOT JSONExtractBool(s, 'ok'))
                          {stepped} GROUP BY t, idx ORDER BY t, idx""", parameters=pb).result_rows:
        step_series.setdefault(int(r[1]) - 1, []).append({"t": iso(r[0]), "avg_ms": _ms(r[2]), "failed": int(r[3])})
    for s in steps:
        s["series"] = step_series.get(s["index"], [])
    error_groups = [{"failed_step": r[0], "error": r[1], "count": int(r[2]), "first_seen": iso(r[3]),
                     "last_seen": iso(r[4])}
                    for rs in errors_by_step.values() for r in rs]
    error_groups.sort(key=lambda e: -e["count"])
    points = ch.query(f"""SELECT timestamp, success, failed_step, error {where} ORDER BY timestamp""",
                      parameters=p).result_rows
    outs = outages(((r[0], bool(r[1]), r[2], r[3]) for r in points), until=until)
    downtime = sum(o["duration_s"] for o in outs)
    return {
        "range": {"from": iso(since), "to": iso(until), "bucket_seconds": b},
        "totals": {"runs": runs, "passed": ok, "failed": runs - ok, "uptime_pct": _pct(ok, runs),
                   "avg_ms": _ms(t[2]), "p50_ms": _ms(t[3]), "p95_ms": _ms(t[4]), "max_ms": _ms(t[5]),
                   "first_run_at": iso(t[6]) if runs else None, "last_run_at": iso(t[7]) if runs else None,
                   "outages": len(outs), "downtime_s": downtime},
        "series": series, "steps": steps, "errors": error_groups[:20], "outages": outs[:100],
    }


def runs(ch, monitor_id: str, since: datetime, until: datetime, *, status: str = "all",
         step: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
    p: dict[str, Any] = {"id": uuid.UUID(str(monitor_id)), "since": since, "until": until,
                         "lim": limit, "off": offset}
    cond = ""
    if status == "failed":
        cond += " AND success = 0"
    elif status == "passed":
        cond += " AND success = 1"
    if step:
        cond += " AND failed_step = %(step)s"
        p["step"] = step
    where = f"FROM {_T} WHERE monitor_id = %(id)s AND timestamp >= %(since)s AND timestamp < %(until)s{cond}"
    total = int(ch.query(f"SELECT count() {where}", parameters=p).result_rows[0][0])
    rows = ch.query(f"""SELECT timestamp, status, success, total_ms, steps_total, steps_passed, failed_step, error,
                               steps_json
                        {where} ORDER BY timestamp DESC LIMIT %(lim)s OFFSET %(off)s""", parameters=p).result_rows
    return {"total": total, "runs": [{
        "timestamp": iso(r[0]), "status": r[1], "success": bool(r[2]), "total_ms": int(r[3]),
        "steps_total": int(r[4]), "steps_passed": int(r[5]), "failed_step": r[6], "error": r[7],
        "steps": _step_brief(r[8]),
    } for r in rows]}


def _step_brief(raw) -> list[dict]:
    """Per-step outline of a run for list rows (no bodies or assertion text)."""
    try:
        steps = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [{"name": s.get("name"), "ok": bool(s.get("ok")), "status_code": int(s.get("status_code") or 0),
             "ms": _ms(s.get("ms"))} for s in steps if isinstance(s, dict)]


def run_detail(ch, monitor_id: str, at: datetime) -> Optional[dict]:
    at = at.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    rows = ch.query(f"""SELECT timestamp, status, success, total_ms, steps_total, steps_passed, failed_step,
                               error, steps_json, location
                        FROM {_T} WHERE monitor_id = %(id)s AND timestamp = %(at)s LIMIT 1""",
                    parameters={"id": uuid.UUID(str(monitor_id)), "at": at}).result_rows
    if not rows:
        return None
    r = rows[0]
    try:
        steps = json.loads(r[8] or "[]")
    except (TypeError, ValueError):
        steps = []
    return {"timestamp": iso(r[0]), "status": r[1], "success": bool(r[2]), "total_ms": int(r[3]),
            "steps_total": int(r[4]), "steps_passed": int(r[5]), "failed_step": r[6], "error": r[7],
            "location": r[9], "steps": steps}
