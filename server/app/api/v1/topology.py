from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.core.database import AsyncSessionLocal, get_clickhouse_client, get_db
from app.core.security import require_operator_user
from app.models.user import User

router = APIRouter(prefix="/topology", tags=["Topology"])

LLDP_REM_CHASSIS = "1.0.8802.1.1.2.1.4.1.1.5"
LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"

CDP_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
CDP_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
CDP_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"

UPSTREAM_RANK = {
    "firewall": 10,
    "router": 20,
    "switch": 40,
    "access_point": 60,
    "server": 80,
    "printer": 90,
    "other": 100,
}


class DependencyCreate(BaseModel):
    parent_device_id: uuid.UUID
    child_device_id: uuid.UUID
    dependency_type: str = Field(default="uplink", pattern="^(uplink|wan|power|site|service|manual)$")
    suppress_alerts: bool = True
    enabled: bool = True
    notes: Optional[str] = None


class DependencyUpdate(BaseModel):
    dependency_type: Optional[str] = Field(default=None, pattern="^(uplink|wan|power|site|service|manual)$")
    suppress_alerts: Optional[bool] = None
    enabled: Optional[bool] = None
    notes: Optional[str] = None


class TopologyDiscoveryRequest(BaseModel):
    device_ids: list[uuid.UUID] = Field(default_factory=list)
    auto_dependencies: bool = True
    stale_after_hours: int = Field(default=168, ge=1, le=8760)


def _clean_snmp_value(value: str) -> str:
    value = value.strip()
    if ": " in value:
        _kind, value = value.split(": ", 1)
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.strip()


# Per-row SNMP sentinels that net-snmp prints into STDOUT when a column has no
# value for a given row. These are NOT neighbors — skip them so they don't get
# ingested as bogus remote hostnames / inflate the link count.
_SNMP_NONVALUES = (
    "No Such Instance", "No Such Object", "No more variables",
    "End of MIB", "= NULL",
)


def _parse_walk(output: str, oid_prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    prefix = "." + oid_prefix.lstrip(".")
    for line in output.splitlines():
        line = line.strip()
        if not line or " = " not in line:
            continue
        if any(s in line for s in _SNMP_NONVALUES):
            continue
        oid, value = line.split(" = ", 1)
        oid = oid.strip()
        if oid.startswith(oid_prefix):
            suffix = oid[len(oid_prefix):].lstrip(".")
        elif oid.startswith(prefix):
            suffix = oid[len(prefix):].lstrip(".")
        else:
            continue
        cleaned = _clean_snmp_value(value)
        if cleaned:
            out[suffix] = cleaned
    return out


def _suffix_local_port(suffix: str) -> Optional[int]:
    parts = [p for p in suffix.split(".") if p]
    if not parts:
        return None
    try:
        if len(parts) >= 3:
            return int(parts[-2])
        return int(parts[0])
    except ValueError:
        return None


def _suffix_first_index(suffix: str) -> Optional[int]:
    parts = [p for p in suffix.split(".") if p]
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def _host_key(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower().rstrip(".")
    return value.split(".", 1)[0]


def _device_rank(device_type: str | None) -> int:
    return UPSTREAM_RANK.get((device_type or "other").lower(), 100)


def _decrypt_maybe(value) -> Optional[str]:
    if not value:
        return None
    try:
        return decrypt(value)
    except Exception:
        # snmp_credentials store passphrases/communities in PLAINTEXT (the write
        # path never calls encrypt()). decrypt() raises on a plaintext value, so
        # fall back to the raw string instead of dropping it to None — otherwise
        # SNMP v3 -A/-X args go missing and every walk fails with "USM generic
        # error" (0 topology links). Stays correct if a value is ever encrypted.
        if isinstance(value, (bytes, memoryview)):
            try:
                return bytes(value).decode("utf-8")
            except Exception:
                return None
        return str(value)


def _effective_snmp(row: dict) -> dict:
    version = row.get("cred_snmp_version") or row.get("snmp_version") or "2c"
    return {
        "version": version,
        "host": row["ip_address"],
        "port": int(row.get("cred_port") or row.get("snmp_port") or 161),
        "community": row.get("cred_community") or row.get("snmp_community") or "public",
        "timeout_ms": int(row.get("cred_timeout_ms") or row.get("snmp_timeout_ms") or 2000),
        "retries": int(row.get("cred_retries") or row.get("snmp_retries") or 1),
        "v3_username": row.get("cred_v3_username") or row.get("snmp_v3_username"),
        "v3_context": row.get("cred_v3_context") or row.get("snmp_v3_context"),
        "v3_security_level": row.get("cred_v3_security_level") or "authPriv",
        "v3_auth_protocol": row.get("cred_v3_auth_protocol") or row.get("snmp_auth_protocol"),
        "v3_auth_passphrase": _decrypt_maybe(row.get("cred_v3_auth_passphrase") or row.get("snmp_auth_passphrase")),
        "v3_priv_protocol": row.get("cred_v3_priv_protocol") or row.get("snmp_priv_protocol"),
        "v3_priv_passphrase": _decrypt_maybe(row.get("cred_v3_priv_passphrase") or row.get("snmp_priv_passphrase")),
    }


async def _snmpwalk(settings: dict, oid: str) -> dict[str, str]:
    if shutil.which("snmpwalk") is None:
        raise RuntimeError("snmpwalk binary is not installed")

    timeout_s = max(1, int(settings["timeout_ms"] / 1000))
    args = [
        "snmpwalk",
        "-On",
        "-r", str(settings["retries"]),
        "-t", str(timeout_s),
        "-v", settings["version"],
    ]
    if settings["version"] == "3":
        sec = settings.get("v3_security_level") or "authPriv"
        args += ["-l", sec]
        if settings.get("v3_username"):
            args += ["-u", settings["v3_username"]]
        if settings.get("v3_context"):
            args += ["-n", settings["v3_context"]]
        if sec in ("authNoPriv", "authPriv"):
            if settings.get("v3_auth_protocol"):
                args += ["-a", settings["v3_auth_protocol"]]
            if settings.get("v3_auth_passphrase"):
                args += ["-A", settings["v3_auth_passphrase"]]
        if sec == "authPriv":
            if settings.get("v3_priv_protocol"):
                args += ["-x", settings["v3_priv_protocol"]]
            if settings.get("v3_priv_passphrase"):
                args += ["-X", settings["v3_priv_passphrase"]]
    else:
        args += ["-c", settings["community"]]
    args += [f"{settings['host']}:{settings['port']}", oid]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    if proc.returncode != 0:
        err = err_b.decode("utf-8", errors="replace").strip()
        if "No Such Object" in err or "No Such Instance" in err:
            return {}
        raise RuntimeError(err or f"snmpwalk exited with {proc.returncode}")
    return _parse_walk(out_b.decode("utf-8", errors="replace"), oid)


async def _load_devices(db: AsyncSession, ids: list[uuid.UUID] | None = None) -> list[dict]:
    where = "WHERE d.snmp_enabled = true"
    params: dict = {}
    if ids:
        where += " AND d.id = ANY(:ids)"
        params["ids"] = ids
    rows = (await db.execute(
        text(f"""
            SELECT
                d.id, d.hostname, host(d.ip_address) AS ip_address, d.device_type,
                d.status, d.location, d.group_id, d.snmp_enabled, d.snmp_version,
                d.snmp_port, d.snmp_community, d.snmp_timeout_ms, d.snmp_retries,
                d.snmp_v3_username, d.snmp_v3_context, d.snmp_auth_protocol,
                d.snmp_auth_passphrase, d.snmp_priv_protocol, d.snmp_priv_passphrase,
                c.snmp_version AS cred_snmp_version, c.community AS cred_community,
                c.port AS cred_port, c.timeout_ms AS cred_timeout_ms, c.retries AS cred_retries,
                c.v3_username AS cred_v3_username, c.v3_context AS cred_v3_context,
                c.v3_security_level AS cred_v3_security_level,
                c.v3_auth_protocol AS cred_v3_auth_protocol,
                c.v3_auth_passphrase AS cred_v3_auth_passphrase,
                c.v3_priv_protocol AS cred_v3_priv_protocol,
                c.v3_priv_passphrase AS cred_v3_priv_passphrase
            FROM devices d
            LEFT JOIN snmp_credentials c ON c.id = d.snmp_credential_id
            {where}
            ORDER BY d.hostname
            LIMIT 250
        """),
        params,
    )).mappings().all()
    return [dict(r) for r in rows]


async def _interface_lookup(db: AsyncSession, device_id: uuid.UUID) -> dict[str, dict]:
    rows = (await db.execute(
        text("""
            SELECT if_index, if_name, if_descr, if_alias
            FROM device_interfaces
            WHERE device_id = :device_id
        """),
        {"device_id": device_id},
    )).mappings().all()
    lookup: dict[str, dict] = {}
    for row in rows:
        data = dict(row)
        lookup[str(row["if_index"])] = data
        for key in ("if_name", "if_descr", "if_alias"):
            if row[key]:
                lookup[row[key].strip().lower()] = data
    return lookup


def _resolve_interface(lookup: dict[str, dict], if_index: Optional[int], name: str | None) -> tuple[Optional[int], str | None]:
    if if_index is not None and str(if_index) in lookup:
        row = lookup[str(if_index)]
        return row["if_index"], row["if_name"] or row["if_descr"] or name
    if name:
        row = lookup.get(name.strip().lower())
        if row:
            return row["if_index"], row["if_name"] or row["if_descr"] or name
    return if_index, name


def _resolve_remote_device(devices: list[dict], remote_hostname: str | None) -> Optional[uuid.UUID]:
    key = _host_key(remote_hostname)
    if not key:
        return None
    for device in devices:
        if _host_key(device.get("hostname")) == key:
            return device["id"]
    return None


async def _upsert_link(db: AsyncSession, link: dict) -> uuid.UUID:
    existing = (await db.execute(
        text("""
            SELECT id
            FROM topology_links
            WHERE local_device_id = :local_device_id
              AND COALESCE(local_if_index, -1) = COALESCE(:local_if_index, -1)
              AND protocol = :protocol
              AND COALESCE(remote_chassis_id, '') = COALESCE(:remote_chassis_id, '')
              AND COALESCE(remote_port_id, '') = COALESCE(:remote_port_id, '')
              AND COALESCE(remote_hostname, '') = COALESCE(:remote_hostname, '')
            LIMIT 1
        """),
        link,
    )).first()
    if existing:
        await db.execute(
            text("""
                UPDATE topology_links
                SET local_if_name = :local_if_name,
                    remote_device_id = :remote_device_id,
                    remote_if_name = :remote_if_name,
                    confidence = :confidence,
                    source = :source,
                    metadata = CAST(:metadata AS jsonb),
                    last_seen_at = :last_seen_at,
                    updated_at = :last_seen_at
                WHERE id = :id
            """),
            {**link, "id": existing.id},
        )
        return existing.id

    row = (await db.execute(
        text("""
            INSERT INTO topology_links (
                local_device_id, local_if_index, local_if_name, remote_device_id,
                remote_chassis_id, remote_port_id, remote_hostname, remote_if_name,
                protocol, confidence, source, metadata, first_seen_at, last_seen_at, updated_at
            )
            VALUES (
                :local_device_id, :local_if_index, :local_if_name, :remote_device_id,
                :remote_chassis_id, :remote_port_id, :remote_hostname, :remote_if_name,
                :protocol, :confidence, :source, CAST(:metadata AS jsonb),
                :last_seen_at, :last_seen_at, :last_seen_at
            )
            RETURNING id
        """),
        link,
    )).first()
    return row.id


async def _ensure_dependency(db: AsyncSession, devices_by_id: dict[str, dict], local_id, remote_id, protocol: str) -> None:
    if not local_id or not remote_id or str(local_id) == str(remote_id):
        return
    local = devices_by_id.get(str(local_id))
    remote = devices_by_id.get(str(remote_id))
    if not local or not remote:
        return

    local_rank = _device_rank(local.get("device_type"))
    remote_rank = _device_rank(remote.get("device_type"))
    if local_rank == remote_rank:
        return
    parent_id, child_id = (remote_id, local_id) if remote_rank < local_rank else (local_id, remote_id)
    await db.execute(
        text("""
            INSERT INTO topology_dependencies (
                parent_device_id, child_device_id, dependency_type, suppress_alerts,
                enabled, notes, created_at, updated_at
            )
            VALUES (
                :parent_id, :child_id, 'uplink', true, true,
                :notes, NOW(), NOW()
            )
            ON CONFLICT (parent_device_id, child_device_id, dependency_type)
            DO UPDATE SET updated_at = NOW()
        """),
        {
            "parent_id": parent_id,
            "child_id": child_id,
            "notes": f"Auto-created from {protocol.upper()} topology. Verify before relying on suppression.",
        },
    )


async def _discover_device(db: AsyncSession, device: dict, all_devices: list[dict], auto_dependencies: bool) -> dict:
    settings = _effective_snmp(device)
    interfaces = await _interface_lookup(db, device["id"])
    now = datetime.now(timezone.utc)
    found = 0
    protocol_counts = Counter()
    errors: list[str] = []
    devices_by_id = {str(d["id"]): d for d in all_devices}

    try:
        loc_id = await _snmpwalk(settings, LLDP_LOC_PORT_ID)
        loc_desc = await _snmpwalk(settings, LLDP_LOC_PORT_DESC)
        rem_sys = await _snmpwalk(settings, LLDP_REM_SYS_NAME)
        rem_port = await _snmpwalk(settings, LLDP_REM_PORT_ID)
        rem_port_desc = await _snmpwalk(settings, LLDP_REM_PORT_DESC)
        rem_chassis = await _snmpwalk(settings, LLDP_REM_CHASSIS)
        for suffix, remote_name in rem_sys.items():
            local_port = _suffix_local_port(suffix)
            local_name = loc_desc.get(str(local_port)) or loc_id.get(str(local_port)) if local_port is not None else None
            local_if_index, local_if_name = _resolve_interface(interfaces, local_port, local_name)
            remote_port_id = rem_port_desc.get(suffix) or rem_port.get(suffix)
            remote_id = _resolve_remote_device(all_devices, remote_name)
            link = {
                "local_device_id": device["id"],
                "local_if_index": local_if_index,
                "local_if_name": local_if_name,
                "remote_device_id": remote_id,
                "remote_chassis_id": rem_chassis.get(suffix),
                "remote_port_id": rem_port.get(suffix),
                "remote_hostname": remote_name,
                "remote_if_name": remote_port_id,
                "protocol": "lldp",
                "confidence": 95 if remote_id else 80,
                "source": "snmpwalk",
                "metadata": json.dumps({"suffix": suffix, "local_lldp_port": local_port}),
                "last_seen_at": now,
            }
            await _upsert_link(db, link)
            if auto_dependencies and remote_id:
                await _ensure_dependency(db, devices_by_id, device["id"], remote_id, "lldp")
            found += 1
            protocol_counts["lldp"] += 1
    except Exception as exc:
        errors.append(f"LLDP: {exc}")

    try:
        cdp_name = await _snmpwalk(settings, CDP_DEVICE_ID)
        cdp_port = await _snmpwalk(settings, CDP_DEVICE_PORT)
        cdp_platform = await _snmpwalk(settings, CDP_PLATFORM)
        for suffix, remote_name in cdp_name.items():
            if_index = _suffix_first_index(suffix)
            local_if_index, local_if_name = _resolve_interface(interfaces, if_index, None)
            remote_id = _resolve_remote_device(all_devices, remote_name)
            link = {
                "local_device_id": device["id"],
                "local_if_index": local_if_index,
                "local_if_name": local_if_name,
                "remote_device_id": remote_id,
                "remote_chassis_id": remote_name,
                "remote_port_id": cdp_port.get(suffix),
                "remote_hostname": remote_name,
                "remote_if_name": cdp_port.get(suffix),
                "protocol": "cdp",
                "confidence": 95 if remote_id else 80,
                "source": "snmpwalk",
                "metadata": json.dumps({"suffix": suffix, "platform": cdp_platform.get(suffix)}),
                "last_seen_at": now,
            }
            await _upsert_link(db, link)
            if auto_dependencies and remote_id:
                await _ensure_dependency(db, devices_by_id, device["id"], remote_id, "cdp")
            found += 1
            protocol_counts["cdp"] += 1
    except Exception as exc:
        errors.append(f"CDP: {exc}")

    return {
        "device_id": str(device["id"]),
        "hostname": device["hostname"],
        "links_found": found,
        "protocol_counts": dict(protocol_counts),
        "errors": errors,
    }


@router.get("/map")
async def get_topology_map(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    devices = (await db.execute(
        text("""
            SELECT d.id, d.hostname, host(d.ip_address) AS ip_address, d.device_type,
                   d.status, d.location, d.vendor, d.model, dg.name AS group_name,
                   COALESCE(ifc.interface_count, 0) AS interface_count
            FROM devices d
            LEFT JOIN device_groups dg ON dg.id = d.group_id
            LEFT JOIN (
                SELECT device_id, COUNT(*) AS interface_count
                FROM device_interfaces
                GROUP BY device_id
            ) ifc ON ifc.device_id = d.id
            ORDER BY d.hostname
        """)
    )).mappings().all()

    links = (await db.execute(
        text("""
            SELECT tl.*, ld.hostname AS local_hostname, rd.hostname AS remote_device_hostname
            FROM topology_links tl
            JOIN devices ld ON ld.id = tl.local_device_id
            LEFT JOIN devices rd ON rd.id = tl.remote_device_id
            WHERE tl.last_seen_at >= NOW() - INTERVAL '30 days'
               OR tl.protocol IN ('manual', 'inferred')
            ORDER BY tl.last_seen_at DESC
        """)
    )).mappings().all()

    dependencies = (await db.execute(
        text("""
            SELECT td.*, p.hostname AS parent_hostname, p.status AS parent_status,
                   c.hostname AS child_hostname, c.status AS child_status
            FROM topology_dependencies td
            JOIN devices p ON p.id = td.parent_device_id
            JOIN devices c ON c.id = td.child_device_id
            ORDER BY td.enabled DESC, p.hostname, c.hostname
        """)
    )).mappings().all()

    suppressed = (await db.execute(
        text("""
            SELECT COUNT(*) AS count
            FROM alerts
            WHERE triggered_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(metadata->>'suppressed_by_dependency', 'false') = 'true'
        """)
    )).scalar_one()

    linked_ids: set[str] = set()
    for link in links:
        linked_ids.add(str(link["local_device_id"]))
        if link["remote_device_id"]:
            linked_ids.add(str(link["remote_device_id"]))

    parent_ids = {str(dep["parent_device_id"]) for dep in dependencies if dep["enabled"]}
    child_ids = {str(dep["child_device_id"]) for dep in dependencies if dep["enabled"]}
    status_counts = Counter(row["status"] for row in devices)
    protocol_counts = Counter(row["protocol"] for row in links)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "devices": len(devices),
            "links": len(links),
            "dependencies": len(dependencies),
            "dependency_suppression": sum(1 for dep in dependencies if dep["enabled"] and dep["suppress_alerts"]),
            "unmapped_devices": sum(1 for row in devices if str(row["id"]) not in linked_ids),
            "suppressed_alerts_24h": int(suppressed or 0),
            "status_counts": dict(status_counts),
            "protocol_counts": dict(protocol_counts),
        },
        "nodes": [
            {
                "id": str(row["id"]),
                "hostname": row["hostname"],
                "ip_address": row["ip_address"],
                "device_type": row["device_type"],
                "status": row["status"],
                "location": row["location"],
                "group_name": row["group_name"],
                "vendor": row["vendor"],
                "model": row["model"],
                "interface_count": row["interface_count"],
                "is_dependency_parent": str(row["id"]) in parent_ids,
                "is_dependency_child": str(row["id"]) in child_ids,
                "is_mapped": str(row["id"]) in linked_ids,
            }
            for row in devices
        ],
        "links": [
            {
                "id": str(row["id"]),
                "source": str(row["local_device_id"]),
                "target": str(row["remote_device_id"]) if row["remote_device_id"] else None,
                "local_hostname": row["local_hostname"],
                "remote_hostname": row["remote_device_hostname"] or row["remote_hostname"],
                "local_if_index": row["local_if_index"],
                "local_if_name": row["local_if_name"],
                "remote_if_name": row["remote_if_name"] or row["remote_port_id"],
                "protocol": row["protocol"],
                "confidence": row["confidence"],
                "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
                "metadata": row["metadata"] or {},
            }
            for row in links
        ],
        "dependencies": [
            {
                "id": str(row["id"]),
                "parent_device_id": str(row["parent_device_id"]),
                "child_device_id": str(row["child_device_id"]),
                "parent_hostname": row["parent_hostname"],
                "child_hostname": row["child_hostname"],
                "parent_status": row["parent_status"],
                "child_status": row["child_status"],
                "dependency_type": row["dependency_type"],
                "suppress_alerts": row["suppress_alerts"],
                "enabled": row["enabled"],
                "notes": row["notes"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in dependencies
        ],
    }


@router.get("/links-live")
async def topology_links_live(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Live utilization for every resolved topology link, from SNMP interface
    counters (snmp_if_metrics in ClickHouse). Topology links already carry the
    local device + ifIndex, so this is a direct key join — no name matching.

    Response: {"data": {<link_id>: {in_bps, out_bps, util_pct, oper_status, speed}}}
    """
    rows = (await db.execute(
        text("""
            SELECT id, local_device_id, local_if_index
            FROM topology_links
            WHERE remote_device_id IS NOT NULL AND local_if_index IS NOT NULL
        """),
    )).mappings().all()
    if not rows:
        return {"data": {}, "generated_at": datetime.now(timezone.utc).isoformat()}

    # Interface speeds (bps) for util% — keyed by (device_id, if_index).
    pairs = {(str(r["local_device_id"]), int(r["local_if_index"])) for r in rows}
    device_ids = sorted({d for d, _ in pairs})
    speed_rows = (await db.execute(
        text("""SELECT device_id, if_index, if_speed FROM device_interfaces
                WHERE device_id = ANY(:ids)"""),
        {"ids": device_ids},
    )).mappings().all()
    speed_by = {(str(s["device_id"]), int(s["if_index"])): (s["if_speed"] or 0) for s in speed_rows}

    # Latest in/out bps per (device, ifIndex) over a short window.
    live: dict[tuple[str, int], dict] = {}
    try:
        client = get_clickhouse_client()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        res = client.query(
            """
            SELECT toString(device_id) AS device_id, if_index,
                   argMax(in_bps, timestamp)  AS in_bps,
                   argMax(out_bps, timestamp) AS out_bps,
                   argMax(oper_status, timestamp) AS oper_status
            FROM zenplus.snmp_if_metrics
            WHERE timestamp > %(cutoff)s
              AND toString(device_id) IN %(ids)s
            GROUP BY device_id, if_index
            """,
            parameters={"cutoff": cutoff, "ids": device_ids},
        )
        for row in res.result_rows:
            did, idx, in_bps, out_bps, oper = row
            live[(str(did), int(idx))] = {
                "in_bps": float(in_bps or 0), "out_bps": float(out_bps or 0),
                "oper_status": int(oper or 0),
            }
    except Exception:
        live = {}

    out: dict[str, dict] = {}
    for r in rows:
        key = (str(r["local_device_id"]), int(r["local_if_index"]))
        m = live.get(key)
        if not m:
            continue
        speed = speed_by.get(key, 0)
        bps = max(m["in_bps"], m["out_bps"])
        util = (bps / speed * 100.0) if speed else None
        out[str(r["id"])] = {
            "in_bps": m["in_bps"], "out_bps": m["out_bps"],
            "util_pct": round(util, 1) if util is not None else None,
            "oper_status": m["oper_status"], "speed": speed,
        }
    return {"data": out, "generated_at": datetime.now(timezone.utc).isoformat()}


@router.post("/discover")
async def discover_topology(
    request: TopologyDiscoveryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    devices = await _load_devices(db, request.device_ids or None)
    run = (await db.execute(
        text("""
            INSERT INTO topology_discovery_runs (status, created_by)
            VALUES ('running', :user_id)
            RETURNING id
        """),
        {"user_id": user.id},
    )).first()
    await db.commit()

    results = []
    protocol_counts = Counter()
    links_found = 0
    status = "completed"
    error_message = None
    try:
        sem = asyncio.Semaphore(8)

        async def scan_one(device: dict) -> dict:
            async with sem:
                async with AsyncSessionLocal() as task_db:
                    try:
                        result = await _discover_device(task_db, device, devices, request.auto_dependencies)
                        await task_db.commit()
                        return result
                    except Exception as exc:
                        await task_db.rollback()
                        return {
                            "device_id": str(device["id"]),
                            "hostname": device["hostname"],
                            "links_found": 0,
                            "protocol_counts": {},
                            "errors": [str(exc)],
                        }

        results = await asyncio.gather(*(scan_one(device) for device in devices))
        for result in results:
            links_found += result["links_found"]
            protocol_counts.update(result["protocol_counts"])
        await db.execute(
            text("""
                UPDATE topology_discovery_runs
                SET status = :status, completed_at = NOW(),
                    devices_scanned = :devices_scanned, links_found = :links_found,
                    protocol_counts = CAST(:protocol_counts AS jsonb)
                WHERE id = :id
            """),
            {
                "id": run.id,
                "status": status,
                "devices_scanned": len(devices),
                "links_found": links_found,
                "protocol_counts": json.dumps(dict(protocol_counts)),
            },
        )
        await db.commit()
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        await db.rollback()
        await db.execute(
            text("""
                UPDATE topology_discovery_runs
                SET status = 'failed', completed_at = NOW(), error_message = :error
                WHERE id = :id
            """),
            {"id": run.id, "error": error_message},
        )
        await db.commit()

    return {
        "run_id": str(run.id),
        "status": status,
        "devices_scanned": len(devices),
        "links_found": links_found,
        "protocol_counts": dict(protocol_counts),
        "results": results,
        "error": error_message,
    }


@router.get("/discovery-runs")
async def list_discovery_runs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    rows = (await db.execute(
        text("""
            SELECT id, started_at, completed_at, status, protocol_counts,
                   devices_scanned, links_found, error_message
            FROM topology_discovery_runs
            ORDER BY started_at DESC
            LIMIT 20
        """)
    )).mappings().all()
    return [
        {
            "id": str(row["id"]),
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            "status": row["status"],
            "protocol_counts": row["protocol_counts"] or {},
            "devices_scanned": row["devices_scanned"],
            "links_found": row["links_found"],
            "error_message": row["error_message"],
        }
        for row in rows
    ]


@router.post("/dependencies", status_code=201)
async def create_dependency(
    data: DependencyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if data.parent_device_id == data.child_device_id:
        raise HTTPException(status_code=400, detail="Parent and child device must be different")
    row = (await db.execute(
        text("""
            INSERT INTO topology_dependencies (
                parent_device_id, child_device_id, dependency_type, suppress_alerts,
                enabled, notes, created_at, updated_at
            )
            VALUES (
                :parent_device_id, :child_device_id, :dependency_type, :suppress_alerts,
                :enabled, :notes, NOW(), NOW()
            )
            ON CONFLICT (parent_device_id, child_device_id, dependency_type)
            DO UPDATE SET suppress_alerts = EXCLUDED.suppress_alerts,
                          enabled = EXCLUDED.enabled,
                          notes = EXCLUDED.notes,
                          updated_at = NOW()
            RETURNING id
        """),
        data.model_dump(),
    )).first()
    await db.commit()
    return {"id": str(row.id)}


@router.put("/dependencies/{dependency_id}")
async def update_dependency(
    dependency_id: uuid.UUID,
    data: DependencyUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    parts = ["updated_at = NOW()"]
    params = {"id": dependency_id}
    for key, value in fields.items():
        parts.append(f"{key} = :{key}")
        params[key] = value
    row = (await db.execute(
        text(f"UPDATE topology_dependencies SET {', '.join(parts)} WHERE id = :id RETURNING id"),
        params,
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Dependency not found")
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/dependencies/{dependency_id}", status_code=204)
async def delete_dependency(
    dependency_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("DELETE FROM topology_dependencies WHERE id = :id RETURNING id"),
        {"id": dependency_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Dependency not found")
    await db.commit()


# --------------------------------------------------------------------------- #
# Manual links — let operators draw a connection between two devices that the
# LLDP/CDP crawl didn't observe (or to annotate logical links). Stored in
# topology_links with protocol='manual'; only manual links are editable here so
# discovered topology can't be clobbered.
# --------------------------------------------------------------------------- #
class ManualLinkIn(BaseModel):
    source_device_id: uuid.UUID
    target_device_id: uuid.UUID
    shape: str = Field(default="curve", pattern="^(curve|straight|orthogonal)$")
    label: Optional[str] = Field(default=None, max_length=120)


class ManualLinkPatch(BaseModel):
    shape: Optional[str] = Field(default=None, pattern="^(curve|straight|orthogonal)$")
    label: Optional[str] = Field(default=None, max_length=120)


@router.post("/links", status_code=201)
async def create_manual_link(data: ManualLinkIn, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_operator_user)):
    if data.source_device_id == data.target_device_id:
        raise HTTPException(status_code=400, detail="Source and target must differ")
    found = (await db.execute(
        text("SELECT id FROM devices WHERE id = ANY(:ids)"),
        {"ids": [data.source_device_id, data.target_device_id]},
    )).all()
    if len(found) != 2:
        raise HTTPException(status_code=400, detail="Both devices must exist")
    # One manual link per ordered pair — update its shape if it already exists.
    existing = (await db.execute(
        text("""SELECT id FROM topology_links
                WHERE protocol='manual' AND local_device_id=:s AND remote_device_id=:t LIMIT 1"""),
        {"s": data.source_device_id, "t": data.target_device_id},
    )).first()
    md = json.dumps({"shape": data.shape, "manual": True, "label": data.label})
    if existing:
        await db.execute(
            text("UPDATE topology_links SET metadata=CAST(:md AS jsonb), updated_at=NOW() WHERE id=:id"),
            {"md": md, "id": existing.id},
        )
        await db.commit()
        return {"id": str(existing.id), "updated": True}
    row = (await db.execute(
        text("""
            INSERT INTO topology_links (
                local_device_id, remote_device_id, protocol, confidence, source,
                metadata, first_seen_at, last_seen_at, updated_at
            ) VALUES (:s, :t, 'manual', 100, 'manual', CAST(:md AS jsonb), NOW(), NOW(), NOW())
            RETURNING id
        """),
        {"s": data.source_device_id, "t": data.target_device_id, "md": md},
    )).first()
    await db.commit()
    return {"id": str(row.id), "updated": False}


@router.put("/links/{link_id}")
async def update_manual_link(link_id: uuid.UUID, data: ManualLinkPatch,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_operator_user)):
    row = (await db.execute(
        text("SELECT metadata FROM topology_links WHERE id=:id AND protocol='manual'"),
        {"id": link_id},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Manual link not found")
    md = dict(row.metadata or {})
    if data.shape is not None:
        md["shape"] = data.shape
    if data.label is not None:
        md["label"] = data.label
    await db.execute(
        text("UPDATE topology_links SET metadata=CAST(:md AS jsonb), updated_at=NOW() WHERE id=:id"),
        {"md": json.dumps(md), "id": link_id},
    )
    await db.commit()
    return {"id": str(link_id)}


@router.delete("/links/{link_id}", status_code=204)
async def delete_manual_link(link_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_operator_user)):
    row = (await db.execute(
        text("DELETE FROM topology_links WHERE id=:id AND protocol='manual' RETURNING id"),
        {"id": link_id},
    )).first()
    await db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Manual link not found (only manual links can be deleted)")
