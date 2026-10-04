import json
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.core.database import get_clickhouse_client
from app.schemas.service_check import ServiceMetricPoint, ServiceMetricResponse


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). ClickHouse returns naive datetimes."""
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


RAW_RETENTION = timedelta(days=30)
AUTO_RAW_MAX_EXPECTED = 2000
RAW_LIMIT_DEFAULT = 5000
RAW_LIMIT_MAX = 20000
BUCKET_SEC = {"5m": 300, "1h": 3600}


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _num(value):
    """Finite float or None (ClickHouse returns nan/0 for empty aggregates)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def choose_granularity(granularity: str, span: timedelta, interval_sec: int | float) -> str:
    """`auto`: raw when the expected probe count fits (<= 2000), else 5m up to
    7 days, else 1h. Unknown values fall back to auto."""
    if granularity in ("raw", "5m", "1h"):
        return granularity
    expected = span.total_seconds() / max(1, interval_sec or 60)
    if expected <= AUTO_RAW_MAX_EXPECTED:
        return "raw"
    if span <= timedelta(days=7):
        return "5m"
    return "1h"


def _raw_point(row) -> ServiceMetricPoint:
    return ServiceMetricPoint(
        timestamp=_ensure_utc(row[0]),
        response_ms=_num(row[1]),
        is_up=bool(row[2]),
        status_code=row[3],
        tls_days_remaining=row[4],
        error_message=row[5] or None,
        network_diagnostics=json.loads(row[6]) if len(row) > 6 and row[6] else None,
    )


def get_service_metrics(
    service_check_id: UUID,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    granularity: str = "auto",
    limit: int = RAW_LIMIT_DEFAULT,
    interval_sec: int | float = 60,
    client=None,
    now: datetime | None = None,
) -> ServiceMetricResponse:
    """Probe series for one check.

    raw: the newest `limit` probes of the window, returned ascending, with
    `truncated`/`total_samples` so the chart always includes the latest
    observation and discloses the cut (B07).
    5m/1h: exact per-bucket evidence from raw probes inside raw retention —
    sample/failed counts, success ratio, mean/p95/max of successful probes
    (B06/B08). Older windows fall back to the 5m rollup with sample-weighted
    means and no percentile.
    """
    now = now or datetime.now(timezone.utc)
    to_time = _ensure_utc(to_time) if to_time is not None else now
    from_time = _ensure_utc(from_time) if from_time is not None else to_time - timedelta(hours=24)
    limit = max(1, min(int(limit or RAW_LIMIT_DEFAULT), RAW_LIMIT_MAX))
    granularity = choose_granularity(granularity, to_time - from_time, interval_sec)
    client = client or get_clickhouse_client()
    params = {"check_id": str(service_check_id), "from_time": _fmt(from_time), "to_time": _fmt(to_time)}

    if granularity == "raw":
        total = 0
        count_rows = client.query(
            """
            SELECT count() FROM zenplus.service_metrics
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s AND timestamp <= %(to_time)s
            """,
            parameters=params,
        ).result_rows
        if count_rows:
            total = int(count_rows[0][0] or 0)
        rows = client.query(
            f"""
            SELECT timestamp, response_ms, is_up, status_code,
                   tls_days_remaining, error_message, network_diagnostics
            FROM zenplus.service_metrics
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            ORDER BY timestamp DESC
            LIMIT {limit}
            """,
            parameters=params,
        ).result_rows
        points = [_raw_point(r) for r in reversed(rows)]
        total = max(total, len(points))
        return ServiceMetricResponse(
            service_check_id=service_check_id, granularity="raw", from_time=from_time, to_time=to_time,
            points=points, bucket_sec=None, truncated=total > len(points), total_samples=total,
            percentile_basis="per_probe",
        )

    bucket = BUCKET_SEC[granularity]
    points: list[ServiceMetricPoint] = []
    if from_time >= now - RAW_RETENTION:
        rows = client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS ts,
                   count() AS samples,
                   countIf(is_up = 0) AS failed,
                   countIf(is_up = 1) AS ok,
                   avgIf(response_ms, is_up = 1) AS resp,
                   quantileIf(0.95)(response_ms, is_up = 1) AS p95,
                   maxIf(response_ms, is_up = 1) AS mx,
                   argMaxIf(error_message, timestamp, is_up = 0) AS last_error,
                   argMax(tls_days_remaining, timestamp) AS tls_days
            FROM zenplus.service_metrics
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s AND timestamp <= %(to_time)s
            GROUP BY ts
            ORDER BY ts
            LIMIT 20000
            """,
            parameters=params,
        ).result_rows
        total = 0
        for ts, samples, failed, ok, resp, p95, mx, last_error, tls_days in rows:
            samples, failed, ok = int(samples or 0), int(failed or 0), int(ok or 0)
            total += samples
            points.append(ServiceMetricPoint(
                timestamp=_ensure_utc(ts),
                response_ms=_num(resp) if ok else None,
                is_up=failed == 0,
                status_code=None,
                tls_days_remaining=tls_days,
                error_message=(last_error or None) if failed else None,
                uptime_pct=(ok / samples * 100.0) if samples else None,
                sample_count=samples,
                failed_count=failed,
                p95_response_ms=_num(p95) if ok else None,
                max_response_ms=_num(mx) if ok else None,
            ))
        return ServiceMetricResponse(
            service_check_id=service_check_id, granularity=granularity, from_time=from_time, to_time=to_time,
            points=points, bucket_sec=bucket, truncated=False, total_samples=total,
            percentile_basis="exact_per_bucket",
        )

    # Beyond raw retention: the 5m rollup holds one row per insert batch per
    # bucket, so every mean is weighted by its batch's sample_count (B06).
    rows = client.query(
        f"""
        SELECT toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS ts,
               sum(sample_count) AS samples,
               sum(uptime_pct * sample_count) AS up_weighted,
               sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0) AS resp,
               max(max_response_ms) AS mx
        FROM zenplus.service_metrics_5m
        WHERE service_check_id = %(check_id)s
          AND timestamp >= %(from_time)s AND timestamp <= %(to_time)s
        GROUP BY ts
        ORDER BY ts
        LIMIT 20000
        """,
        parameters=params,
    ).result_rows
    total = 0
    for ts, samples, up_weighted, resp, mx in rows:
        samples = int(samples or 0)
        total += samples
        ok = _num(up_weighted) or 0.0
        failed = max(0, int(round(samples - ok))) if samples else 0
        points.append(ServiceMetricPoint(
            timestamp=_ensure_utc(ts),
            response_ms=_num(resp) if samples else None,
            is_up=failed == 0,
            uptime_pct=(ok / samples * 100.0) if samples else None,
            sample_count=samples,
            failed_count=failed,
            p95_response_ms=None,
            max_response_ms=_num(mx) if samples else None,
        ))
    return ServiceMetricResponse(
        service_check_id=service_check_id, granularity=granularity, from_time=from_time, to_time=to_time,
        points=points, bucket_sec=bucket, truncated=False, total_samples=total,
        percentile_basis="unavailable",
    )


def get_latest_probes(service_check_id: UUID, limit: int = 6, client=None) -> list[ServiceMetricPoint]:
    """Newest individual probes regardless of any chart range (B13)."""
    limit = max(1, min(int(limit or 6), 50))
    client = client or get_clickhouse_client()
    rows = client.query(
        f"""
        SELECT timestamp, response_ms, is_up, status_code,
               tls_days_remaining, error_message, network_diagnostics, poller_id
        FROM zenplus.service_metrics
        WHERE service_check_id = %(check_id)s
        ORDER BY timestamp DESC
        LIMIT {limit}
        """,
        parameters={"check_id": str(service_check_id)},
    ).result_rows
    out = []
    for r in rows:
        point = _raw_point(r[:7])
        point.manual = (len(r) > 7 and r[7] == "api-test")
        out.append(point)
    return out


def get_service_status_history(
    service_check_id: UUID,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 100,
):
    if to_time is None:
        to_time = datetime.utcnow()
    if from_time is None:
        from_time = to_time - timedelta(days=30)

    client = get_clickhouse_client()
    result = client.query(
        """
        SELECT service_check_id, timestamp, old_status, new_status, reason, duration_sec
        FROM service_status_log
        WHERE service_check_id = %(check_id)s
          AND timestamp >= %(from_time)s
          AND timestamp <= %(to_time)s
        ORDER BY timestamp DESC
        LIMIT %(limit)s
        """,
        parameters={
            "check_id": str(service_check_id),
            "from_time": from_time.strftime("%Y-%m-%d %H:%M:%S"),
            "to_time": to_time.strftime("%Y-%m-%d %H:%M:%S"),
            "limit": limit,
        },
    )

    events = []
    for row in result.result_rows:
        events.append({
            "service_check_id": row[0],
            "timestamp": _ensure_utc(row[1]),
            "old_status": row[2],
            "new_status": row[3],
            "reason": row[4],
            "duration_sec": row[5],
        })

    return events
