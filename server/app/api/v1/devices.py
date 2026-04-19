from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.device import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceSummary, DeviceGroupResponse,
    BulkImportRequest, BulkImportResult,
)
from app.schemas.metric import MetricResponse, StatusChangeEvent
from app.services import device_service, metric_service

router = APIRouter(prefix="/devices", tags=["Devices"])


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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
):
    device = await device_service.create_device(db, data, user.id)
    return _device_to_response(device)


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_devices(
    data: BulkImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
):
    device = await device_service.update_device(db, device_id, data)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_to_response(device)


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
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
    hours: int = Query(default=24, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Scalar SNMP metrics (CPU, memory, temperature...) from ClickHouse.

    Returns a dict keyed by metric_key with an array of {ts, value} points.
    Uses 5m rollups for windows > 6h, raw otherwise.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    table = "snmp_metrics" if hours <= 6 else "snmp_metrics_5m"
    val_col = "value" if hours <= 6 else "avg_value"

    try:
        res = client.query(
            f"""
            SELECT metric_key,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   {val_col} AS val,
                   any(unit) AS unit
            FROM zenplus.{table}
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY metric_key, timestamp, val
            ORDER BY metric_key, timestamp
            """,
            parameters={"id": str(device_id), "hours": hours},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

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
    """Per-interface bps/errors time series from ClickHouse."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    table = "snmp_if_metrics" if hours <= 6 else "snmp_if_metrics_5m"
    in_col = "in_bps" if hours <= 6 else "avg_in_bps"
    out_col = "out_bps" if hours <= 6 else "avg_out_bps"

    where = "device_id = %(id)s AND timestamp >= now() - INTERVAL %(hours)s HOUR"
    params: dict = {"id": str(device_id), "hours": hours}
    if if_index is not None:
        where += " AND if_index = %(if)s"
        params["if"] = if_index

    try:
        res = client.query(
            f"""
            SELECT if_index,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   {in_col} AS in_bps,
                   {out_col} AS out_bps
            FROM zenplus.{table}
            WHERE {where}
            ORDER BY if_index, timestamp
            """,
            parameters=params,
        )
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

    if hours <= 6:
        table = "snmp_if_metrics"
        sql = f"""
            SELECT toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   in_bps, out_bps,
                   in_errors, out_errors,
                   in_discards, out_discards,
                   in_ucast_pkts, out_ucast_pkts,
                   in_octets, out_octets
            FROM zenplus.{table}
            WHERE device_id = %(id)s AND if_index = %(if)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            ORDER BY timestamp
        """
    else:
        table = "snmp_if_metrics_5m"
        sql = f"""
            SELECT toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   avg_in_bps AS in_bps, avg_out_bps AS out_bps,
                   sum_in_errors AS in_errors, sum_out_errors AS out_errors,
                   sum_in_discards AS in_discards, sum_out_discards AS out_discards,
                   0 AS in_ucast_pkts, 0 AS out_ucast_pkts,
                   0 AS in_octets, 0 AS out_octets
            FROM zenplus.{table}
            WHERE device_id = %(id)s AND if_index = %(if)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            ORDER BY timestamp
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
