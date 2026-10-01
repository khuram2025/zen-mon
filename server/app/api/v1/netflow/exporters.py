"""NetFlow exporter -> device mapping (FE-08).

    GET    /exporter-devices                     exporters with their owner device (search, paging)
    GET    /exporter-devices/{ip}/suggestions    ranked candidate devices with evidence
    GET    /exporter-devices/{ip}/impact         what linking to a device would change
    GET    /exporter-devices/{ip}/history        mapping and settings changes (audit log)
    PUT    /exporter-devices/{ip}                link (netflow.manage, exporter and device visible)
    DELETE /exporter-devices/{ip}                unlink (404 when there is no mapping)

Ownership has one definition, ``access.resolve_exporter_owners``: an explicit
mapping wins over a management-IP match (X-46) everywhere -- the listing,
interface names and who may read the exporter's flows. A mapping decides who
can see an exporter, so writes need ``netflow.manage``, an exporter the caller
can already see and a target device inside the caller's scope (X-16); every
write is audited with the previous owner and invalidates the visibility cache.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import SUPERUSER_PERMISSION
from app.core.security import get_role_permissions
from app.models.user import User
from app.services import netflow_health as nh

from .access import (
    FlowVisibility,
    authorize_mapping_write,
    canonical_ip,
    flow_visibility,
    invalidate_visibility_cache,
    require_device_visible,
    require_exporter_visible,
    require_view,
    resolve_exporter_owners,
    visibility_predicate,
)
from .common import (
    EXPORTER_MINUTES,
    RDNS_PENDING,
    _resolve_hostnames,
    _to_iso_utc,
    current_meta,
    now_utc,
    resolve_window,
    run_query,
    table_ref,
)
from .sql import EXPORTER

logger = logging.getLogger(__name__)

router = APIRouter()

LIST_MAX = 1000
NCM_SCAN_DEVICES = 5000
NCM_SCAN_BUDGET_S = 4.0
NCM_CACHE_TTL_S = 600.0


class ExporterDeviceIn(BaseModel):
    device_id: str
    note: str | None = Field(default=None, max_length=2000)


# ── Listing ──────────────────────────────────────────────────────────────────

@router.get("/exporter-devices")
async def list_exporter_devices(
    hours: int = Query(default=168, description="Exporters that sent anything in this many hours (1..2232)"),
    search: str | None = Query(default=None, max_length=120, description="Exporter address, name or device"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=LIST_MAX),
    unmapped: bool = Query(default=False, description="Only exporters without an owner device"),
    tz: str | None = Query(default=None, description="IANA time zone the window is labelled in"),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Exporters (recent receipts, plus any with a mapping or settings row)
    and how each resolves to a device.

    ``source``: 'mapped' (explicit link, wins), 'ip-match' (the exporter is a
    device's management IP) or null (interfaces show as raw ifIndex). ``flows``
    counts records received in the window (receipt clock, volume guard);
    ``last_seen`` is the last receipt. Sorted by records, then address.
    """
    win = resolve_window(hours=hours, tz=tz, max_span=timedelta(days=93))
    current_meta().set_source("rollup", win.start, win.end, 60, retention_checked=True)
    current_meta().restricted = vis.restricted
    pred, params = visibility_predicate(vis, param="nf_vis")
    params.update({"start": win.start, "end": win.end})
    rows = (await run_query(
        f"""
        SELECT {EXPORTER} AS e, sum(records) AS flows, max(last_received_at) AS last_seen
        FROM {table_ref(EXPORTER_MINUTES)}
        WHERE minute >= %(start)s AND minute < %(end)s{(' AND ' + pred) if pred else ''}
        GROUP BY e
        """,
        params, label="exporter-devices",
    )).result_rows
    seen: dict[str, tuple[int, object]] = {}
    for r in rows:
        ip = canonical_ip(r[0])
        if ip:
            seen[ip] = (int(r[1] or 0), r[2])
    settings = await nh.load_exporter_settings(db)
    mapped = await _mapped_rows(db)
    universe = [ip for ip in set(seen) | set(settings) | set(mapped) if vis.allows(ip)]
    owners = await resolve_exporter_owners(db, universe)
    shadow_ids = {o.overrides_device_id for o in owners.values() if o.overrides_device_id}
    shadow_names = await _hostnames(db, shadow_ids)

    items = []
    for ip in universe:
        owner = owners.get(ip)
        flows, last = seen.get(ip, (0, None))
        st = settings.get(ip) or {}
        items.append({
            "exporter_ip": ip,
            "display_name": st.get("display_name"),
            "flows": flows,
            "last_seen": _to_iso_utc(last),
            "device_id": owner.device_id if owner else None,
            "device_hostname": owner.hostname if owner else None,
            "source": owner.source if owner else None,
            "overrides_device_id": owner.overrides_device_id if owner else None,
            "overrides_device_hostname": shadow_names.get(owner.overrides_device_id) if owner and owner.overrides_device_id else None,
            "note": (mapped.get(ip) or {}).get("note"),
        })
    if search:
        q = search.strip().lower()
        items = [i for i in items if q in i["exporter_ip"].lower()
                 or q in (i["display_name"] or "").lower() or q in (i["device_hostname"] or "").lower()]
    if unmapped:
        items = [i for i in items if not i["source"]]
    items.sort(key=lambda i: (-i["flows"], nh._ip_sort_key(i["exporter_ip"])))
    total = len(items)
    page = items[offset:offset + limit]
    current_meta().set_truncated(limit, len(page), more=offset + len(page) < total)
    return page


async def _mapped_rows(db: AsyncSession) -> dict[str, dict]:
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT host(m.exporter_ip) AS ip, m.device_id::text AS device_id, m.note, m.updated_at, "
                "m.created_by::text AS created_by FROM netflow_exporter_devices m"
            ))).mappings().all()
    except Exception:
        return {}
    return {canonical_ip(r["ip"]): dict(r) for r in rows if canonical_ip(r["ip"])}


async def _hostnames(db: AsyncSession, ids) -> dict[str, str]:
    uuids = []
    for i in ids or []:
        try:
            uuids.append(uuid.UUID(str(i)))
        except ValueError:
            continue
    if not uuids:
        return {}
    rows = (await db.execute(text("SELECT id::text AS id, hostname FROM devices WHERE id = ANY(:ids)"),
                             {"ids": uuids})).all()
    return {r.id: r.hostname for r in rows}


# ── Suggestions (evidence) ───────────────────────────────────────────────────

def _scope_sql(scope: list[str] | None, alias: str = "d") -> tuple[str, dict]:
    if scope is None:
        return "", {}
    return " AND " + scoping.jsonb_tags_visible(f"{alias}.tags"), {scoping.SCOPE_PARAM: scope}


_ADDR_ASSIGN = re.compile(
    r"^\s*(?:ip(?:v6)?\s+address|ipv4\s+address|address|set\s+ip6?|set\s+ip-address|inet6?\s+address|"
    r"set\s+interfaces\s+\S+(?:\s+unit\s+\d+)?\s+family\s+inet6?\s+address|"
    r"/interface\s+ip\s+address|add\s+address=|ip\s+address\s+add)\b",
    re.IGNORECASE,
)
_IFACE_HEADER = re.compile(r"^\s*(?:interface\s+(\S+(?:\s\S+)?)|edit\s+\"?([^\"\s]+)\"?)\s*$", re.IGNORECASE)
_JUNOS_IFACE = re.compile(r"set\s+interfaces\s+(\S+)(?:\s+unit\s+(\d+))?", re.IGNORECASE)


def config_evidence(content: str, ip: str, max_hits: int = 5) -> list[dict]:
    """Lines of a device configuration that mention ``ip``: an interface
    address assignment (strong ownership evidence, with the interface when it
    can be told) or a plain mention (weak: often a peer, a route or this
    appliance as the flow destination). Returns line numbers, never content."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return []
    if addr.version == 4:
        pat = re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d])")
    else:
        pat = re.compile(r"(?<![0-9A-Fa-f:])" + re.escape(ip) + r"(?![0-9A-Fa-f:])", re.IGNORECASE)
    hits: list[dict] = []
    lines = content.splitlines()
    for n, line in enumerate(lines, start=1):
        if not pat.search(line):
            continue
        assign = bool(_ADDR_ASSIGN.search(line))
        iface = None
        if assign:
            m = _JUNOS_IFACE.search(line)
            if m:
                iface = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            else:
                for back in range(n - 2, max(-1, n - 60), -1):
                    h = _IFACE_HEADER.match(lines[back])
                    if h:
                        iface = (h.group(1) or h.group(2)).strip()
                        break
                    if lines[back] and not lines[back][:1].isspace() and not lines[back].lstrip().startswith(("set ", "ip ", "ipv6 ", "!")):
                        break
        hits.append({"line": n, "kind": "interface_address" if assign else "mention", "interface": iface})
        if len(hits) >= max_hits:
            break
    return hits


_ncm_cache: dict[str, tuple[float, dict]] = {}


async def _ncm_evidence(db: AsyncSession, ip: str, scope: list[str] | None) -> tuple[dict[str, list[dict]], dict]:
    """{device_id -> hits} from each visible device's latest running config.
    Bounded by device count and time; the scan state is returned so the UI
    can say when it was partial."""
    key = f"{ip}|{','.join(scope) if scope is not None else '*'}"
    hit = _ncm_cache.get(key)
    if hit and time.monotonic() - hit[0] < NCM_CACHE_TTL_S:
        return hit[1]["hits"], hit[1]["scan"]
    scope_sql, params = _scope_sql(scope)
    params["lim"] = NCM_SCAN_DEVICES
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(f"""
                SELECT DISTINCT ON (c.device_id) c.device_id::text AS device_id, c.content, c.content_enc,
                       c.content_hash, c.captured_at
                FROM device_configs c JOIN devices d ON d.id = c.device_id
                WHERE c.config_type = 'running'{scope_sql}
                ORDER BY c.device_id, c.captured_at DESC
                LIMIT :lim
            """), params)).all()
    except Exception:
        logger.debug("netflow: NCM configs unavailable for suggestions", exc_info=True)
        return {}, {"scanned": 0, "complete": False, "available": False}

    from app.services.ncm_content import read_content

    def scan() -> tuple[dict[str, list[dict]], int, bool]:
        found: dict[str, list[dict]] = {}
        deadline = time.monotonic() + NCM_SCAN_BUDGET_S
        scanned = 0
        for r in rows:
            if time.monotonic() > deadline:
                return found, scanned, False
            scanned += 1
            try:
                content = read_content(r)
            except Exception:
                continue
            if ip not in content and ip.lower() not in content.lower():
                continue
            hits = config_evidence(content, ip)
            if hits:
                found[r.device_id] = [{**h, "captured_at": _to_iso_utc(r.captured_at)} for h in hits]
        return found, scanned, True

    found, scanned, complete = await asyncio.to_thread(scan)
    scan_state = {"scanned": scanned, "complete": complete and len(rows) < NCM_SCAN_DEVICES, "available": True}
    _ncm_cache[key] = (time.monotonic(), {"hits": found, "scan": scan_state})
    if len(_ncm_cache) > 256:
        for k in sorted(_ncm_cache, key=lambda k: _ncm_cache[k][0])[:64]:
            _ncm_cache.pop(k, None)
    return found, scan_state


def _hostname_matches(rdns: str, hostname: str | None) -> bool:
    if not rdns or not hostname:
        return False
    a, b = rdns.lower().rstrip("."), hostname.lower().rstrip(".")
    return a == b or a.split(".")[0] == b.split(".")[0]


async def _device_facts(db: AsyncSession, ids: list[str]) -> dict[str, dict]:
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except ValueError:
            continue
    if not uuids:
        return {}
    rows = (await db.execute(text("""
        SELECT d.id::text AS id, d.hostname, host(d.ip_address) AS ip, d.vendor, d.model, d.status,
               d.snmp_enabled, d.tags, d.device_type,
               (SELECT count(*) FROM device_interfaces i WHERE i.device_id = d.id) AS interfaces,
               (SELECT count(*) FROM device_interfaces i WHERE i.device_id = d.id
                  AND COALESCE(i.configured_speed_bps, i.if_speed, 0) > 0) AS with_speed
        FROM devices d WHERE d.id = ANY(:ids)
    """), {"ids": uuids})).mappings().all()
    out = {}
    for r in rows:
        tags = r["tags"] if isinstance(r["tags"], list) else []
        out[r["id"]] = {
            "device_id": r["id"], "hostname": r["hostname"], "ip_address": r["ip"], "vendor": r["vendor"],
            "model": r["model"], "status": r["status"], "device_type": r["device_type"],
            "snmp_enabled": bool(r["snmp_enabled"]), "tags": [str(t) for t in tags],
            "interfaces": int(r["interfaces"] or 0), "interfaces_with_speed": int(r["with_speed"] or 0),
        }
    return out


def guidance_for(device: dict) -> list[dict]:
    """What the operator should fix on a device so a mapping is useful."""
    out = []
    if not device.get("snmp_enabled"):
        out.append({"code": "no_snmp", "message": "SNMP polling is off for this device, so its interfaces are not "
                                                  "discovered: flows keep showing raw ifIndex numbers. Enable SNMP "
                                                  "on the device page."})
    if device.get("interfaces", 0) == 0:
        if device.get("snmp_enabled"):
            out.append({"code": "no_interfaces", "message": "No interfaces have been discovered for this device yet; "
                                                            "check its SNMP credentials and wait for a poll."})
    elif device.get("interfaces_with_speed", 0) == 0:
        out.append({"code": "no_speeds", "message": "None of the device's interfaces has a speed, so capacity and "
                                                    "utilization stay unknown. Set circuit speeds on the device's "
                                                    "interfaces."})
    return out


async def _can_read_configs(db: AsyncSession, user: User) -> bool:
    perms = set(await get_role_permissions(db, user.role))
    return SUPERUSER_PERMISSION in perms or "ncm.view" in perms


@router.get("/exporter-devices/{exporter}/suggestions")
async def exporter_suggestions(
    exporter: str,
    user: User = Depends(require_view),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Devices that probably own this exporter address, best first, each with
    the evidence behind it: management or reported IP match, the device's own
    interface MAC answering ARP for the address (UDT), an interface address in
    its latest running configuration (NCM; needs ``ncm.view``; line numbers
    only), discovery scans of the address (the device they matched, or the
    SNMP sysName answering there) and reverse DNS matching its hostname.
    Only devices inside the caller's scope are considered."""
    ip = require_exporter_visible(vis, exporter)
    scope = await scoping.visible_tags(db, user)
    scope_sql, sparams = _scope_sql(scope)
    evidence: dict[str, list[dict]] = {}

    def add(device_id: str, kind: str, score: int, text_: str, **extra) -> None:
        evidence.setdefault(str(device_id), []).append({"kind": kind, "score": score, "text": text_, **extra})

    rows = (await db.execute(text(
        f"SELECT d.id::text AS id, host(d.ip_address) AS mip, host(d.managed_ip) AS rip FROM devices d "
        f"WHERE (host(d.ip_address) = :ip OR host(d.managed_ip) = :ip){scope_sql}"
    ), {"ip": ip, **sparams})).all()
    for r in rows:
        if canonical_ip(r.mip) == ip:
            add(r.id, "management_ip", 100, f"{ip} is this device's management address")
        if canonical_ip(r.rip) == ip:
            add(r.id, "reported_ip", 80, f"{ip} is the address this device's controller reports for it")

    # UDT: the endpoint answering for the address is the device (MAC of one of
    # its interfaces, or UDT's own correlation).
    try:
        async with db.begin_nested():
            udt = (await db.execute(text(f"""
                SELECT DISTINCT ON (d.id) d.id::text AS id, e.mac::text AS mac, i.if_name,
                       h.last_seen, rd.hostname AS reporter, (i.id IS NOT NULL) AS by_mac
                FROM udt_ip_history h
                JOIN udt_endpoints e ON e.id = h.endpoint_id
                LEFT JOIN device_interfaces i ON i.mac_address = e.mac
                JOIN devices d ON d.id = COALESCE(i.device_id, e.device_id)
                LEFT JOIN devices rd ON rd.id = h.reporting_device_id
                WHERE h.ip = CAST(:ip AS inet){scope_sql}
                ORDER BY d.id, h.last_seen DESC
                LIMIT 20
            """), {"ip": ip, **sparams})).all()
    except Exception:
        logger.debug("netflow: UDT evidence unavailable", exc_info=True)
        udt = []
    for r in udt:
        seen = _to_iso_utc(r.last_seen)
        if r.by_mac:
            add(r.id, "interface_mac", 90,
                f"MAC {r.mac} of this device's interface {r.if_name or '(unnamed)'} answered ARP for {ip}"
                + (f" (seen by {r.reporter})" if r.reporter else ""), seen_at=seen)
        else:
            add(r.id, "udt_endpoint", 70,
                f"User Device Tracker matched the endpoint answering for {ip} (MAC {r.mac}) to this device",
                seen_at=seen)

    # Discovery: the SNMP agent answering at the address (sysName), or the
    # device a scan matched/imported for it.
    discovery_rows = 0
    for table, linked_cols, name_col, at_col in (
        ("discovery_results_v2", ("matched_device_id", "imported_device_id"), "sys_name", "scanned_at"),
        ("discovery_results", ("imported_device_id",), "sys_name", "scanned_at"),
    ):
        linked = " OR ".join(f"d.id = r.{c}" for c in linked_cols)
        try:
            async with db.begin_nested():
                rows = (await db.execute(text(f"""
                    SELECT DISTINCT ON (d.id) d.id::text AS id, d.hostname, r.{name_col} AS sys_name,
                           r.{at_col} AS seen, ({linked}) AS linked
                    FROM (SELECT * FROM {table} WHERE ip_address = CAST(:ip AS inet)
                          ORDER BY {at_col} DESC LIMIT 20) r
                    JOIN devices d ON ({linked})
                         OR (COALESCE(r.{name_col}, '') <> ''
                             AND split_part(lower(d.hostname), '.', 1) = split_part(lower(r.{name_col}), '.', 1))
                    WHERE TRUE{scope_sql}
                    ORDER BY d.id, ({linked}) DESC, r.{at_col} DESC
                    LIMIT 20
                """), {"ip": ip, **sparams})).all()
        except Exception:
            logger.debug("netflow: %s evidence unavailable", table, exc_info=True)
            continue
        discovery_rows += len(rows)
        for r in rows:
            seen = _to_iso_utc(r.seen)
            if r.linked:
                add(r.id, "discovery_match", 75,
                    f"Discovery matched the device answering at {ip}"
                    + (f" (sysName {r.sys_name})" if r.sys_name else "") + " to this device", seen_at=seen)
            elif _hostname_matches(r.sys_name or "", r.hostname):
                add(r.id, "sys_name", 60,
                    f"The SNMP agent answering at {ip} reports sysName {r.sys_name}, matching this device's hostname",
                    seen_at=seen)

    ncm_state = {"available": False, "permitted": False, "scanned": 0, "complete": False}
    if await _can_read_configs(db, user):
        found, scan = await _ncm_evidence(db, ip, scope)
        ncm_state = {**scan, "permitted": True}
        for device_id, hits in found.items():
            for h in hits:
                if h["kind"] == "interface_address":
                    add(device_id, "config_interface", 85,
                        f"Running configuration assigns {ip} to "
                        + (f"interface {h['interface']}" if h["interface"] else "an interface")
                        + f" (line {h['line']})", line=h["line"], captured_at=h["captured_at"])
                else:
                    add(device_id, "config_mention", 25,
                        f"Running configuration mentions {ip} (line {h['line']}); often a peer or a route, "
                        "not the device's own address", line=h["line"], captured_at=h["captured_at"])

    # Reverse DNS follows the operator's switch (Settings → Enrichment): when it
    # is off no PTR query leaves the appliance from here either.
    from .settings import rdns_config

    rdns_cfg = await rdns_config()
    if rdns_cfg.get("enabled"):
        rdns = (await asyncio.to_thread(_resolve_hostnames, [ip], min(1.0, float(rdns_cfg["deadline_s"])))).get(ip)
        rdns_name = rdns if isinstance(rdns, str) else None
        rdns_status = "ok" if rdns_name else ("pending" if rdns is RDNS_PENDING else "no_record")
    else:
        rdns_name, rdns_status = None, "disabled"
    if rdns_name:
        short = rdns_name.lower().rstrip(".").split(".")[0]
        rows = (await db.execute(text(
            f"SELECT d.id::text AS id, d.hostname FROM devices d "
            f"WHERE (lower(d.hostname) = :full OR split_part(lower(d.hostname), '.', 1) = :short){scope_sql} LIMIT 10"
        ), {"full": rdns_name.lower().rstrip("."), "short": short, **sparams})).all()
        for r in rows:
            if _hostname_matches(rdns_name, r.hostname):
                add(r.id, "reverse_dns", 45, f"Reverse DNS of {ip} is {rdns_name}, matching the device hostname")

    owners = await resolve_exporter_owners(db, [ip])
    owner = owners.get(ip)
    facts = await _device_facts(db, list(evidence))
    candidates = []
    for device_id, ev in evidence.items():
        f = facts.get(device_id)
        if f is None:
            continue
        ev.sort(key=lambda e: -e["score"])
        best = ev[0]["score"]
        score = min(100, best + 5 * (len({e["kind"] for e in ev}) - 1))
        candidates.append({
            **f, "score": score,
            "confidence": "high" if score >= 85 else ("medium" if score >= 45 else "low"),
            "evidence": ev,
            "current_owner": bool(owner and owner.device_id == device_id),
            "guidance": guidance_for(f),
        })
    candidates.sort(key=lambda c: (-c["score"], c["hostname"] or ""))
    return {
        "exporter": ip,
        "reverse_dns": rdns_name,
        "current_owner": ({"device_id": owner.device_id, "hostname": owner.hostname, "source": owner.source}
                          if owner else None),
        "candidates": candidates[:20],
        "sources": {"ncm": ncm_state, "udt": True, "discovery": discovery_rows > 0,
                    "reverse_dns": rdns_name is not None,
                    # 'disabled' (Settings → Enrichment), 'ok', 'no_record' or 'pending'
                    "reverse_dns_status": rdns_status},
    }


# ── Impact preview ───────────────────────────────────────────────────────────

@router.get("/exporter-devices/{exporter}/impact")
async def exporter_mapping_impact(
    exporter: str,
    device_id: str = Query(..., description="Device the exporter would be linked to"),
    user: User = Depends(require_view),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """What linking the exporter to ``device_id`` would change, for an explicit
    confirmation: the current owner and how it resolved, whether the link
    overrides a management-IP match, how many of the interface indexes the
    exporter reported in the last 24 h exist on the target (names, speeds),
    which team tags will see its flows afterwards, and setup gaps on the
    target device."""
    ip = require_exporter_visible(vis, exporter)
    target = await require_device_visible(db, user, device_id)
    owners = await resolve_exporter_owners(db, [ip])
    owner = owners.get(ip)
    ip_match = (await db.execute(text(
        "SELECT id::text AS id, hostname FROM devices WHERE host(ip_address) = :ip ORDER BY id LIMIT 1"
    ), {"ip": ip})).first()
    now = now_utc()
    ifaces = await nh.exporter_ifindexes(ip, now - timedelta(hours=24), now, limit=500) or []
    seen = {i["ifindex"] for i in ifaces}
    ids = [target.id] + ([owner.device_id] if owner and owner.device_id != target.id else [])
    facts = await _device_facts(db, ids)
    known = await _interface_indexes(db, ids)
    t_idx = known.get(target.id, {})
    resolves = sorted(i for i in seen if i in t_idx)
    with_speed = sorted(i for i in resolves if t_idx[i])
    current_resolves = None
    if owner and owner.device_id != target.id:
        cur = known.get(owner.device_id, {})
        current_resolves = len([i for i in seen if i in cur])
    tfacts = facts.get(target.id, {})
    guidance = guidance_for(tfacts) if tfacts else []
    if seen and len(resolves) < len(seen) / 2 and tfacts.get("interfaces"):
        guidance.append({"code": "ifindex_mismatch",
                         "message": f"Only {len(resolves)} of the {len(seen)} interface indexes this exporter "
                                    "reported exist on this device; check that it is the right device."})
    shadow = None
    if ip_match and ip_match.id != target.id:
        shadow = {"device_id": ip_match.id, "hostname": ip_match.hostname}
        guidance.append({"code": "overrides_ip_match",
                         "message": f"{ip} is the management address of {ip_match.hostname}; this link overrides "
                                    "that match for names, capacity and visibility."})
    return {
        "exporter": ip,
        "current_owner": ({"device_id": owner.device_id, "hostname": owner.hostname, "source": owner.source,
                           "tags": sorted(owner.tags)} if owner else None),
        "target": tfacts or {"device_id": target.id, "hostname": target.hostname},
        "unchanged": bool(owner and owner.device_id == target.id and owner.source == "mapped"),
        "overrides_ip_match": shadow,
        "interfaces": {
            "seen_24h": len(seen),
            "resolve_on_target": len(resolves),
            "with_speed_on_target": len(with_speed),
            "resolve_on_current": current_resolves,
            "unresolved_sample": sorted(seen - set(resolves))[:10],
        },
        "visibility": {
            "current_tags": sorted(owner.tags) if owner else [],
            "target_tags": sorted(target.tags),
            "note": ("Users limited to tags see this exporter's flows only when the owner device carries one of "
                     "their tags; unrestricted users always see it."),
        },
        "guidance": guidance,
    }


async def _interface_indexes(db: AsyncSession, ids: list[str]) -> dict[str, dict[int, bool]]:
    uuids = [uuid.UUID(i) for i in ids if i]
    if not uuids:
        return {}
    rows = (await db.execute(text(
        "SELECT device_id::text AS d, if_index, COALESCE(configured_speed_bps, if_speed, 0) > 0 AS sp "
        "FROM device_interfaces WHERE device_id = ANY(:ids)"
    ), {"ids": uuids})).all()
    out: dict[str, dict[int, bool]] = {}
    for r in rows:
        out.setdefault(r.d, {})[int(r.if_index)] = bool(r.sp)
    return out


# ── History ──────────────────────────────────────────────────────────────────

@router.get("/exporter-devices/{exporter}/history")
async def exporter_history(
    exporter: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_view),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Mapping and settings changes for one exporter, newest first (audit log).
    Devices outside the caller's scope are named only as such."""
    ip = require_exporter_visible(vis, exporter)
    rows = (await db.execute(text("""
        SELECT created_at, actor_username, action, resource_type, metadata
        FROM audit_logs
        WHERE resource_type IN ('netflow_exporter_device', 'netflow_exporter_settings')
          AND resource_id = :ip
        ORDER BY created_at DESC LIMIT :lim
    """), {"ip": ip, "lim": limit})).mappings().all()
    scope = await scoping.visible_tags(db, user)
    device_ids = set()
    for r in rows:
        md = r["metadata"] or {}
        for k in ("device_id", "previous_device_id"):
            if md.get(k):
                device_ids.add(str(md[k]))
    visible_ids = await _visible_device_ids(db, device_ids, scope)
    out = []
    for r in rows:
        md = dict(r["metadata"] or {})
        if r["resource_type"] == "netflow_exporter_device":
            for k, name in (("device_id", "device_hostname"), ("previous_device_id", "previous_device_hostname")):
                if md.get(k) and str(md[k]) not in visible_ids:
                    md[k] = None
                    md[name] = "a device outside your scope"
        out.append({
            "at": _to_iso_utc(r["created_at"]),
            "actor": r["actor_username"],
            "action": r["action"],
            "kind": "mapping" if r["resource_type"] == "netflow_exporter_device" else "settings",
            "details": md,
        })
    return out


async def _visible_device_ids(db: AsyncSession, ids: set[str], scope: list[str] | None) -> set[str]:
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except ValueError:
            continue
    if not uuids:
        return set()
    rows = (await db.execute(text("SELECT id::text AS id, tags FROM devices WHERE id = ANY(:ids)"),
                             {"ids": uuids})).all()
    return {r.id for r in rows if scoping.entity_visible(r.tags, scope)}


# ── Writes ───────────────────────────────────────────────────────────────────

@router.put("/exporter-devices/{exporter_ip}")
async def set_exporter_device(
    exporter_ip: str,
    body: ExporterDeviceIn,
    user: User = Depends(require_view),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Link a NetFlow exporter address to the device that owns it.

    Requires ``netflow.manage`` (403), an exporter the caller can see and a
    target device inside the caller's scope (404 for either, so neither can
    be probed). The link wins over a management-IP match."""
    ip, device = await authorize_mapping_write(db, user, vis, exporter_ip, body.device_id)
    previous = (await db.execute(text(
        "SELECT m.device_id::text AS device_id, d.hostname FROM netflow_exporter_devices m "
        "LEFT JOIN devices d ON d.id = m.device_id WHERE m.exporter_ip = CAST(:ip AS inet)"
    ), {"ip": ip})).first()
    prior_owner = (await resolve_exporter_owners(db, [ip])).get(ip)
    await db.execute(
        text("""
            INSERT INTO netflow_exporter_devices (exporter_ip, device_id, note, created_by)
            VALUES (CAST(:ip AS inet), CAST(:did AS uuid), :note, CAST(:uid AS uuid))
            ON CONFLICT (exporter_ip) DO UPDATE
              SET device_id = EXCLUDED.device_id,
                  note = EXCLUDED.note,
                  updated_at = NOW()
        """),
        {"ip": ip, "did": device.id, "note": (body.note or "").strip() or None, "uid": str(user.id)},
    )
    from app.services.audit_service import write_audit_log

    await write_audit_log(
        db, actor=user, action="update", resource_type="netflow_exporter_device", resource_id=ip,
        metadata={
            "device_id": device.id, "device_hostname": device.hostname,
            "previous_device_id": previous.device_id if previous else (prior_owner.device_id if prior_owner else None),
            "previous_device_hostname": previous.hostname if previous else (prior_owner.hostname if prior_owner else None),
            "previous_source": "mapped" if previous else (prior_owner.source if prior_owner else None),
            "note": (body.note or "").strip() or None,
        },
    )
    await db.commit()
    invalidate_visibility_cache()
    nh.invalidate_status_cache()
    return {
        "exporter_ip": ip, "device_id": device.id, "device_hostname": device.hostname, "source": "mapped",
        "previous_device_id": previous.device_id if previous else None,
    }


@router.delete("/exporter-devices/{exporter_ip}", status_code=204)
async def delete_exporter_device(
    exporter_ip: str,
    user: User = Depends(require_view),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Remove an explicit link (the exporter falls back to a management-IP
    match, if any). 404 when no link exists; nothing is audited then."""
    ip, _ = await authorize_mapping_write(db, user, vis, exporter_ip)
    previous = (await db.execute(text(
        "DELETE FROM netflow_exporter_devices WHERE exporter_ip = CAST(:ip AS inet) "
        "RETURNING device_id::text AS device_id"
    ), {"ip": ip})).first()
    if previous is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="This exporter has no device link")
    names = await _hostnames(db, [previous.device_id])
    from app.services.audit_service import write_audit_log

    await write_audit_log(
        db, actor=user, action="delete", resource_type="netflow_exporter_device", resource_id=ip,
        metadata={"previous_device_id": previous.device_id,
                  "previous_device_hostname": names.get(previous.device_id)},
    )
    await db.commit()
    invalidate_visibility_cache()
    nh.invalidate_status_cache()


# ── The caller's flow scope ──────────────────────────────────────────────────

@router.get("/scope")
async def netflow_scope(vis: FlowVisibility = Depends(flow_visibility)):
    """What the caller's NetFlow scope holds, so every NetFlow page can say
    "your scope includes no flow exporters" once, instead of describing an
    empty answer as a collection gap. Counts only exporters that have sent
    flows (a visible device's management address that never exported is not
    one); ``flow_exporters`` is null when unrestricted or unknown, and
    ``scope_empty`` null when ClickHouse cannot say."""
    from .access import EXPORT_PERMISSION, MANAGE_PERMISSION, flow_exporter_count, flow_exporters

    flowing = await flow_exporters() if vis.restricted else None
    count = flow_exporter_count(vis, flowing)
    current_meta().restricted = vis.restricted
    return {
        "restricted": vis.restricted,
        "flow_exporters": count,
        "scope_empty": (count == 0) if count is not None else (False if not vis.restricted else None),
        "can_manage": vis.can(MANAGE_PERMISSION),
        "can_export": vis.can(EXPORT_PERMISSION),
    }
