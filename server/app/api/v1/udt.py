"""User Device Tracker (UDT) API.

Endpoint discovery, switch-port mapping, watch/allow/ignore lists,
user-login correlation, capacity trends, port actions and the UDT
activity feed.

Read endpoints require an authenticated user; mutations require
operator; remote port control and DC credential ops require operator
and are audit-logged.

Routes (prefix /api/v1/udt):
    GET    /summary                         dashboard KPIs
    GET    /endpoints                        search/list endpoints
    GET    /endpoints/{id}                   endpoint detail + history
    PATCH  /endpoints/{id}                   edit (type/notes/watch/authorized)
    GET    /devices/{device_id}/ports        switch-port map for a device
    GET    /ports                            global port list (capacity)
    POST   /ports/{device_id}/{if_index}/action   shutdown/enable/monitor
    PATCH  /ports/{device_id}/{if_index}     set uplink override / monitored
    GET    /rules                            list watch/allow/ignore rules
    POST   /rules                            create a rule
    PATCH  /rules/{id}                       update a rule
    DELETE /rules/{id}                       delete a rule
    GET    /rogues                           current rogue endpoints
    GET    /users                            user-login rollup
    GET    /users/{user}                     one user's endpoints/logins
    GET    /events                           activity feed
    GET    /capacity                         per-device capacity + trend
    GET    /vendors                          OUI vendor rollup
    GET    /settings                         global UDT settings (poll interval)
    PUT    /settings                         update global UDT settings
    GET    /settings/devices                 per-device UDT settings + credentials
    PUT    /settings/devices/{device_id}     set one device's UDT settings
    POST   /settings/devices/bulk            bulk enable/disable/credential/interval
    POST   /devices/{device_id}/ports/bulk-monitor   bulk port monitor toggle
    GET    /domain-controllers               list DCs
    POST   /domain-controllers               add a DC
    PATCH  /domain-controllers/{id}          update a DC
    DELETE /domain-controllers/{id}          delete a DC
    POST   /domain-controllers/{id}/poll     poll a DC now
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.udt_service import normalize_mac, ENDPOINT_TYPES

router = APIRouter(prefix="/udt", tags=["User Device Tracker"])

_BRIDGE_IFADMIN_OID = "1.3.6.1.2.1.2.2.1.7"  # ifAdminStatus.<ifIndex>

# Must match the poller's udtIntervalFromEnv default (engine.go).
_UDT_DEFAULT_INTERVAL_S = 300


# ── Schemas ──────────────────────────────────────────────────────────

class EndpointUpdate(BaseModel):
    endpoint_type: Optional[str] = None
    notes: Optional[str] = None
    is_watched: Optional[bool] = None
    authorized: Optional[bool] = None
    ignored: Optional[bool] = None


class RuleCreate(BaseModel):
    list_type: str = Field(..., pattern="^(watch|allow|ignore)$")
    match_type: str = Field(..., pattern="^(mac|mac_prefix|ip|ip_range|subnet|hostname|vendor|user)$")
    pattern: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True


class RuleUpdate(BaseModel):
    pattern: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None


class PortAction(BaseModel):
    action: str = Field(..., pattern="^(shutdown|enable|monitor|unmonitor)$")


class PortUpdate(BaseModel):
    uplink_override: Optional[str] = Field(default=None, pattern="^(uplink|access|auto)$")
    monitored: Optional[bool] = None


class DCCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    host: str = Field(..., min_length=1, max_length=255)
    windows_credential_id: str
    poll_interval_s: int = Field(default=300, ge=60, le=86400)
    enabled: bool = True


class DCUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    windows_credential_id: Optional[str] = None
    poll_interval_s: Optional[int] = Field(default=None, ge=60, le=86400)
    enabled: Optional[bool] = None


class UdtGlobalSettingsUpdate(BaseModel):
    poll_interval_s: int = Field(..., ge=30, le=86400)


class UdtDeviceSettingsUpdate(BaseModel):
    """Full-state upsert for one device (PUT semantics)."""
    enabled: bool = True
    snmp_credential_id: Optional[str] = None
    poll_interval_s: Optional[int] = Field(default=None, ge=60, le=86400)


class UdtBulkDeviceSettings(BaseModel):
    """Partial bulk update: each set_* flag gates its field so callers
    can e.g. flip enabled without clobbering credential choices."""
    device_ids: list[str] = Field(..., min_length=1, max_length=1000)
    set_enabled: Optional[bool] = None
    set_credential: bool = False
    snmp_credential_id: Optional[str] = None
    set_interval: bool = False
    poll_interval_s: Optional[int] = Field(default=None, ge=60, le=86400)


class PortBulkMonitor(BaseModel):
    if_indexes: list[int] = Field(..., min_length=1, max_length=1000)
    monitored: bool


# ── Summary ──────────────────────────────────────────────────────────

@router.get("/summary")
async def udt_summary(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    row = (await db.execute(text(
        """SELECT
             (SELECT COUNT(*) FROM udt_endpoints WHERE NOT ignored) AS total_endpoints,
             (SELECT COUNT(*) FROM udt_endpoints WHERE NOT ignored AND last_seen > NOW() - INTERVAL '15 minutes') AS active_endpoints,
             (SELECT COUNT(*) FROM udt_endpoints WHERE NOT ignored AND first_seen > NOW() - INTERVAL '24 hours') AS new_24h,
             (SELECT COUNT(*) FROM udt_endpoints WHERE authorized = FALSE AND NOT ignored) AS rogue,
             (SELECT COUNT(*) FROM udt_endpoints WHERE is_watched AND NOT ignored) AS watched,
             (SELECT COUNT(*) FROM udt_endpoints WHERE is_randomized AND NOT ignored) AS randomized,
             (SELECT COUNT(DISTINCT device_id) FROM udt_port_state) AS switches,
             (SELECT COUNT(*) FROM udt_user_logins WHERE event_time > NOW() - INTERVAL '24 hours') AS logins_24h"""
    ))).mappings().first()

    cap = (await db.execute(text(
        """SELECT COALESCE(SUM(total),0) AS total_ports, COALESCE(SUM(used),0) AS used_ports
           FROM (
             SELECT di.device_id,
                    COUNT(*) FILTER (WHERE di.if_type IS NULL OR di.if_type IN (6,117)) AS total,
                    COUNT(*) FILTER (WHERE di.oper_status='up' AND (di.if_type IS NULL OR di.if_type IN (6,117))) AS used
             FROM device_interfaces di
             WHERE EXISTS (SELECT 1 FROM udt_port_state p WHERE p.device_id = di.device_id)
             GROUP BY di.device_id
           ) t"""
    ))).mappings().first()

    top = (await db.execute(text(
        """SELECT id, hostname, total, used FROM (
             SELECT d.id, d.hostname,
                    COUNT(*) FILTER (WHERE di.if_type IS NULL OR di.if_type IN (6,117)) AS total,
                    COUNT(*) FILTER (WHERE di.oper_status='up' AND (di.if_type IS NULL OR di.if_type IN (6,117))) AS used
             FROM devices d JOIN device_interfaces di ON di.device_id = d.id
             WHERE EXISTS (SELECT 1 FROM udt_port_state p WHERE p.device_id = d.id)
             GROUP BY d.id, d.hostname
           ) t ORDER BY used::float / NULLIF(total,0) DESC NULLS LAST LIMIT 5"""
    ))).mappings().all()

    total_ports = cap["total_ports"] or 0
    return {
        **{k: row[k] for k in row.keys()},
        "total_ports": total_ports,
        "used_ports": cap["used_ports"] or 0,
        "port_utilization_pct": round((cap["used_ports"] or 0) / total_ports * 100, 1) if total_ports else 0.0,
        "top_switches": [
            {"id": str(t["id"]), "hostname": t["hostname"], "total": t["total"], "used": t["used"],
             "pct": round((t["used"] or 0) / t["total"] * 100, 1) if t["total"] else 0.0}
            for t in top
        ],
    }


# ── Endpoint search ──────────────────────────────────────────────────

@router.get("/endpoints")
async def list_endpoints(
    q: Optional[str] = None,
    status: Optional[str] = Query(default=None, pattern="^(active|inactive|all)$"),
    endpoint_type: Optional[str] = None,
    vlan: Optional[int] = None,
    device_id: Optional[str] = None,
    flag: Optional[str] = Query(default=None, pattern="^(rogue|watched|randomized|new)$"),
    include_ignored: bool = False,
    sort: str = Query(default="last_seen"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    skip: int = 0,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    where = []
    params: dict = {}
    if not include_ignored:
        where.append("NOT e.ignored")
    if q:
        term = q.strip()
        mac = normalize_mac(term)
        if mac:
            where.append("e.mac = CAST(:mac AS macaddr)")
            params["mac"] = mac
        else:
            where.append("(e.hostname ILIKE :q OR e.vendor ILIKE :q OR e.user_name ILIKE :q "
                         "OR host(e.ip_address) ILIKE :q OR e.mac::text ILIKE :q "
                         "OR EXISTS (SELECT 1 FROM udt_ip_history h WHERE h.endpoint_id=e.id AND host(h.ip) ILIKE :q))")
            params["q"] = f"%{term}%"
    if status == "active":
        where.append("e.last_seen > NOW() - INTERVAL '15 minutes'")
    elif status == "inactive":
        where.append("e.last_seen <= NOW() - INTERVAL '15 minutes'")
    if endpoint_type:
        where.append("e.endpoint_type = :etype")
        params["etype"] = endpoint_type
    if device_id:
        where.append("EXISTS (SELECT 1 FROM udt_endpoint_locations l WHERE l.endpoint_id=e.id AND l.active AND l.device_id = :dev)")
        params["dev"] = device_id
    if vlan is not None:
        where.append("EXISTS (SELECT 1 FROM udt_endpoint_locations l WHERE l.endpoint_id=e.id AND l.active AND l.vlan_id = :vlan)")
        params["vlan"] = vlan
    if flag == "rogue":
        where.append("e.authorized = FALSE")
    elif flag == "watched":
        where.append("e.is_watched")
    elif flag == "randomized":
        where.append("e.is_randomized")
    elif flag == "new":
        where.append("e.first_seen > NOW() - INTERVAL '24 hours'")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sort_cols = {"last_seen": "e.last_seen", "first_seen": "e.first_seen",
                 "mac": "e.mac", "ip": "e.ip_address", "hostname": "e.hostname",
                 "vendor": "e.vendor", "type": "e.endpoint_type"}
    sort_col = sort_cols.get(sort, "e.last_seen")

    total = (await db.execute(text(f"SELECT COUNT(*) FROM udt_endpoints e{where_sql}"), params)).scalar()
    params.update({"skip": skip, "limit": limit})
    rows = (await db.execute(text(
        f"""SELECT e.id, e.mac::text AS mac, e.vendor, e.hostname, host(e.ip_address) AS ip,
                   e.endpoint_type, e.is_randomized, e.is_watched, e.authorized, e.ignored,
                   e.user_name, e.user_domain, e.device_id,
                   e.first_seen, e.last_seen,
                   loc.device_id AS loc_device_id, dv.hostname AS switch_hostname,
                   loc.if_index, di.if_name, loc.vlan_id, loc.is_direct,
                   (e.last_seen > NOW() - INTERVAL '15 minutes') AS online
            FROM udt_endpoints e
            LEFT JOIN LATERAL (
                SELECT device_id, if_index, vlan_id, is_direct FROM udt_endpoint_locations
                WHERE endpoint_id = e.id AND active ORDER BY is_direct DESC, last_seen DESC LIMIT 1
            ) loc ON TRUE
            LEFT JOIN devices dv ON dv.id = loc.device_id
            LEFT JOIN device_interfaces di ON di.device_id = loc.device_id AND di.if_index = loc.if_index
            {where_sql}
            ORDER BY {sort_col} {order.upper()} NULLS LAST, e.id
            OFFSET :skip LIMIT :limit"""
    ), params)).mappings().all()

    return {
        "data": [dict(r) | {"id": str(r["id"]),
                            "loc_device_id": str(r["loc_device_id"]) if r["loc_device_id"] else None,
                            "device_id": str(r["device_id"]) if r["device_id"] else None}
                 for r in rows],
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


@router.get("/endpoints/{endpoint_id}")
async def endpoint_detail(endpoint_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    e = (await db.execute(text(
        """SELECT e.id, e.mac::text AS mac, e.vendor, e.hostname, host(e.ip_address) AS ip,
                  e.endpoint_type, e.is_randomized, e.is_watched, e.authorized, e.ignored,
                  e.user_name, e.user_domain, e.user_seen_at, e.notes, e.device_id,
                  e.first_seen, e.last_seen,
                  d.hostname AS managed_hostname
           FROM udt_endpoints e LEFT JOIN devices d ON d.id = e.device_id
           WHERE e.id = :id"""
    ), {"id": endpoint_id})).mappings().first()
    if not e:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    locations = (await db.execute(text(
        """SELECT l.id, l.device_id, dv.hostname AS switch, l.if_index, di.if_name, di.if_alias,
                  l.vlan_id, l.is_direct, l.active, l.first_seen, l.last_seen, l.closed_at
           FROM udt_endpoint_locations l
           JOIN devices dv ON dv.id = l.device_id
           LEFT JOIN device_interfaces di ON di.device_id = l.device_id AND di.if_index = l.if_index
           WHERE l.endpoint_id = :id ORDER BY l.active DESC, l.is_direct DESC, l.last_seen DESC LIMIT 200"""
    ), {"id": endpoint_id})).mappings().all()

    ips = (await db.execute(text(
        """SELECT host(ip) AS ip, source, active, first_seen, last_seen
           FROM udt_ip_history WHERE endpoint_id = :id ORDER BY active DESC, last_seen DESC LIMIT 100"""
    ), {"id": endpoint_id})).mappings().all()

    logins = (await db.execute(text(
        """SELECT user_name, user_domain, event_id, logon_type, host(ip) AS ip, hostname, event_time
           FROM udt_user_logins WHERE endpoint_id = :id ORDER BY event_time DESC LIMIT 50"""
    ), {"id": endpoint_id})).mappings().all()

    events = (await db.execute(text(
        """SELECT ev.event_type, ev.device_id, d.hostname AS switch, ev.if_index, ev.details, ev.created_at
           FROM udt_events ev LEFT JOIN devices d ON d.id = ev.device_id
           WHERE ev.endpoint_id = :id ORDER BY ev.created_at DESC LIMIT 50"""
    ), {"id": endpoint_id})).mappings().all()

    def _fix(rows):
        out = []
        for r in rows:
            d = dict(r)
            if d.get("device_id"):
                d["device_id"] = str(d["device_id"])
            out.append(d)
        return out

    return {
        "endpoint": dict(e) | {"id": str(e["id"]), "device_id": str(e["device_id"]) if e["device_id"] else None},
        "locations": _fix(locations),
        "ip_history": [dict(r) for r in ips],
        "logins": [dict(r) for r in logins],
        "events": _fix(events),
    }


@router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(
    endpoint_id: str, payload: EndpointUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    sets, params = [], {"id": endpoint_id}
    if payload.endpoint_type is not None:
        if payload.endpoint_type not in ENDPOINT_TYPES:
            raise HTTPException(status_code=400, detail=f"invalid endpoint_type; one of {ENDPOINT_TYPES}")
        sets.append("endpoint_type = :etype"); params["etype"] = payload.endpoint_type
    if payload.notes is not None:
        sets.append("notes = :notes"); params["notes"] = payload.notes
    if payload.is_watched is not None:
        sets.append("is_watched = :w"); params["w"] = payload.is_watched
    if payload.authorized is not None:
        # Pin the operator's decision so the rule-driven sweeper won't undo it.
        sets.append("authorized = :a"); sets.append("authorized_override = :a")
        params["a"] = payload.authorized
    if payload.ignored is not None:
        sets.append("ignored = :ig"); sets.append("ignored_manual = :ig")
        params["ig"] = payload.ignored
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at = NOW()")
    res = await db.execute(text(f"UPDATE udt_endpoints SET {', '.join(sets)} WHERE id = :id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await write_audit_log(db, actor=user, action="udt.endpoint.update",
                          resource_type="udt_endpoint", resource_id=endpoint_id,
                          metadata=payload.model_dump(exclude_none=True))
    await db.commit()
    return {"status": "ok"}


# ── Switch-port map ──────────────────────────────────────────────────

@router.get("/devices/{device_id}/ports")
async def device_ports(
    device_id: str, include_empty: bool = True,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    dev = (await db.execute(text("SELECT id, hostname FROM devices WHERE id = :id"), {"id": device_id})).mappings().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    rows = (await db.execute(text(
        """SELECT di.if_index, di.if_name, di.if_descr, di.if_alias, di.if_type,
                  di.admin_status, di.oper_status, di.if_speed,
                  ps.is_uplink, ps.uplink_reason, ps.uplink_override, ps.monitored,
                  ps.mac_count, ps.vlan_ids, ps.pvid, ps.last_endpoint_seen,
                  (SELECT COUNT(*) FROM udt_endpoint_locations l
                   WHERE l.device_id = di.device_id AND l.if_index = di.if_index AND l.active) AS active_endpoints
           FROM device_interfaces di
           LEFT JOIN udt_port_state ps ON ps.device_id = di.device_id AND ps.if_index = di.if_index
           WHERE di.device_id = :id
             AND (di.if_type IS NULL OR di.if_type IN (6,117,161))
           ORDER BY di.if_index"""
    ), {"id": device_id})).mappings().all()
    ports = []
    for r in rows:
        if not include_empty and (r["active_endpoints"] or 0) == 0 and not r["is_uplink"]:
            continue
        d = dict(r)
        d["vlan_ids"] = r["vlan_ids"] or []
        ports.append(d)
    return {"device": {"id": str(dev["id"]), "hostname": dev["hostname"]}, "ports": ports}


@router.get("/devices/{device_id}/ports/{if_index}/endpoints")
async def port_endpoints(
    device_id: str, if_index: int, active_only: bool = True,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    cond = "AND l.active" if active_only else ""
    rows = (await db.execute(text(
        f"""SELECT e.id, e.mac::text AS mac, e.vendor, e.hostname, host(e.ip_address) AS ip,
                   e.endpoint_type, e.is_watched, e.authorized, l.vlan_id, l.is_direct,
                   l.active, l.first_seen, l.last_seen
            FROM udt_endpoint_locations l JOIN udt_endpoints e ON e.id = l.endpoint_id
            WHERE l.device_id = :dev AND l.if_index = :ifx {cond}
            ORDER BY l.last_seen DESC LIMIT 500"""
    ), {"dev": device_id, "ifx": if_index})).mappings().all()
    return {"data": [dict(r) | {"id": str(r["id"])} for r in rows]}


@router.get("/ports")
async def list_ports(
    q: Optional[str] = None, only_uplinks: bool = False, only_used: bool = False,
    skip: int = 0, limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    where = ["EXISTS (SELECT 1 FROM udt_port_state p WHERE p.device_id = di.device_id)"]
    params: dict = {"skip": skip, "limit": limit}
    if q:
        where.append("(d.hostname ILIKE :q OR di.if_name ILIKE :q OR di.if_alias ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if only_uplinks:
        where.append("ps.is_uplink")
    if only_used:
        where.append("di.oper_status = 'up'")
    where_sql = " AND ".join(where)
    total = (await db.execute(text(
        f"SELECT COUNT(*) FROM device_interfaces di JOIN devices d ON d.id=di.device_id "
        f"LEFT JOIN udt_port_state ps ON ps.device_id=di.device_id AND ps.if_index=di.if_index WHERE {where_sql}"
    ), params)).scalar()
    rows = (await db.execute(text(
        f"""SELECT d.id AS device_id, d.hostname, di.if_index, di.if_name, di.if_alias,
                   di.oper_status, di.admin_status, di.if_speed,
                   ps.is_uplink, ps.uplink_reason, ps.mac_count, ps.last_endpoint_seen
            FROM device_interfaces di JOIN devices d ON d.id = di.device_id
            LEFT JOIN udt_port_state ps ON ps.device_id=di.device_id AND ps.if_index=di.if_index
            WHERE {where_sql}
            ORDER BY d.hostname, di.if_index OFFSET :skip LIMIT :limit"""
    ), params)).mappings().all()
    return {"data": [dict(r) | {"device_id": str(r["device_id"])} for r in rows],
            "meta": {"total": total, "skip": skip, "limit": limit}}


@router.patch("/ports/{device_id}/{if_index}")
async def update_port(
    device_id: str, if_index: int, payload: PortUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    # Ensure the port-state row exists.
    await db.execute(text(
        "INSERT INTO udt_port_state (device_id, if_index) VALUES (:d, :i) ON CONFLICT DO NOTHING"
    ), {"d": device_id, "i": if_index})
    sets, params = [], {"d": device_id, "i": if_index}
    if payload.uplink_override is not None:
        if payload.uplink_override == "auto":
            sets.append("uplink_override = NULL")
        else:
            sets.append("uplink_override = :ov")
            sets.append("is_uplink = :isup")
            params["ov"] = payload.uplink_override
            params["isup"] = payload.uplink_override == "uplink"
    if payload.monitored is not None:
        sets.append("monitored = :m"); params["m"] = payload.monitored
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at = NOW()")
    await db.execute(text(f"UPDATE udt_port_state SET {', '.join(sets)} WHERE device_id=:d AND if_index=:i"), params)
    if payload.monitored is False:
        await _close_port_sessions(db, device_id, [if_index])
    await write_audit_log(db, actor=user, action="udt.port.update",
                          resource_type="udt_port", resource_id=f"{device_id}/{if_index}",
                          metadata=payload.model_dump(exclude_none=True))
    await db.commit()
    return {"status": "ok"}


@router.post("/ports/{device_id}/{if_index}/action")
async def port_action(
    device_id: str, if_index: int, payload: PortAction,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    if payload.action in ("monitor", "unmonitor"):
        await db.execute(text(
            "INSERT INTO udt_port_state (device_id, if_index, monitored) VALUES (:d,:i,:m) "
            "ON CONFLICT (device_id, if_index) DO UPDATE SET monitored = :m, updated_at = NOW()"
        ), {"d": device_id, "i": if_index, "m": payload.action == "monitor"})
        if payload.action == "unmonitor":
            await _close_port_sessions(db, device_id, [if_index])
        await write_audit_log(db, actor=user, action=f"udt.port.{payload.action}",
                              resource_type="udt_port", resource_id=f"{device_id}/{if_index}", metadata={})
        await db.commit()
        return {"status": "ok", "action": payload.action}

    # shutdown / enable — SNMP SET ifAdminStatus (needs write community/v3).
    dev = (await db.execute(text("SELECT * FROM devices WHERE id = :id"), {"id": device_id})).mappings().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    from app.api.v1.devices import _device_snmp_settings

    class _D:  # adapter so _device_snmp_settings can read attributes
        pass
    d = _D()
    for k, v in dev.items():
        setattr(d, k, v)
    s = await _device_snmp_settings(db, d)
    admin_value = "2" if payload.action == "shutdown" else "1"
    ok, err = await _snmpset_int(
        ip=str(dev["ip_address"]), settings=s,
        oid=f"{_BRIDGE_IFADMIN_OID}.{if_index}", value=admin_value,
    )
    await write_audit_log(db, actor=user, action=f"udt.port.{payload.action}",
                          resource_type="udt_port", resource_id=f"{device_id}/{if_index}",
                          metadata={"ok": ok, "error": err})
    if not ok:
        raise HTTPException(status_code=502, detail=f"SNMP set failed: {err}")
    await db.execute(text(
        "INSERT INTO udt_events (event_type, device_id, if_index, details) "
        "VALUES ('port_admin', :d, :i, CAST(:dj AS jsonb))"
    ), {"d": device_id, "i": if_index, "dj": json.dumps({"action": payload.action, "by": user.username})})
    await db.commit()
    return {"status": "ok", "action": payload.action}


async def _snmpset_int(ip: str, settings: dict, oid: str, value: str) -> tuple[bool, Optional[str]]:
    """Shell out to snmpset for an INTEGER value. Requires a write
    community (v1/v2c) or a v3 user with write access."""
    if shutil.which("snmpset") is None:
        return False, "snmpset binary not installed in the API runtime"
    version = settings.get("version", "2c")
    port = settings.get("port", 161)
    timeout_s = max(1, (settings.get("timeout_ms") or 2000) // 1000)
    args = ["snmpset", "-v", version, "-r", "1", "-t", str(timeout_s)]
    if version == "3":
        sec = settings.get("v3_security_level") or "authPriv"
        args += ["-l", sec]
        if settings.get("v3_username"):
            args += ["-u", settings["v3_username"]]
        if settings.get("v3_context"):
            args += ["-n", settings["v3_context"]]
        if sec in ("authNoPriv", "authPriv") and settings.get("v3_auth_protocol"):
            amap = {"SHA224": "SHA-224", "SHA256": "SHA-256", "SHA384": "SHA-384", "SHA512": "SHA-512"}
            args += ["-a", amap.get(settings["v3_auth_protocol"], settings["v3_auth_protocol"])]
            if settings.get("v3_auth_passphrase"):
                args += ["-A", settings["v3_auth_passphrase"]]
        if sec == "authPriv" and settings.get("v3_priv_protocol"):
            pmap = {"AES128": "AES", "AES192": "AES-192", "AES256": "AES-256", "AES": "AES"}
            args += ["-x", pmap.get(settings["v3_priv_protocol"], settings["v3_priv_protocol"])]
            if settings.get("v3_priv_passphrase"):
                args += ["-X", settings["v3_priv_passphrase"]]
    else:
        args += ["-c", settings.get("community") or "private"]
    args += [f"{ip}:{port}", oid, "i", value]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_b, err_b = await proc.communicate()
        if proc.returncode != 0:
            return False, (err_b.decode("utf-8", "replace").strip()
                           or out_b.decode("utf-8", "replace").strip()
                           or f"snmpset exited {proc.returncode}")
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"snmpset invocation failed: {exc}"


# ── Settings ─────────────────────────────────────────────────────────

@router.get("/settings")
async def udt_settings(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    val = (await db.execute(text(
        "SELECT value FROM system_settings WHERE key = 'udt'"
    ))).scalar()
    interval = _UDT_DEFAULT_INTERVAL_S
    if isinstance(val, dict):
        try:
            interval = int(val.get("poll_interval_s") or _UDT_DEFAULT_INTERVAL_S)
        except (TypeError, ValueError):
            pass
    counts = (await db.execute(text(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE COALESCE(us.enabled, TRUE)) AS enabled
           FROM devices d
           LEFT JOIN udt_device_settings us ON us.device_id = d.id
           WHERE d.snmp_enabled = TRUE"""
    ))).mappings().first()
    return {"poll_interval_s": interval,
            "devices_total": counts["total"], "devices_enabled": counts["enabled"]}


@router.put("/settings")
async def update_udt_settings(
    payload: UdtGlobalSettingsUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    # Merge so future keys under 'udt' survive an interval change. The
    # poller re-reads this on its device-sync cadence (~1 min).
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES ('udt', CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET "
        "value = system_settings.value || EXCLUDED.value, updated_at = NOW()"
    ), {"v": json.dumps({"poll_interval_s": payload.poll_interval_s})})
    await write_audit_log(db, actor=user, action="udt.settings.update",
                          resource_type="udt_settings", resource_id="global",
                          metadata={"poll_interval_s": payload.poll_interval_s})
    await db.commit()
    return {"status": "ok", "poll_interval_s": payload.poll_interval_s}


@router.get("/settings/devices")
async def udt_device_settings(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text(
        """SELECT d.id AS device_id, d.hostname, host(d.ip_address) AS ip,
                  d.vendor, d.model, d.device_type,
                  (d.device_type = 'switch'
                   OR EXISTS (SELECT 1 FROM udt_port_state ps
                              WHERE ps.device_id = d.id AND ps.mac_count > 0)) AS is_l2,
                  COALESCE(us.enabled, TRUE) AS enabled,
                  us.snmp_credential_id, sc.name AS credential_name,
                  us.poll_interval_s,
                  (SELECT COUNT(*) FROM udt_port_state ps WHERE ps.device_id = d.id) AS ports_total,
                  (SELECT COUNT(*) FROM udt_port_state ps WHERE ps.device_id = d.id AND ps.monitored) AS ports_monitored,
                  (SELECT COUNT(*) FROM udt_endpoint_locations l WHERE l.device_id = d.id AND l.active) AS active_endpoints,
                  (SELECT MAX(ps.updated_at) FROM udt_port_state ps WHERE ps.device_id = d.id) AS last_udt_at
           FROM devices d
           LEFT JOIN udt_device_settings us ON us.device_id = d.id
           LEFT JOIN snmp_credentials sc ON sc.id = us.snmp_credential_id
           WHERE d.snmp_enabled = TRUE
           ORDER BY is_l2 DESC, d.hostname"""
    ))).mappings().all()
    # id+name only, so operators get a working picker without the
    # admin-only /snmp-credentials routes (which expose secrets).
    creds = (await db.execute(text(
        "SELECT id, name, snmp_version FROM snmp_credentials ORDER BY name"
    ))).mappings().all()
    return {
        "data": [dict(r) | {"device_id": str(r["device_id"]),
                            "snmp_credential_id": str(r["snmp_credential_id"]) if r["snmp_credential_id"] else None}
                 for r in rows],
        "credentials": [dict(c) | {"id": str(c["id"])} for c in creds],
    }


@router.put("/settings/devices/{device_id}")
async def update_udt_device_settings(
    device_id: str, payload: UdtDeviceSettingsUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    dev = (await db.execute(text(
        "SELECT id FROM devices WHERE id = :id AND snmp_enabled = TRUE"
    ), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="SNMP device not found")
    if payload.snmp_credential_id:
        cred = (await db.execute(text(
            "SELECT id FROM snmp_credentials WHERE id = :id"
        ), {"id": payload.snmp_credential_id})).first()
        if not cred:
            raise HTTPException(status_code=404, detail="SNMP credential not found")
    await db.execute(text(
        """INSERT INTO udt_device_settings (device_id, enabled, snmp_credential_id, poll_interval_s, updated_at)
           VALUES (:d, :e, :c, :p, NOW())
           ON CONFLICT (device_id) DO UPDATE SET
               enabled = :e, snmp_credential_id = :c, poll_interval_s = :p, updated_at = NOW()"""
    ), {"d": device_id, "e": payload.enabled,
        "c": payload.snmp_credential_id, "p": payload.poll_interval_s})
    await write_audit_log(db, actor=user, action="udt.device_settings.update",
                          resource_type="udt_device_settings", resource_id=device_id,
                          metadata=payload.model_dump())
    await db.commit()
    return {"status": "ok"}


@router.post("/settings/devices/bulk")
async def bulk_udt_device_settings(
    payload: UdtBulkDeviceSettings,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    if payload.set_enabled is None and not payload.set_credential and not payload.set_interval:
        raise HTTPException(status_code=400, detail="no fields to update")
    if payload.set_credential and payload.snmp_credential_id:
        cred = (await db.execute(text(
            "SELECT id FROM snmp_credentials WHERE id = :id"
        ), {"id": payload.snmp_credential_id})).first()
        if not cred:
            raise HTTPException(status_code=404, detail="SNMP credential not found")
    try:
        ids = [uuid.UUID(s) for s in payload.device_ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid device id")
    result = await db.execute(text(
        """INSERT INTO udt_device_settings (device_id, enabled, snmp_credential_id, poll_interval_s, updated_at)
           SELECT d.id, COALESCE(CAST(:en AS boolean), TRUE),
                  CASE WHEN :has_cred THEN CAST(:cred AS uuid) ELSE NULL END,
                  CASE WHEN :has_int THEN CAST(:ival AS int) ELSE NULL END,
                  NOW()
           FROM devices d WHERE d.id = ANY(:ids) AND d.snmp_enabled = TRUE
           ON CONFLICT (device_id) DO UPDATE SET
               enabled = CASE WHEN :has_en THEN CAST(:en AS boolean) ELSE udt_device_settings.enabled END,
               snmp_credential_id = CASE WHEN :has_cred THEN CAST(:cred AS uuid)
                                         ELSE udt_device_settings.snmp_credential_id END,
               poll_interval_s = CASE WHEN :has_int THEN CAST(:ival AS int)
                                      ELSE udt_device_settings.poll_interval_s END,
               updated_at = NOW()"""
    ), {"ids": ids,
        "has_en": payload.set_enabled is not None, "en": payload.set_enabled,
        "has_cred": payload.set_credential, "cred": payload.snmp_credential_id,
        "has_int": payload.set_interval, "ival": payload.poll_interval_s})
    await write_audit_log(db, actor=user, action="udt.device_settings.bulk",
                          resource_type="udt_device_settings", resource_id=f"{len(ids)} devices",
                          metadata=payload.model_dump(exclude={"device_ids"}))
    await db.commit()
    return {"status": "ok", "updated": result.rowcount}


@router.post("/devices/{device_id}/ports/bulk-monitor")
async def bulk_port_monitor(
    device_id: str, payload: PortBulkMonitor,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user),
):
    dev = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    await db.execute(text(
        """INSERT INTO udt_port_state (device_id, if_index, monitored)
           SELECT :d, x.if_index, :m FROM unnest(CAST(:idx AS int[])) AS x(if_index)
           ON CONFLICT (device_id, if_index) DO UPDATE SET monitored = :m, updated_at = NOW()"""
    ), {"d": device_id, "idx": payload.if_indexes, "m": payload.monitored})
    if not payload.monitored:
        await _close_port_sessions(db, device_id, payload.if_indexes)
    await write_audit_log(db, actor=user, action="udt.port.bulk-monitor",
                          resource_type="udt_port", resource_id=device_id,
                          metadata={"if_indexes": payload.if_indexes, "monitored": payload.monitored})
    await db.commit()
    return {"status": "ok", "updated": len(payload.if_indexes)}


async def _close_port_sessions(db: AsyncSession, device_id: str, if_indexes: list[int]) -> None:
    """Un-monitoring a port ends its endpoint sessions right away
    instead of waiting for the sweeper's stale-session grace period."""
    await db.execute(text(
        "UPDATE udt_endpoint_locations SET active = FALSE, closed_at = NOW() "
        "WHERE device_id = :d AND if_index = ANY(:idx) AND active"
    ), {"d": device_id, "idx": if_indexes})


# ── Rules ────────────────────────────────────────────────────────────

@router.get("/rules")
async def list_rules(list_type: Optional[str] = None,
                     db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    where = "WHERE list_type = :lt" if list_type else ""
    params = {"lt": list_type} if list_type else {}
    rows = (await db.execute(text(
        f"SELECT id, list_type, match_type, pattern, description, enabled, created_at, updated_at "
        f"FROM udt_rules {where} ORDER BY list_type, created_at"
    ), params)).mappings().all()
    return {"data": [dict(r) | {"id": str(r["id"])} for r in rows]}


@router.post("/rules")
async def create_rule(payload: RuleCreate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_operator_user)):
    from app.services.udt_service import rule_condition
    if rule_condition({"match_type": payload.match_type, "pattern": payload.pattern}, {}, 0) is None:
        raise HTTPException(status_code=400, detail="pattern does not parse for the chosen match_type")
    row = (await db.execute(text(
        """INSERT INTO udt_rules (list_type, match_type, pattern, description, enabled, created_by)
           VALUES (:lt, :mt, :p, :d, :e, :u) RETURNING id"""
    ), {"lt": payload.list_type, "mt": payload.match_type, "p": payload.pattern,
        "d": payload.description, "e": payload.enabled, "u": str(user.id)})).first()
    await write_audit_log(db, actor=user, action="udt.rule.create",
                          resource_type="udt_rule", resource_id=str(row[0]),
                          metadata=payload.model_dump())
    await db.commit()
    return {"status": "ok", "id": str(row[0])}


@router.patch("/rules/{rule_id}")
async def update_rule(rule_id: str, payload: RuleUpdate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_operator_user)):
    sets, params = [], {"id": rule_id}
    if payload.pattern is not None:
        sets.append("pattern = :p"); params["p"] = payload.pattern
    if payload.description is not None:
        sets.append("description = :d"); params["d"] = payload.description
    if payload.enabled is not None:
        sets.append("enabled = :e"); params["e"] = payload.enabled
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at = NOW()")
    res = await db.execute(text(f"UPDATE udt_rules SET {', '.join(sets)} WHERE id = :id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.commit()
    return {"status": "ok"}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_operator_user)):
    res = await db.execute(text("DELETE FROM udt_rules WHERE id = :id RETURNING id"), {"id": rule_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Rule not found")
    await write_audit_log(db, actor=user, action="udt.rule.delete",
                          resource_type="udt_rule", resource_id=rule_id, metadata={})
    await db.commit()
    return {"status": "ok"}


# ── Rogues / users / events / capacity / vendors ─────────────────────

@router.get("/rogues")
async def list_rogues(skip: int = 0, limit: int = Query(default=100, ge=1, le=500),
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    total = (await db.execute(text(
        "SELECT COUNT(*) FROM udt_endpoints WHERE authorized = FALSE AND NOT ignored"))).scalar()
    rows = (await db.execute(text(
        """SELECT e.id, e.mac::text AS mac, e.vendor, e.hostname, host(e.ip_address) AS ip,
                  e.endpoint_type, e.first_seen, e.last_seen,
                  dv.hostname AS switch, loc.if_index, di.if_name
           FROM udt_endpoints e
           LEFT JOIN LATERAL (SELECT device_id, if_index FROM udt_endpoint_locations
                              WHERE endpoint_id=e.id AND active ORDER BY is_direct DESC, last_seen DESC LIMIT 1) loc ON TRUE
           LEFT JOIN devices dv ON dv.id = loc.device_id
           LEFT JOIN device_interfaces di ON di.device_id = loc.device_id AND di.if_index = loc.if_index
           WHERE e.authorized = FALSE AND NOT e.ignored
           ORDER BY e.last_seen DESC OFFSET :skip LIMIT :limit"""
    ), {"skip": skip, "limit": limit})).mappings().all()
    return {"data": [dict(r) | {"id": str(r["id"])} for r in rows], "meta": {"total": total}}


@router.get("/users")
async def list_users(q: Optional[str] = None, skip: int = 0, limit: int = Query(default=50, ge=1, le=200),
                     db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    where = "WHERE user_name ILIKE :q" if q else ""
    params = {"q": f"%{q.strip()}%"} if q else {}
    params.update({"skip": skip, "limit": limit})
    rows = (await db.execute(text(
        f"""SELECT user_name, MAX(user_domain) AS domain, COUNT(*) AS logins,
                   COUNT(DISTINCT endpoint_id) AS endpoints, MAX(event_time) AS last_login
            FROM udt_user_logins {where}
            GROUP BY user_name ORDER BY MAX(event_time) DESC OFFSET :skip LIMIT :limit"""
    ), params)).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.get("/users/{user_name}")
async def user_detail(user_name: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    logins = (await db.execute(text(
        """SELECT l.event_id, l.logon_type, host(l.ip) AS ip, l.hostname, l.event_time,
                  l.endpoint_id, e.mac::text AS mac, e.vendor
           FROM udt_user_logins l LEFT JOIN udt_endpoints e ON e.id = l.endpoint_id
           WHERE LOWER(l.user_name) = LOWER(:u) ORDER BY l.event_time DESC LIMIT 200"""
    ), {"u": user_name})).mappings().all()
    endpoints = (await db.execute(text(
        """SELECT DISTINCT e.id, e.mac::text AS mac, e.hostname, host(e.ip_address) AS ip, e.vendor,
                  MAX(l.event_time) AS last_login
           FROM udt_user_logins l JOIN udt_endpoints e ON e.id = l.endpoint_id
           WHERE LOWER(l.user_name) = LOWER(:u) GROUP BY e.id, e.mac, e.hostname, e.ip_address, e.vendor
           ORDER BY MAX(l.event_time) DESC LIMIT 100"""
    ), {"u": user_name})).mappings().all()
    return {
        "user": user_name,
        "logins": [dict(r) | {"endpoint_id": str(r["endpoint_id"]) if r["endpoint_id"] else None} for r in logins],
        "endpoints": [dict(r) | {"id": str(r["id"])} for r in endpoints],
    }


@router.get("/events")
async def list_events(
    event_type: Optional[str] = None, hours: int = Query(default=24, ge=1, le=720),
    skip: int = 0, limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    where = [f"ev.created_at > NOW() - make_interval(hours => :h)"]
    params: dict = {"h": hours, "skip": skip, "limit": limit}
    if event_type:
        where.append("ev.event_type = :et"); params["et"] = event_type
    where_sql = " AND ".join(where)
    rows = (await db.execute(text(
        f"""SELECT ev.id, ev.event_type, ev.endpoint_id, e.mac::text AS mac, e.hostname,
                   ev.device_id, d.hostname AS switch, ev.if_index, ev.details, ev.created_at
            FROM udt_events ev
            LEFT JOIN udt_endpoints e ON e.id = ev.endpoint_id
            LEFT JOIN devices d ON d.id = ev.device_id
            WHERE {where_sql} ORDER BY ev.created_at DESC OFFSET :skip LIMIT :limit"""
    ), params)).mappings().all()
    return {"data": [dict(r) | {"id": r["id"],
                                "endpoint_id": str(r["endpoint_id"]) if r["endpoint_id"] else None,
                                "device_id": str(r["device_id"]) if r["device_id"] else None}
                     for r in rows]}


@router.get("/capacity")
async def capacity(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    devices = (await db.execute(text(
        """SELECT d.id, d.hostname, d.location,
                  COUNT(*) FILTER (WHERE di.if_type IS NULL OR di.if_type IN (6,117)) AS total,
                  COUNT(*) FILTER (WHERE di.oper_status='up' AND (di.if_type IS NULL OR di.if_type IN (6,117))) AS used,
                  COUNT(*) FILTER (WHERE ps.is_uplink) AS uplinks,
                  COUNT(*) FILTER (WHERE ps.last_endpoint_seen > NOW() - INTERVAL '1 day') AS active
           FROM devices d JOIN device_interfaces di ON di.device_id = d.id
           LEFT JOIN udt_port_state ps ON ps.device_id=di.device_id AND ps.if_index=di.if_index
           WHERE EXISTS (SELECT 1 FROM udt_port_state p WHERE p.device_id=d.id)
           GROUP BY d.id ORDER BY d.hostname"""
    ))).mappings().all()
    return {"data": [
        dict(r) | {"id": str(r["id"]),
                   "free": (r["total"] or 0) - (r["used"] or 0),
                   "pct": round((r["used"] or 0) / r["total"] * 100, 1) if r["total"] else 0.0}
        for r in devices
    ]}


@router.get("/capacity/{device_id}/trend")
async def capacity_trend(device_id: str, days: int = Query(default=30, ge=1, le=365),
                         db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text(
        """SELECT day, total_ports, used_ports, active_ports, uplink_ports
           FROM udt_port_capacity_daily
           WHERE device_id = :id AND day > CURRENT_DATE - make_interval(days => :d)
           ORDER BY day"""
    ), {"id": device_id, "d": days})).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.get("/vendors")
async def vendor_rollup(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text(
        """SELECT COALESCE(vendor, 'Unknown') AS vendor, endpoint_type, COUNT(*) AS count
           FROM udt_endpoints WHERE NOT ignored
           GROUP BY COALESCE(vendor,'Unknown'), endpoint_type ORDER BY count DESC LIMIT 200"""
    ))).mappings().all()
    by_vendor: dict = {}
    for r in rows:
        by_vendor.setdefault(r["vendor"], {"vendor": r["vendor"], "count": 0, "types": {}})
        by_vendor[r["vendor"]]["count"] += r["count"]
        by_vendor[r["vendor"]]["types"][r["endpoint_type"]] = r["count"]
    return {"data": sorted(by_vendor.values(), key=lambda x: -x["count"])}


# ── Domain controllers (AD user correlation) ─────────────────────────

@router.get("/domain-controllers")
async def list_dcs(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text(
        """SELECT dc.id, dc.name, dc.host, dc.windows_credential_id, wc.name AS credential_name,
                  dc.enabled, dc.poll_interval_s, dc.last_poll_at, dc.last_status, dc.last_error, dc.last_event_time
           FROM udt_domain_controllers dc LEFT JOIN windows_credentials wc ON wc.id = dc.windows_credential_id
           ORDER BY dc.name"""
    ))).mappings().all()
    return {"data": [dict(r) | {"id": str(r["id"]),
                                "windows_credential_id": str(r["windows_credential_id"]) if r["windows_credential_id"] else None}
                     for r in rows]}


@router.post("/domain-controllers")
async def create_dc(payload: DCCreate, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_operator_user)):
    row = (await db.execute(text(
        """INSERT INTO udt_domain_controllers (name, host, windows_credential_id, poll_interval_s, enabled)
           VALUES (:n, :h, :c, :p, :e) RETURNING id"""
    ), {"n": payload.name, "h": payload.host, "c": payload.windows_credential_id,
        "p": payload.poll_interval_s, "e": payload.enabled})).first()
    await write_audit_log(db, actor=user, action="udt.dc.create",
                          resource_type="udt_domain_controller", resource_id=str(row[0]),
                          metadata={"name": payload.name, "host": payload.host})
    await db.commit()
    return {"status": "ok", "id": str(row[0])}


@router.patch("/domain-controllers/{dc_id}")
async def update_dc(dc_id: str, payload: DCUpdate, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_operator_user)):
    sets, params = [], {"id": dc_id}
    for field, col in (("name", "name"), ("host", "host"),
                       ("windows_credential_id", "windows_credential_id"),
                       ("poll_interval_s", "poll_interval_s"), ("enabled", "enabled")):
        val = getattr(payload, field)
        if val is not None:
            sets.append(f"{col} = :{field}"); params[field] = val
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets.append("updated_at = NOW()")
    res = await db.execute(text(f"UPDATE udt_domain_controllers SET {', '.join(sets)} WHERE id = :id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Domain controller not found")
    await db.commit()
    return {"status": "ok"}


@router.delete("/domain-controllers/{dc_id}")
async def delete_dc(dc_id: str, db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_operator_user)):
    res = await db.execute(text("DELETE FROM udt_domain_controllers WHERE id = :id RETURNING id"), {"id": dc_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Domain controller not found")
    await db.commit()
    return {"status": "ok"}


@router.post("/domain-controllers/{dc_id}/poll")
async def poll_dc_now(dc_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_operator_user)):
    dc = (await db.execute(text(
        """SELECT dc.id, dc.name, dc.host, dc.poll_interval_s, dc.last_event_time,
                  wc.username, wc.domain, wc.password_enc, wc.auth_method, wc.transport,
                  wc.port, wc.ssl_verify
           FROM udt_domain_controllers dc
           JOIN windows_credentials wc ON wc.id = dc.windows_credential_id
           WHERE dc.id = :id"""
    ), {"id": dc_id})).mappings().first()
    if not dc:
        raise HTTPException(status_code=404, detail="Domain controller not found (or no credential linked)")
    from app.services.udt_ad_service import poll_controller
    result = await poll_controller(db, dc)
    await db.commit()
    return result
