"""Global SNMP trap / events browser.

Surfaces the traps ingested by the poller's UDP/162 listener (stored in the
ClickHouse ``zenplus.snmp_traps`` table) across all devices. Per-device traps
live at ``/devices/{id}/traps``; this router is the network-wide view.
"""
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, get_clickhouse_client
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/traps", tags=["Traps"])

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@router.get("")
async def list_traps(
    hours: int = Query(default=24, ge=1, le=720),
    severity: Optional[str] = Query(default=None, pattern="^(info|warning|critical)$"),
    search: Optional[str] = None,
    device_id: Optional[UUID] = None,
    limit: int = Query(default=200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recent SNMP traps across all devices (network-wide events feed)."""
    client = get_clickhouse_client()

    where = ["timestamp >= now() - INTERVAL %(hours)s HOUR"]
    params: dict = {"hours": hours, "limit": limit}
    if severity:
        where.append("severity = %(severity)s")
        params["severity"] = severity
    if device_id:
        where.append("device_id = %(device_id)s")
        params["device_id"] = str(device_id)
    if search:
        where.append(
            "positionCaseInsensitive("
            "concat(trap_oid, ' ', trap_name, ' ', message, ' ', toString(source_ip)),"
            " %(q)s) > 0"
        )
        params["q"] = search

    sql = f"""
        SELECT toString(device_id), toString(source_ip), trap_oid, trap_name,
               severity, message, bindings, toUnixTimestamp64Milli(timestamp)
        FROM zenplus.snmp_traps
        WHERE {' AND '.join(where)}
        ORDER BY timestamp DESC
        LIMIT %(limit)s
    """
    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    rows = res.result_rows

    # Resolve device hostnames for rows that matched a known device.
    dev_ids = {r[0] for r in rows if r[0] and r[0] != _ZERO_UUID}
    host_map: dict[str, str] = {}
    if dev_ids:
        dres = await db.execute(
            text("SELECT id::text AS id, hostname FROM devices WHERE id::text = ANY(:ids)"),
            {"ids": list(dev_ids)},
        )
        host_map = {row.id: row.hostname for row in dres.fetchall()}

    out = []
    for r in rows:
        did = r[0] if r[0] and r[0] != _ZERO_UUID else None
        out.append({
            "device_id": did,
            "device_hostname": host_map.get(did) if did else None,
            "source_ip": r[1],
            "trap_oid": r[2],
            "trap_name": r[3],
            "severity": r[4],
            "message": r[5],
            "bindings": r[6],
            "timestamp": datetime.fromtimestamp(r[7] / 1000, tz=timezone.utc).isoformat(),
        })
    return {"data": out, "count": len(out)}


@router.get("/stats")
async def trap_stats(
    hours: int = Query(default=24, ge=1, le=720),
    user: User = Depends(get_current_user),
):
    """Severity counts for the header cards."""
    client = get_clickhouse_client()
    try:
        res = client.query(
            """
            SELECT severity, count()
            FROM zenplus.snmp_traps
            WHERE timestamp >= now() - INTERVAL %(hours)s HOUR
            GROUP BY severity
            """,
            parameters={"hours": hours},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    counts = {"info": 0, "warning": 0, "critical": 0}
    total = 0
    for sev, c in res.result_rows:
        if sev in counts:
            counts[sev] = int(c)
        total += int(c)
    return {"total": total, **counts}
