from datetime import datetime, timedelta
from uuid import UUID

from app.core.database import get_clickhouse_client
from app.schemas.metric import MetricPoint, MetricResponse, StatusChangeEvent


def get_device_metrics(
    device_id: UUID,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    granularity: str = "auto",
) -> MetricResponse:
    if to_time is None:
        to_time = datetime.utcnow()
    if from_time is None:
        from_time = to_time - timedelta(hours=24)

    # Auto-select table based on time range
    time_range = to_time - from_time
    if granularity == "auto":
        if time_range <= timedelta(hours=6):
            granularity = "raw"
        elif time_range <= timedelta(days=7):
            granularity = "5m"
        else:
            granularity = "1h"

    table_map = {
        "raw": "ping_metrics",
        "5m": "ping_metrics_5m",
        "1h": "ping_metrics_1h",
    }
    table = table_map.get(granularity, "ping_metrics_5m")

    client = get_clickhouse_client()

    if granularity == "raw":
        query = f"""
            SELECT
                timestamp,
                rtt_ms,
                packet_loss,
                jitter_ms,
                min_rtt_ms,
                max_rtt_ms,
                is_up
            FROM {table}
            WHERE device_id = %(device_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            ORDER BY timestamp
            LIMIT 5000
        """
    else:
        query = f"""
            SELECT
                timestamp,
                avg_rtt_ms AS rtt_ms,
                avg_packet_loss AS packet_loss,
                avg_jitter_ms AS jitter_ms,
                min_rtt_ms,
                max_rtt_ms,
                uptime_pct AS is_up
            FROM {table}
            WHERE device_id = %(device_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            ORDER BY timestamp
            LIMIT 5000
        """

    result = client.query(
        query,
        parameters={
            "device_id": str(device_id),
            "from_time": from_time.strftime("%Y-%m-%d %H:%M:%S"),
            "to_time": to_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    points = []
    for row in result.result_rows:
        points.append(MetricPoint(
            timestamp=row[0],
            rtt_ms=row[1],
            packet_loss=row[2],
            jitter_ms=row[3],
            min_rtt_ms=row[4],
            max_rtt_ms=row[5],
            is_up=bool(row[6]) if granularity == "raw" else row[6] > 0.5,
        ))

    client.close()

    return MetricResponse(
        device_id=device_id,
        granularity=granularity,
        from_time=from_time,
        to_time=to_time,
        points=points,
    )


def get_status_history(
    device_id: UUID,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 100,
) -> list[StatusChangeEvent]:
    if to_time is None:
        to_time = datetime.utcnow()
    if from_time is None:
        from_time = to_time - timedelta(days=30)

    client = get_clickhouse_client()

    result = client.query(
        """
        SELECT device_id, timestamp, old_status, new_status, reason, duration_sec
        FROM device_status_log
        WHERE device_id = %(device_id)s
          AND timestamp >= %(from_time)s
          AND timestamp <= %(to_time)s
        ORDER BY timestamp DESC
        LIMIT %(limit)s
        """,
        parameters={
            "device_id": str(device_id),
            "from_time": from_time.strftime("%Y-%m-%d %H:%M:%S"),
            "to_time": to_time.strftime("%Y-%m-%d %H:%M:%S"),
            "limit": limit,
        },
    )

    events = []
    for row in result.result_rows:
        events.append(StatusChangeEvent(
            device_id=row[0],
            old_status=row[2],
            new_status=row[3],
            reason=row[4],
            timestamp=row[1],
            duration_sec=row[5],
        ))

    client.close()
    return events
