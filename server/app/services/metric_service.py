from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.core.database import get_clickhouse_client
from app.schemas.metric import MetricPoint, MetricResponse, StatusChangeEvent


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). ClickHouse returns naive datetimes."""
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_unknown_clickhouse_table(exc: Exception) -> bool:
    msg = str(exc)
    return "Unknown table" in msg or "Unknown table expression identifier" in msg or "UNKNOWN_TABLE" in msg


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

    params = {
        "device_id": str(device_id),
        "from_time": from_time.strftime("%Y-%m-%d %H:%M:%S"),
        "to_time": to_time.strftime("%Y-%m-%d %H:%M:%S"),
    }

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
                if(sum(sample_count) = 0, 0, sum(avg_rtt_ms * sample_count) / sum(sample_count)) AS rtt_ms,
                if(sum(sample_count) = 0, 0, sum(avg_packet_loss * sample_count) / sum(sample_count)) AS packet_loss,
                if(sum(sample_count) = 0, 0, sum(avg_jitter_ms * sample_count) / sum(sample_count)) AS jitter_ms,
                min(min_rtt_ms) AS min_rtt_ms,
                max(max_rtt_ms) AS max_rtt_ms,
                if(sum(sample_count) = 0, 0, sum(uptime_pct * sample_count) / sum(sample_count)) AS is_up
            FROM {table}
            WHERE device_id = %(device_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            GROUP BY timestamp
            ORDER BY timestamp
            LIMIT 5000
        """

    result = None
    try:
        result = client.query(query, parameters=params)
    except Exception as exc:
        if granularity == "raw" or not _is_unknown_clickhouse_table(exc):
            raise

    # Fallback: if rollup table is unavailable/empty, aggregate from raw data.
    if (result is None or not result.result_rows) and granularity != "raw":
        interval = "5 MINUTE" if granularity == "5m" else "1 HOUR"
        query = f"""
            SELECT
                toStartOfInterval(timestamp, INTERVAL {interval}) AS ts,
                avg(rtt_ms) AS rtt_ms,
                avg(packet_loss) AS packet_loss,
                avg(jitter_ms) AS jitter_ms,
                min(min_rtt_ms) AS min_rtt_ms,
                max(max_rtt_ms) AS max_rtt_ms,
                avg(is_up) AS is_up
            FROM ping_metrics
            WHERE device_id = %(device_id)s
              AND timestamp >= %(from_time)s
              AND timestamp <= %(to_time)s
            GROUP BY ts
            ORDER BY ts
            LIMIT 5000
        """
        result = client.query(query, parameters=params)

    points = []
    for row in result.result_rows:
        raw_is_up = row[6]
        if granularity == "raw":
            is_up = bool(raw_is_up)  # 0/1 -> False/True
        else:
            is_up = float(raw_is_up) > 0.5 if raw_is_up is not None else None

        # rtt_ms: if device was down, rtt is 0 - show as null for cleaner charts
        rtt = row[1]
        if not is_up:
            rtt = None

        points.append(MetricPoint(
            timestamp=_ensure_utc(row[0]),
            rtt_ms=rtt,
            packet_loss=row[2],
            jitter_ms=row[3],
            min_rtt_ms=row[4],
            max_rtt_ms=row[5],
            is_up=is_up,
        ))

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

    try:
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
    except Exception as exc:
        if _is_unknown_clickhouse_table(exc):
            return []
        raise

    events = []
    for row in result.result_rows:
        events.append(StatusChangeEvent(
            device_id=row[0],
            old_status=row[2],
            new_status=row[3],
            reason=row[4],
            timestamp=_ensure_utc(row[1]),
            duration_sec=row[5],
        ))

    return events
