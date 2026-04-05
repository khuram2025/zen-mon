from datetime import datetime, timedelta
from uuid import UUID

from app.core.database import get_clickhouse_client
from app.schemas.service_check import ServiceMetricPoint, ServiceMetricResponse


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
    else:
        table = "service_metrics_5m"
        query = f"""
            SELECT timestamp, avg_response_ms AS response_ms, uptime_pct AS is_up,
                   NULL AS status_code, NULL AS tls_days_remaining, NULL AS error_message
            FROM {table}
            WHERE service_check_id = %(check_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
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
        if not is_up and granularity == "raw":
            resp_ms = None

        points.append(ServiceMetricPoint(
            timestamp=row[0],
            response_ms=resp_ms,
            is_up=is_up,
            status_code=row[3],
            tls_days_remaining=row[4],
            error_message=row[5],
        ))

    client.close()

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
            "timestamp": row[1],
            "old_status": row[2],
            "new_status": row[3],
            "reason": row[4],
            "duration_sec": row[5],
        })

    client.close()
    return events
