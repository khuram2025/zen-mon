"""Explain ping status using the owning poller's observations, not chart averages."""
from datetime import datetime, timedelta, timezone

from app.core.database import get_clickhouse_client


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def measurement(sample, thresholds):
    if not sample:
        return None
    timestamp, rtt_ms, packet_loss, is_up = sample
    loss_pct = float(packet_loss) * 100
    breaches = []
    if is_up:
        for name, value, limit, unit in (
            ("Packet loss", loss_pct, thresholds["degraded_loss_pct"], "percentage points"),
            ("Latency", float(rtt_ms), thresholds["degraded_rtt_ms"], "ms"),
        ):
            # Float32 packet loss is compared in its stored ratio domain by the
            # controller. Round display arithmetic, never average samples here.
            excess = round(value - limit, 6)
            if excess > 0:
                breaches.append({"metric": name, "value": value, "threshold": limit,
                                 "exceeded_by": excess, "excess_unit": unit})
    return {"timestamp": utc(timestamp), "rtt_ms": float(rtt_ms), "packet_loss_pct": loss_pct,
            "is_up": bool(is_up), "breaches": breaches}


def degraded_reason(rtt_ms, loss_ratio, rtt_limit, loss_limit_pct):
    parts = []
    loss = float(loss_ratio) * 100
    if rtt_ms > rtt_limit:
        parts.append(f"Latency {rtt_ms:.3f} ms exceeds {rtt_limit:g} ms by {rtt_ms-rtt_limit:.3f} ms")
    if loss > loss_limit_pct:
        parts.append(f"Packet loss {loss:.2f}% exceeds {loss_limit_pct:g}% by {loss-loss_limit_pct:.2f} percentage points")
    return "; ".join(parts) or "High latency or packet loss"


def evidence(device_id, poller_id, thresholds, settings_updated_at, interval):
    client = get_clickhouse_client()
    params = {"id": str(device_id), "poller": poller_id}
    rows = client.query("""SELECT timestamp, rtt_ms, packet_loss, is_up
        FROM ping_metrics WHERE device_id = {id:UUID} AND poller_id = {poller:String}
        AND timestamp >= now() - INTERVAL 24 HOUR ORDER BY timestamp DESC LIMIT 1""",
        parameters=params).result_rows
    latest = measurement(rows[0] if rows else None, thresholds)
    now = datetime.now(timezone.utc)
    if latest:
        latest["stale"] = now - latest["timestamp"] > timedelta(seconds=max(60, interval * 2))
        latest["settings_pending"] = bool(settings_updated_at and latest["timestamp"] < utc(settings_updated_at) + timedelta(seconds=60))

    events = client.query("""SELECT timestamp, reason FROM device_status_log
        WHERE device_id = {id:UUID} AND new_status = 'degraded'
        AND timestamp >= now() - INTERVAL 30 DAY ORDER BY timestamp DESC LIMIT 1""",
        parameters=params).result_rows
    recent = None
    if events:
        at, reason = events[0]
        params["at"] = utc(at)
        samples = client.query("""SELECT timestamp, rtt_ms, packet_loss, is_up
            FROM ping_metrics WHERE device_id = {id:UUID} AND poller_id = {poller:String}
            AND timestamp >= {at:DateTime} - INTERVAL 5 SECOND
            AND timestamp <= {at:DateTime} + INTERVAL 1 SECOND
            ORDER BY abs(dateDiff('millisecond', timestamp, {at:DateTime})) LIMIT 1""",
            parameters=params).result_rows
        recovered = client.query("""SELECT timestamp FROM device_status_log
            WHERE device_id = {id:UUID} AND new_status = 'up' AND timestamp > {at:DateTime}
            ORDER BY timestamp LIMIT 1""", parameters=params).result_rows
        unchanged = not settings_updated_at or utc(settings_updated_at) + timedelta(seconds=60) <= utc(at)
        recent = {"timestamp": utc(at), "reason": reason,
                  "sample": measurement(samples[0] if samples else None, thresholds),
                  "thresholds_unchanged": unchanged,
                  "recovered_at": utc(recovered[0][0]) if recovered else None}
        if recent["sample"] and not unchanged:
            # Current thresholds must not be represented as historical limits.
            recent["sample"]["breaches"] = []
    return {"latest": latest, "recent_degraded": recent}
