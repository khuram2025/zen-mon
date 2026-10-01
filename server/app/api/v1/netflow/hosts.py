"""NetFlow host profile (CR-14, FE-09): everything the appliance knows about one
address, from flows and from the rest of ZenPlus.

``GET /netflow/hosts/{ip}`` (window + FlowFilter, envelope on ``envelope=1``):

* **identity** -- reverse DNS with the time it was resolved (the operator's
  rDNS settings apply), GeoIP country/ASN with the database build date (or why
  there is none: private address, no database, no match), the IP groups it
  belongs to, its network class, a managed device with this address, and UDT
  identity -- MAC and vendor, switch and port, last AD user -- gated by
  ``udt.view`` / ``udt.view_users`` and by the switch's tag scope;
* **traffic in the window** -- in/out time series, top peers, applications,
  service ports (as server or client), exporters and interfaces that saw it;
* **security** -- threat-intelligence feed hits for the host and its top peers
  (B5's ``netflow_ti_*`` tables) and its open findings (``netflow_findings``);
* **first/last seen within retention** (hourly rollup, visible exporters).

Scope: every flow query carries the caller's exporter visibility. A scoped
user asking for a host that no visible exporter has seen within retention gets
404, the same answer as for an address that never appeared, so profiles
cannot be used to probe other teams' traffic. Unrestricted users get a
profile for any valid address (identity sections still apply).

Each section is computed independently: one failing (a ClickHouse timeout, a
missing UDT table) leaves ``null`` data and ``sections[name] = {status,
error_id?, message?}`` instead of failing the page or showing zeros.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.models.user import User
from app.services import geoip, netflow_classify, netflow_groups

from . import common
from .access import FlowVisibility, canonical_ip, flow_visibility, resolve_exporter_owners, require_view
from .common import (
    CONVERSATIONS_1H,
    NetflowError,
    Window,
    _interface_display_name,
    _resolve_interface_names,
    _to_iso_utc,
    current_meta,
    get_retention,
    run_query,
    table_ref,
    window_params,
)
from .filters import AddrMatch, FlowFilter, compile_filter, flow_filter
from .settings import geoip_status, rdns_config
from .sql import DST, EXPORTER, NET_CLASSES, PUBLIC_CLASS, SPECIAL_USE_CLASS, SRC, plan_source, side_in_network

logger = logging.getLogger(__name__)

router = APIRouter()

PEERS_LIMIT = 25
IFACE_MAX_SPAN = timedelta(days=7)
LIST_LIMIT = 30
_BUCKETS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
_TARGET_POINTS = 120
_GEOIP_STATUS_TTL_S = 300.0
_geoip_status_cache: dict[str, Any] = {"at": 0.0, "value": None}


# ── Small helpers ───────────────────────────────────────────────────────────

def _section_error(exc: BaseException) -> dict:
    if isinstance(exc, NetflowError):
        status = {"query_timeout": "timeout", "unavailable": "unavailable"}.get(exc.code, "failed")
        return {"status": status, "error_id": exc.error_id, "message": exc.detail}
    if isinstance(exc, HTTPException):
        return {"status": "failed", "error_id": None, "message": str(exc.detail)}
    error_id = common.new_error_id()
    logger.error("host profile section failed [%s]", error_id, exc_info=exc)
    return {"status": "failed", "error_id": error_id, "message": f"This part could not be loaded (ref {error_id})"}


def _ip_text(value) -> str | None:
    return canonical_ip(str(value)) if value is not None else None


def netclass_of(ip: str) -> str:
    """Python twin of ``sql.netclass_expr`` (the netclass filter's classes)."""
    addr = ipaddress.ip_address(ip)
    for name, cidrs in NET_CLASSES.items():
        for c in cidrs:
            net = ipaddress.ip_network(c)
            if net.version == addr.version and addr in net:
                return name
    return PUBLIC_CLASS


def pick_bucket(span_s: int, granularity_s: int | None) -> int:
    """Epoch-aligned bucket for ~120 points, a multiple of the source's
    granularity (hourly rollup parts cannot be split below an hour)."""
    gran = granularity_s if granularity_s and granularity_s > 1 else 1
    target = max(span_s / _TARGET_POINTS, gran)
    for b in _BUCKETS:
        if b >= target and (gran == 1 or b % gran == 0):
            return b
    return _BUCKETS[-1]


def fill_series(rows: dict[int, tuple], start: datetime, end: datetime, bucket: int,
                covered: set[int] | None) -> list[dict]:
    """Buckets covering [start, end). A bucket without host traffic is a
    measured zero only when the exporters in scope delivered records in it
    (``covered``); otherwise its values are null (unknown, e.g. before
    collection started or while the collector was down). ``covered=None``
    (coverage unreadable) makes every empty bucket unknown. ``partial`` marks
    buckets cut by the range."""
    points = []
    s, e = int(start.timestamp()), int(end.timestamp())
    t = s - s % bucket
    while t < e:
        row = rows.get(t)
        is_covered = row is not None or (covered is not None and t in covered)
        if row is None:
            row = (0, 0, 0, 0, 0) if is_covered else (None,) * 5
        b_in, b_out, p_in, p_out, flows = row
        points.append({
            "t": _to_iso_utc(datetime.fromtimestamp(t, timezone.utc)),
            "bytes_in": None if b_in is None else int(b_in), "bytes_out": None if b_out is None else int(b_out),
            "packets_in": None if p_in is None else int(p_in), "packets_out": None if p_out is None else int(p_out),
            "flows": None if flows is None else int(flows),
            "covered": is_covered, "partial": t < s or t + bucket > e,
        })
        t += bucket
    return points


def _rdns_resolved_at(ip: str) -> datetime | None:
    """When the cached answer for ``ip`` was obtained (the rDNS cache keeps
    its expiry on the monotonic clock; the TTL depends on the answer)."""
    with common._rdns_lock:
        ent = common._rdns_cache.get(ip)
    if not ent:
        return None
    expiry, host = ent
    ttl = common._RDNS_TTL if host else common._RDNS_NEGATIVE_TTL
    age = time.monotonic() - (expiry - ttl)
    return datetime.now(timezone.utc) - timedelta(seconds=max(0.0, age))


async def _rdns_section(ip: str) -> dict:
    cfg = await rdns_config()
    if not cfg["enabled"]:
        return {"status": "disabled", "hostname": None, "resolved_at": None,
                "message": "Reverse DNS lookups are turned off in NetFlow settings"}
    result = await asyncio.to_thread(common._resolve_hostnames, [ip], cfg["deadline_s"])
    host = result.get(ip)
    if host is common.RDNS_PENDING:
        return {"status": "pending", "hostname": None, "resolved_at": None,
                "message": f"The lookup did not finish within {cfg['deadline_s']:g} s; it continues in the background"}
    at = _rdns_resolved_at(ip)
    return {"status": "ok" if host else "no_record", "hostname": host or None,
            "resolved_at": _to_iso_utc(at) if at else None,
            "ttl_s": int(common._RDNS_TTL if host else common._RDNS_NEGATIVE_TTL)}


async def _geoip_dates() -> dict:
    now = time.monotonic()
    if _geoip_status_cache["value"] is None or now - _geoip_status_cache["at"] > _GEOIP_STATUS_TTL_S:
        _geoip_status_cache["value"] = await asyncio.to_thread(geoip_status)
        _geoip_status_cache["at"] = now
    return {d["kind"]: d.get("build_date") for d in _geoip_status_cache["value"]["databases"]}


async def _geoip_section(ip: str) -> dict:
    addr = ipaddress.ip_address(ip)
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        cls = netclass_of(str(addr))
        if cls in (PUBLIC_CLASS, SPECIAL_USE_CLASS):
            # Not one of the filter's private classes, yet not globally routed:
            # documentation, benchmarking, IETF-reserved or similar ranges.
            message = ("Special-purpose address (documentation, benchmarking or reserved range): "
                       "not covered by public GeoIP databases")
        else:
            message = f"{cls} address: private addresses are not in public GeoIP databases"
        return {"status": "not_applicable", "country": None, "country_name": None, "asn": None, "as_name": None,
                "message": message}
    if not geoip.available():
        return {"status": "no_database", "country": None, "country_name": None, "asn": None, "as_name": None,
                "message": "No GeoIP database is installed (NetFlow settings → Enrichment)"}
    g = await asyncio.to_thread(geoip.enrich, str(addr))
    dates = await _geoip_dates()
    status = "ok" if (g["country"] or g["asn"]) else "no_match"
    return {"status": status, **g, "database": {"country_built": dates.get("country"), "asn_built": dates.get("asn")}}


def _groups_section(ip: str) -> dict:
    primary = netflow_groups.classify_ip(ip)
    items = []
    for g in netflow_groups.groups_snapshot():
        own = [n for n in g.cidrs if netflow_groups.ip_in(ip, (n,))]
        if own:
            best = max(own, key=lambda n: n.prefixlen + (96 if n.version == 4 else 0))
            items.append({"id": g.id, "name": g.name, "color": g.color, "cidr": str(best), "via_subgroup": False})
        elif netflow_groups.ip_in(ip, g.effective):
            items.append({"id": g.id, "name": g.name, "color": g.color, "cidr": None, "via_subgroup": True})
    items.sort(key=lambda x: (x["name"] != primary, x["via_subgroup"], x["name"].lower()))
    return {"primary": primary, "items": items}


async def _device_section(db: AsyncSession, user: User, ip: str) -> dict | None:
    row = (await db.execute(text(
        "SELECT id::text AS id, hostname, tags, status FROM devices WHERE host(ip_address) = :ip "
        "ORDER BY id LIMIT 1"), {"ip": ip})).mappings().first()
    if row is None:
        return None
    if not scoping.entity_visible(row["tags"], await scoping.visible_tags(db, user)):
        return None
    return {"id": row["id"], "hostname": row["hostname"], "status": row["status"]}


async def _identity_section(db: AsyncSession, user: User, vis: FlowVisibility, ip: str) -> dict:
    if not vis.can("udt.view"):
        return {"status": "forbidden", "message": "Endpoint identity needs the UDT view permission"}
    see_users = vis.can("udt.view_users") or vis.can("udt.manage")
    try:
        async with db.begin_nested():
            eps = (await db.execute(text(
                """SELECT e.id, e.mac::text AS mac, e.vendor, e.hostname, e.endpoint_type, e.is_randomized,
                          e.authorized, e.is_watched, e.first_seen, e.last_seen, e.user_name, e.user_domain,
                          e.user_seen_at, (e.ip_address = CAST(:ip AS inet)) AS current_ip,
                          (SELECT max(h.last_seen) FROM udt_ip_history h
                            WHERE h.endpoint_id = e.id AND h.ip = CAST(:ip AS inet)) AS ip_last_seen
                     FROM udt_endpoints e
                    WHERE e.ip_address = CAST(:ip AS inet)
                       OR EXISTS (SELECT 1 FROM udt_ip_history h WHERE h.endpoint_id = e.id AND h.ip = CAST(:ip AS inet))
                    ORDER BY (e.ip_address = CAST(:ip AS inet)) DESC NULLS LAST, e.last_seen DESC
                    LIMIT 6"""), {"ip": ip})).mappings().all()
            if not eps:
                return {"status": "not_found",
                        "message": "The User Device Tracker has not seen this address in ARP/ND tables"}
            ep = eps[0]
            locs = (await db.execute(text(
                """SELECT l.device_id::text AS device_id, d.hostname AS switch, d.tags, l.if_index,
                          di.if_name, di.if_alias, l.vlan_id, l.is_direct, l.active, l.last_seen
                     FROM udt_endpoint_locations l
                     JOIN devices d ON d.id = l.device_id
                     LEFT JOIN device_interfaces di ON di.device_id = l.device_id AND di.if_index = l.if_index
                    WHERE l.endpoint_id = :eid
                    ORDER BY l.active DESC, l.is_direct DESC, l.last_seen DESC LIMIT 10"""),
                {"eid": ep["id"]})).mappings().all()
            login = None
            if see_users:
                login = (await db.execute(text(
                    """SELECT user_name, user_domain, event_time, host(ip) AS ip FROM udt_user_logins
                        WHERE endpoint_id = :eid OR ip = CAST(:ip AS inet)
                        ORDER BY event_time DESC LIMIT 1"""), {"eid": ep["id"], "ip": ip})).mappings().first()
    except Exception as exc:  # noqa: BLE001 -- UDT is optional (older appliances, module off)
        logger.info("host profile: UDT identity unavailable: %s", exc.__class__.__name__)
        return {"status": "unavailable", "message": "User Device Tracker data is not available on this appliance"}
    scope = await scoping.visible_tags(db, user)
    location = None
    hidden = False
    for loc in locs:
        if not scoping.entity_visible(loc["tags"], scope):
            hidden = True
            continue
        location = {
            "switch_id": loc["device_id"], "switch": loc["switch"], "if_index": loc["if_index"],
            "if_name": loc["if_name"] or loc["if_alias"] or f"ifIndex {loc['if_index']}",
            "if_alias": loc["if_alias"], "vlan_id": loc["vlan_id"], "direct": loc["is_direct"],
            "active": loc["active"], "last_seen": _to_iso_utc(loc["last_seen"]),
        }
        break
    user_info: dict[str, Any]
    if not see_users:
        user_info = {"status": "forbidden", "message": "Seeing who used a device needs the UDT user-activity permission"}
    elif login is not None:
        user_info = {"status": "ok", "user_name": login["user_name"], "user_domain": login["user_domain"],
                     "at": _to_iso_utc(login["event_time"]), "source": "domain controller logon"}
    elif ep["user_name"]:
        user_info = {"status": "ok", "user_name": ep["user_name"], "user_domain": ep["user_domain"],
                     "at": _to_iso_utc(ep["user_seen_at"]), "source": "endpoint record"}
    else:
        user_info = {"status": "none", "message": "No domain logon has been correlated with this device"}
    return {
        "status": "ok",
        "endpoint": {
            "id": str(ep["id"]), "mac": ep["mac"], "vendor": ep["vendor"], "hostname": ep["hostname"],
            "type": ep["endpoint_type"], "randomized_mac": ep["is_randomized"], "authorized": ep["authorized"],
            "watched": ep["is_watched"], "first_seen": _to_iso_utc(ep["first_seen"]),
            "last_seen": _to_iso_utc(ep["last_seen"]), "current_ip": bool(ep["current_ip"]),
            "ip_last_seen": _to_iso_utc(ep["ip_last_seen"]),
        },
        "location": location,
        "location_hidden": hidden and location is None,
        "user": user_info,
        "other_macs": [{"mac": e["mac"], "vendor": e["vendor"], "last_seen": _to_iso_utc(e["last_seen"])}
                       for e in eps[1:]],
    }


async def _ti_section(db: AsyncSession, ip: str, peers: list[str]) -> dict:
    try:
        async with db.begin_nested():
            feeds = (await db.execute(text(
                "SELECT count(*) FILTER (WHERE enabled) AS enabled, count(*) AS total, "
                "max(last_refreshed_at) FILTER (WHERE enabled) AS refreshed FROM netflow_ti_feeds"))).mappings().first()
            host_hits = (await db.execute(text(
                """SELECT f.name, f.category, e.prefix::text AS prefix, f.last_refreshed_at
                     FROM netflow_ti_entries e JOIN netflow_ti_feeds f ON f.id = e.feed_id
                    WHERE f.enabled AND e.prefix >>= CAST(:ip AS inet)
                    ORDER BY masklen(e.prefix) DESC, f.name LIMIT 20"""), {"ip": ip})).mappings().all()
            peer_hits = []
            if peers:
                peer_hits = (await db.execute(text(
                    """SELECT host(p.ip) AS ip, f.name, f.category, e.prefix::text AS prefix
                         FROM unnest(CAST(:ips AS inet[])) AS p(ip)
                         JOIN netflow_ti_entries e ON e.prefix >>= p.ip
                         JOIN netflow_ti_feeds f ON f.id = e.feed_id AND f.enabled
                        ORDER BY 1, 2 LIMIT 200"""), {"ips": peers})).mappings().all()
    except Exception as exc:  # noqa: BLE001 -- tables arrive with migrate-138
        logger.info("host profile: threat intel unavailable: %s", exc.__class__.__name__)
        return {"status": "unavailable", "message": "Threat-intelligence tables are not available"}
    if not feeds or not feeds["enabled"]:
        return {"status": "no_feeds", "host_hits": [], "peer_hits": [],
                "message": "No threat-intelligence feed is enabled, so reputation is unknown (not clean)"}
    return {
        "status": "ok", "feeds_enabled": int(feeds["enabled"]),
        "refreshed_at": _to_iso_utc(feeds["refreshed"]),
        "host_hits": [{"feed": h["name"], "category": h["category"], "prefix": h["prefix"],
                       "refreshed_at": _to_iso_utc(h["last_refreshed_at"])} for h in host_hits],
        "peer_hits": [{"ip": canonical_ip(h["ip"]), "feed": h["name"], "category": h["category"],
                       "prefix": h["prefix"]} for h in peer_hits],
    }


async def _findings_section(db: AsyncSession, vis: FlowVisibility, ip: str) -> dict:
    scope_sql = ""
    params: dict[str, Any] = {"ip": ip}
    if vis.exporters is not None:
        # Findings from exporters the caller cannot see (or with none) stay hidden.
        scope_sql = " AND exporter_ip IS NOT NULL AND host(exporter_ip) = ANY(CAST(:vis AS text[]))"
        params["vis"] = sorted(vis.exporters)
    base = f"FROM netflow_findings WHERE (src_ip = CAST(:ip AS inet) OR dst_ip = CAST(:ip AS inet)){scope_sql}"
    try:
        async with db.begin_nested():
            counts = (await db.execute(text(f"SELECT status, count(*) AS n {base} GROUP BY status"), params)).all()
            rows = (await db.execute(text(
                f"""SELECT id::text AS id, kind, severity, confidence, status, title, host(src_ip) AS src_ip,
                           host(dst_ip) AS dst_ip, host(exporter_ip) AS exporter_ip, first_seen, last_seen, occurrences
                    {base} AND status IN ('open', 'acknowledged')
                    ORDER BY last_seen DESC LIMIT 20"""), params)).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.info("host profile: findings unavailable: %s", exc.__class__.__name__)
        return {"status": "unavailable", "message": "Detection findings are not available"}
    by_status = {r.status: int(r.n) for r in counts}
    return {
        "status": "ok",
        "counts": {s: by_status.get(s, 0) for s in ("open", "acknowledged", "suppressed", "resolved")},
        "items": [{**dict(r), "first_seen": _to_iso_utc(r["first_seen"]), "last_seen": _to_iso_utc(r["last_seen"]),
                   "role": "source" if r["src_ip"] == ip else "destination"} for r in rows],
    }


# ── Flow queries ────────────────────────────────────────────────────────────

async def _seen(vis: FlowVisibility, host: AddrMatch) -> dict:
    """First/last flow time and total flows within rollup retention, over the
    visible exporters only (no page filters: this is about the address)."""
    where, params = compile_filter(FlowFilter(talker=host), CONVERSATIONS_1H, vis)
    res = await run_query(
        f"SELECT min(first_seen), max(last_seen), sum(flows) FROM {table_ref(CONVERSATIONS_1H)} WHERE {where}",
        params, label="host-seen")
    first, last, flows = res.result_rows[0] if res.result_rows else (None, None, 0)
    retention = await get_retention()
    flows = int(flows or 0)
    return {"first_seen": _to_iso_utc(first) if flows else None, "last_seen": _to_iso_utc(last) if flows else None,
            "flows": flows, "retention_days": retention.rollup_days, "source": "rollup"}


def _light_columns(host: AddrMatch) -> dict:
    out = side_in_network("src", host.network, None, None)
    inn = side_in_network("dst", host.network, None, None)
    return {
        "t": "toDateTime(timestamp, 'UTC')",
        "h_out": out, "h_in": inn,
        "peer": f"if({out}, {DST}, {SRC})",
        "exp": EXPORTER,
        "bytes": "bytes", "packets": "packets",
        "flows": ("flows", "toUInt64(1)"),
    }


def _class_columns(host: AddrMatch) -> dict:
    kw = dict(app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    roll = netflow_classify.classification_sql(netflow_classify.ROLLUP_SRC_PORT_SQL, "dst_port", "protocol", **kw)
    raw = netflow_classify.classification_sql("src_port", "dst_port", "protocol", **kw)
    return {
        "h_out": side_in_network("src", host.network, None, None),
        "h_in": side_in_network("dst", host.network, None, None),
        "proto": "protocol",
        "app": (roll["application"], raw["application"]),
        "sport": (roll["service_port"], raw["service_port"]),
        "dir": (roll["direction"], raw["direction"]),
        "bytes": "bytes",
        "flows": ("flows", "toUInt64(1)"),
    }


@router.get("/hosts/{ip}")
async def host_profile(
    ip: str,
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    addr = canonical_ip(ip)
    if addr is None or "/" in ip or "%" in ip:
        return JSONResponse({"detail": f"'{ip[:64]}' is not a single IP address", "code": "invalid_value",
                             "error_id": None, "fields": {"ip": "Enter a single IPv4 or IPv6 address"}},
                            status_code=400)
    host = AddrMatch.parse(addr)
    meta = current_meta()
    if f.talker is not None and f.talker != host:
        meta.warn("talker_replaced", f"The profile is about {addr}; the talker filter {f.talker.text} was replaced")
    fh = replace(f, talker=host)
    meta.filter = fh
    sections: dict[str, dict] = {}

    # Scope first: a scoped caller only gets profiles of hosts its exporters saw.
    try:
        seen = await _seen(vis, host)
        sections["seen"] = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        if vis.restricted:
            raise
        seen = None
        sections["seen"] = _section_error(exc)
    if vis.restricted and not (seen and seen["flows"]):
        raise HTTPException(status_code=404, detail="Host not found")
    if vis.restricted:
        meta.restricted = True

    await netflow_groups.load_groups()
    await netflow_classify.load_rules()
    plan = await plan_source(win, fh, rollup=CONVERSATIONS_1H)
    light, lparams = plan.from_sql(_light_columns(host), fh, vis)
    classed, cparams = plan.from_sql(_class_columns(host), fh, vis)
    bucket = pick_bucket(win.seconds, plan.granularity_s)
    # Interfaces need per-record ifIndexes (raw only); the last 7 days of the
    # window at most, so a long window does not turn into a month-long raw scan.
    iface_win = Window(start=max(win.start, win.end - IFACE_MAX_SPAN), end=win.end, mode=win.mode, tz=win.tz)
    raw_plan = await plan_source(iface_win, fh, rollup=None, record=False)
    iface_sub, iparams = raw_plan.from_sql(
        {"exp": EXPORTER, "ii": "input_if", "oi": "output_if", "bytes": "bytes"}, fh, vis)
    if raw_plan.retention_limited or iface_win.start > win.start:
        meta.warn("interfaces_partial",
                  "Interfaces come from raw flow records and cover only the part of the range shown with them")

    q_series = (f"SELECT intDiv(toUInt32(t), {bucket}) * {bucket} AS b, sumIf(bytes, h_in), sumIf(bytes, h_out), "
                f"sumIf(packets, h_in), sumIf(packets, h_out), sum(flows) FROM {light} AS src GROUP BY b ORDER BY b")
    q_peers = (f"SELECT peer, sumIf(bytes, h_in), sumIf(bytes, h_out), sum(bytes) AS tb, sum(flows), "
               f"count() OVER () FROM {light} AS src GROUP BY peer ORDER BY tb DESC LIMIT {PEERS_LIMIT}")
    q_exporters = (f"SELECT exp, sum(bytes) AS tb, sum(flows), min(t), max(t) FROM {light} AS src "
                   f"GROUP BY exp ORDER BY tb DESC LIMIT {LIST_LIMIT}")
    q_apps = (f"SELECT app, sum(bytes) AS tb, sum(flows), sumIf(bytes, h_in), sumIf(bytes, h_out) "
              f"FROM {classed} AS src GROUP BY app ORDER BY tb DESC LIMIT {LIST_LIMIT}")
    role = ("multiIf((dir = 'request' AND h_in) OR (dir = 'response' AND h_out), 'server', "
            "(dir = 'request' AND h_out) OR (dir = 'response' AND h_in), 'client', 'unknown')")
    q_ports = (f"SELECT proto, sport, {role} AS role, any(app), sum(bytes) AS tb, sum(flows) FROM {classed} AS src "
               f"WHERE sport != 0 GROUP BY proto, sport, role ORDER BY tb DESC LIMIT {LIST_LIMIT}")
    # Coverage in flow time: buckets in which the exporters in scope delivered
    # any record at all. A bucket without host traffic is a measured zero only
    # there; elsewhere (before collection started, collector down) it is unknown.
    cov_filter = FlowFilter(exporter=fh.exporter, tz=fh.tz)
    cov_plan = await plan_source(win, cov_filter, rollup=CONVERSATIONS_1H, record=False)
    cov_sub, cov_params = cov_plan.from_sql({"t": "toDateTime(timestamp, 'UTC')"}, cov_filter, vis)
    q_cov = f"SELECT DISTINCT intDiv(toUInt32(t), {bucket}) * {bucket} FROM {cov_sub} AS src"
    q_ifaces = (f"SELECT exp, x.1 AS ifindex, x.2 AS dir, sum(bytes) AS tb, count() FROM "
                f"(SELECT exp, arrayJoin([(ii, 'in'), (oi, 'out')]) AS x, bytes FROM {iface_sub} AS src) "
                f"WHERE x.1 != 0 GROUP BY exp, ifindex, dir ORDER BY tb DESC LIMIT {LIST_LIMIT}")

    results = await asyncio.gather(
        run_query(q_series, lparams, label="host-series"),
        run_query(q_peers, lparams, label="host-peers"),
        run_query(q_exporters, lparams, label="host-exporters"),
        run_query(q_apps, cparams, label="host-apps"),
        run_query(q_ports, cparams, label="host-ports"),
        run_query(q_ifaces, iparams, label="host-interfaces"),
        run_query(q_cov, cov_params, label="host-coverage"),
        _rdns_section(addr),
        _geoip_section(addr),
        return_exceptions=True,
    )
    r_series, r_peers, r_exp, r_apps, r_ports, r_ifaces, r_cov, r_rdns, r_geo = results
    if isinstance(r_cov, BaseException):
        covered = None
        meta.warn("coverage_unknown", "Collection coverage could not be read; quiet periods are shown as unknown")
    else:
        covered = {int(r[0]) for r in r_cov.result_rows}
    data: dict[str, Any] = {
        "ip": addr,
        "version": host.network.version,
        "netclass": netclass_of(addr),
        "seen": seen,
    }

    def take(name: str, value, build):
        if isinstance(value, BaseException):
            sections[name] = _section_error(value)
            return None
        try:
            out = build(value)
        except Exception as exc:  # noqa: BLE001
            sections[name] = _section_error(exc)
            return None
        sections[name] = {"status": "ok"}
        return out

    eff_from = plan.effective_from
    eff_to = plan.effective_to

    def build_series(res):
        rows = {int(r[0]): tuple(r[1:6]) for r in res.result_rows}
        points = fill_series(rows, eff_from, eff_to, bucket, covered) if eff_from < eff_to else []
        totals = {
            "bytes_in": sum(int(r[0]) for r in rows.values()), "bytes_out": sum(int(r[1]) for r in rows.values()),
            "packets_in": sum(int(r[2]) for r in rows.values()), "packets_out": sum(int(r[3]) for r in rows.values()),
            "flows": sum(int(r[4]) for r in rows.values()),
        }
        return {"bucket_s": bucket, "points": points, "totals": totals}

    data["timeseries"] = take("timeseries", r_series, build_series)

    def build_peers(res):
        rows = res.result_rows
        items = []
        for peer, b_in, b_out, tb, fl, _n in rows:
            pip = _ip_text(peer)
            g = geoip.enrich(pip) if pip and ipaddress.ip_address(pip).is_global else None
            items.append({"ip": pip, "bytes_in": int(b_in), "bytes_out": int(b_out), "bytes": int(tb),
                          "flows": int(fl), "group": netflow_groups.classify_ip(pip) if pip else None,
                          "country": g["country"] if g else None, "asn": g["asn"] if g else None,
                          "as_name": g["as_name"] if g else None})
        total = int(rows[0][5]) if rows else 0
        return {"items": items, "total": total, "truncated": total > len(items)}

    data["peers"] = take("peers", r_peers, build_peers)

    async def build_exporters(res):
        rows = res.result_rows
        owners = await resolve_exporter_owners(db, [_ip_text(r[0]) for r in rows if _ip_text(r[0])])
        items = []
        for e, tb, fl, first, last in rows:
            eip = _ip_text(e)
            owner = owners.get(eip or "")
            items.append({"exporter": eip, "device_id": owner.device_id if owner else None,
                          "device_hostname": owner.hostname if owner else None, "bytes": int(tb), "flows": int(fl),
                          "first_seen": _to_iso_utc(first), "last_seen": _to_iso_utc(last)})
        return {"items": items}

    if isinstance(r_exp, BaseException):
        sections["exporters"] = _section_error(r_exp)
        data["exporters"] = None
    else:
        try:
            data["exporters"] = await build_exporters(r_exp)
            sections["exporters"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            sections["exporters"] = _section_error(exc)
            data["exporters"] = None

    def build_apps(res):
        rs = netflow_classify.current_rules()
        return {"items": [{"application": a, "category": netflow_classify.category_for(a, rs), "bytes": int(tb),
                           "flows": int(fl), "bytes_in": int(bi), "bytes_out": int(bo)}
                          for a, tb, fl, bi, bo in res.result_rows]}

    data["applications"] = take("applications", r_apps, build_apps)

    def build_ports(res):
        rs = netflow_classify.current_rules()
        return {"items": [{"protocol": int(p), "protocol_name": netflow_classify.protocol_name(int(p)),
                           "port": int(sp), "service": netflow_classify.service_name(int(p), int(sp), rs),
                           "role": role_, "application": app, "bytes": int(tb), "flows": int(fl)}
                          for p, sp, role_, app, tb, fl in res.result_rows]}

    data["ports"] = take("ports", r_ports, build_ports)

    async def build_ifaces(res):
        rows = [(_ip_text(r[0]), int(r[1]), r[2], int(r[3]), int(r[4])) for r in res.result_rows]
        names = await _resolve_interface_names([(e, i) for e, i, *_ in rows if e], db)
        items = []
        for e, i, d, tb, fl in rows:
            m = names.get((e, i)) or {}
            items.append({"exporter": e, "ifindex": i, "direction": d, "name": _interface_display_name(m, i),
                          "if_alias": m.get("if_alias"), "device_hostname": m.get("device_hostname"),
                          "bytes": tb, "flows": fl})
        return {"items": items, "source": "raw",
                "from": _to_iso_utc(raw_plan.effective_from), "to": _to_iso_utc(raw_plan.effective_to)}

    if isinstance(r_ifaces, BaseException):
        sections["interfaces"] = _section_error(r_ifaces)
        data["interfaces"] = None
    else:
        try:
            data["interfaces"] = await build_ifaces(r_ifaces)
            sections["interfaces"] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            sections["interfaces"] = _section_error(exc)
            data["interfaces"] = None

    data["rdns"] = r_rdns if not isinstance(r_rdns, BaseException) else {"status": "failed", "hostname": None}
    sections["rdns"] = {"status": "ok"} if not isinstance(r_rdns, BaseException) else _section_error(r_rdns)
    data["geoip"] = r_geo if not isinstance(r_geo, BaseException) else {"status": "failed"}
    sections["geoip"] = {"status": "ok"} if not isinstance(r_geo, BaseException) else _section_error(r_geo)
    data["groups"] = _groups_section(addr)
    sections["groups"] = {"status": "ok"}

    # PostgreSQL-backed sections, one after another on the request session.
    data["device"] = await _device_section(db, user, addr)
    data["identity"] = await _identity_section(db, user, vis, addr)
    peer_ips = [p["ip"] for p in (data["peers"] or {}).get("items", []) if p.get("ip")]
    data["threat_intel"] = await _ti_section(db, addr, peer_ips)
    data["findings"] = await _findings_section(db, vis, addr)
    data["sections"] = sections
    return data
