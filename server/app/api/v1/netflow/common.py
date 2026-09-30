"""Shared NetFlow API helpers.

Time-window resolution, the ClickHouse query wrappers, protocol/port/application
classification, host enrichment (reverse DNS, IP groups, GeoIP), drill-down
scope filters, rollup eligibility, DSCP labels and exporter/interface name
resolution. Imported by every endpoint module in this package.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Query
from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_ch_client
from app.services import geoip
from app.models.device import Device
from app.models.device_interface import DeviceInterface

import logging

logger = logging.getLogger(__name__)


def _resolve_window(
    hours: int, from_ts: str | None, to_ts: str | None, minutes: int | None = None
) -> tuple[datetime, datetime]:
    """Return (start, end) UTC bounds. Custom from/to wins, then an explicit
    `minutes` (used by the NOC dashboard for sub-day windows), then `hours`."""
    if from_ts and to_ts:
        try:
            start = datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
            end = datetime.fromisoformat(to_ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid from/to timestamp: {exc}") from exc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= start:
            raise HTTPException(status_code=400, detail="`to` must be after `from`")
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    end = datetime.now(timezone.utc)
    start = end - (timedelta(minutes=minutes) if minutes else timedelta(hours=hours))
    return start, end


def _window_seconds(start: datetime, end: datetime) -> int:
    return max(1, int((end - start).total_seconds()))


def _to_iso_utc(value) -> str | None:
    """Format a datetime as RFC3339 UTC (with `Z` suffix) so JS clients don't parse it as local."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.startswith("1970-01-01"):
            return None
        return value if value.endswith("Z") or "+" in value else value + "Z"
    if isinstance(value, datetime):
        if value.year < 2000:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    return str(value)


PROTO_LABELS = {
    1: "ICMP",
    2: "IGMP",
    6: "TCP",
    17: "UDP",
    41: "IPv6",
    47: "GRE",
    50: "ESP",
    51: "AH",
    58: "ICMPv6",
    88: "EIGRP",
    89: "OSPF",
    103: "PIM",
    112: "VRRP",
    115: "L2TP",
    132: "SCTP",
}

COMMON_PORTS = {
    20: "FTP Data",
    21: "FTP",
    22: "SSH",
    23: "Telnet",
    25: "SMTP",
    37: "Time",
    42: "WINS",
    43: "WHOIS",
    49: "TACACS",
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    69: "TFTP",
    79: "Finger",
    80: "HTTP",
    88: "Kerberos",
    102: "MS-RPC/ISO-TSAP",
    110: "POP3",
    111: "RPCbind",
    113: "Ident",
    119: "NNTP",
    123: "NTP",
    135: "MS-RPC",
    137: "NetBIOS-NS",
    138: "NetBIOS-DGM",
    139: "NetBIOS-SSN",
    143: "IMAP",
    161: "SNMP",
    162: "SNMP Trap",
    179: "BGP",
    194: "IRC",
    201: "AppleTalk",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    465: "SMTPS",
    500: "IKE",
    514: "Syslog",
    515: "LPD",
    520: "RIP",
    523: "DB2",
    540: "UUCP",
    546: "DHCPv6",
    547: "DHCPv6",
    554: "RTSP",
    587: "SMTP Submission",
    593: "MS-RPC HTTP",
    623: "IPMI",
    636: "LDAPS",
    646: "LDP",
    873: "rsync",
    902: "VMware",
    989: "FTPS Data",
    990: "FTPS",
    993: "IMAPS",
    995: "POP3S",
    1080: "SOCKS",
    1194: "OpenVPN",
    1352: "Lotus Notes",
    1433: "MSSQL",
    1434: "MSSQL Monitor",
    1521: "Oracle DB",
    1645: "RADIUS",
    1646: "RADIUS",
    1701: "L2TP",
    1719: "H.323",
    1720: "H.323",
    1723: "PPTP",
    1755: "MMS",
    1812: "RADIUS",
    1813: "RADIUS Acct",
    1883: "MQTT",
    1900: "SSDP/UPnP",
    1935: "RTMP",
    2049: "NFS",
    2055: "NetFlow",
    2082: "cPanel",
    2083: "cPanel SSL",
    2181: "ZooKeeper",
    2375: "Docker",
    2376: "Docker TLS",
    2379: "etcd",
    3000: "Grafana/Dev",
    3128: "Squid Proxy",
    3260: "iSCSI",
    3268: "Global Catalog",
    3269: "Global Catalog SSL",
    3306: "MySQL",
    3389: "RDP",
    3478: "STUN/TURN",
    3690: "SVN",
    4369: "EPMD",
    4500: "IPsec NAT-T",
    4789: "VXLAN",
    5004: "RTP",
    5005: "RTP",
    5060: "SIP",
    5061: "SIP TLS",
    5222: "XMPP",
    5269: "XMPP Server",
    5353: "mDNS",
    5432: "PostgreSQL",
    5601: "Kibana",
    5666: "NRPE",
    5672: "AMQP",
    5900: "VNC",
    5938: "TeamViewer",
    5985: "WinRM",
    5986: "WinRM SSL",
    6343: "sFlow",
    6379: "Redis",
    6443: "Kubernetes API",
    6514: "Syslog TLS",
    6660: "IRC",
    7001: "WebLogic",
    8000: "HTTP Alt",
    8006: "Proxmox",
    8080: "HTTP Alt",
    8086: "InfluxDB",
    8123: "ClickHouse HTTP",
    8443: "HTTPS Alt",
    8554: "RTSP Alt",
    8883: "MQTT TLS",
    9000: "ClickHouse/PHP-FPM",
    9042: "Cassandra",
    9090: "Prometheus",
    9092: "Kafka",
    9100: "Node Exporter",
    9200: "Elasticsearch",
    9300: "Elasticsearch",
    9418: "Git",
    10050: "Zabbix Agent",
    11211: "Memcached",
    15672: "RabbitMQ Mgmt",
    16384: "RTP",
    19302: "STUN",
    27017: "MongoDB",
    50000: "SAP",
}


# Maps ports to higher-level "applications" for the dashboard donut.
APPLICATION_BUCKETS = [
    ("Web (HTTP/HTTPS)", {80, 443, 8000, 8080, 8443, 3000, 8006}),
    ("Streaming Media", {554, 1755, 1935, 5004, 5005, 8554}),
    ("DNS", {53, 5353}),
    ("Email (SMTP/IMAP/POP)", {25, 110, 143, 465, 587, 993, 995}),
    ("File Transfer (FTP/SMB/NFS)", {20, 21, 69, 115, 445, 989, 990, 2049, 873}),
    ("Remote Access (SSH/RDP/VNC)", {22, 23, 3389, 5900, 5938, 5985, 5986}),
    ("Database", {1433, 1434, 1521, 3306, 5432, 6379, 9042, 27017, 11211, 9200, 9300, 8123, 9000}),
    ("VoIP / Video Conf.", {1719, 1720, 3478, 3479, 5060, 5061, 16384, 19302}),
    ("Messaging / Queue", {1883, 5222, 5269, 5672, 8883, 9092, 15672, 4369}),
    ("Directory / Auth", {88, 389, 636, 49, 1645, 1646, 1812, 1813, 3268, 3269}),
    ("VPN / Tunneling", {500, 1194, 1701, 1723, 4500, 4789}),
    ("Container / Orchestration", {2375, 2376, 2379, 6443, 10250, 2181}),
    ("Observability", {9090, 9100, 5601, 8086, 10050, 5666, 6514}),
    ("Network Mgmt", {123, 161, 162, 514, 6343, 2055, 179, 520}),
]


def _application_for_port(port: int) -> str:
    if port == 0:
        return "Other"
    for name, ports in APPLICATION_BUCKETS:
        if port in ports:
            return name
    if 1 <= port <= 1023:
        return "System Services"
    return "Other"


# Portless protocols that still identify an application. Without this, ESP/GRE
# tunnel traffic (often the biggest slice on a site-to-site network) all lands
# in "Other" and the applications donut becomes meaningless.
_PROTO_APPLICATIONS = {
    47: "VPN / Tunneling",   # GRE
    50: "VPN / Tunneling",   # ESP
    51: "VPN / Tunneling",   # AH
    1: "ICMP",
    58: "ICMP",              # ICMPv6
}


def _application_for_flow(port: int, proto: int) -> str:
    by_proto = _PROTO_APPLICATIONS.get(int(proto))
    if by_proto:
        return by_proto
    return _application_for_port(int(port))


def _query_sync(sql: str, params: dict):
    """Blocking ClickHouse query on the calling thread's own client session."""
    try:
        return get_ch_client().query(sql, parameters=params)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse query failed: {exc}") from exc


async def _query(sql: str, params: dict):
    """Run a ClickHouse query in the threadpool so a slow scan never blocks the
    event loop (the dashboard fires ~15 of these concurrently)."""
    return await asyncio.to_thread(_query_sync, sql, params)


def _protocol_name(proto: int) -> str:
    return PROTO_LABELS.get(int(proto), f"Protocol {proto}")


def _port_name(port: int, proto: int | None = None) -> str:
    if port == 0:
        return "none"
    label = COMMON_PORTS.get(int(port))
    if label:
        return label
    if proto is not None and proto == 17:
        return f"UDP/{port}"
    if proto is not None and proto == 6:
        return f"TCP/{port}"
    return str(port)


def _port_class(port: int) -> str:
    if port == 0:
        return "No transport port"
    if 1 <= port <= 1023:
        return "Well-known system port"
    if 1024 <= port <= 49151:
        return "Registered/user port"
    return "Dynamic/private port"


def _port_summary(port: int, proto: int | None = None, bytes_: int = 0, packets: int = 0, flows: int = 0) -> dict:
    return {
        "port": int(port),
        "service": _port_name(int(port), proto),
        "application": _application_for_port(int(port)),
        "bytes": int(bytes_ or 0),
        "packets": int(packets or 0),
        "flows": int(flows or 0),
    }


# ── Reverse-DNS enrichment (Phase 2) ──────────────────────────────────────────
# Best-effort PTR lookups for the small set of IPs shown in top-N views. TTL
# cached; bounded by an overall deadline so a slow resolver never stalls the API.
# Opt-in per request (?resolve=1) because it adds latency and outbound DNS.
_RDNS_TTL = 3600.0
_rdns_cache: dict[str, tuple[float, str | None]] = {}
_rdns_lock = threading.Lock()


def _rdns_one(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror, socket.gaierror):
        return None


def _resolve_hostnames(ips: list[str], deadline: float = 2.0) -> dict[str, str | None]:
    now = time.monotonic()
    out: dict[str, str | None] = {}
    todo: list[str] = []
    with _rdns_lock:
        for ip in dict.fromkeys(ips):  # de-dup, preserve order
            ent = _rdns_cache.get(ip)
            if ent and ent[0] > now:
                out[ip] = ent[1]
            else:
                todo.append(ip)
    if todo:
        results: dict[str, str | None] = {ip: None for ip in todo}
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(todo)))
        futs = {ex.submit(_rdns_one, ip): ip for ip in todo}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=deadline):
                results[futs[fut]] = fut.result()
        except concurrent.futures.TimeoutError:
            pass  # slow lookups finish in the background and populate the cache
        ex.shutdown(wait=False, cancel_futures=True)
        exp = time.monotonic() + _RDNS_TTL
        with _rdns_lock:
            for ip, host in results.items():
                _rdns_cache[ip] = (exp, host)
                out[ip] = host
    return out


# ── User-defined IP groups / sites (Phase 2) ──────────────────────────────────
# Appliance-local config (NOT shipped in releases) mapping named groups to CIDRs:
#   [{"name": "Datacenter A", "cidrs": ["10.0.0.0/16", "10.1.0.0/16"]}, ...]
_IP_GROUPS_PATH = os.getenv("NETFLOW_IP_GROUPS_FILE", "/opt/zenplus/data/netflow-ip-groups.json")
_ip_groups_cache: dict = {"mtime": None, "groups": []}
_ip_groups_lock = threading.Lock()


def _load_ip_groups() -> list[tuple[str, list]]:
    """Returns [(name, [ip_network, ...])], reloaded when the JSON file changes."""
    try:
        mtime = os.stat(_IP_GROUPS_PATH).st_mtime
    except OSError:
        return []
    with _ip_groups_lock:
        if _ip_groups_cache["mtime"] == mtime:
            return _ip_groups_cache["groups"]
        try:
            raw = json.loads(open(_IP_GROUPS_PATH).read())
        except (OSError, ValueError):
            return _ip_groups_cache["groups"]
        groups: list[tuple[str, list]] = []
        for g in raw if isinstance(raw, list) else []:
            name = str(g.get("name", "")).strip()
            nets = []
            for c in g.get("cidrs", []):
                try:
                    nets.append(ipaddress.ip_network(str(c), strict=False))
                except ValueError:
                    continue
            if name and nets:
                groups.append((name, nets))
        _ip_groups_cache["mtime"] = mtime
        _ip_groups_cache["groups"] = groups
        return groups


def _classify_ip(ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for name, nets in _load_ip_groups():
        if any(addr in n for n in nets):
            return name
    return None


def _group_predicate(name: str) -> str | None:
    """ClickHouse predicate: a flow touches this group on src OR dst. Returns
    None if the group is unknown. CIDRs are validated, so inlining is safe."""
    nets = next((nets for gname, nets in _load_ip_groups() if gname == name), None)
    if not nets:
        return None
    terms = []
    for n in nets:
        c = str(n)
        terms.append(f"isIPAddressInRange(toString(src_addr), '{c}')")
        terms.append(f"isIPAddressInRange(toString(dst_addr), '{c}')")
    return "(" + " OR ".join(terms) + ")"


async def _enrich_hosts_async(rows: list[dict], resolve: bool) -> list[dict]:
    """Like _enrich_hosts, but keeps blocking PTR lookups off the event loop."""
    if resolve:
        return await asyncio.to_thread(_enrich_hosts, rows, True)
    return _enrich_hosts(rows, False)


def _enrich_hosts(rows: list[dict], resolve: bool) -> list[dict]:
    """Attach `group` (IP-group membership), GeoIP `country`/`asn` (no-op without a
    DB), and — when resolve is set — reverse-DNS `hostname` to each per-IP row."""
    for row in rows:
        ip = str(row.get("ip", ""))
        row["group"] = _classify_ip(ip)
        g = geoip.enrich(ip)
        row["country"] = g["country"]
        row["country_name"] = g["country_name"]
        row["asn"] = g["asn"]
        row["as_name"] = g["as_name"]
    if resolve:
        hosts = _resolve_hostnames([str(row["ip"]) for row in rows if row.get("ip")])
        for row in rows:
            row["hostname"] = hosts.get(str(row.get("ip")))
    else:
        for row in rows:
            row["hostname"] = None
    return rows


def _exporter_filter(exporter: str | None) -> tuple[str, dict]:
    """Returns (extra SQL WHERE-clause fragment, extra params) for an optional exporter filter."""
    if exporter:
        return " AND exporter_ip = %(exporter)s", {"exporter": exporter}
    return "", {}


def _interface_filter(iface: int | None) -> tuple[str, dict]:
    """Filter flows that touched the given SNMP ifIndex on either ingress or egress."""
    if iface is not None:
        return " AND (input_snmp = %(iface)s OR output_snmp = %(iface)s)", {"iface": int(iface)}
    return "", {}


# Used by the network-class filter to translate the human label back to a CIDR.
_NET_CLASS_RANGES = {
    "Private 10/8": "10.0.0.0/8",
    "Private 172.16/12": "172.16.0.0/12",
    "Private 192.168/16": "192.168.0.0/16",
    "Loopback": "127.0.0.0/8",
    "Link-local": "169.254.0.0/16",
    "Multicast": "224.0.0.0/4",
    "CGNAT": "100.64.0.0/10",
}


def scope_params(
    exporter: str | None = Query(default=None),
    iface: int | None = Query(default=None),
    talker: str | None = Query(default=None),
    protocol: int | None = Query(default=None),
    dscp_filter: int | None = Query(default=None, alias="dscp"),
    application: str | None = Query(default=None, alias="app"),
    net_class: str | None = Query(default=None, alias="netclass"),
    tcp_flag: str | None = Query(default=None, alias="tcpflag"),
    hour: int | None = Query(default=None),
    dow: int | None = Query(default=None),
    group: str | None = Query(default=None),
) -> dict:
    """FastAPI dependency: collects every drill-down filter into a single dict the endpoint can splat into _scope()."""
    return {
        "exporter": exporter,
        "iface": iface,
        "talker": talker,
        "protocol": protocol,
        "dscp_filter": dscp_filter,
        "application": application,
        "net_class": net_class,
        "tcp_flag": tcp_flag,
        "hour": hour,
        "dow": dow,
        "group": group,
    }


ROLLUP_TABLE = "zenplus.flow_conversations_1h"
# Below this span the rollup's hour-bucket edges would visibly skew the
# numbers, and raw scans are fast enough anyway.
ROLLUP_MIN_SECONDS = 72 * 3600


def _rollup_eligible(start: datetime, end: datetime, scope: dict) -> bool:
    """True when the hourly conversation rollup can serve this request.

    Needs a long window (short ones stay on raw for exact edges and speed),
    and no drill-down filter that requires per-flow columns the rollup does
    not keep: ifIndex equality (only uniq-array states survive), DSCP (tos is
    dropped), and TCP flag patterns (per-flow flags are OR-merged).
    """
    if _window_seconds(start, end) < ROLLUP_MIN_SECONDS:
        return False
    return (
        scope.get("iface") is None
        and scope.get("dscp_filter") is None
        and scope.get("tcp_flag") is None
    )


def _floor_hour(dt: datetime) -> datetime:
    """Widen a window edge to its hour bucket so the rollup drops no data."""
    return dt.replace(minute=0, second=0, microsecond=0)


QOS_ROLLUP_TABLE = "zenplus.flow_qos_1h"


def _qos_rollup_eligible(start: datetime, end: datetime, scope: dict) -> bool:
    """True when the hourly QoS rollup can serve this request.

    That table keys on exporter/protocol/tos/tcp_flags only, so DSCP and
    flag-pattern filters are fine, but any filter needing addresses, ports or
    ifIndexes forces the raw path.
    """
    if _window_seconds(start, end) < ROLLUP_MIN_SECONDS:
        return False
    return all(
        scope.get(k) is None
        for k in ("iface", "talker", "application", "net_class", "group")
    )


def _scope(
    exporter: str | None = None,
    iface: int | None = None,
    talker: str | None = None,
    protocol: int | None = None,
    dscp_filter: int | None = None,
    application: str | None = None,
    net_class: str | None = None,
    tcp_flag: str | None = None,
    hour: int | None = None,
    dow: int | None = None,
    group: str | None = None,
    table: str = "flow_records",
) -> tuple[str, dict]:
    """Build a combined WHERE-clause fragment for every supported drill-down dimension."""
    # Defence in depth (BUG-01): a single corrupt/garbage flow with an
    # astronomically large byte count must never dominate an aggregate. The
    # collector now prevents and drops such rows at ingest, but we also bound
    # them out of every API aggregate that uses this shared scope. 1e12 bytes
    # (~1 TB) is far above any plausible single flow within a poll window.
    # Qualified as flow_records.bytes so it resolves to the column, never to a
    # "sum(bytes) AS bytes" SELECT alias (which would raise ILLEGAL_AGGREGATION).
    # Stored bytes are sampling-adjusted (collector multiplies by the 1-in-N
    # rate), so the guard divides by sampling_interval to test the unsampled-
    # equivalent value — legitimate sampled flows are kept, corruption is not.
    # The rollup table has no sampling_interval and was ingested through the
    # same guard (see migrate-071's MV), so it skips the predicate.
    parts: list[str] = []
    if table == "flow_records":
        parts.append("flow_records.bytes / greatest(flow_records.sampling_interval, 1) <= 1000000000000")
    params: dict = {}
    if exporter:
        parts.append("exporter_ip = %(exporter)s"); params["exporter"] = exporter
    if iface is not None:
        parts.append("(input_snmp = %(iface)s OR output_snmp = %(iface)s)"); params["iface"] = int(iface)
    if talker:
        parts.append("(toString(src_addr) = %(talker)s OR toString(dst_addr) = %(talker)s)"); params["talker"] = talker
    if protocol is not None:
        parts.append("protocol = %(proto_eq)s"); params["proto_eq"] = int(protocol)
    if dscp_filter is not None:
        parts.append("bitShiftRight(tos, 2) = %(dscp_eq)s"); params["dscp_eq"] = int(dscp_filter)
    if application:
        # Protocol-derived buckets (tunnels, ICMP) match on protocol; port
        # buckets exclude those protocols so the filters partition the traffic
        # the same way /applications does.
        app_protos = sorted(p for p, name in _PROTO_APPLICATIONS.items() if name == application)
        proto_excl = ",".join(str(p) for p in sorted(_PROTO_APPLICATIONS))
        port_set = next((ports for name, ports in APPLICATION_BUCKETS if name == application), None)
        if port_set:
            ports_sql = ",".join(str(int(p)) for p in port_set)
            clause = f"dst_port IN ({ports_sql}) AND protocol NOT IN ({proto_excl})"
            if app_protos:  # e.g. "VPN / Tunneling" is both a port bucket and GRE/ESP/AH
                clause = f"({clause} OR protocol IN ({','.join(str(p) for p in app_protos)}))"
            parts.append(clause)
        elif app_protos:
            parts.append(f"protocol IN ({','.join(str(p) for p in app_protos)})")
        elif application == "System Services":
            all_bucket_ports = set().union(*(ports for _, ports in APPLICATION_BUCKETS))
            sys_ports = sorted(set(range(1, 1024)) - all_bucket_ports)
            ports_sql = ",".join(str(p) for p in sys_ports)
            parts.append(f"dst_port BETWEEN 1 AND 1023 AND dst_port IN ({ports_sql}) AND protocol NOT IN ({proto_excl})")
        elif application == "Other":
            all_bucket_ports = set().union(*(ports for _, ports in APPLICATION_BUCKETS))
            ports_sql = ",".join(str(p) for p in sorted(all_bucket_ports))
            parts.append(f"(dst_port = 0 OR dst_port > 1023) AND dst_port NOT IN ({ports_sql}) AND protocol NOT IN ({proto_excl})")
    if net_class:
        rng = _NET_CLASS_RANGES.get(net_class)
        if rng:
            parts.append("(isIPAddressInRange(toString(src_addr), %(nc_cidr)s) OR isIPAddressInRange(toString(dst_addr), %(nc_cidr)s))")
            params["nc_cidr"] = rng
        elif net_class == "Public":
            cidrs = list(_NET_CLASS_RANGES.values())
            src_neg = " AND ".join(f"NOT isIPAddressInRange(toString(src_addr), '{c}')" for c in cidrs)
            dst_neg = " AND ".join(f"NOT isIPAddressInRange(toString(dst_addr), '{c}')" for c in cidrs)
            parts.append(f"(({src_neg}) OR ({dst_neg}))")
    if tcp_flag:
        flag_clause = {
            "syn_only": "protocol = 6 AND bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0",
            "ack_only": "protocol = 6 AND bitAnd(tcp_flags, 16) != 0 AND bitAnd(tcp_flags, 2) = 0",
            "rst": "protocol = 6 AND bitAnd(tcp_flags, 4) != 0",
            "fin": "protocol = 6 AND bitAnd(tcp_flags, 1) != 0",
            "psh": "protocol = 6 AND bitAnd(tcp_flags, 8) != 0",
            "urg": "protocol = 6 AND bitAnd(tcp_flags, 32) != 0",
            "no_flags": "protocol = 6 AND tcp_flags = 0",
        }.get(tcp_flag)
        if flag_clause:
            parts.append(f"({flag_clause})")
    if hour is not None:
        parts.append("toHour(timestamp) = %(hour_eq)s"); params["hour_eq"] = int(hour)
    if dow is not None:
        parts.append("toDayOfWeek(timestamp) = %(dow_eq)s"); params["dow_eq"] = int(dow)
    if group:
        pred = _group_predicate(group)
        if pred:
            parts.append(pred)

    return ((" AND " + " AND ".join(parts)) if parts else "", params)


async def _exporter_device_overrides(
    db: AsyncSession, exporter_ips: list[str]
) -> dict[str, tuple]:
    """{exporter_ip -> (device_id, hostname)} from netflow_exporter_devices.

    Tolerates the table not existing yet so an appliance running this code
    before migrate-054 keeps the previous behaviour instead of erroring.
    """
    if not exporter_ips:
        return {}
    try:
        rows = (await db.execute(
            text("""
                SELECT host(m.exporter_ip) AS exporter_ip, d.id, d.hostname
                FROM netflow_exporter_devices m
                JOIN devices d ON d.id = m.device_id
                WHERE host(m.exporter_ip) = ANY(:ips)
            """),
            {"ips": exporter_ips},
        )).all()
    except Exception:
        logger.debug("exporter->device mapping lookup skipped", exc_info=True)
        return {}
    return {r.exporter_ip: (r.id, r.hostname) for r in rows}


async def _resolve_interface_names(rows: list[tuple], db: AsyncSession) -> dict[tuple[str, int], dict]:
    """Build a {(exporter_ip, ifindex) -> {if_name, if_alias, if_speed}} map from device_interfaces."""
    keys = {(str(r[0]), int(r[1])) for r in rows}
    if not keys:
        return {}
    exporter_ips = list({ip for ip, _ in keys})
    # `Device.ip_address` is INET; compare via text cast so we can use a plain str list.
    devices_q = await db.execute(
        select(Device.id, Device.ip_address, Device.hostname).where(
            cast(Device.ip_address, String).in_([f"{ip}/32" for ip in exporter_ips] + exporter_ips)
        )
    )
    ip_to_device = {str(row.ip_address).split("/")[0]: (row.id, row.hostname) for row in devices_q.all()}

    # Exporters whose source IP is not the device's management IP (loopback or
    # WAN-sourced export) never match above; fall back to the operator-defined
    # mapping so their ifIndex values still resolve to real interface names.
    unmapped = [ip for ip in exporter_ips if ip not in ip_to_device]
    if unmapped:
        ip_to_device.update(await _exporter_device_overrides(db, unmapped))

    if not ip_to_device:
        return {}
    device_ids = [d[0] for d in ip_to_device.values()]
    ifs_q = await db.execute(
        select(
            DeviceInterface.device_id,
            DeviceInterface.if_index,
            DeviceInterface.if_name,
            DeviceInterface.if_descr,
            DeviceInterface.if_alias,
            DeviceInterface.if_speed,
        ).where(DeviceInterface.device_id.in_(device_ids))
    )
    by_device: dict = {}
    for row in ifs_q.all():
        by_device.setdefault(row.device_id, {})[row.if_index] = {
            "if_name": row.if_name,
            "if_descr": row.if_descr,
            "if_alias": row.if_alias,
            "if_speed": int(row.if_speed) if row.if_speed else None,
        }
    out: dict[tuple[str, int], dict] = {}
    for ip, idx in keys:
        dev = ip_to_device.get(ip)
        if not dev:
            continue
        ifs = by_device.get(dev[0]) or {}
        meta = ifs.get(idx)
        if meta:
            out[(ip, idx)] = {**meta, "device_hostname": dev[1]}
    return out


def _interface_display_name(meta: dict, ifindex: int) -> str:
    return meta.get("if_name") or meta.get("if_descr") or meta.get("if_alias") or f"ifIndex {ifindex}"


def _interface_payload(exporter_ip: str, ifindex: int, meta: dict | None = None) -> dict:
    meta = meta or {}
    return {
        "exporter_ip": exporter_ip,
        "ifindex": ifindex,
        "if_name": meta.get("if_name"),
        "if_descr": meta.get("if_descr"),
        "if_alias": meta.get("if_alias"),
        "if_speed": meta.get("if_speed"),
        "device_hostname": meta.get("device_hostname"),
        "display_name": _interface_display_name(meta, ifindex),
    }


# RFC mapping: DSCP code-point -> friendly name. Most operators only really see
# a handful of these; the rest fall back to the raw "DSCP N" label.
DSCP_LABELS = {
    0: "BE (Best Effort)",
    8: "CS1",
    10: "AF11",
    12: "AF12",
    14: "AF13",
    16: "CS2",
    18: "AF21",
    20: "AF22",
    22: "AF23",
    24: "CS3",
    26: "AF31",
    28: "AF32",
    30: "AF33",
    32: "CS4",
    34: "AF41",
    36: "AF42",
    38: "AF43",
    40: "CS5",
    46: "EF (Voice)",
    48: "CS6 (Network Control)",
    56: "CS7",
}


def _dscp_label(dscp: int) -> str:
    return DSCP_LABELS.get(int(dscp), f"DSCP {dscp}")
