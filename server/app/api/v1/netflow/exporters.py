"""NetFlow exporter -> device mapping (list, set and clear the manual
exporter IP to monitored device assignment)."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.device import Device
from app.models.user import User
from pydantic import BaseModel, Field

from .common import (
    _exporter_device_overrides,
    _query,
    _resolve_window,
)

router = APIRouter()


class ExporterDeviceIn(BaseModel):
    device_id: str
    note: str | None = Field(default=None, max_length=2000)


@router.get("/exporter-devices")
async def list_exporter_devices(
    hours: int = Query(default=168, ge=1, le=720),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Exporters seen in the window, each with how its interface names resolve.

    `source` is 'ip-match' when the exporter IP is a device's management IP,
    'mapped' when an operator linked it explicitly, and null when neither —
    the case where the UI can only show raw ifIndex numbers.
    """
    start, end = _resolve_window(hours, None, None)
    rows = (await _query(
        """
        SELECT exporter_ip, count() AS flows, max(timestamp) AS last_seen
        FROM zenplus.flow_records
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        GROUP BY exporter_ip ORDER BY flows DESC LIMIT 200
        """,
        {"start": start, "end": end},
    )).result_rows
    exporter_ips = [str(r[0]) for r in rows]
    if not exporter_ips:
        return []

    devices_q = await db.execute(
        select(Device.id, Device.ip_address, Device.hostname).where(
            cast(Device.ip_address, String).in_([f"{ip}/32" for ip in exporter_ips] + exporter_ips)
        )
    )
    by_ip = {str(r.ip_address).split("/")[0]: (r.id, r.hostname) for r in devices_q.all()}
    overrides = await _exporter_device_overrides(db, exporter_ips)

    out = []
    for r in rows:
        ip = str(r[0])
        dev = by_ip.get(ip)
        source = "ip-match" if dev else ("mapped" if ip in overrides else None)
        if not dev:
            dev = overrides.get(ip)
        out.append({
            "exporter_ip": ip,
            "flows": int(r[1] or 0),
            "last_seen": r[2],
            "device_id": str(dev[0]) if dev else None,
            "device_hostname": dev[1] if dev else None,
            "source": source,
        })
    return out


@router.put("/exporter-devices/{exporter_ip}")
async def set_exporter_device(
    exporter_ip: str,
    body: ExporterDeviceIn,
    user: User = Depends(require_operator_user),
    db: AsyncSession = Depends(get_db),
):
    """Link a NetFlow exporter IP to the device that owns it."""
    try:
        ipaddress.ip_address(exporter_ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="exporter_ip must be a valid IP address")

    dev = (await db.execute(
        select(Device.id, Device.hostname).where(Device.id == body.device_id)
    )).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")

    await db.execute(
        text("""
            INSERT INTO netflow_exporter_devices (exporter_ip, device_id, note, created_by)
            VALUES (CAST(:ip AS inet), :did, :note, :uid)
            ON CONFLICT (exporter_ip) DO UPDATE
              SET device_id = EXCLUDED.device_id,
                  note = EXCLUDED.note,
                  updated_at = NOW()
        """),
        {"ip": exporter_ip, "did": str(body.device_id), "note": body.note, "uid": str(user.id)},
    )
    from app.services.audit_service import write_audit_log
    await write_audit_log(
        db, actor=user, action="update", resource_type="netflow_exporter_device",
        resource_id=exporter_ip,
        metadata={"device_id": str(body.device_id), "device_hostname": dev.hostname},
    )
    await db.commit()
    return {"exporter_ip": exporter_ip, "device_id": str(dev.id), "device_hostname": dev.hostname}


@router.delete("/exporter-devices/{exporter_ip}", status_code=204)
async def delete_exporter_device(
    exporter_ip: str,
    user: User = Depends(require_operator_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        text("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = :ip"),
        {"ip": exporter_ip},
    )
    from app.services.audit_service import write_audit_log
    await write_audit_log(
        db, actor=user, action="delete", resource_type="netflow_exporter_device",
        resource_id=exporter_ip,
    )
    await db.commit()
