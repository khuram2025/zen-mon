"""Detailed host metrics for the server detail page.

One request returns every breakdown the page charts, bucketed so a window of
any length yields a few hundred points per series:

- CPU: total, user and system time, processor queue length, per-core usage
- memory: used, available, cached, committed and swap
- disk I/O per device: throughput, IOPS, latency, utilisation, queue
- filesystems per mount: usage, plus growth over the last week and a
  days-until-full forecast
- network per interface: throughput, packets, errors and drops

The agent has collected all of this for a long time; the page previously
charted only total CPU, memory, and summed disk and network throughput.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.filesystem_monitoring import ch_capacity_filter

# About this many points per series, whatever the window.
TARGET_POINTS = 240
# The agent reports once a minute, so smaller buckets only add gaps.
MIN_BUCKET_S = 60
# Raw host metrics are kept for 30 days.
MAX_RANGE = timedelta(days=30)
# Filesystem growth is measured over this window, independent of the chart.
GROWTH_WINDOW = timedelta(days=7)


def bucket_seconds(range_s: float) -> int:
    """Bucket width giving about TARGET_POINTS points, in whole minutes."""
    raw = max(MIN_BUCKET_S, range_s / TARGET_POINTS)
    return int(math.ceil(raw / 60) * 60)


def days_until_full(total: float, used: float, growth_per_day: float) -> float | None:
    """Days until a volume fills at its current growth, or None if not growing."""
    if total <= 0 or growth_per_day <= 0:
        return None
    return max(0.0, (total - used) / growth_per_day)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts(value: datetime) -> str:
    return _utc(value).isoformat()


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, 3)


def query_server_metric_detail(client: Any, server_id: str, from_time: datetime, to_time: datetime) -> dict:
    """Run the breakdown queries for one server and window."""
    to_time = _utc(to_time)
    from_time = max(_utc(from_time), to_time - MAX_RANGE)
    bucket = bucket_seconds((to_time - from_time).total_seconds())
    params = {
        "sid": server_id,
        "f": from_time.strftime("%Y-%m-%d %H:%M:%S"),
        "t": to_time.strftime("%Y-%m-%d %H:%M:%S"),
        "b": bucket,
    }
    window = "server_id = %(sid)s AND timestamp >= %(f)s AND timestamp <= %(t)s"
    bucketed = "toStartOfInterval(timestamp, toIntervalSecond(%(b)s))"

    def rows(sql: str, extra: dict | None = None) -> list:
        return client.query(sql, parameters={**params, **(extra or {})}).result_rows

    cpu = [
        {"ts": _ts(r[0]), "total": _num(r[1]), "user": _num(r[2]), "system": _num(r[3]),
         "queue": _num(r[4]), "peak": _num(r[5])}
        for r in rows(f"""
            SELECT {bucketed} AS ts, avg(cpu_total_pct), avg(cpu_user_pct), avg(cpu_system_pct),
                   avg(load_1), max(cpu_total_pct)
            FROM zenplus.host_cpu_metrics WHERE {window}
            GROUP BY ts ORDER BY ts""")
    ]
    core_rows = rows(f"""
        SELECT {bucketed} AS ts, avgForEach(per_core)
        FROM zenplus.host_cpu_metrics WHERE {window} AND length(per_core) > 0
        GROUP BY ts ORDER BY ts""")
    cores = {
        "series": [{"ts": _ts(r[0]), "values": [_num(v) for v in r[1]]} for r in core_rows],
        "latest": [_num(v) for v in core_rows[-1][1]] if core_rows else [],
    }

    memory = [
        {"ts": _ts(r[0]), "total": _num(r[1]), "used": _num(r[2]), "available": _num(r[3]),
         "cached": _num(r[4]), "committed": _num(r[5]), "swap_total": _num(r[6]),
         "swap_used": _num(r[7]), "used_pct": _num(r[8])}
        for r in rows(f"""
            SELECT {bucketed} AS ts, avg(total_bytes), avg(used_bytes), avg(available_bytes),
                   avg(cached_bytes), avg(committed_bytes), avg(swap_total_bytes),
                   avg(swap_used_bytes), avg(used_pct)
            FROM zenplus.host_memory_metrics WHERE {window}
            GROUP BY ts ORDER BY ts""")
    ]

    disks: dict[str, list] = {}
    for r in rows(f"""
            SELECT device, {bucketed} AS ts, avg(read_bytes_ps), avg(write_bytes_ps),
                   avg(read_iops), avg(write_iops), avg(util_pct), avg(avg_read_ms),
                   avg(avg_write_ms), avg(queue_length)
            FROM zenplus.host_disk_io_metrics WHERE {window}
            GROUP BY device, ts ORDER BY device, ts"""):
        disks.setdefault(r[0], []).append({
            "ts": _ts(r[1]), "read_bps": _num(r[2]), "write_bps": _num(r[3]),
            "read_iops": _num(r[4]), "write_iops": _num(r[5]), "util_pct": _num(r[6]),
            "read_ms": _num(r[7]), "write_ms": _num(r[8]), "queue": _num(r[9]),
        })

    capacity = ch_capacity_filter()
    filesystems: dict[str, dict] = {}
    for r in rows(f"""
            SELECT mount, any(fs_type), {bucketed} AS ts, avg(used_bytes), avg(total_bytes),
                   avg(used_pct)
            FROM zenplus.host_filesystem_metrics WHERE {window} AND {capacity}
            GROUP BY mount, ts ORDER BY mount, ts"""):
        fs = filesystems.setdefault(r[0], {"mount": r[0], "fs_type": r[1], "points": []})
        fs["points"].append({"ts": _ts(r[2]), "used_bytes": _num(r[3]),
                             "total_bytes": _num(r[4]), "used_pct": _num(r[5])})
    # Growth over the last week from a least-squares fit, so a single
    # clean-up or burst does not dominate the forecast.
    growth_from = (to_time - GROWTH_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    for r in rows(f"""
            SELECT mount, tupleElement(simpleLinearRegression(toUnixTimestamp(timestamp), toFloat64(used_bytes)), 1),
                   argMax(used_bytes, timestamp), argMax(total_bytes, timestamp), count()
            FROM zenplus.host_filesystem_metrics
            WHERE server_id = %(sid)s AND timestamp >= %(gf)s AND timestamp <= %(t)s AND {capacity}
            GROUP BY mount""", {"gf": growth_from}):
        fs = filesystems.get(r[0])
        if fs is None:
            continue
        # The slope is bytes per second; scale before rounding.
        slope = float(r[1]) if r[1] is not None and not math.isnan(float(r[1])) else 0.0
        per_day = slope * 86400 if (r[4] or 0) >= 10 else None
        fs["growth_bytes_per_day"] = _num(per_day) if per_day is not None else None
        fs["days_until_full"] = _num(days_until_full(float(r[3] or 0), float(r[2] or 0), per_day or 0))

    interfaces: dict[str, list] = {}
    for r in rows(f"""
            SELECT if_name, {bucketed} AS ts, avg(rx_bytes_ps), avg(tx_bytes_ps),
                   avg(rx_packets_ps), avg(tx_packets_ps),
                   avg(rx_errors_ps + tx_errors_ps), avg(rx_dropped_ps + tx_dropped_ps),
                   max(is_up)
            FROM zenplus.host_network_metrics WHERE {window}
            GROUP BY if_name, ts ORDER BY if_name, ts"""):
        interfaces.setdefault(r[0], []).append({
            "ts": _ts(r[1]), "rx_bytes_ps": _num(r[2]), "tx_bytes_ps": _num(r[3]),
            "rx_pps": _num(r[4]), "tx_pps": _num(r[5]), "errors_ps": _num(r[6]),
            "drops_ps": _num(r[7]), "up": bool(r[8]),
        })

    return {
        "server_id": server_id,
        "from": _ts(from_time),
        "to": _ts(to_time),
        "bucket_s": bucket,
        "cpu": cpu,
        "cores": cores,
        "memory": memory,
        "disks": [{"device": d, "points": p} for d, p in sorted(disks.items())],
        "filesystems": sorted(filesystems.values(), key=lambda fs: fs["mount"]),
        "interfaces": [{"name": n, "points": p} for n, p in sorted(interfaces.items())],
    }
