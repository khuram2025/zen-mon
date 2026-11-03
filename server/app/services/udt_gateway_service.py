"""Supplement UDT with controller-managed ports and missing gateway ARP.

Independent inventory avoids silently enrolling controller children for ping
or alerts. All history is retained; gaps become unknown, never empty proof.
"""

from __future__ import annotations
import asyncio
import logging
from types import SimpleNamespace

from sqlalchemy import text
from app.services.job_lock_keys import JOB_LOCK_KEYS
from app.core.database import AsyncSessionLocal, engine
from app.services.udt_gateway_collection import canonical_mac
from app.services.udt_gateway_collection import (
    SWITCH_ROOT,
    PORT_ROOT,
    ARP_COMMANDS,
    snmp_cells,
    fortinet_switches,
    fortinet_ports,
    cli_snapshot,
)

logger = logging.getLogger("zenplus.udt_gateway")
LOCK = JOB_LOCK_KEYS["udt.gateway"]
INTERVAL = 300


async def _walk(dev, settings, oid):
    from app.api.v1.snmp import _private_snmpget

    args = [
        "snmpwalk" if settings["version"] == "1" else "snmpbulkwalk",
        "-v",
        settings["version"],
        "-r",
        "0",
        "-t",
        "2",
        "-On",
    ]
    if settings["version"] == "3":
        level = settings.get("v3_security_level") or (
            "authPriv"
            if settings.get("v3_priv_passphrase")
            else "authNoPriv" if settings.get("v3_auth_passphrase") else "noAuthNoPriv"
        )
        args += ["-l", level, "-u", settings.get("v3_username") or ""]
        auth_map = {
            "SHA224": "SHA-224",
            "SHA256": "SHA-256",
            "SHA384": "SHA-384",
            "SHA512": "SHA-512",
        }
        priv_map = {"AES128": "AES", "AES192": "AES-192", "AES256": "AES-256"}
        for key, flag in [
            ("v3_context", "-n"),
            ("v3_auth_protocol", "-a"),
            ("v3_auth_passphrase", "-A"),
            ("v3_priv_protocol", "-x"),
            ("v3_priv_passphrase", "-X"),
        ]:
            value = settings.get(key)
            if value:
                args += [
                    flag,
                    (
                        auth_map.get(value, value)
                        if flag == "-a"
                        else priv_map.get(value, value) if flag == "-x" else value
                    ),
                ]
    else:
        args += ["-c", settings.get("community") or ""]
    args += [f'{dev.ip_address}:{settings["port"]}', oid]
    proc, out, _ = await _private_snmpget(args, 15)
    if proc.returncode:
        raise ValueError(
            "Managed-switch SNMP walk unavailable; check read-only access and context"
        )
    if len(out) > 16 * 1024 * 1024:
        raise ValueError("Managed-switch snapshot exceeds collection limit")
    return snmp_cells(out.decode(errors="replace").splitlines(), oid)


async def collect_managed(db, device):
    from app.api.v1.devices import _device_snmp_settings

    settings = await _device_snmp_settings(db, device)
    # Apply the same per-device UDT override as the regular poller.
    override = (
        await db.execute(
            text(
                "SELECT snmp_credential_id FROM udt_device_settings WHERE device_id=:id"
            ),
            {"id": str(device.id)},
        )
    ).scalar()
    if override:
        copy = SimpleNamespace(
            **{
                column.key: getattr(device, column.key)
                for column in device.__table__.columns
            }
        )
        copy.snmp_credential_id = override
        settings = await _device_snmp_settings(db, copy)
    switches = fortinet_switches(
        {
            key: await _walk(device, settings, f"{SWITCH_ROOT}.{col}")
            for key, col in [("serial", 3), ("name", 4), ("status", 7)]
        }
    )
    if not switches:
        return [], []
    vdoms = await _walk(device, settings, "1.3.6.1.4.1.12356.101.3.2.1.1.2")
    for switch in switches:
        switch["vdom"] = vdoms.get(switch["instance"].split(".")[0])
    ports = fortinet_ports(
        {
            key: await _walk(device, settings, f"{PORT_ROOT}.{col}")
            for key, col in [("name", 5), ("status", 6), ("pvid", 8)]
        },
        switches,
    )
    # Older FortiOS reports configured rather than operational port status.
    import re

    version = re.search(r"(\d+)\.(\d+)\.(\d+)", device.os_version or "")
    if version and tuple(map(int, version.groups())) < (7, 0, 1):
        for port in ports:
            port["oper_status"] = "unknown"
    return switches, ports


async def save_inventory(db, controller_id, switches, ports):
    ids = {}
    for switch in switches:
        row = (
            await db.execute(
                text(
                    """INSERT INTO udt_managed_switches(controller_id,serial,hostname,instance,status,last_seen)
   VALUES(:cid,:serial,:hostname,:instance,:status,NOW())
   ON CONFLICT(controller_id,serial) DO UPDATE SET hostname=EXCLUDED.hostname,instance=EXCLUDED.instance,status=EXCLUDED.status,last_seen=NOW() RETURNING id"""
                ),
                {"cid": str(controller_id), **switch},
            )
        ).first()
        ids[switch["serial"]] = str(row[0])
    if ports:
        await db.execute(
            text(
                """INSERT INTO udt_managed_ports(switch_id,port_number,if_name,oper_status,pvid,last_seen)
   VALUES(:sid,:port_number,:if_name,:oper_status,:pvid,NOW())
   ON CONFLICT(switch_id,port_number) DO UPDATE SET if_name=EXCLUDED.if_name,oper_status=EXCLUDED.oper_status,pvid=EXCLUDED.pvid,last_seen=NOW()"""
            ),
            [{"sid": ids[p["serial"]], **p} for p in ports if p["serial"] in ids],
        )
    return ids


async def _endpoints(db, macs):
    if not macs:
        return {}
    rows = (
        await db.execute(
            text(
                """INSERT INTO udt_endpoints AS e(mac,last_seen,is_randomized,vendor)
   SELECT CAST(m AS macaddr),NOW(),(get_byte(decode(replace(m,':',''),'hex'),0)&2)=2,
          (SELECT vendor FROM udt_oui WHERE prefix=left(replace(m,':',''),6) LIMIT 1)
   FROM unnest(CAST(:macs AS text[])) m ORDER BY m
   ON CONFLICT(mac) DO UPDATE SET last_seen=GREATEST(e.last_seen,EXCLUDED.last_seen)
   RETURNING id,mac::text"""
            ),
            {"macs": sorted(set(macs))},
        )
    ).all()
    return {r[1]: str(r[0]) for r in rows}


async def save_macs(db, ids, ports, macs):
    valid = {(p["serial"], p["port_number"]): p["if_name"] for p in ports}
    # No port-number guessing or own-MAC(port0) attachment; only known ports.
    macs = [
        m
        for m in macs
        if (m["serial"], m["port_number"]) in valid
        and m["serial"] in ids
        and (
            not m.get("if_name")
            or m["if_name"] == valid[(m["serial"], m["port_number"])]
        )
    ]
    endpoints = await _endpoints(db, [m["mac"] for m in macs])
    rows = [{"eid": endpoints[m["mac"]], "sid": ids[m["serial"]], **m} for m in macs]
    if rows:
        await db.execute(
            text(
                """INSERT INTO udt_managed_locations(endpoint_id,switch_id,port_number,vlan_id,is_direct,active,first_seen,last_seen)
   VALUES(:eid,:sid,:port_number,:vlan_id,:is_direct,TRUE,NOW(),NOW())
   ON CONFLICT(endpoint_id,switch_id,port_number,COALESCE(vlan_id,-1)) WHERE active
   DO UPDATE SET last_seen=NOW(),is_direct=EXCLUDED.is_direct"""
            ),
            rows,
        )
    return len(rows)


async def save_arp(db, reporter, arp):
    interfaces = (
        await db.execute(
            text(
                "SELECT if_index,if_name,if_descr,mac_address FROM device_interfaces WHERE device_id=:id"
            ),
            {"id": str(reporter)},
        )
    ).all()
    by_name = {
        str(name).lower(): r[0] for r in interfaces for name in (r[1], r[2]) if name
    }
    own = {canonical_mac(str(r[3])) for r in interfaces if r[3]}
    # Unknown CLI interface names are not merged under fictitious ifIndex 0.
    arp = [
        a | {"ifx": by_name[a["interface"].lower()]}
        for a in arp
        if a["interface"].lower() in by_name and a["mac"] not in own
    ]
    endpoints = await _endpoints(db, [a["mac"] for a in arp])
    rows = [a | {"eid": endpoints[a["mac"]], "reporter": str(reporter)} for a in arp]
    if not rows:
        return 0
    await db.execute(
        text(
            """INSERT INTO udt_ip_evidence AS e(endpoint_id,ip,reporting_device_id,if_index,source,first_seen,last_seen,observation_count,recent_sightings)
  VALUES(:eid,CAST(:ip AS inet),:reporter,:ifx,'cli_arp',NOW(),NOW(),1,ARRAY[NOW()])
  ON CONFLICT(endpoint_id,ip,reporting_device_id,if_index,source) DO UPDATE SET
   last_seen=EXCLUDED.last_seen,observation_count=e.observation_count+1,
   recent_sightings=ARRAY(SELECT sighting FROM unnest(e.recent_sightings||EXCLUDED.recent_sightings) sighting ORDER BY sighting DESC LIMIT 3)
  WHERE EXCLUDED.last_seen>e.last_seen"""
        ),
        rows,
    )
    await db.execute(
        text(
            """INSERT INTO udt_ip_history(endpoint_id,ip,source,reporting_device_id,active,first_seen,last_seen)
  VALUES(:eid,CAST(:ip AS inet),'cli_arp',:reporter,TRUE,NOW(),NOW())
  ON CONFLICT(endpoint_id,ip) WHERE active DO UPDATE SET last_seen=GREATEST(udt_ip_history.last_seen,EXCLUDED.last_seen)"""
        ),
        list({(r["eid"], r["ip"]): r for r in rows}.values()),
    )
    await db.execute(
        text(
            """UPDATE udt_endpoints e SET ip_address=choice.ip,updated_at=NOW()
  FROM (SELECT ep.id,candidate.ip FROM udt_endpoints ep CROSS JOIN LATERAL (
   SELECT h.ip FROM udt_ip_history h WHERE h.endpoint_id=ep.id AND h.active AND h.last_seen>NOW()-INTERVAL '24 hours'
    AND (ep.ip_address IS NULL OR h.ip=ep.ip_address OR EXISTS (
     SELECT 1 FROM udt_ip_evidence evidence WHERE evidence.endpoint_id=ep.id AND evidence.ip=h.ip
      AND (SELECT count(*) FROM unnest(evidence.recent_sightings) observed_at WHERE observed_at>NOW()-INTERVAL '24 hours')>=3))
   ORDER BY family(h.ip),(h.ip=ep.ip_address) DESC NULLS LAST,h.last_seen DESC,h.ip LIMIT 1
  ) candidate WHERE ep.id=ANY(CAST(:ids AS uuid[]))) choice
  WHERE e.id=choice.id AND e.ip_address IS DISTINCT FROM choice.ip"""
        ),
        {"ids": list(endpoints.values())},
    )
    return len(rows)


async def poll_gateway(db, device):
    cid = str(device.id)
    switches = []
    ports = []
    macs = []
    arp = []
    messages = []
    status = "ok"
    if "fort" in (device.vendor or "").lower() and device.snmp_enabled:
        try:
            switches, ports = await collect_managed(db, device)
        except Exception:
            messages.append(
                "Managed-switch inventory unavailable; check SNMP view/context"
            )
            status = "partial"
    if switches and not ports:
        status = "partial"
        messages.append("Managed switches discovered but port inventory unavailable")
    ids = await save_inventory(db, cid, switches, ports)
    has_recent_arp = (
        await db.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM udt_ip_evidence WHERE reporting_device_id=:id AND source IN ('arp','nd') AND last_seen>NOW()-INTERVAL '15 minutes')"
            ),
            {"id": cid},
        )
    ).scalar()
    # No speculative SSH logins when SNMP already supplies gateway bindings.
    if switches or not has_recent_arp:
        profile = (
            (
                await db.execute(
                    text(
                        """SELECT host(d.ip_address) ip,n.platform,c.* FROM devices d
   JOIN device_ncm n ON n.device_id=d.id AND n.enabled LEFT JOIN udt_device_settings u ON u.device_id=d.id
   JOIN ncm_credentials c ON c.id=COALESCE(u.ncm_credential_id,n.credential_id) WHERE d.id=:id AND c.protocol='ssh' """
                    ),
                    {"id": cid},
                )
            )
            .mappings()
            .first()
        )
        if profile and profile["platform"] in ARP_COMMANDS:
            keys = (
                await db.execute(
                    text(
                        "SELECT key_type,key_base64 FROM ncm_host_keys WHERE host=:host AND port=:port AND retired_at IS NULL "
                        # Keys recorded by the SSH terminal or NCCM (first use) are not enforced here.
                        "AND verification_method NOT IN ('first_use','terminal_confirmed')"
                    ),
                    {"host": profile["ip"], "port": profile["port"] or 22},
                )
            ).all()
            try:
                snapshot = await asyncio.to_thread(
                    cli_snapshot,
                    dict(profile),
                    managed=bool(switches),
                    arp=not has_recent_arp,
                    known_keys=set(tuple(k) for k in keys),
                    switches=switches,
                )
                macs = snapshot["macs"]
                arp = snapshot["arp"]
            except Exception:
                messages.append(
                    "MAC/address collection unavailable; check NCCM account command permissions and firmware support"
                )
                status = "partial"
        else:
            messages.append(
                "Link an enabled NCCM read-only connection for supplemental MAC/address collection"
            )
            status = "partial"
    mac_count = await save_macs(db, ids, ports, macs)
    arp_count = await save_arp(db, cid, arp)
    await db.execute(
        text(
            """INSERT INTO udt_gateway_collection(device_id,last_poll_at,last_success_at,status,message,switch_count,port_count,mac_count,arp_count)
  VALUES(:id,NOW(),CASE WHEN :status='ok' THEN NOW() ELSE NULL END,:status,:message,:switches,:ports,:macs,:arp)
  ON CONFLICT(device_id) DO UPDATE SET last_poll_at=NOW(),last_success_at=COALESCE(EXCLUDED.last_success_at,udt_gateway_collection.last_success_at),status=EXCLUDED.status,message=EXCLUDED.message,
  switch_count=EXCLUDED.switch_count,port_count=EXCLUDED.port_count,mac_count=EXCLUDED.mac_count,arp_count=EXCLUDED.arp_count"""
        ),
        {
            "id": cid,
            "status": status,
            "message": "; ".join(messages)[:500] or "Gateway collection healthy",
            "switches": len(switches),
            "ports": len(ports),
            "macs": mac_count,
            "arp": arp_count,
        },
    )
    return {
        "status": status,
        "switches": len(switches),
        "ports": len(ports),
        "macs": mac_count,
        "arp": arp_count,
    }


async def udt_gateway_loop():
    from sqlalchemy import select
    from app.models.device import Device

    await asyncio.sleep(45)
    while True:
        try:
            async with engine.connect() as lock:
                if not (
                    await lock.execute(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK}
                    )
                ).scalar():
                    await asyncio.sleep(30)
                    continue
                try:
                    async with AsyncSessionLocal() as db:
                        devices = (
                            (
                                await db.execute(
                                    select(Device).where(
                                        Device.device_type.in_(
                                            ["firewall", "router", "switch"]
                                        ),
                                        Device.ip_address.is_not(None),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for device in devices:
                            enabled = (
                                await db.execute(
                                    text(
                                        "SELECT COALESCE((SELECT enabled FROM udt_device_settings WHERE device_id=:id),TRUE)"
                                    ),
                                    {"id": str(device.id)},
                                )
                            ).scalar()
                            if not enabled:
                                continue
                            due = (
                                await db.execute(
                                    text(
                                        "SELECT NOT EXISTS(SELECT 1 FROM udt_gateway_collection WHERE device_id=:id AND last_poll_at>NOW()-INTERVAL '5 minutes')"
                                    ),
                                    {"id": str(device.id)},
                                )
                            ).scalar()
                            if not due:
                                continue
                            try:
                                await poll_gateway(db, device)
                                await db.commit()
                            except Exception:
                                await db.rollback()
                                logger.warning(
                                    "Supplemental UDT collection failed for device %s",
                                    device.id,
                                )
                finally:
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK}
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Supplemental UDT scheduler unavailable")
        await asyncio.sleep(30)
