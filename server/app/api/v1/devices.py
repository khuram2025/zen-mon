from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.schemas.device import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceSummary, DeviceGroupResponse,
    BulkImportRequest, BulkImportResult,
)
from app.schemas.metric import MetricResponse, StatusChangeEvent
from app.services import device_service, metric_service
from app.api.v1.snmp import (
    _snmpget,
    _snmpget_detail,
    _ping_once,
    SYS_DESCR_OID,
    SYS_OBJECT_OID,
    SYS_NAME_OID,
)
from app.core.crypto import decrypt
from sqlalchemy import text
import time
import asyncio

router = APIRouter(prefix="/devices", tags=["Devices"])

SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"


async def _device_snmp_settings(db: AsyncSession, device) -> dict:
    """Resolve effective SNMP settings for a device, including saved credential
    overrides. Decrypts any v3 passphrases. Returns a dict ready for _snmpget."""
    out = {
        "version": device.snmp_version or "2c",
        "port": device.snmp_port or 161,
        "timeout_ms": device.snmp_timeout_ms or 2000,
        "community": device.snmp_community,
        "v3_username": device.snmp_v3_username,
        "v3_context": device.snmp_v3_context,
        "v3_auth_protocol": device.snmp_auth_protocol,
        "v3_auth_passphrase": None,
        "v3_priv_protocol": device.snmp_priv_protocol,
        "v3_priv_passphrase": None,
        "v3_security_level": None,
    }
    try:
        if device.snmp_auth_passphrase:
            out["v3_auth_passphrase"] = decrypt(device.snmp_auth_passphrase)
        if device.snmp_priv_passphrase:
            out["v3_priv_passphrase"] = decrypt(device.snmp_priv_passphrase)
    except Exception:
        pass

    # If a saved credential is linked, its values take precedence.
    if device.snmp_credential_id:
        row = (await db.execute(
            text("""SELECT snmp_version, community, port, timeout_ms,
                           v3_username, v3_context, v3_security_level,
                           v3_auth_protocol, v3_auth_passphrase,
                           v3_priv_protocol, v3_priv_passphrase
                    FROM snmp_credentials WHERE id = :id"""),
            {"id": device.snmp_credential_id},
        )).mappings().first()
        if row:
            out["version"] = row["snmp_version"] or out["version"]
            out["port"] = row["port"] or out["port"]
            out["timeout_ms"] = row["timeout_ms"] or out["timeout_ms"]
            out["community"] = row["community"] or out["community"]
            out["v3_username"] = row["v3_username"] or out["v3_username"]
            out["v3_context"] = row["v3_context"] or out["v3_context"]
            out["v3_security_level"] = row["v3_security_level"] or out["v3_security_level"]
            out["v3_auth_protocol"] = row["v3_auth_protocol"] or out["v3_auth_protocol"]
            out["v3_priv_protocol"] = row["v3_priv_protocol"] or out["v3_priv_protocol"]
            try:
                if row["v3_auth_passphrase"]:
                    out["v3_auth_passphrase"] = decrypt(row["v3_auth_passphrase"])
                if row["v3_priv_passphrase"]:
                    out["v3_priv_passphrase"] = decrypt(row["v3_priv_passphrase"])
            except Exception:
                pass
    return out


@router.get("", response_model=dict)
async def list_devices(
    status: str | None = None,
    group_id: UUID | None = None,
    device_type: str | None = None,
    location: str | None = None,
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices, total = await device_service.get_devices(
        db, status, group_id, device_type, location, search, skip, limit
    )
    return {
        "data": [_device_to_response(d) for d in devices],
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


@router.get("/summary", response_model=DeviceSummary)
async def device_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_device_summary(db)




@router.get("/dashboard/uptime-stats")
async def dashboard_uptime_stats(
    hours: int = Query(default=24, ge=1, le=8760),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get uptime percentages per device over a time range from ClickHouse metrics."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    to_time = datetime.utcnow()
    from_time = to_time - timedelta(hours=hours)

    # Try rollup tables first, fall back to raw
    tables_to_try = []
    if hours <= 6:
        tables_to_try = ["ping_metrics"]
    elif hours <= 168:
        tables_to_try = ["ping_metrics_5m", "ping_metrics"]
    else:
        tables_to_try = ["ping_metrics_1h", "ping_metrics_5m", "ping_metrics"]

    uptime_map = {}
    for table in tables_to_try:
        query = f"""
            SELECT device_id,
                   countIf(is_up = 1) AS up_count,
                   count() AS total_count
            FROM zenplus.{table}
            WHERE timestamp >= %(from)s AND timestamp <= %(to)s
            GROUP BY device_id
        """
        try:
            result = client.query(query, parameters={"from": from_time, "to": to_time})
            if len(result.result_rows) > 0:
                for row in result.result_rows:
                    device_id = str(row[0])
                    up = row[1]
                    total = row[2]
                    uptime_map[device_id] = round((up / total * 100) if total > 0 else 0, 2)
                break
        except Exception:
            continue

    return {"hours": hours, "from": from_time.isoformat(), "to": to_time.isoformat(), "devices": uptime_map}


@router.get("/current-uptime")
async def current_uptime(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the current continuous uptime (seconds) for each up device.

    For each device whose most recent status log transition is to `up`, we
    return `now - transition_timestamp`. Devices with no log fall back to
    `created_at` (best guess for devices that have been up since creation).
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Latest transition row per device.
    try:
        res = client.query(
            """
            SELECT
              device_id,
              argMax(timestamp, timestamp) AS last_ts,
              argMax(new_status, timestamp) AS last_status
            FROM zenplus.device_status_log
            GROUP BY device_id
            """
        )
    except Exception:
        res = None

    now = datetime.now(timezone.utc)
    uptime: dict[str, float] = {}
    seen_ids: set[str] = set()
    if res is not None:
        for row in res.result_rows:
            did = str(row[0])
            seen_ids.add(did)
            last_ts = row[1]
            last_status = row[2]
            if last_status != "up":
                continue
            # ClickHouse can return naive datetimes.
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            delta = (now - last_ts).total_seconds()
            if delta > 0:
                uptime[did] = delta

    # Fallback: devices that are currently up but have no status log entry —
    # use created_at as the uptime origin. Only applies to `up` devices.
    rows = (await db.execute(text(
        "SELECT id, created_at FROM devices WHERE status = 'up'"
    ))).all()
    for row in rows:
        did = str(row[0])
        if did in uptime:
            continue
        created_at = row[1]
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        delta = (now - created_at).total_seconds()
        if delta > 0:
            uptime[did] = delta

    return {"generated_at": now.isoformat(), "devices": uptime}


@router.get("/current-metrics")
async def current_metrics(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Latest scalar SNMP metric values per device (cpu, memory, ...).

    Returns `{"devices": {device_id: {metric_key: value, ...}}}` where each
    value is the most recent point from `zenplus.snmp_metrics` within the
    last 15 minutes. Used by the devices list to show real CPU/memory
    instead of synthesizing them on the client.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    try:
        res = client.query(
            """
            SELECT device_id, metric_key,
                   argMax(value, timestamp) AS val,
                   max(timestamp) AS ts
            FROM zenplus.snmp_metrics
            WHERE timestamp >= now() - INTERVAL 15 MINUTE
            GROUP BY device_id, metric_key
            """
        )
    except Exception:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "devices": {}}

    out: dict[str, dict] = {}
    latest_ts: dict[str, datetime] = {}
    for row in res.result_rows:
        did = str(row[0])
        key = row[1]
        val = float(row[2])
        ts = row[3]
        bucket = out.setdefault(did, {})
        bucket[key] = val
        if did not in latest_ts or ts > latest_ts[did]:
            latest_ts[did] = ts
    for did, ts in latest_ts.items():
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out[did]["_ts"] = ts.isoformat()

    return {"generated_at": datetime.now(timezone.utc).isoformat(), "devices": out}


@router.get("/groups", response_model=list[DeviceGroupResponse])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    groups = await device_service.get_device_groups(db)
    return groups


@router.get("/locations")
async def list_locations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_distinct_locations(db)


@router.get("/device-types")
async def list_device_types(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_distinct_device_types(db)


@router.post("/bulk-delete")
async def bulk_delete_devices(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    ids = data.get("device_ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No device IDs provided")
    deleted = await device_service.bulk_delete_devices(db, [UUID(i) for i in ids])
    return {"deleted": deleted}


@router.post("", response_model=DeviceResponse, status_code=201)
async def create_device(
    data: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    device = await device_service.create_device(db, data, user.id)
    return _device_to_response(device)


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_devices(
    data: BulkImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    return await device_service.bulk_import_devices(db, data.devices, user.id)


@router.get("/export")
async def export_devices(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices = await device_service.export_devices(db)
    return JSONResponse(content={"devices": devices})


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: UUID,
    data: DeviceUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    device = await device_service.update_device(db, device_id, data)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    deleted = await device_service.delete_device(db, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")


@router.get("/{device_id}/metrics", response_model=MetricResponse)
async def get_device_metrics(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="auto", pattern="^(raw|5m|1h|auto)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Verify device exists
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_device_metrics(device_id, from_time, to_time, granularity)


@router.get("/{device_id}/ping-series")
async def get_device_ping_series(
    device_id: UUID,
    minutes: int = Query(default=60, ge=5, le=1440),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-device ping time-series for the manual-map inspector sparkline.

    Returns ~60 evenly-bucketed samples over the requested window.
    Uses 5-minute rollup for windows > 6 h, raw otherwise.
    """
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    bucket_seconds = max(30, (minutes * 60) // 60)
    table = "ping_metrics" if minutes <= 360 else "ping_metrics_5m"
    try:
        res = client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL %(bs)s SECOND) AS ts,
                   avg(rtt_ms)      AS rtt_ms,
                   avg(packet_loss) AS packet_loss,
                   max(is_up)       AS is_up
            FROM zenplus.{table}
            WHERE device_id = %(id)s
              AND timestamp BETWEEN %(start)s AND %(end)s
            GROUP BY ts ORDER BY ts
            """,
            parameters={"id": str(device_id), "bs": bucket_seconds, "start": start, "end": end},
        )
        points = [
            {
                "ts": r[0].isoformat() if r[0] else None,
                "rtt_ms": round(float(r[1] or 0.0), 2),
                "packet_loss": round(float(r[2] or 0.0), 3),
                "is_up": bool(r[3]) if r[3] is not None else None,
            }
            for r in res.result_rows
        ]
    except Exception:
        points = []

    return {
        "device_id": str(device_id),
        "minutes": minutes,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "points": points,
    }


@router.get("/{device_id}/interfaces")
async def get_device_interfaces(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List SNMP-discovered interfaces for a device."""
    from sqlalchemy import text
    rows = (await db.execute(
        text("""
            SELECT id, if_index, if_name, if_descr, if_alias, if_type, if_speed,
                   mac_address::text AS mac_address, admin_status, oper_status,
                   monitored, first_seen, last_seen
            FROM device_interfaces
            WHERE device_id = :id
            ORDER BY if_index
        """),
        {"id": device_id},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{device_id}/entities")
async def get_device_entities(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import text
    rows = (await db.execute(
        text("""
            SELECT id, ent_index, parent_index, class, name, serial_number,
                   model_name, hw_revision, fw_revision, first_seen, last_seen
            FROM device_entities WHERE device_id = :id ORDER BY ent_index
        """),
        {"id": device_id},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{device_id}/sensors")
async def get_device_sensors(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import text
    rows = (await db.execute(
        text("""
            SELECT id, sensor_index, sensor_type, description, unit, monitored,
                   first_seen, last_seen
            FROM device_sensors WHERE device_id = :id ORDER BY sensor_index
        """),
        {"id": device_id},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{device_id}/snmp-metrics")
async def get_device_snmp_metrics(
    device_id: UUID,
    hours: int = Query(default=24, ge=1, le=2160),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Scalar SNMP metrics (CPU, memory, temperature...) from ClickHouse.

    Returns a dict keyed by metric_key with an array of {ts, value} points.
    Uses 5m rollups for windows > 6h, raw otherwise.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Pick a table + bucket based on window. Some upgraded appliances have an
    # empty 5m SNMP rollup even though raw samples exist, so larger windows
    # fall back to on-the-fly raw aggregation when the rollup returns no rows.
    if hours <= 6:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   value AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, timestamp, val
            ORDER BY metric_key, timestamp
        """]
    elif hours <= 168:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   avg(avg_value) AS val,
                   '' AS unit
            FROM zenplus.snmp_metrics_5m
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, timestamp
            ORDER BY metric_key, timestamp
        """, """
            SELECT metric_key,
                   toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL 300 SECOND)) * 1000 AS ts_ms,
                   avg(value) AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """]
    else:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp(toStartOfHour(timestamp)) * 1000 AS ts_ms,
                   avg(avg_value) AS val,
                   '' AS unit
            FROM zenplus.snmp_metrics_5m
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """, """
            SELECT metric_key,
                   toUnixTimestamp(toStartOfHour(timestamp)) * 1000 AS ts_ms,
                   avg(value) AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """]

    res = None
    last_error = None
    params = {"id": str(device_id), "hours": hours}
    for sql in sql_candidates:
        try:
            candidate = client.query(sql, parameters=params)
        except Exception as e:
            last_error = e
            continue
        if candidate.result_rows or res is None:
            res = candidate
        if candidate.result_rows:
            break
    if res is None:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {last_error}")

    out: dict[str, dict] = {}
    for r in res.result_rows:
        key = r[0]
        if key not in out:
            out[key] = {"unit": r[3] or "", "points": []}
        out[key]["points"].append({"ts": r[1], "value": float(r[2])})
    return out


@router.get("/{device_id}/snmp-if-metrics")
async def get_device_snmp_if_metrics(
    device_id: UUID,
    hours: int = Query(default=1, ge=1, le=720),
    if_index: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-interface bps/errors time series from ClickHouse.

    Always bucketed from the raw `snmp_if_metrics` table (30-day TTL). Bucket
    width is chosen to keep response payloads reasonable while preserving
    enough resolution for the chart.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Bucket width: raw for short windows, then progressively coarser.
    if hours <= 6:
        bucket_seconds = 0  # no bucketing — return raw points
    elif hours <= 24:
        bucket_seconds = 300       # 5 minutes
    elif hours <= 24 * 7:
        bucket_seconds = 1800      # 30 minutes
    else:
        bucket_seconds = 3600 * 2  # 2 hours

    where = "device_id = %(id)s AND timestamp >= now() - INTERVAL %(hours)s HOUR"
    params: dict = {"id": str(device_id), "hours": hours}
    if if_index is not None:
        where += " AND if_index = %(if)s"
        params["if"] = if_index

    if bucket_seconds == 0:
        sql = f"""
            SELECT if_index,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   in_bps,
                   out_bps
            FROM zenplus.snmp_if_metrics
            WHERE {where}
            ORDER BY if_index, timestamp
        """
    else:
        sql = f"""
            SELECT if_index,
                   toUnixTimestamp(
                     toStartOfInterval(timestamp, INTERVAL {bucket_seconds} SECOND)
                   ) * 1000 AS ts_ms,
                   avg(in_bps) AS in_bps,
                   avg(out_bps) AS out_bps
            FROM zenplus.snmp_if_metrics
            WHERE {where}
            GROUP BY if_index, ts_ms
            ORDER BY if_index, ts_ms
        """

    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    out: dict[int, list] = {}
    for r in res.result_rows:
        idx = int(r[0])
        out.setdefault(idx, []).append({
            "ts": r[1],
            "in_bps": float(r[2]),
            "out_bps": float(r[3]),
        })
    return out


@router.get("/{device_id}/interfaces/{if_index}/metrics")
async def get_interface_detail_metrics(
    device_id: UUID,
    if_index: int,
    hours: int = Query(default=6, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Detailed per-interface metrics including traffic, errors, discards, packets."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Bucket from the raw 30-day-retention table; rollups are unreliable.
    if hours <= 6:
        sql = """
            SELECT toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   in_bps, out_bps,
                   in_errors, out_errors,
                   in_discards, out_discards,
                   in_ucast_pkts, out_ucast_pkts,
                   in_octets, out_octets
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s AND if_index = %(if)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            ORDER BY timestamp
        """
    else:
        bucket_seconds = 300 if hours <= 24 else (1800 if hours <= 24 * 7 else 7200)
        sql = f"""
            SELECT toUnixTimestamp(
                     toStartOfInterval(timestamp, INTERVAL {bucket_seconds} SECOND)
                   ) * 1000 AS ts_ms,
                   avg(in_bps) AS in_bps,
                   avg(out_bps) AS out_bps,
                   sum(in_errors) AS in_errors,
                   sum(out_errors) AS out_errors,
                   sum(in_discards) AS in_discards,
                   sum(out_discards) AS out_discards,
                   sum(in_ucast_pkts) AS in_ucast_pkts,
                   sum(out_ucast_pkts) AS out_ucast_pkts,
                   sum(in_octets) AS in_octets,
                   sum(out_octets) AS out_octets
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s AND if_index = %(if)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY ts_ms
            ORDER BY ts_ms
        """

    params = {"id": str(device_id), "if": if_index, "hours": hours}

    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    traffic = []
    errors = []
    for r in res.result_rows:
        traffic.append({
            "ts": r[0], "in_bps": float(r[1]), "out_bps": float(r[2]),
        })
        errors.append({
            "ts": r[0],
            "in_errors": int(r[3]), "out_errors": int(r[4]),
            "in_discards": int(r[5]), "out_discards": int(r[6]),
        })

    # Summary stats
    if traffic:
        in_vals = [p["in_bps"] for p in traffic]
        out_vals = [p["out_bps"] for p in traffic]
        err_total = sum(e["in_errors"] + e["out_errors"] for e in errors)
        disc_total = sum(e["in_discards"] + e["out_discards"] for e in errors)
        summary = {
            "in_avg_bps": sum(in_vals) / len(in_vals),
            "in_max_bps": max(in_vals),
            "in_current_bps": in_vals[-1],
            "out_avg_bps": sum(out_vals) / len(out_vals),
            "out_max_bps": max(out_vals),
            "out_current_bps": out_vals[-1],
            "total_errors": err_total,
            "total_discards": disc_total,
            "samples": len(traffic),
        }
    else:
        summary = {}

    return {"traffic": traffic, "errors": errors, "summary": summary}


@router.get("/{device_id}/traps")
async def get_device_traps(
    device_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    hours: int = Query(default=24, ge=1, le=720),
    user: User = Depends(get_current_user),
):
    """Recent SNMP traps for this device, sourced from ClickHouse."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    try:
        res = client.query(
            """
            SELECT toString(source_ip), trap_oid, trap_name, severity, message,
                   bindings, toUnixTimestamp64Milli(timestamp)
            FROM zenplus.snmp_traps
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            ORDER BY timestamp DESC
            LIMIT %(limit)s
            """,
            parameters={"id": str(device_id), "hours": hours, "limit": limit},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    return [
        {
            "source_ip": r[0],
            "trap_oid": r[1],
            "trap_name": r[2],
            "severity": r[3],
            "message": r[4],
            "bindings": r[5],
            "timestamp": datetime.fromtimestamp(r[6] / 1000, tz=timezone.utc).isoformat(),
        }
        for r in res.result_rows
    ]


@router.get("/{device_id}/status-history", response_model=list[StatusChangeEvent])
async def get_status_history(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_status_history(device_id, from_time, to_time, limit)


@router.post("/{device_id}/ping-test")
async def test_device_ping(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Run an on-demand ICMP probe (3 packets) and return whether the device
    replied, along with an approximate round-trip time."""
    import subprocess

    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    ip = str(device.ip_address)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "3", "-W", "1", "-n", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await proc.communicate()
        raw = out_b.decode("utf-8", errors="replace")
        duration_ms = int((time.monotonic() - started) * 1000)

        # Parse "3 packets transmitted, 3 received" and "rtt min/avg/max/mdev = 0.123/0.456/0.789/…"
        import re as _re
        received = 0
        transmitted = 0
        rtt_avg_ms = None
        m = _re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received", raw)
        if m:
            transmitted = int(m.group(1))
            received = int(m.group(2))
        m = _re.search(r"rtt min/avg/max/[^\s=]*\s*=\s*[\d.]+/([\d.]+)/[\d.]+/", raw)
        if m:
            rtt_avg_ms = float(m.group(1))

        ok = received > 0
        loss_pct = ((transmitted - received) / transmitted * 100) if transmitted else 100
        return {
            "ok": ok,
            "reachable": ok,
            "transmitted": transmitted,
            "received": received,
            "loss_pct": round(loss_pct, 1),
            "rtt_avg_ms": rtt_avg_ms,
            "duration_ms": duration_ms,
            "reason": None if ok else "Host did not reply to ICMP echo",
        }
    except Exception as e:
        return {
            "ok": False,
            "reachable": False,
            "transmitted": 0,
            "received": 0,
            "loss_pct": 100,
            "rtt_avg_ms": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "reason": f"ping failed: {e}",
        }


@router.post("/{device_id}/snmp-test")
async def test_device_snmp(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Run an on-demand SNMP probe against the device and return whether it
    responded, plus the sysDescr/sysName/sysObjectID/sysUpTime values we got."""
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device.snmp_enabled:
        return {
            "ok": False,
            "reason": "SNMP monitoring is disabled for this device",
            "snmp_responded": False,
            "reachable": None,
            "duration_ms": 0,
        }

    cfg = await _device_snmp_settings(db, device)

    ip = str(device.ip_address)
    reachable = await _ping_once(ip, timeout_s=2.0)

    # Give the manual probe more headroom than the configured timeout so we
    # don't falsely report "timed out" on slow devices. Minimum 5s.
    probe_timeout_ms = max(5000, cfg["timeout_ms"] * 3)

    started = time.monotonic()
    oids = [SYS_DESCR_OID, SYS_OBJECT_OID, SYS_NAME_OID, SYS_UPTIME_OID]
    result, snmp_err = await _snmpget_detail(
        ip=ip,
        community=cfg["community"] or "public",
        version=cfg["version"],
        port=cfg["port"],
        timeout_ms=probe_timeout_ms,
        oids=oids,
        v3_username=cfg["v3_username"],
        v3_security_level=cfg["v3_security_level"],
        v3_auth_protocol=cfg["v3_auth_protocol"],
        v3_auth_passphrase=cfg["v3_auth_passphrase"],
        v3_priv_protocol=cfg["v3_priv_protocol"],
        v3_priv_passphrase=cfg["v3_priv_passphrase"],
        v3_context=cfg["v3_context"],
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    if result is None:
        # Map net-snmp's stderr to a more actionable reason where we can.
        err_lc = (snmp_err or "").lower()
        if not reachable:
            reason = "Host is unreachable (ping failed)"
        elif "timeout" in err_lc and "no response" in err_lc:
            reason = "SNMP timed out — packets reach the host but no reply (ACL, wrong community/v3 user, or SNMP service disabled)"
        elif "authentication failure" in err_lc or "usm" in err_lc:
            reason = "SNMPv3 authentication failed — wrong username/auth/priv protocol or passphrase"
        elif "unknown" in err_lc and "protocol" in err_lc:
            reason = f"net-snmp rejected the protocol — {snmp_err}"
        elif "snmpget binary not installed" in err_lc:
            reason = "snmpget is missing in the API server image — rebuild with net-snmp installed"
        elif snmp_err:
            reason = snmp_err
        else:
            reason = "SNMP probe failed for an unknown reason"
        return {
            "ok": False,
            "snmp_responded": False,
            "reachable": reachable,
            "reason": reason,
            "snmp_error": snmp_err,
            "duration_ms": duration_ms,
            "config": {
                "version": cfg["version"],
                "port": cfg["port"],
                "timeout_ms": cfg["timeout_ms"],
            },
        }

    # Parse uptime (timeticks: "(12345) 0:02:03.45")
    uptime_raw = result.get(SYS_UPTIME_OID, "")
    uptime_seconds = None
    try:
        if uptime_raw:
            # Some net-snmp versions return bare timeticks like "12345"
            digits = "".join(ch for ch in uptime_raw if ch.isdigit())
            if digits:
                uptime_seconds = int(digits) / 100
    except Exception:
        pass

    return {
        "ok": True,
        "snmp_responded": True,
        "reachable": reachable,
        "duration_ms": duration_ms,
        "sys_descr": result.get(SYS_DESCR_OID),
        "sys_object_id": result.get(SYS_OBJECT_OID),
        "sys_name": result.get(SYS_NAME_OID),
        "sys_uptime_seconds": uptime_seconds,
        "sys_uptime_raw": uptime_raw,
        "config": {
            "version": cfg["version"],
            "port": cfg["port"],
            "timeout_ms": cfg["timeout_ms"],
        },
    }


def _device_to_response(device) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        hostname=device.hostname,
        ip_address=str(device.ip_address),
        device_type=device.device_type,
        location=device.location,
        group_id=device.group_id,
        group_name=device.group.name if device.group else None,
        tags=device.tags or [],
        ping_enabled=device.ping_enabled,
        ping_interval=device.ping_interval,
        status=device.status,
        last_seen=device.last_seen,
        last_rtt_ms=device.last_rtt_ms,
        description=device.description,
        created_at=device.created_at,
        updated_at=device.updated_at,
        # SNMP — passphrases never exposed; presence signalled by *_configured flags.
        snmp_enabled=bool(device.snmp_enabled),
        snmp_version=device.snmp_version,
        snmp_port=device.snmp_port,
        snmp_community=device.snmp_community,
        snmp_v3_username=device.snmp_v3_username,
        snmp_v3_context=device.snmp_v3_context,
        snmp_auth_protocol=device.snmp_auth_protocol,
        snmp_priv_protocol=device.snmp_priv_protocol,
        snmp_timeout_ms=device.snmp_timeout_ms,
        snmp_retries=device.snmp_retries,
        snmp_max_repetitions=device.snmp_max_repetitions,
        snmp_poll_interval=device.snmp_poll_interval,
        sys_object_id=device.sys_object_id,
        vendor=device.vendor,
        model=device.model,
        os_version=device.os_version,
        profile_id=device.profile_id,
        snmp_credential_id=device.snmp_credential_id,
        snmp_auth_configured=device.snmp_auth_passphrase is not None,
        snmp_priv_configured=device.snmp_priv_passphrase is not None,
    )
