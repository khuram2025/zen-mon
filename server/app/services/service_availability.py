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
    rollups, raw = [], []
    if start < cutoff:
        rollups = ch.query("""
            SELECT timestamp, sum(uptime_pct * sample_count) / nullIf(sum(sample_count), 0),
                   sum(sample_count)
            FROM zenplus.service_metrics_5m
            WHERE service_check_id = %(id)s AND timestamp >= %(f)s AND timestamp < %(t)s
            GROUP BY timestamp ORDER BY timestamp
        """, parameters={"id": str(check_id), "f": (start - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),
                          "t": min(end, cutoff).strftime('%Y-%m-%d %H:%M:%S')}).result_rows
    if end > cutoff:
        raw = ch.query("""
            SELECT timestamp, min(is_up), count()
            FROM zenplus.service_metrics
            WHERE service_check_id = %(id)s AND timestamp >= %(f)s AND timestamp < %(t)s
            GROUP BY timestamp ORDER BY timestamp
        """, parameters={"id": str(check_id), "f": (max(start, cutoff) - timedelta(seconds=grace)).strftime('%Y-%m-%d %H:%M:%S'),
                          "t": end.strftime('%Y-%m-%d %H:%M:%S')}).result_rows
    return segments_from_rows(rollups, raw, start, end, interval, cutoff)


def segments_from_rows(rollups, raw, start, end, interval, cutoff):
    """Shared conversion for single-check and fleet queries."""
    pieces, count = [], 0
    grace = max(1, interval) * 2
    if end <= start:
        return pieces, count
    for ts, up, samples in rollups:
        ts = aware(ts)
        lo, hi = max(start, ts), min(end, cutoff, ts + timedelta(seconds=min(300, samples * max(1, interval))))
        if hi > lo and up is not None:
            pieces.append((lo, hi, max(0., min(1., float(up)))))
            count += int(samples)
    raw_start = max(start, cutoff)
    for i, (ts, up, samples) in enumerate(raw):
        ts = aware(ts)
        next_ts = aware(raw[i + 1][0]) if i + 1 < len(raw) else end
        lo, hi = max(raw_start, ts), min(end, next_ts, ts + timedelta(seconds=grace))
        if hi > lo:
            pieces.append((lo, hi, float(bool(up))))
        if raw_start <= ts < end:
            count += int(samples)
    return pieces, count


def read_fleet_segments(ch, checks, start, end, basis="confirmed"):
    """Batch the same evidence used by detail SLA, without a query per monitor.

    checks maps IDs to (created_at, interval). Scope is applied before querying.
    Process bounded groups so a large fleet does not materialize all history at once.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    ids = list(checks)
    for offset in range(0, len(ids), 50):
        group = ids[offset:offset + 50]
        grace = max(max(1, checks[cid][1]) * 2 for cid in group)
        raw, rollups, events = {}, {}, {}
        params = {"ids": group, "f": (max(start, cutoff) - timedelta(seconds=grace)).strftime('%Y-%m-%d %H:%M:%S'),
                  "t": end.strftime('%Y-%m-%d %H:%M:%S')}
        if end > cutoff:
            rows = ch.query("""
                SELECT service_check_id, timestamp, min(is_up), count()
                FROM zenplus.service_metrics
                WHERE service_check_id IN %(ids)s AND timestamp >= %(f)s AND timestamp < %(t)s
                GROUP BY service_check_id, timestamp ORDER BY service_check_id, timestamp
            """, parameters=params).result_rows
            for cid, *row in rows:
                raw.setdefault(str(cid), []).append(row)
        if start < cutoff:
            rows = ch.query("""
                SELECT service_check_id, timestamp,
                       sum(uptime_pct * sample_count) / nullIf(sum(sample_count), 0), sum(sample_count)
                FROM zenplus.service_metrics_5m
                WHERE service_check_id IN %(ids)s AND timestamp >= %(f)s AND timestamp < %(t)s
                GROUP BY service_check_id, timestamp ORDER BY service_check_id, timestamp
            """, parameters={**params, "f": (start - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),
                              "t": min(end, cutoff).strftime('%Y-%m-%d %H:%M:%S')}).result_rows
            for cid, *row in rows:
                rollups.setdefault(str(cid), []).append(row)
        if basis == "confirmed":
            params["f"] = start.strftime('%Y-%m-%d %H:%M:%S')
            prior = ch.query("""
                SELECT service_check_id, timestamp, new_status FROM zenplus.service_status_log
                WHERE service_check_id IN %(ids)s AND timestamp < %(f)s
                ORDER BY service_check_id, timestamp DESC LIMIT 1 BY service_check_id
            """, parameters=params).result_rows
            changes = ch.query("""
                SELECT service_check_id, timestamp, new_status FROM zenplus.service_status_log
                WHERE service_check_id IN %(ids)s AND timestamp >= %(f)s AND timestamp < %(t)s
                ORDER BY service_check_id, timestamp
            """, parameters=params).result_rows
            for cid, ts, status in [*prior, *changes]:
                events.setdefault(str(cid), []).append((ts, status))
        for cid in group:
            created, interval = checks[cid]
            observed_start = max(start, aware(created)) if created else start
            observations, count = segments_from_rows(rollups.get(cid, []), raw.get(cid, []), observed_start, end, interval, cutoff)
            segments = confirmed_segments(observations, events.get(cid, [])) if basis == "confirmed" else observations
            yield cid, segments, count, observed_start


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


def daily_summaries(segments, start, end, zone):
    """Split observed coverage at local midnights, including DST boundaries."""
    bins = {}
    for lo, hi, fraction in segments:
        while lo < hi:
            local_day = lo.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            boundary = (local_day + timedelta(days=1)).astimezone(timezone.utc)
            stop = min(hi, boundary)
            bins.setdefault(local_day, []).append((lo, stop, fraction))
            lo = stop
    out = []
    for day, values in sorted(bins.items()):
        stats = summarize(values, max(start, day.astimezone(timezone.utc)),
                          min(end, (day + timedelta(days=1)).astimezone(timezone.utc)))
        out.append({"date": day.date().isoformat(), "uptime_pct": stats["uptime_pct"],
                    "covered_sec": stats["covered_sec"], "sample_count": 0})
    return out


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


def confirmed_segments(observations, events):
    """Intersect recorded monitor state with observed coverage, never fill probe gaps.

    A failed probe while the confirmed state is Up is diagnostic evidence only.
    Down starts at its recorded confirmation timestamp, not the first failed probe.
    Successful observations can establish an initial Up state when no older state
    record exists. Failures alone cannot establish either Up or confirmed Down.
    """
    events = sorted((aware(ts), status) for ts, status in events)
    index, state = 0, None
    output = []
    for lo, hi, raw_up in observations:
        lo, hi = aware(lo), aware(hi)
        while index < len(events) and events[index][0] <= lo:
            state = events[index][1]
            index += 1
        if state is None and raw_up == 1:
            state = 'up'
        cursor = lo
        while cursor < hi:
            boundary = min(hi, events[index][0]) if index < len(events) else hi
            if boundary > cursor and state in ('up', 'warning', 'degraded', 'down'):
                output.append((cursor, boundary, 0. if state == 'down' else 1.))
            cursor = boundary
            if index < len(events) and events[index][0] == cursor:
                state = events[index][1]
                index += 1
    return output


def read_confirmed_segments(ch, check_id, start, end, observations):
    """Use historical state changes, not today's retry settings, to score confirmation."""
    if not observations or end <= start:
        return []
    params = {'id': str(check_id), 'f': start.strftime('%Y-%m-%d %H:%M:%S'),
              't': end.strftime('%Y-%m-%d %H:%M:%S')}
    prior = ch.query("""
        SELECT timestamp, new_status FROM zenplus.service_status_log
        WHERE service_check_id = %(id)s AND timestamp < %(f)s
        ORDER BY timestamp DESC LIMIT 1
    """, parameters=params).result_rows
    events = ch.query("""
        SELECT timestamp, new_status FROM zenplus.service_status_log
        WHERE service_check_id = %(id)s AND timestamp >= %(f)s AND timestamp < %(t)s
        ORDER BY timestamp
    """, parameters=params).result_rows
    return confirmed_segments(observations, [*prior, *events])
