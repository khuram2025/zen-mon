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


def get_service_metrics(
    service_check_id: UUID,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    granularity: str = "auto",
) -> ServiceMetricResponse:
    if to_time is None:
        to_time = datetime.utcnow()
    if from_time is None:
        from_time = to_time - timedelta(hours=24)

    time_range = to_time - from_time
    if granularity == "auto":
        if time_range <= timedelta(hours=6):
            granularity = "raw"
        elif time_range <= timedelta(days=7):
            granularity = "5m"
        else:
            granularity = "1h"

    if granularity == "raw":
        table = "service_metrics"
        query = f"""
            SELECT timestamp, response_ms, is_up, status_code,
                   tls_days_remaining, error_message
            FROM {table}
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            ORDER BY timestamp
            LIMIT 5000
        """
    elif granularity == "1h":
        # On-the-fly 1h aggregation from the 5m rollup. The 5m_mv MV is
        # populated; there's no dedicated 1h table. 30 days = 720 buckets,
        # so we comfortably fit under the 5000 row guard.
        table = "service_metrics_5m"
        query = f"""
            SELECT toStartOfHour(timestamp) AS ts,
                   avg(avg_response_ms) AS response_ms,
                   avg(uptime_pct) AS is_up,
                   NULL AS status_code, NULL AS tls_days_remaining, NULL AS error_message
            FROM {table}
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            GROUP BY ts
            ORDER BY ts
            LIMIT 5000
        """
    else:
        # 5m: pre-aggregated rollup, used for ≤ 7 days (≤ 2016 buckets).
        # The 5m_mv inserts one row per source INSERT batch, so the rollup
        # table holds multiple rows per (check_id, ts) bucket (no dedup).
        # GROUP BY timestamp here to collapse duplicates — the underlying
        # values are sample-count weighted, but a sample-count-weighted
        # average is correct even when re-averaged batch-wise here.
        granularity = "5m"
        table = "service_metrics_5m"
        query = f"""
            SELECT timestamp,
                   avg(avg_response_ms) AS response_ms,
                   avg(uptime_pct) AS is_up,
                   NULL AS status_code, NULL AS tls_days_remaining, NULL AS error_message
            FROM {table}
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            GROUP BY timestamp
            ORDER BY timestamp
            LIMIT 5000
        """

    client = get_clickhouse_client()
    result = client.query(
        query,
        parameters={
            "check_id": str(service_check_id),
            "from_time": from_time.strftime("%Y-%m-%d %H:%M:%S"),
            "to_time": to_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    points = []
    for row in result.result_rows:
        raw_is_up = row[2]
        if granularity == "raw":
            is_up = bool(raw_is_up)
        else:
            is_up = float(raw_is_up) > 0.5 if raw_is_up is not None else None

        resp_ms = row[1]

        points.append(ServiceMetricPoint(
            timestamp=_ensure_utc(row[0]),
            response_ms=resp_ms,
            is_up=is_up,
            status_code=row[3],
            tls_days_remaining=row[4],
            error_message=row[5],
        ))

    return ServiceMetricResponse(
        service_check_id=service_check_id,
        granularity=granularity,
        from_time=from_time,
        to_time=to_time,
        points=points,
    )


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
