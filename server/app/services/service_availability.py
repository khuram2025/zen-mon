"""Availability over observed time. Missing probes never imply up or down."""
from datetime import datetime, timedelta, timezone


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_segments(ch, check_id, start, end, interval):
    """Return non-overlapping (start, end, up fraction) and sample count.

    A raw result remains valid for at most two check intervals. Beyond the raw
    retention period, weight each rollup by its sample count and limit coverage
    to that bucket. No transition is extrapolated across an ingestion outage.
    """
    grace = max(1, interval) * 2
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    pieces, count = [], 0
    if start < cutoff:
        rows = ch.query("""
            SELECT timestamp, sum(uptime_pct * sample_count) / nullIf(sum(sample_count), 0),
                   sum(sample_count)
            FROM zenplus.service_metrics_5m
            WHERE service_check_id = %(id)s AND timestamp >= %(f)s AND timestamp < %(t)s
            GROUP BY timestamp ORDER BY timestamp
        """, parameters={"id": str(check_id), "f": (start - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),
                          "t": min(end, cutoff).strftime('%Y-%m-%d %H:%M:%S')}).result_rows
        for ts, up, samples in rows:
            ts = aware(ts)
            lo, hi = max(start, ts), min(end, cutoff, ts + timedelta(seconds=min(300, samples * max(1, interval))))
            if hi > lo and up is not None:
                pieces.append((lo, hi, max(0., min(1., float(up)))))
                count += int(samples)
    if end > cutoff:
        raw_start = max(start, cutoff)
        rows = ch.query("""
            SELECT timestamp, min(is_up), count()
            FROM zenplus.service_metrics
            WHERE service_check_id = %(id)s AND timestamp >= %(f)s AND timestamp < %(t)s
            GROUP BY timestamp ORDER BY timestamp
        """, parameters={"id": str(check_id), "f": (raw_start - timedelta(seconds=grace)).strftime('%Y-%m-%d %H:%M:%S'),
                          "t": end.strftime('%Y-%m-%d %H:%M:%S')}).result_rows
        for i, (ts, up, samples) in enumerate(rows):
            ts = aware(ts)
            next_ts = aware(rows[i + 1][0]) if i + 1 < len(rows) else end
            lo, hi = max(raw_start, ts), min(end, next_ts, ts + timedelta(seconds=grace))
            if hi > lo:
                pieces.append((lo, hi, float(bool(up))))
            if raw_start <= ts < end:
                count += int(samples)
    return pieces, count


def summarize(segments, start, end):
    covered = up = longest = outage = 0.
    incidents = 0
    previous_end = None
    for lo, hi, fraction in segments:
        lo, hi = max(lo, start), min(hi, end)
        if hi <= lo:
            continue
        duration = (hi - lo).total_seconds()
        covered += duration
        up += duration * fraction
        if fraction < 1:
            if outage == 0 or previous_end != lo:
                incidents += 1
                outage = 0.
            outage += duration * (1 - fraction)
            longest = max(longest, outage)
        else:
            outage = 0.
        previous_end = hi
    span = max(0., (end - start).total_seconds())
    return {"uptime_pct": up * 100 / covered if covered else None,
            "covered_sec": covered, "coverage_pct": covered * 100 / span if span else 0.,
            "unknown_sec": max(0., span - covered), "total_downtime_sec": covered - up,
            "incident_count": incidents, "longest_incident_sec": longest}


def recovery_start(rows, now, last_check, interval, status):
    """A current streak begins at recovery, and is unavailable for stale checks."""
    if status != 'up' or last_check is None or (now - aware(last_check)).total_seconds() > max(1, interval) * 2:
        return None
    if not rows:
        return None
    ts, new_status = rows[0]
    if new_status != 'up':
        return None
    ts = aware(ts)
    return ts if ts <= now else None


def continuous_up_start(segments, now):
    """A gap or failed result interrupts an observed healthy streak."""
    start, end = None, None
    for lo, hi, fraction in segments:
        if fraction < 1:
            start = None
        elif start is None or end != lo:
            start = lo
        end = hi
    return start if end == now else None
