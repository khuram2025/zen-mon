"""The single NetFlow traffic classifier (NF-20 / CR-12).

Every NetFlow surface -- breakdowns, forensics rows, the ``app=`` and
``service_port=`` filters, conversations, alerts and reports -- labels a flow
through this module, in Python (``classify``) or in ClickHouse SQL
(``classification_sql`` and friends). Both are generated from one ordered
rule list, and the SQL mirrors the Python decision step for step, so a flow
lands in the same service and application whichever path counts it
(``tests/test_netflow_classify.py`` checks this on random tuples).

What a flow is classified into:

* **service port** -- the port that identifies the service, on whichever
  side it is. A response from 443 to an ephemeral client port is HTTPS just
  like the request; the old dst-port-only rule called it ``TCP/51234`` and
  filed most bytes of every download under "Other".
* **direction** -- ``request`` (the service port is the destination),
  ``response`` (it is the source), ``unknown`` (cannot tell), ``n/a``
  (portless protocol).
* **application** / **category** -- from, in order: the exporter's own
  NBAR2/App-ID (``flow_app_names``), the rule that matched the service port
  or a protocol/address rule (whichever has precedence), then "System
  Services" (an unmatched well-known port) or "Other".
* **basis** / **confidence** -- how the answer was reached, so the UI can say
  "inferred from server port 443" and grade it instead of presenting a guess
  as fact.

Service-port inference for ports-carrying protocols (TCP, UDP, SCTP, DCCP,
UDP-Lite):

1. exactly one side matches a port rule -> that side (confidence high);
2. both match -> the non-ephemeral side, else the lower port (medium) --
   443 -> 50000 is HTTPS, not SAP; 443 -> 3306 (a PAT'd client port) is HTTPS;
3. neither -> a well-known side (1-1023; both: the lower) (low), or a
   registered side (1024 .. ephemeral_min-1) talking to an ephemeral one
   (low); otherwise unknown.

Port rules are protocol-aware (22/tcp is SSH, 22/udp is not; 443/udp is
QUIC). Portless protocols get ``port_applicable = False`` and no service
port: ESP is "VPN / Tunneling" from the protocol itself, with no claim about
the applications inside the tunnel. ICMP/ICMPv6 carry ``type * 256 + code``
in ``dst_port``; it is decoded to a label ("Echo request", "Port
unreachable") instead of being read as a port.

Rules come from three places: the built-ins below (converted from the old
``COMMON_PORTS``/``APPLICATION_BUCKETS``), operator rules in
``netflow_app_rules`` (migrate-138; B7's settings API edits them), and
exporter-announced application names in ``zenplus.flow_app_names``. The
merged set is cached (``CACHE_TTL_S``); ``load_rules()`` refreshes it and
``invalidate_rules_cache()`` forces the next load. Rule precedence:
``priority`` ascending (operator default 100, built-ins 1000), operator
rules before built-ins at equal priority, then the narrower port range.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.netflow_groups import Network, networks_sql, ip_in, parse_network

logger = logging.getLogger(__name__)

CACHE_TTL_S = 60.0
BUILTIN_PRIORITY = 1000
DEFAULT_EPHEMERAL_MIN = 32768
SETTINGS_KEY = "netflow_classification"

# IP protocols whose transport header carries ports.
PORT_PROTOCOLS = frozenset({6, 17, 33, 132, 136})
ICMP_PROTOCOLS = frozenset({1, 58})

IP_PROTOCOL_NAMES = {
    1: "ICMP", 2: "IGMP", 4: "IP-in-IP", 6: "TCP", 17: "UDP", 33: "DCCP",
    41: "IPv6", 47: "GRE", 50: "ESP", 51: "AH", 58: "ICMPv6", 88: "EIGRP",
    89: "OSPF", 103: "PIM", 112: "VRRP", 115: "L2TP", 132: "SCTP", 136: "UDP-Lite",
}


def protocol_name(proto: int) -> str:
    return IP_PROTOCOL_NAMES.get(int(proto), f"Protocol {int(proto)}")


# Application names shared with the pre-review API, so saved filters,
# alert rules and report presets that name them keep working.
APP_WEB = "Web (HTTP/HTTPS)"
APP_VPN = "VPN / Tunneling"
APP_ICMP = "ICMP"
APP_NETMGMT = "Network Mgmt"
APP_SYSTEM = "System Services"
APP_OTHER = "Other"

# Built-in port rules: application -> {port: service label}. Ports not listed
# in TRANSPORT default to TCP only.
_BUILTIN_APPS: list[tuple[str, str, dict[int, str]]] = [
    (APP_WEB, "Web", {80: "HTTP", 443: "HTTPS", 8000: "HTTP Alt", 8080: "HTTP Alt",
                      8443: "HTTPS Alt", 3000: "Grafana/Dev", 8006: "Proxmox"}),
    ("Streaming Media", "Media", {554: "RTSP", 1755: "MMS", 1935: "RTMP", 5004: "RTP",
                                  5005: "RTP", 8554: "RTSP Alt"}),
    ("DNS", "Infrastructure", {53: "DNS", 5353: "mDNS"}),
    ("Email (SMTP/IMAP/POP)", "Email", {25: "SMTP", 110: "POP3", 143: "IMAP", 465: "SMTPS",
                                        587: "SMTP Submission", 993: "IMAPS", 995: "POP3S"}),
    ("File Transfer (FTP/SMB/NFS)", "File transfer", {20: "FTP Data", 21: "FTP", 69: "TFTP",
                                                      115: "SFTP", 445: "SMB", 989: "FTPS Data",
                                                      990: "FTPS", 2049: "NFS", 873: "rsync"}),
    ("Remote Access (SSH/RDP/VNC)", "Remote access", {22: "SSH", 23: "Telnet", 3389: "RDP",
                                                      5900: "VNC", 5938: "TeamViewer",
                                                      5985: "WinRM", 5986: "WinRM SSL"}),
    ("Database", "Database", {1433: "MSSQL", 1434: "MSSQL Monitor", 1521: "Oracle DB",
                              3306: "MySQL", 5432: "PostgreSQL", 6379: "Redis",
                              9042: "Cassandra", 27017: "MongoDB", 11211: "Memcached",
                              9200: "Elasticsearch", 9300: "Elasticsearch",
                              8123: "ClickHouse HTTP", 9000: "ClickHouse/PHP-FPM"}),
    ("VoIP / Video Conf.", "Collaboration", {1719: "H.323", 1720: "H.323", 3478: "STUN/TURN",
                                             3479: "STUN/TURN", 5060: "SIP", 5061: "SIP TLS",
                                             16384: "RTP", 19302: "STUN"}),
    ("Messaging / Queue", "Messaging", {1883: "MQTT", 5222: "XMPP", 5269: "XMPP Server",
                                        5672: "AMQP", 8883: "MQTT TLS", 9092: "Kafka",
                                        15672: "RabbitMQ Mgmt", 4369: "EPMD"}),
    ("Directory / Auth", "Infrastructure", {88: "Kerberos", 389: "LDAP", 636: "LDAPS",
                                            49: "TACACS", 1645: "RADIUS", 1646: "RADIUS",
                                            1812: "RADIUS", 1813: "RADIUS Acct",
                                            3268: "Global Catalog", 3269: "Global Catalog SSL"}),
    (APP_VPN, "Tunnel", {500: "IKE", 1194: "OpenVPN", 1701: "L2TP", 1723: "PPTP",
                         4500: "IPsec NAT-T", 4789: "VXLAN"}),
    ("Container / Orchestration", "Infrastructure", {2375: "Docker", 2376: "Docker TLS",
                                                     2379: "etcd", 6443: "Kubernetes API",
                                                     10250: "Kubelet", 2181: "ZooKeeper"}),
    ("Observability", "Infrastructure", {9090: "Prometheus", 9100: "Node Exporter",
                                         5601: "Kibana", 8086: "InfluxDB",
                                         10050: "Zabbix Agent", 5666: "NRPE",
                                         6514: "Syslog TLS"}),
    (APP_NETMGMT, "Network control", {123: "NTP", 161: "SNMP", 162: "SNMP Trap",
                                      514: "Syslog", 6343: "sFlow", 2055: "NetFlow",
                                      179: "BGP", 520: "RIP"}),
    # Named services with no application bucket keep the pre-review answer:
    # System Services below 1024, Other above.
    (APP_SYSTEM, "Other", {37: "Time", 42: "WINS", 43: "WHOIS", 67: "DHCP", 68: "DHCP",
                           79: "Finger", 102: "MS-RPC/ISO-TSAP", 111: "RPCbind",
                           113: "Ident", 119: "NNTP", 135: "MS-RPC", 137: "NetBIOS-NS",
                           138: "NetBIOS-DGM", 139: "NetBIOS-SSN", 194: "IRC",
                           201: "AppleTalk", 515: "LPD", 523: "DB2", 540: "UUCP",
                           546: "DHCPv6", 547: "DHCPv6", 593: "MS-RPC HTTP", 623: "IPMI",
                           646: "LDP", 902: "VMware"}),
    (APP_OTHER, "Other", {1080: "SOCKS", 1352: "Lotus Notes", 1900: "SSDP/UPnP",
                          2082: "cPanel", 2083: "cPanel SSL", 3128: "Squid Proxy",
                          3260: "iSCSI", 3690: "SVN", 6660: "IRC", 7001: "WebLogic",
                          9418: "Git", 50000: "SAP"}),
]

_TCP, _UDP = frozenset({6}), frozenset({17})
_BOTH = frozenset({6, 17})
_UDP_ONLY = {67, 68, 69, 123, 137, 138, 161, 162, 500, 520, 546, 547, 623, 1434, 1645,
             1646, 1701, 1719, 1812, 1813, 1900, 2055, 4500, 4789, 5004, 5005, 5353,
             6343, 16384, 19302}
_TCP_AND_UDP = {53, 88, 111, 389, 514, 902, 1194, 2049, 3389, 3478, 3479, 5060, 5938, 11211}
# Same port, different service per transport.
_UDP_VARIANTS: dict[int, tuple[str, str]] = {443: ("QUIC", APP_WEB)}

# Built-in protocol rules for portless protocols.
_BUILTIN_PROTOCOLS: list[tuple[int, str]] = [
    (1, APP_ICMP), (58, APP_ICMP),
    (47, APP_VPN), (50, APP_VPN), (51, APP_VPN), (4, APP_VPN), (41, APP_VPN), (115, APP_VPN),
    (2, APP_NETMGMT), (88, APP_NETMGMT), (89, APP_NETMGMT), (103, APP_NETMGMT),
    (112, APP_NETMGMT),
]

_BUILTIN_CATEGORY = {app: cat for app, cat, _ in _BUILTIN_APPS}
_BUILTIN_CATEGORY[APP_ICMP] = "Network control"

# NBAR2 / App-ID names (lower case) that denote exactly one built-in
# application, so the same protocol is not listed twice ("icmp" next to
# "ICMP", "ipsec" next to "VPN / Tunneling") in breakdowns, filters and
# reports. Any other exporter name is kept as announced (a product such as
# "youtube" has no built-in twin) and only folded case-insensitively onto an
# existing built-in or operator application of the same name. The basis
# "exporter" still says where a classification came from.
NBAR_EQUIVALENTS: dict[str, str] = {
    **dict.fromkeys(("icmp", "ipv6-icmp", "icmpv6", "icmp6"), APP_ICMP),
    **dict.fromkeys(("ipsec", "esp", "ah", "gre", "isakmp", "ike", "ipinip", "ipv6inip", "l2tp", "pptp",
                     "openvpn", "vxlan"), APP_VPN),
    **dict.fromkeys(("http", "https", "ssl", "tls", "secure-http", "http-alt", "quic"), APP_WEB),
    **dict.fromkeys(("dns", "mdns", "domain"), "DNS"),
    **dict.fromkeys(("ssh", "telnet", "rdp", "ms-wbt-server", "vnc", "winrm"), "Remote Access (SSH/RDP/VNC)"),
    **dict.fromkeys(("cifs", "smb", "netbios-ssn", "ftp", "ftp-data", "ftps", "ftps-data", "tftp", "nfs", "sftp",
                     "rsync"), "File Transfer (FTP/SMB/NFS)"),
    **dict.fromkeys(("smtp", "secure-smtp", "submission", "pop3", "secure-pop3", "imap", "secure-imap"),
                    "Email (SMTP/IMAP/POP)"),
    **dict.fromkeys(("ldap", "secure-ldap", "kerberos", "radius", "tacacs"), "Directory / Auth"),
    **dict.fromkeys(("sip", "sip-tls", "h323", "rtp", "rtcp", "stun"), "VoIP / Video Conf."),
    **dict.fromkeys(("ntp", "snmp", "snmptrap", "syslog", "bgp", "ospf", "eigrp", "rip", "netflow", "sflow",
                     "igmp", "vrrp", "pim"), APP_NETMGMT),
    **dict.fromkeys(("mysql", "sqlserver", "ms-sql-s", "oracle", "postgresql", "redis", "mongodb"), "Database"),
}


def _canonical_application(raw: str, by_lower: dict[str, str]) -> str:
    """The application an exporter name stands for (see NBAR_EQUIVALENTS)."""
    key = raw.strip().lower()
    target = NBAR_EQUIVALENTS.get(key)
    if target is not None and target.lower() in by_lower:
        return by_lower[target.lower()]
    return by_lower.get(key, raw.strip())

# ICMP type/code labels (dst_port = type * 256 + code).
_ICMP4_TYPES = {
    0: "Echo reply", 3: "Destination unreachable", 4: "Source quench", 5: "Redirect",
    8: "Echo request", 9: "Router advertisement", 10: "Router solicitation",
    11: "Time exceeded", 12: "Parameter problem", 13: "Timestamp", 14: "Timestamp reply",
}
_ICMP4_UNREACH = {
    0: "Network unreachable", 1: "Host unreachable", 2: "Protocol unreachable",
    3: "Port unreachable", 4: "Fragmentation needed", 9: "Network prohibited",
    10: "Host prohibited", 13: "Administratively prohibited",
}
_ICMP4_TIME_EXCEEDED = {0: "TTL exceeded", 1: "Fragment reassembly time exceeded"}
_ICMP6_TYPES = {
    1: "Destination unreachable", 2: "Packet too big", 3: "Time exceeded",
    4: "Parameter problem", 128: "Echo request", 129: "Echo reply",
    130: "Multicast listener query", 131: "Multicast listener report",
    132: "Multicast listener done", 133: "Router solicitation", 134: "Router advertisement",
    135: "Neighbor solicitation", 136: "Neighbor advertisement", 137: "Redirect",
    143: "Multicast listener report v2",
}
_ICMP6_UNREACH = {0: "No route", 1: "Administratively prohibited", 3: "Address unreachable",
                  4: "Port unreachable"}


def icmp_label(proto: int, type_code: int) -> str | None:
    """"Echo request", "Port unreachable", ... for an ICMP/ICMPv6 record whose
    ``dst_port`` carries ``type * 256 + code``; None for other protocols."""
    proto, type_code = int(proto), int(type_code)
    t, c = type_code >> 8, type_code & 0xFF
    if proto == 1:
        if t == 3 and c in _ICMP4_UNREACH:
            return _ICMP4_UNREACH[c]
        if t == 11 and c in _ICMP4_TIME_EXCEEDED:
            return _ICMP4_TIME_EXCEEDED[c]
        return _ICMP4_TYPES.get(t, f"Type {t} code {c}")
    if proto == 58:
        if t == 1 and c in _ICMP6_UNREACH:
            return _ICMP6_UNREACH[c]
        if t == 0:
            return "Type not exported"   # ICMPv6 type 0 is reserved: nothing was sent
        return _ICMP6_TYPES.get(t, f"Type {t} code {c}")
    return None


# ── Rule model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortClause:
    """One or more port rules sharing a rank (a built-in application's ports
    on one transport, or a single operator rule)."""

    rank: int
    application: str
    protocols: frozenset[int] | None      # None = every port-carrying protocol
    ports: frozenset[int] | None          # explicit set (built-ins) ...
    port_lo: int = 0                       # ... or an inclusive range (operator rules)
    port_hi: int = 0
    labels: dict[int, str] = field(default_factory=dict, compare=False, hash=False)
    service: str | None = None             # operator rule's service label
    cidrs: tuple[Network, ...] = ()        # restricts the service side's address
    source: str = "builtin"                # 'builtin' | 'db'

    def matches(self, proto: int, port: int, addr: str | None) -> bool:
        if self.protocols is not None and proto not in self.protocols:
            return False
        if self.ports is not None:
            if port not in self.ports:
                return False
        elif not (self.port_lo <= port <= self.port_hi):
            return False
        if self.cidrs:
            return addr is not None and ip_in(addr, self.cidrs)
        return True

    def label(self, port: int) -> str:
        return self.service or self.labels.get(port) or self.application


@dataclass(frozen=True)
class ProtocolClause:
    """A whole-flow rule: an IP protocol (None = any) and/or CIDRs that
    either endpoint must fall in."""

    rank: int
    application: str
    protocols: frozenset[int] | None
    cidrs: tuple[Network, ...] = ()
    source: str = "builtin"

    def matches(self, proto: int, src: str | None, dst: str | None) -> bool:
        if self.protocols is not None and proto not in self.protocols:
            return False
        if self.cidrs:
            return (src is not None and ip_in(src, self.cidrs)) or (
                dst is not None and ip_in(dst, self.cidrs))
        return True


@dataclass(frozen=True)
class RuleSet:
    port_clauses: tuple[PortClause, ...]
    protocol_clauses: tuple[ProtocolClause, ...]
    apps_by_rank: tuple[str, ...]          # index = rank - 1
    app_names: dict[int, tuple[str, str]]  # NBAR app_id -> (name, category)
    categories: dict[str, str]             # application -> category
    ephemeral_min: int = DEFAULT_EPHEMERAL_MIN
    db_rules: int = 0
    fingerprint: str = ""                  # content hash; names SQL aliases
    # lower-case application or raw exporter name -> the application it is
    # classified as (accepts old links and "icmp"-style names in app=)
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AppRule:
    """An operator rule as stored in ``netflow_app_rules``."""

    id: str
    application: str
    service: str | None = None
    category: str | None = None
    protocol: int | None = None
    port_lo: int | None = None
    port_hi: int | None = None
    cidrs: tuple[str, ...] = ()
    priority: int = 100


def build_rules(
    db_rules: Iterable[AppRule] = (),
    app_names: dict[int, tuple[str, str]] | None = None,
    ephemeral_min: int = DEFAULT_EPHEMERAL_MIN,
) -> RuleSet:
    """Merge built-ins, operator rules and NBAR names into a ranked RuleSet."""
    entries: list[tuple[tuple, object]] = []   # (sort key, spec)
    n_db = 0
    for seq, r in enumerate(db_rules):
        app = (r.application or "").strip()
        if not app:
            continue
        cidrs = tuple(n for n in (parse_network(c) for c in (r.cidrs or ())) if n is not None)
        if r.cidrs and not cidrs:
            continue   # every CIDR invalid: the rule could never match as intended
        protos = frozenset({int(r.protocol)}) if r.protocol is not None else None
        if r.port_lo is not None:
            lo = int(r.port_lo)
            hi = int(r.port_hi) if r.port_hi is not None else lo
            if not (1 <= lo <= hi <= 65535):
                continue
            if protos is not None and not (protos & PORT_PROTOCOLS):
                continue   # ports on a portless protocol can never match
            spec = ("port", app, protos, lo, hi, (r.service or "").strip() or None, cidrs, "db", r.category)
            entries.append(((int(r.priority), 0, hi - lo, lo, seq), spec))
        elif protos is not None or cidrs:
            spec = ("proto", app, protos, cidrs, "db", r.category)
            entries.append(((int(r.priority), 0, 65535, 0, seq), spec))
        else:
            continue
        n_db += 1
    seq = 0
    for app, _cat, ports in _BUILTIN_APPS:
        tcp: dict[int, str] = {}
        udp: dict[int, str] = {}
        for port, label in ports.items():
            if port in _UDP_ONLY:
                udp[port] = label
            elif port in _TCP_AND_UDP:
                tcp[port] = label
                udp[port] = label
            else:
                tcp[port] = label
        for port, (label, variant_app) in _UDP_VARIANTS.items():
            if variant_app == app:
                udp[port] = label
        for protos, labels in ((_TCP, tcp), (_UDP, udp)):
            if labels:
                seq += 1
                spec = ("builtin_port", app, protos, labels)
                entries.append(((BUILTIN_PRIORITY, 1, 0, 0, seq), spec))
    for proto, app in _BUILTIN_PROTOCOLS:
        seq += 1
        entries.append(((BUILTIN_PRIORITY, 1, 65535, 0, seq), ("proto", app, frozenset({proto}), (), "builtin", None)))
    entries.sort(key=lambda e: e[0])

    port_clauses: list[PortClause] = []
    protocol_clauses: list[ProtocolClause] = []
    apps: list[str] = []
    categories: dict[str, str] = {}
    for rank, (_key, spec) in enumerate(entries, start=1):
        kind, app = spec[0], spec[1]
        apps.append(app)
        if kind == "builtin_port":
            _k, _a, protos, labels = spec
            port_clauses.append(PortClause(rank, app, protos, frozenset(labels), labels=dict(labels)))
        elif kind == "port":
            _k, _a, protos, lo, hi, service, cidrs, source, category = spec
            port_clauses.append(PortClause(rank, app, protos, None, lo, hi, service=service,
                                           cidrs=cidrs, source=source))
            if category and app not in categories:
                categories[app] = category
        else:
            _k, _a, protos, cidrs, source, category = spec
            protocol_clauses.append(ProtocolClause(rank, app, protos, cidrs, source))
            if category and app not in categories:
                categories[app] = category
    for app, cat in _BUILTIN_CATEGORY.items():
        categories.setdefault(app, cat)
    categories.setdefault(APP_SYSTEM, "Other")
    categories.setdefault(APP_OTHER, "Other")
    by_lower: dict[str, str] = {}
    for app in (*apps, APP_SYSTEM, APP_OTHER):
        by_lower.setdefault(app.lower(), app)
    aliases = dict(by_lower)
    names: dict[int, tuple[str, str]] = {}
    for k, v in (app_names or {}).items():
        if not k or not v or not str(v[0]).strip():
            continue
        canonical = _canonical_application(str(v[0]), by_lower)
        names[int(k)] = (canonical, str(v[1] or ""))
        aliases.setdefault(str(v[0]).strip().lower(), canonical)
        aliases.setdefault(canonical.lower(), canonical)
    for _aid, (name, cat) in sorted(names.items()):
        categories.setdefault(name, cat or "Other")
    eph = min(max(int(ephemeral_min), 1024), 65535)
    fingerprint = hashlib.sha1(repr((
        [(c.rank, c.application, sorted(c.protocols or ()), sorted(c.ports or ()), c.port_lo,
          c.port_hi, [str(n) for n in c.cidrs]) for c in port_clauses],
        [(c.rank, c.application, sorted(c.protocols or ()), [str(n) for n in c.cidrs], c.source)
         for c in protocol_clauses],
        sorted(names.items()), eph,
    )).encode()).hexdigest()[:10]
    return RuleSet(tuple(port_clauses), tuple(protocol_clauses), tuple(apps), names,
                   categories, eph, n_db, fingerprint, aliases)


# ── Cache ───────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_rules: RuleSet = build_rules()
_last_db_rules: list[AppRule] = []    # last successful read, reused if PG fails
_loaded_at = 0.0
_generation = 0


def current_rules() -> RuleSet:
    """The cached rule set (built-ins only until the first ``load_rules``).
    Take it once per request and pass it to every call that must agree."""
    with _lock:
        return _rules


def invalidate_rules_cache() -> None:
    """Force the next ``load_rules`` to re-read operator rules and NBAR names
    (B7 calls this after every application-rule or settings write)."""
    global _loaded_at, _generation
    with _lock:
        _generation += 1
        _loaded_at = 0.0


async def _read_db_rules(db: AsyncSession) -> tuple[list[AppRule], int]:
    rows = (await db.execute(text(
        "SELECT id::text AS id, application, service, category, protocol, port_lo, port_hi, "
        "cidrs, priority FROM netflow_app_rules WHERE enabled ORDER BY priority, created_at, id"
    ))).all()
    rules = [AppRule(id=r.id, application=r.application, service=r.service, category=r.category,
                     protocol=r.protocol, port_lo=r.port_lo, port_hi=r.port_hi,
                     cidrs=tuple(r.cidrs or ()), priority=r.priority) for r in rows]
    eph = DEFAULT_EPHEMERAL_MIN
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": SETTINGS_KEY})).first()
    if row is not None:
        value = row[0]
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict) and isinstance(value.get("ephemeral_min"), int):
            eph = value["ephemeral_min"]
    return rules, eph


# ── NBAR names refresh (NF-31) ──────────────────────────────────────────────
# Every NetFlow read builds its filter after ``load_rules()``, so the refresh
# of the exporter-announced application names must never hold a request
# hostage to ClickHouse: a server that accepts and never answers used to stall
# each request for the driver's default 300 s receive timeout before its own
# 20 s query budget even started, and every stalled request held another
# thread-pool worker. The refresh is therefore
#   * bounded: its own client with short connect/receive timeouts and a
#     ``max_execution_time``;
#   * single-flight per event loop: concurrent requests share one read;
#   * awaited for at most NAMES_WAIT_S from when it started, after which the
#     request goes on with the last names read (a late answer is applied as
#     soon as it arrives);
#   * backed off for NAMES_RETRY_S after a failure, so a down ClickHouse is
#     not re-dialled by every request.
NAMES_WAIT_S = 2.0
NAMES_CONNECT_TIMEOUT_S = 3
NAMES_RECEIVE_TIMEOUT_S = 8
NAMES_QUERY_BUDGET_S = 5
NAMES_RETRY_S = 30.0

_names_local = threading.local()


@dataclass
class _NamesRefresh:
    task: "asyncio.Future | None" = None
    loop: "asyncio.AbstractEventLoop | None" = None
    started: float = 0.0
    value: dict | None = None        # last successful read
    failed_at: float = 0.0
    pending_apply: bool = False      # a read finished that the rule set does not use yet


_names = _NamesRefresh()


def _names_client():
    """Thread-local ClickHouse client with short timeouts, used only for the
    NBAR names (the shared client waits up to 300 s for an answer)."""
    client = getattr(_names_local, "client", None)
    if client is None:
        import clickhouse_connect

        from app.core.database import settings

        client = clickhouse_connect.get_client(
            host=settings.CLICKHOUSE_HOST, port=settings.CLICKHOUSE_HTTP_PORT,
            database=settings.CLICKHOUSE_DB, username=settings.CLICKHOUSE_USER,
            password=settings.CLICKHOUSE_PASSWORD,
            connect_timeout=NAMES_CONNECT_TIMEOUT_S, send_receive_timeout=NAMES_RECEIVE_TIMEOUT_S,
        )
        _names_local.client = client
    return client


def _read_app_names() -> dict[int, tuple[str, str]]:
    try:
        res = _names_client().query(
            "SELECT app_id, argMax(name, updated_at) AS n, argMax(category, updated_at) AS c "
            "FROM zenplus.flow_app_names WHERE app_id != 0 GROUP BY app_id HAVING n != ''",
            settings={"max_execution_time": NAMES_QUERY_BUDGET_S},
        )
    except Exception:
        # A broken connection must not be reused by the next refresh.
        _names_local.client = None
        raise
    return {int(r[0]): (str(r[1]), str(r[2] or "")) for r in res.result_rows}


def _names_done(task: "asyncio.Future") -> None:
    """Store a finished refresh. A result that arrives after every waiter
    gave up still lands: the rule cache is marked stale so the next
    ``load_rules`` rebuilds with it."""
    if task.cancelled():
        _names.failed_at = time.monotonic()
        return
    exc = task.exception()
    if exc is not None:
        _names.failed_at = time.monotonic()
        logger.warning("flow_app_names unavailable; keeping previous NBAR names (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        return
    value = task.result()
    _names.failed_at = 0.0
    _names.value = value
    if value != current_rules().app_names:
        _names.pending_apply = True


async def _refresh_app_names(previous: dict, *, must_refresh: bool) -> dict:
    """NBAR names for a rebuild: a fresh read when it answers within
    NAMES_WAIT_S of its start, else the last good read (``previous`` when
    none). Never raises."""
    if _names.pending_apply and not must_refresh and _names.value is not None:
        _names.pending_apply = False
        return _names.value
    loop = asyncio.get_running_loop()
    now = time.monotonic()
    task = _names.task if _names.loop is loop else None
    if task is not None and task.done():
        task = _names.task = None
    if task is None:
        if not must_refresh and _names.failed_at and now - _names.failed_at < NAMES_RETRY_S:
            return _names.value if _names.value is not None else previous
        task = asyncio.ensure_future(asyncio.to_thread(_read_app_names))
        task.add_done_callback(_names_done)
        _names.task, _names.loop, _names.started = task, loop, now
    remaining = _names.started + NAMES_WAIT_S - now
    if remaining > 0 and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.TimeoutError:
            logger.warning("flow_app_names refresh still running after %.1f s; using previous NBAR names",
                           NAMES_WAIT_S)
        except Exception:  # noqa: BLE001 - reported by _names_done
            pass
    if task.done() and not task.cancelled() and task.exception() is None:
        _names.pending_apply = False
        return task.result()
    return _names.value if _names.value is not None else previous


async def load_rules(db: AsyncSession | None = None, *, force: bool = False) -> RuleSet:
    """Refresh the cached rule set if stale and return it.

    Operator rules come from PostgreSQL (own short-lived session unless one
    is passed) and NBAR names from ClickHouse. Either source failing keeps
    its previous contribution rather than dropping to built-ins, so a
    ClickHouse hiccup does not relabel every NBAR-classified flow.
    """
    global _rules, _loaded_at, _last_db_rules
    with _lock:
        if (not force and _loaded_at and time.monotonic() - _loaded_at < CACHE_TTL_S
                and not _names.pending_apply):
            return _rules
        generation = _generation
        previous = _rules
        # Invalidated (a rule or setting changed) or forced: try a fresh read
        # even inside the failure back-off.
        must_refresh = force or _loaded_at == 0.0
    db_rules: list[AppRule] | None = None
    eph = previous.ephemeral_min
    try:
        if db is None:
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as own:
                db_rules, eph = await _read_db_rules(own)
        else:
            async with db.begin_nested():
                db_rules, eph = await _read_db_rules(db)
    except Exception:
        logger.warning("netflow_app_rules unavailable; keeping previous operator rules", exc_info=True)
    names = await _refresh_app_names(previous.app_names, must_refresh=must_refresh)
    if db_rules is None:
        db_rules = _last_db_rules
    rebuilt = build_rules(db_rules, names, eph)
    with _lock:
        if _generation == generation:
            _rules = rebuilt
            _last_db_rules = db_rules
            _loaded_at = time.monotonic()
        return _rules


# ── Python classifier ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Classification:
    service_port: int | None
    direction: str        # 'request' | 'response' | 'unknown' | 'n/a'
    service: str | None   # "HTTPS", "TCP/8081", "Echo request"; None = not applicable/unknown
    application: str
    category: str
    ip_protocol: str
    basis: str            # 'exporter' | 'dst_rule' | 'src_rule' | 'rule' | 'protocol' | 'lower_port' | 'ephemeral' | 'none'
    confidence: str       # 'high' | 'medium' | 'low' | 'n/a'
    port_applicable: bool

    def to_dict(self) -> dict:
        return asdict(self)


_DST, _SRC = 1, 2
# Pick kinds (how the service port was chosen).
_K_ONE_RULE, _K_BOTH_RULES, _K_LOWER, _K_EPHEMERAL = 1, 2, 3, 4


def _port_rank(rs: RuleSet, proto: int, port: int, addr: str | None) -> int:
    for c in rs.port_clauses:
        if c.matches(proto, port, addr):
            return c.rank
    return 0


def _protocol_rank(rs: RuleSet, proto: int, src: str | None, dst: str | None) -> int:
    for c in rs.protocol_clauses:
        if c.matches(proto, src, dst):
            return c.rank
    return 0


def _pick(s: int, d: int, s_rank: int, d_rank: int, eph: int) -> tuple[int, int]:
    """(side, kind); side 0 = no service port. Mirrors ``_pick_sql``."""
    def wk(x):
        return 1 <= x <= 1023

    if d_rank and not s_rank:
        return _DST, _K_ONE_RULE
    if s_rank and not d_rank:
        return _SRC, _K_ONE_RULE
    if s_rank and d_rank:
        if s == d:
            return _DST, _K_BOTH_RULES
        if (d >= eph) != (s >= eph):
            return (_DST if d < eph else _SRC), _K_BOTH_RULES
        return (_DST if d <= s else _SRC), _K_BOTH_RULES
    if s == 0 and d == 0:
        return 0, 0
    if wk(s) or wk(d):
        if wk(s) and wk(d):
            return (_DST if d <= s else _SRC), _K_LOWER
        return (_DST if wk(d) else _SRC), _K_LOWER
    if 1024 <= d < eph and s >= eph:
        return _DST, _K_EPHEMERAL
    if 1024 <= s < eph and d >= eph:
        return _SRC, _K_EPHEMERAL
    return 0, 0


def classify(
    protocol: int,
    src_port: int,
    dst_port: int,
    app_id: int = 0,
    *,
    src_ip: str | None = None,
    dst_ip: str | None = None,
    rules: RuleSet | None = None,
) -> Classification:
    """Classify one flow (or one grouped row). Pass both addresses exactly
    when the SQL side was built with address columns, so both apply the same
    CIDR-restricted rules."""
    if (src_ip is None) != (dst_ip is None):
        raise ValueError("classify needs both addresses or neither")
    rs = rules or current_rules()
    p, s, d = int(protocol), int(src_port or 0), int(dst_port or 0)
    ip_proto = protocol_name(p)
    port_applicable = p in PORT_PROTOCOLS
    nbar = rs.app_names.get(int(app_id or 0)) if app_id else None
    pr = _protocol_rank(rs, p, src_ip, dst_ip)

    if not port_applicable:
        service = icmp_label(p, d) if p in ICMP_PROTOCOLS else None
        # Only unambiguous ICMP types get a direction: ICMPv4 echo reply is
        # type 0 / code 0, indistinguishable from "type not exported".
        icmp_type = d >> 8
        if (p == 1 and icmp_type == 8) or (p == 58 and icmp_type == 128):
            direction = "request"
        elif p == 58 and icmp_type == 129:
            direction = "response"
        else:
            direction = "n/a"
        if nbar:
            app, basis, conf = nbar[0], "exporter", "high"
        elif pr:
            app = rs.apps_by_rank[pr - 1]
            basis = "protocol" if _is_builtin_rank(rs, pr) else "rule"
            conf = "high"
        else:
            app, basis, conf = APP_OTHER, "none", "n/a"
        return Classification(None, direction, service, app, rs.categories.get(app, "Other"),
                              ip_proto, basis, conf, False)

    s_rank = _port_rank(rs, p, s, src_ip)
    d_rank = _port_rank(rs, p, d, dst_ip)
    side, kind = _pick(s, d, s_rank, d_rank, rs.ephemeral_min)
    sport = d if side == _DST else (s if side == _SRC else 0)
    picked_rank = d_rank if side == _DST else (s_rank if side == _SRC else 0)
    direction = "unknown" if side == 0 or s == d else ("request" if side == _DST else "response")

    if picked_rank:
        clause = next(c for c in rs.port_clauses if c.rank == picked_rank)
        service = clause.label(sport)
    elif sport:
        service = f"{ip_proto}/{sport}"
    else:
        service = None

    if nbar:
        app, basis, conf = nbar[0], "exporter", "high"
    elif picked_rank and (not pr or picked_rank < pr):
        app = rs.apps_by_rank[picked_rank - 1]
        basis = "dst_rule" if side == _DST else "src_rule"
        conf = "medium" if kind == _K_BOTH_RULES else "high"
    elif pr:
        app = rs.apps_by_rank[pr - 1]
        basis = "protocol" if _is_builtin_rank(rs, pr) else "rule"
        conf = "high"
    else:
        app = APP_SYSTEM if 1 <= sport <= 1023 else APP_OTHER
        basis = "lower_port" if kind == _K_LOWER else ("ephemeral" if kind == _K_EPHEMERAL else "none")
        conf = "low"
    return Classification(sport or None, direction, service, app,
                          rs.categories.get(app, "Other"), ip_proto, basis, conf, True)


def _is_builtin_rank(rs: RuleSet, rank: int) -> bool:
    return any(c.rank == rank and c.source == "builtin" for c in rs.protocol_clauses)


def service_name(protocol: int, port: int, rules: RuleSet | None = None) -> str | None:
    """Label for a (protocol, service port) pair coming back from SQL, e.g.
    "HTTPS" or "UDP/8125"; ICMP type/code for ICMP; None for port 0."""
    rs = rules or current_rules()
    p, port = int(protocol), int(port or 0)
    if p in ICMP_PROTOCOLS:
        return icmp_label(p, port)
    if p not in PORT_PROTOCOLS or port == 0:
        return None
    for c in rs.port_clauses:
        if not c.cidrs and c.matches(p, port, None):
            return c.label(port)
    return f"{protocol_name(p)}/{port}"


def known_applications(rules: RuleSet | None = None) -> list[str]:
    """Every application name the classifier can produce (valid ``app=``)."""
    rs = rules or current_rules()
    names = set(rs.apps_by_rank) | {APP_SYSTEM, APP_OTHER}
    names |= {n for n, _c in rs.app_names.values()}
    return sorted(names, key=str.lower)


def resolve_application(name: str, rules: RuleSet | None = None) -> str | None:
    """The application ``name`` selects in an ``app=`` filter: itself when the
    classifier produces it, else a case-insensitive match or the application
    an exporter name is classified as ("icmp" -> "ICMP"); None when unknown."""
    rs = rules or current_rules()
    if name in known_applications(rs):
        return name
    return rs.aliases.get(name.strip().lower())


def category_for(application: str, rules: RuleSet | None = None) -> str:
    rs = rules or current_rules()
    return rs.categories.get(application, "Other")


def rollup_src_port(src_ports: Iterable[int]) -> int:
    """Python mirror of ``ROLLUP_SRC_PORT_SQL``: the rollup keeps up to 10
    distinct source ports per key; only a single one identifies a side."""
    ports = list(dict.fromkeys(int(p) for p in src_ports))
    return ports[0] if len(ports) == 1 else 0


# ── SQL builders ────────────────────────────────────────────────────────────
#
# The decision above needs the same intermediate values (each side's rule
# rank, the pick) several times. Written as plain nested expressions the text
# grows multiplicatively (~120 KB for one application expression). Instead
# every intermediate is named once with an inline alias and referenced by
# name; each returned expression has the shape
#
#     tupleElement(tuple(<result>, (<def a> AS _nf_a_<h>), (<def b> AS ...), ...), 1)
#
# which matters for three reasons, all measured on ClickHouse 24.10:
#
# * ClickHouse resolves aliases across the whole SELECT (forward references
#   included), so every expression is self-contained and can be combined with
#   the others in one query; ``<h>`` hashes the column expressions and the
#   rule set, so two variants (raw and rollup source ports) never collide.
# * Definitions only ever contain bare alias references, so an alias defined
#   by two expressions in one query has identical text -- the old analyzer
#   rejects an alias bound to two differently spelled expressions.
# * The definitions are plain ``tuple`` arguments, so they are computed once,
#   eagerly. Referenced only inside ``multiIf`` branches, short-circuit
#   evaluation re-computes the shared sub-expressions under every branch mask:
#   5.0 s instead of 0.5 s for the application over 10M rows.

# Source-port expression for flow_conversations_1h, whose key has no
# src_port: a key seen with exactly one source port is as good as raw.
ROLLUP_SRC_PORT_SQL = (
    "if(length(finalizeAggregation(src_ports)) = 1, finalizeAggregation(src_ports)[1], 0)"
)

_SQL_PORT_PROTOCOLS = "(" + ", ".join(str(p) for p in sorted(PORT_PROTOCOLS)) + ")"
_FIELDS = ("service_port", "direction", "application", "basis", "confidence")


def _sql_str(value: str) -> str:
    """ClickHouse string literal. ``%`` is written as ``\\x25``: the query may
    be run with bound parameters, where the client %-formats the text, or
    without, where it does not -- the escape reads the same either way."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("%", "\\x25") + "'"


def _in_list(values: Iterable[int]) -> str:
    return "(" + ", ".join(str(int(v)) for v in sorted(values)) + ")"


def _u32_array(values: Iterable[int]) -> str:
    return f"CAST([{', '.join(str(int(v)) for v in values)}] AS Array(UInt32))"


def _db_port_branch(c: PortClause, port: str, proto: str, addr: str | None) -> str | None:
    if c.cidrs and addr is None:
        return None
    conds = []
    if c.protocols is not None:
        conds.append(f"{proto} IN {_in_list(c.protocols)}")
    conds.append(f"{port} BETWEEN {int(c.port_lo)} AND {int(c.port_hi)}")
    if c.cidrs:
        conds.append(networks_sql(c.cidrs, addr))
    return f"{' AND '.join(conds)}, {int(c.rank)}"


def _port_rank_sql(rs: RuleSet, port: str, proto: str, addr: str | None) -> str:
    """Rank of the first port rule matching this side (0 = none).

    Built-in clauses are disjoint per (protocol, port), so they collapse into
    one ``transform`` lookup per protocol; operator rules are ordered
    branches before (priority <= built-in) or after it.
    """
    builtin = [c for c in rs.port_clauses if c.source == "builtin"]
    first_builtin = min((c.rank for c in builtin), default=None)
    early, late = [], []
    for c in rs.port_clauses:
        if c.source == "builtin":
            continue
        branch = _db_port_branch(c, port, proto, addr)
        if branch is not None:
            (early if first_builtin is None or c.rank < first_builtin else late).append(branch)
    by_proto: dict[int, dict[int, int]] = {}
    for c in builtin:
        for pr in sorted(c.protocols or ()):
            for prt in c.ports or ():
                by_proto.setdefault(pr, {})[prt] = c.rank
    lookups = []
    for pr, mapping in sorted(by_proto.items()):
        ports = sorted(mapping)
        lookups.append(
            f"{proto} = {pr}, transform(toUInt32({port}), {_u32_array(ports)}, "
            f"{_u32_array(mapping[x] for x in ports)}, toUInt32(0))"
        )
    builtin_sql = f"multiIf({', '.join(lookups)}, toUInt32(0))" if lookups else "toUInt32(0)"
    if not early and not late:
        return builtin_sql
    parts = list(early)
    if late:
        parts.append(f"{builtin_sql} != 0, {builtin_sql}")
        parts.extend(late)
        return f"multiIf({', '.join(parts)}, 0)"
    parts.append(f"1, {builtin_sql}")
    return f"multiIf({', '.join(parts)}, 0)"


def _protocol_rank_sql(rs: RuleSet, proto: str, src_addr: str | None, dst_addr: str | None) -> str:
    branches: list[str] = []
    for c in rs.protocol_clauses:
        conds = []
        if c.protocols is not None:
            conds.append(f"{proto} IN {_in_list(c.protocols)}")
        if c.cidrs:
            if src_addr is None or dst_addr is None:
                continue
            conds.append(f"({networks_sql(c.cidrs, src_addr)} OR {networks_sql(c.cidrs, dst_addr)})")
        branches.append(f"{' AND '.join(conds) or '1'}, {int(c.rank)}")
    if not branches:
        return "0"
    return f"multiIf({', '.join(branches)}, 0)"


def _pick_sql(s: str, d: str, sr: str, dr: str, eph: int) -> str:
    """Side*10 + kind, 0 when no service port; mirrors ``_pick``."""
    wk_s, wk_d = f"({s} BETWEEN 1 AND 1023)", f"({d} BETWEEN 1 AND 1023)"
    dst = lambda k: _DST * 10 + k  # noqa: E731
    src = lambda k: _SRC * 10 + k  # noqa: E731
    return (
        "multiIf("
        f"{dr} != 0 AND {sr} = 0, {dst(_K_ONE_RULE)}, "
        f"{sr} != 0 AND {dr} = 0, {src(_K_ONE_RULE)}, "
        f"{sr} != 0 AND {dr} != 0, multiIf({s} = {d}, {dst(_K_BOTH_RULES)}, "
        f"({d} >= {eph}) != ({s} >= {eph}), if({d} < {eph}, {dst(_K_BOTH_RULES)}, {src(_K_BOTH_RULES)}), "
        f"if({d} <= {s}, {dst(_K_BOTH_RULES)}, {src(_K_BOTH_RULES)})), "
        f"{s} = 0 AND {d} = 0, 0, "
        f"{wk_s} OR {wk_d}, multiIf({wk_s} AND {wk_d}, if({d} <= {s}, {dst(_K_LOWER)}, {src(_K_LOWER)}), "
        f"{wk_d}, {dst(_K_LOWER)}, {src(_K_LOWER)}), "
        f"({d} BETWEEN 1024 AND {eph - 1}) AND {s} >= {eph}, {dst(_K_EPHEMERAL)}, "
        f"({s} BETWEEN 1024 AND {eph - 1}) AND {d} >= {eph}, {src(_K_EPHEMERAL)}, "
        "0)"
    )


_TOKEN_RE = re.compile(r"\x00(\w+)\x00")


def _let(name: str) -> str:
    """Placeholder for a named intermediate in a template (see ``_render``)."""
    return f"\x00{name}\x00"


def _render(template: str, defs: dict[str, str], suffix: str) -> str:
    """Self-contained expression for ``template`` (see the section comment)."""
    def bare(text: str) -> str:
        return _TOKEN_RE.sub(lambda m: f"_nf_{m.group(1)}_{suffix}", text)

    needed: list[str] = []
    stack = _TOKEN_RE.findall(template)
    while stack:
        name = stack.pop()
        if name not in needed:
            needed.append(name)
            stack.extend(_TOKEN_RE.findall(defs[name]))
    order = [n for n in defs if n in needed]   # stable definition order
    parts = [bare(template)] + [f"({bare(defs[n])} AS _nf_{n}_{suffix})" for n in order]
    return f"tupleElement(tuple({', '.join(parts)}), 1)"


def _classification_templates(
    src_expr: str, dst_expr: str, proto_expr: str, app_id_expr: str | None,
    src_addr: str | None, dst_addr: str | None, rs: RuleSet,
) -> tuple[dict[str, str], dict[str, str], str]:
    """(output templates, intermediate definitions, alias suffix)."""
    if (src_addr is None) != (dst_addr is None):
        raise ValueError("classification SQL needs both address columns or neither")
    p, s, d = _let("p"), _let("s"), _let("d")
    eph = rs.ephemeral_min
    portproto = _let("pp")
    side, kind, pick = _let("side"), _let("kind"), _let("pk")
    sr, dr, pr, nb = _let("sr"), _let("dr"), _let("pr"), _let("nb")
    sport, picked, rule_wins = _let("sport"), _let("picked"), _let("rw")
    apps = "[" + ", ".join(_sql_str(a) for a in rs.apps_by_rank) + "]"
    if app_id_expr and rs.app_names:
        ids = sorted(rs.app_names)
        nbar = (f"transform(toUInt64({app_id_expr}), CAST([{', '.join(str(i) for i in ids)}] AS Array(UInt64)), "
                f"[{', '.join(_sql_str(rs.app_names[i][0]) for i in ids)}], '')")
    else:
        nbar = "''"
    builtin_protocol_ranks = sorted(c.rank for c in rs.protocol_clauses if c.source == "builtin")
    pr_is_builtin = f"{pr} IN {_in_list(builtin_protocol_ranks)}" if builtin_protocol_ranks else "0"
    icmp_t = f"intDiv({d}, 256)"
    defs = {
        "p": f"toUInt8({proto_expr})",
        "s": f"toUInt16({src_expr})",
        "d": f"toUInt16({dst_expr})",
        "pp": f"{p} IN {_SQL_PORT_PROTOCOLS}",
        "sr": f"toUInt32({_port_rank_sql(rs, s, p, src_addr)})",
        "dr": f"toUInt32({_port_rank_sql(rs, d, p, dst_addr)})",
        "pr": f"toUInt32({_protocol_rank_sql(rs, p, src_addr, dst_addr)})",
        "nb": nbar,
        "pk": f"if({portproto}, {_pick_sql(s, d, sr, dr, eph)}, 0)",
        "side": f"intDiv({pick}, 10)",
        "kind": f"modulo({pick}, 10)",   # never a literal %: see _sql_str
        "sport": f"toUInt16(multiIf({side} = {_DST}, {d}, {side} = {_SRC}, {s}, 0))",
        "picked": f"multiIf({side} = {_DST}, {dr}, {side} = {_SRC}, {sr}, 0)",
        "rw": f"({picked} != 0 AND ({pr} = 0 OR {picked} < {pr}))",
    }
    outputs = {
        "service_port": sport,
        "direction": (
            f"multiIf(NOT {portproto}, multiIf({p} = 1 AND {icmp_t} = 8, 'request', "
            f"{p} = 58 AND {icmp_t} = 128, 'request', {p} = 58 AND {icmp_t} = 129, 'response', 'n/a'), "
            f"{side} = 0 OR {s} = {d}, 'unknown', {side} = {_DST}, 'request', 'response')"
        ),
        "application": (
            f"multiIf({nb} != '', {nb}, {rule_wins}, {apps}[{picked}], {pr} != 0, {apps}[{pr}], "
            f"{portproto} AND {sport} BETWEEN 1 AND 1023, {_sql_str(APP_SYSTEM)}, {_sql_str(APP_OTHER)})"
        ),
        "basis": (
            f"multiIf({nb} != '', 'exporter', {rule_wins}, if({side} = {_DST}, 'dst_rule', 'src_rule'), "
            f"{pr} != 0, if({pr_is_builtin}, 'protocol', 'rule'), "
            f"{kind} = {_K_LOWER}, 'lower_port', {kind} = {_K_EPHEMERAL}, 'ephemeral', 'none')"
        ),
        "confidence": (
            f"multiIf({nb} != '', 'high', {rule_wins}, if({kind} = {_K_BOTH_RULES}, 'medium', 'high'), "
            f"{pr} != 0, 'high', {portproto}, 'low', 'n/a')"
        ),
    }
    suffix = hashlib.sha1(repr((src_expr, dst_expr, proto_expr, app_id_expr, src_addr, dst_addr,
                                rs.fingerprint)).encode()).hexdigest()[:8]
    return outputs, defs, suffix


def classification_sql(
    src_expr: str = "src_port",
    dst_expr: str = "dst_port",
    proto_expr: str = "protocol",
    *,
    app_id_expr: str | None = "app_id",
    src_addr: str | None = None,
    dst_addr: str | None = None,
    rules: RuleSet | None = None,
) -> dict[str, str]:
    """ClickHouse expressions for one flow row: ``service_port`` (UInt16, 0 =
    none), ``direction``, ``application``, ``basis``, ``confidence``.

    Each expression is self-contained and may be used alone or together with
    the others anywhere in one SELECT (WHERE, GROUP BY, aggregates).
    ``src_addr``/``dst_addr`` name the IPv4 address columns (IPv6 twin =
    name + '6'); leave them None where addresses are unavailable, and call
    ``classify`` without addresses for the same rows. ``app_id_expr=None``
    disables NBAR names (tables without ``app_id``); pass ``app_id=0`` to
    ``classify`` for those. All literals are generated from validated rule
    data, never from request text, and contain no ``%``.
    """
    rs = rules or current_rules()
    outputs, defs, suffix = _classification_templates(
        src_expr, dst_expr, proto_expr, app_id_expr, src_addr, dst_addr, rs)
    return {name: _render(t, defs, suffix) for name, t in outputs.items()}


def classification_tuple_sql(
    src_expr: str = "src_port",
    dst_expr: str = "dst_port",
    proto_expr: str = "protocol",
    *,
    app_id_expr: str | None = "app_id",
    src_addr: str | None = None,
    dst_addr: str | None = None,
    rules: RuleSet | None = None,
) -> str:
    """One expression returning (service_port, direction, application, basis,
    confidence) -- same arguments as ``classification_sql``."""
    rs = rules or current_rules()
    outputs, defs, suffix = _classification_templates(
        src_expr, dst_expr, proto_expr, app_id_expr, src_addr, dst_addr, rs)
    body = "tuple(" + ", ".join(outputs[name] for name in _FIELDS) + ")"
    return _render(body, defs, suffix)


def service_port_sql(src_expr: str = "src_port", dst_expr: str = "dst_port",
                     proto_expr: str = "protocol", **kw) -> str:
    """UInt16 service-port expression (0 = none/not applicable)."""
    return classification_sql(src_expr, dst_expr, proto_expr, **kw)["service_port"]


def direction_sql(src_expr: str = "src_port", dst_expr: str = "dst_port",
                  proto_expr: str = "protocol", **kw) -> str:
    return classification_sql(src_expr, dst_expr, proto_expr, **kw)["direction"]


def application_sql(src_expr: str = "src_port", dst_expr: str = "dst_port",
                    proto_expr: str = "protocol", **kw) -> str:
    """Application-name expression (the ``app=`` filter compares it)."""
    return classification_sql(src_expr, dst_expr, proto_expr, **kw)["application"]


__all__ = [
    "Classification", "RuleSet", "AppRule", "PortClause", "ProtocolClause",
    "classify", "classification_sql", "classification_tuple_sql", "service_port_sql", "direction_sql", "application_sql",
    "known_applications", "resolve_application", "NBAR_EQUIVALENTS", "category_for", "service_name",
    "icmp_label", "protocol_name",
    "load_rules", "current_rules", "invalidate_rules_cache", "build_rules",
    "rollup_src_port", "ROLLUP_SRC_PORT_SQL", "IP_PROTOCOL_NAMES", "PORT_PROTOCOLS",
    "APP_WEB", "APP_VPN", "APP_ICMP", "APP_NETMGMT", "APP_SYSTEM", "APP_OTHER",
]
