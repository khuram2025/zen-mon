from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_clickhouse_client, get_db
from app.core.security import get_current_user
from app.models.device import Device
from app.models.device_interface import DeviceInterface
from app.models.netflow_saved_view import NetflowSavedView
from app.models.user import User
from pydantic import BaseModel, Field

router = APIRouter(prefix="/netflow", tags=["NetFlow"])


def _resolve_window(hours: int, from_ts: str | None, to_ts: str | None) -> tuple[datetime, datetime]:
    """Return (start, end) UTC bounds. Custom from/to wins; otherwise use a relative window ending now."""
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
    start = end - timedelta(hours=hours)
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
    6: "TCP",
    17: "UDP",
    47: "GRE",
    50: "ESP",
    89: "OSPF",
}

COMMON_PORTS = {
    20: "FTP Data",
    21: "FTP",
    22: "SSH",
    23: "Telnet",
    25: "SMTP",
    53: "DNS",
    80: "HTTP",
    110: "POP3",
    123: "NTP",
    143: "IMAP",
    161: "SNMP",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    465: "SMTPS",
    500: "IKE",
    587: "SMTP Submission",
    993: "IMAPS",
    995: "POP3S",
    1433: "MSSQL",
    3306: "MySQL",
    3389: "RDP",
    5432: "PostgreSQL",
    6379: "Redis",
    8080: "HTTP Alt",
    8443: "HTTPS Alt",
}


# Maps ports to higher-level "applications" for the dashboard donut.
APPLICATION_BUCKETS = [
    ("Web (HTTP/HTTPS)", {80, 443, 8080, 8443}),
    ("Streaming Media", {554, 1755, 1935, 5004, 5005, 8000, 8554, 5060, 5061}),
    ("DNS", {53}),
    ("Email (SMTP/IMAP/POP)", {25, 110, 143, 465, 587, 993, 995}),
    ("File Transfer (FTP/SMB)", {20, 21, 69, 115, 445, 2049}),
    ("Remote Access (SSH/RDP/Telnet)", {22, 23, 3389, 5900, 5938}),
    ("Database", {1433, 1521, 3306, 5432, 6379, 9042, 27017}),
    ("VoIP / Video Conf.", {1719, 1720, 3478, 3479, 5060, 5061, 16384, 19302}),
    ("Network Mgmt", {123, 161, 162, 514, 6343}),
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


def _client():
    return get_clickhouse_client()


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


def _query(sql: str, params: dict):
    try:
        return _client().query(sql, parameters=params)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse query failed: {exc}") from exc


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
    }


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
) -> tuple[str, dict]:
    """Build a combined WHERE-clause fragment for every supported drill-down dimension."""
    parts: list[str] = []
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
        port_set = next((ports for name, ports in APPLICATION_BUCKETS if name == application), None)
        if port_set:
            ports_sql = ",".join(str(int(p)) for p in port_set)
            parts.append(f"dst_port IN ({ports_sql})")
        elif application == "System Services":
            all_bucket_ports = set().union(*(ports for _, ports in APPLICATION_BUCKETS))
            sys_ports = sorted(set(range(1, 1024)) - all_bucket_ports)
            ports_sql = ",".join(str(p) for p in sys_ports)
            parts.append(f"dst_port BETWEEN 1 AND 1023 AND dst_port IN ({ports_sql})")
        elif application == "Other":
            all_bucket_ports = set().union(*(ports for _, ports in APPLICATION_BUCKETS))
            ports_sql = ",".join(str(p) for p in sorted(all_bucket_ports))
            parts.append(f"(dst_port = 0 OR dst_port > 1023) AND dst_port NOT IN ({ports_sql})")
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

    return ((" AND " + " AND ".join(parts)) if parts else "", params)


@router.get("/overview")
async def netflow_overview(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    rate_window = min(300, _window_seconds(start, end))
    extra_sql, extra_params = _scope(**scope)
    params = {"start": start, "end": end, "rate_window": rate_window, **extra_params}
    summary = _query(
        f"""
        SELECT
            sum(bytes) AS total_bytes,
            sum(packets) AS packets,
            count() AS flows,
            uniqExact(exporter_ip) AS exporters,
            uniqExact(src_addr) AS src_hosts,
            uniqExact(dst_addr) AS dst_hosts,
            max(received_at) AS last_seen,
            sumIf(bytes, timestamp >= %(end)s - toIntervalSecond(%(rate_window)s)) * 8 / %(rate_window)s AS current_bps
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        """,
        params,
    ).result_rows[0]

    top_proto = _query(
        f"""
        SELECT protocol, sum(bytes) AS bytes
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol
        ORDER BY bytes DESC
        LIMIT 1
        """,
        params,
    ).result_rows

    return {
        "bytes": int(summary[0] or 0),
        "packets": int(summary[1] or 0),
        "flows": int(summary[2] or 0),
        "exporters": int(summary[3] or 0),
        "src_hosts": int(summary[4] or 0),
        "dst_hosts": int(summary[5] or 0),
        "last_seen": _to_iso_utc(summary[6]),
        "current_bps": float(summary[7] or 0),
        "top_protocol": (
            {"protocol": int(top_proto[0][0]), "name": _protocol_name(top_proto[0][0]), "bytes": int(top_proto[0][1])}
            if top_proto else None
        ),
    }


@router.get("/timeseries")
async def netflow_timeseries(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    span_hours = _window_seconds(start, end) / 3600
    bucket = 60 if span_hours <= 6 else 300 if span_hours <= 48 else 1800 if span_hours <= 168 else 7200
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT
            toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)) * 1000 AS ts,
            sum(bytes) * 8 / {bucket} AS bps,
            sum(bytes) AS total_bytes,
            sum(packets) AS packets,
            count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY ts
        ORDER BY ts
        """,
        {"start": start, "end": end, **extra_params},
    )
    return [
        {"ts": int(r[0]), "bps": float(r[1]), "bytes": int(r[2]), "packets": int(r[3]), "flows": int(r[4])}
        for r in res.result_rows
    ]


@router.get("/top-talkers")
async def netflow_top_talkers(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=10, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT
            toString(addr) AS ip,
            sum(raw_bytes) AS bytes,
            sum(raw_packets) AS packets,
            count() AS flows,
            sum(src_bytes) AS src_bytes,
            sum(dst_bytes) AS dst_bytes,
            sum(src_flows) AS src_flows,
            sum(dst_flows) AS dst_flows,
            min(timestamp) AS first_seen,
            max(timestamp) AS last_seen,
            groupUniqArray(8)(exporter) AS exporters,
            groupUniqArray(8)(protocol) AS protocols,
            groupUniqArray(8)(dst_port) AS ports
        FROM (
            SELECT
                src_addr AS addr,
                bytes AS raw_bytes,
                packets AS raw_packets,
                bytes AS src_bytes,
                0 AS dst_bytes,
                1 AS src_flows,
                0 AS dst_flows,
                timestamp,
                toString(exporter_ip) AS exporter,
                protocol,
                dst_port
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            UNION ALL
            SELECT
                dst_addr AS addr,
                bytes AS raw_bytes,
                packets AS raw_packets,
                0 AS src_bytes,
                bytes AS dst_bytes,
                0 AS src_flows,
                1 AS dst_flows,
                timestamp,
                toString(exporter_ip) AS exporter,
                protocol,
                dst_port
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        )
        GROUP BY addr
        ORDER BY bytes DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    return [
        {
            "ip": r[0],
            "bytes": int(r[1] or 0),
            "packets": int(r[2] or 0),
            "flows": int(r[3] or 0),
            "src_bytes": int(r[4] or 0),
            "dst_bytes": int(r[5] or 0),
            "src_flows": int(r[6] or 0),
            "dst_flows": int(r[7] or 0),
            "first_seen": _to_iso_utc(r[8]),
            "last_seen": _to_iso_utc(r[9]),
            "exporters": [{"ip": ip} for ip in (r[10] or [])],
            "protocols": [{"protocol": int(p), "name": _protocol_name(p)} for p in (r[11] or [])],
            "ports": [_port_summary(p) for p in (r[12] or []) if int(p) != 0],
        }
        for r in res.result_rows
    ]


@router.get("/top-endpoints")
async def netflow_top_endpoints(
    hours: int = Query(default=1, ge=1, le=720),
    limit: int = Query(default=10, ge=1, le=100),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Top endpoint IPs with source/destination contribution split."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT
            toString(addr) AS ip,
            sum(bytes) AS bytes,
            sum(packets) AS packets,
            sum(flows) AS flows,
            sum(src_bytes) AS src_bytes,
            sum(dst_bytes) AS dst_bytes,
            sum(src_flows) AS src_flows,
            sum(dst_flows) AS dst_flows,
            min(timestamp) AS first_seen,
            max(timestamp) AS last_seen,
            groupUniqArray(8)(exporter) AS exporters,
            groupUniqArray(8)(protocol) AS protocols,
            groupUniqArray(8)(dst_port) AS ports
        FROM (
            SELECT
                src_addr AS addr,
                bytes,
                packets,
                1 AS flows,
                bytes AS src_bytes,
                0 AS dst_bytes,
                1 AS src_flows,
                0 AS dst_flows,
                timestamp,
                toString(exporter_ip) AS exporter,
                protocol,
                dst_port
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            UNION ALL
            SELECT
                dst_addr AS addr,
                bytes,
                packets,
                1 AS flows,
                0 AS src_bytes,
                bytes AS dst_bytes,
                0 AS src_flows,
                1 AS dst_flows,
                timestamp,
                toString(exporter_ip) AS exporter,
                protocol,
                dst_port
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        )
        GROUP BY addr
        ORDER BY bytes DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    return [
        {
            "ip": r[0],
            "bytes": int(r[1] or 0),
            "packets": int(r[2] or 0),
            "flows": int(r[3] or 0),
            "src_bytes": int(r[4] or 0),
            "dst_bytes": int(r[5] or 0),
            "src_flows": int(r[6] or 0),
            "dst_flows": int(r[7] or 0),
            "first_seen": _to_iso_utc(r[8]),
            "last_seen": _to_iso_utc(r[9]),
            "exporters": [{"ip": ip} for ip in (r[10] or [])],
            "protocols": [{"protocol": int(p), "name": _protocol_name(p)} for p in (r[11] or [])],
            "ports": [_port_summary(p) for p in (r[12] or []) if int(p) != 0],
        }
        for r in res.result_rows
    ]


@router.get("/top-conversations")
async def netflow_top_conversations(
    hours: int = Query(default=1, ge=1, le=720),
    limit: int = Query(default=10, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT
            src,
            dst,
            protocol,
            dst_port,
            groupUniqArray(10)(src_port) AS src_ports,
            sum(raw_bytes) AS bytes,
            sum(raw_packets) AS packets,
            count() AS flows,
            groupUniqArray(10)(exporter) AS exporters,
            groupUniqArray(10)(input_snmp) AS input_snmp,
            groupUniqArray(10)(output_snmp) AS output_snmp,
            min(timestamp) AS first_seen,
            max(timestamp) AS last_seen,
            max(received_at) AS received_at,
            avg(toInt64(last_switched_ms) - toInt64(first_switched_ms)) AS avg_duration_ms,
            groupBitOr(toUInt64(tcp_flags)) AS tcp_flags,
            avg(raw_bytes) AS avg_bytes,
            avg(raw_packets) AS avg_packets
        FROM (
            SELECT
                toString(src_addr) AS src,
                toString(dst_addr) AS dst,
                protocol,
                src_port,
                dst_port,
                bytes AS raw_bytes,
                packets AS raw_packets,
                toString(exporter_ip) AS exporter,
                input_snmp,
                output_snmp,
                timestamp,
                received_at,
                last_switched_ms,
                first_switched_ms,
                tcp_flags
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        )
        GROUP BY src, dst, protocol, dst_port
        ORDER BY bytes DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )

    exporter_ips = sorted({ip for row in res.result_rows for ip in (row[8] or [])})
    exporter_names: dict[str, str] = {}
    if exporter_ips:
        devices_q = await db.execute(
            select(Device.ip_address, Device.hostname).where(
                cast(Device.ip_address, String).in_(exporter_ips + [f"{ip}/32" for ip in exporter_ips])
            )
        )
        exporter_names = {
            str(row.ip_address).split("/")[0]: row.hostname
            for row in devices_q.all()
        }

    iface_keys = []
    for row in res.result_rows:
        exporters = row[8] or []
        indexes = [int(v) for v in [*(row[9] or []), *(row[10] or [])] if int(v) != 0]
        iface_keys.extend((ip, idx) for ip in exporters for idx in indexes)
    iface_meta = await _resolve_interface_names(iface_keys, db)

    return [
        {
            "src": r[0],
            "dst": r[1],
            "protocol": int(r[2]),
            "protocol_name": _protocol_name(r[2]),
            "dst_port": int(r[3]),
            "service": _port_name(r[3], r[2]),
            "application": _application_for_port(int(r[3])),
            "port_class": _port_class(int(r[3])),
            "src_ports": [int(v) for v in (r[4] or []) if int(v) != 0],
            "bytes": int(r[5]),
            "packets": int(r[6]),
            "flows": int(r[7]),
            "exporters": [
                {"ip": ip, "hostname": exporter_names.get(ip)}
                for ip in (r[8] or [])
            ],
            "input_snmp": [int(v) for v in (r[9] or []) if int(v) != 0],
            "output_snmp": [int(v) for v in (r[10] or []) if int(v) != 0],
            "input_interfaces": [
                _interface_payload(ip, int(idx), iface_meta.get((ip, int(idx))))
                for ip in (r[8] or [])
                for idx in (r[9] or [])
                if int(idx) != 0
            ],
            "output_interfaces": [
                _interface_payload(ip, int(idx), iface_meta.get((ip, int(idx))))
                for ip in (r[8] or [])
                for idx in (r[10] or [])
                if int(idx) != 0
            ],
            "first_seen": _to_iso_utc(r[11]),
            "last_seen": _to_iso_utc(r[12]),
            "received_at": _to_iso_utc(r[13]),
            "avg_duration_ms": round(float(r[14] or 0), 1),
            "tcp_flags": int(r[15] or 0),
            "avg_bytes": float(r[16] or 0),
            "avg_packets": float(r[17] or 0),
        }
        for r in res.result_rows
    ]


@router.get("/protocols")
async def netflow_protocols(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT protocol, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows,
               min(timestamp) AS first_seen,
               max(timestamp) AS last_seen
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol
        ORDER BY bytes DESC
        LIMIT 20
        """,
        {"start": start, "end": end, **extra_params},
    )
    port_res = _query(
        f"""
        SELECT protocol, dst_port, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND dst_port != 0
        GROUP BY protocol, dst_port
        ORDER BY bytes DESC
        LIMIT 200
        """,
        {"start": start, "end": end, **extra_params},
    )
    ports_by_protocol: dict[int, list[dict]] = {}
    for proto, port, bytes_, packets, flows in port_res.result_rows:
        bucket = ports_by_protocol.setdefault(int(proto), [])
        if len(bucket) < 10:
            bucket.append(_port_summary(port, int(proto), bytes_, packets, flows))
    return [
        {
            "protocol": int(r[0]),
            "name": _protocol_name(r[0]),
            "bytes": int(r[1]),
            "packets": int(r[2]),
            "flows": int(r[3]),
            "ports": ports_by_protocol.get(int(r[0]), []),
            "first_seen": _to_iso_utc(r[4]),
            "last_seen": _to_iso_utc(r[5]),
        }
        for r in res.result_rows
    ]


@router.get("/ports")
async def netflow_ports(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=10, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT protocol, dst_port, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol, dst_port
        ORDER BY bytes DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    return [
        {
            "protocol": int(r[0]),
            "protocol_name": _protocol_name(r[0]),
            "port": int(r[1]),
            "service": _port_name(r[1], r[0]),
            "bytes": int(r[2]),
            "packets": int(r[3]),
            "flows": int(r[4]),
        }
        for r in res.result_rows
    ]


@router.get("/applications")
async def netflow_applications(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """High-level application breakdown driven by destination port heuristics."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Always show all application buckets — pinning to a specific app would collapse
    # the donut to a single slice.
    scope_for_apps = {**scope, "application": None}
    extra_sql, extra_params = _scope(**scope_for_apps)
    res = _query(
        f"""
        SELECT dst_port, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY dst_port
        """,
        {"start": start, "end": end, **extra_params},
    )
    buckets: dict[str, dict] = {}
    for port, bytes_, packets, flows in res.result_rows:
        name = _application_for_port(int(port))
        b = buckets.setdefault(name, {"name": name, "bytes": 0, "packets": 0, "flows": 0, "ports": []})
        b["bytes"] += int(bytes_ or 0)
        b["packets"] += int(packets or 0)
        b["flows"] += int(flows or 0)
        if int(port) != 0:
            b["ports"].append(_port_summary(port, None, bytes_, packets, flows))
    for bucket in buckets.values():
        bucket["ports"] = sorted(bucket["ports"], key=lambda p: p["bytes"], reverse=True)[:8]
    return sorted(buckets.values(), key=lambda r: r["bytes"], reverse=True)


@router.get("/device-status")
async def netflow_device_status(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Synthetic per-collector signals: latency proxy, packet-loss approximation, uptime."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    span_seconds = _window_seconds(start, end)
    extra_sql, extra_params = _scope(**scope)
    res = _query(
        f"""
        SELECT
            count() AS total_flows,
            sum(packets) AS total_packets,
            sum(bytes) AS total_bytes,
            avg(toUInt64(last_switched_ms - first_switched_ms)) AS avg_duration_ms,
            countIf(packets = 0) AS empty_flows,
            countIf(bitAnd(toUInt8(tcp_flags), 4) = 4) AS rst_flows,
            uniqExact(exporter_ip) AS exporter_count,
            max(received_at) AS last_seen
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        """,
        {"start": start, "end": end, **extra_params},
    ).result_rows[0]

    import math
    def _safe_num(v) -> float:
        try:
            f = float(v) if v is not None else 0.0
            return 0.0 if math.isnan(f) or math.isinf(f) else f
        except (TypeError, ValueError):
            return 0.0

    flows = int(res[0] or 0)
    packets = int(res[1] or 0)
    avg_dur_ms = _safe_num(res[3])
    rst_flows = int(res[5] or 0)
    last_seen = res[7]

    # ms-per-flow proxy for latency. Cap to a sane upper bound for the gauge.
    latency_ms = round(min(max(avg_dur_ms / 1000.0 if avg_dur_ms > 1000 else avg_dur_ms, 0), 250), 1)
    # RST-to-flow ratio is our packet-loss proxy (flow-level integrity hint).
    loss_pct = round((rst_flows / flows * 100) if flows else 0, 2)
    # Uptime expressed as fraction of 5-minute slots that produced data over the window.
    slots_total = max(1, int(span_seconds / 300))
    slots_seen = _query(
        f"""
        SELECT countDistinct(toStartOfFiveMinutes(timestamp))
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        """,
        {"start": start, "end": end, **extra_params},
    ).result_rows[0][0]
    uptime_pct = round(min(100, (int(slots_seen or 0) / slots_total) * 100), 1)

    return {
        "latency_ms": latency_ms,
        "packet_loss_pct": loss_pct,
        "uptime_pct": uptime_pct,
        "flows": flows,
        "packets": packets,
        "exporters": int(res[6] or 0),
        "last_seen": _to_iso_utc(last_seen),
    }


@router.get("/heatmap")
async def netflow_heatmap(
    hours: int = Query(default=168, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Hour-of-day x day-of-week traffic heatmap over the selected window."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Don't filter by hour/dow — the heatmap is the picker for those.
    scope_for_hm = {**scope, "hour": None, "dow": None}
    extra_sql, extra_params = _scope(**scope_for_hm)
    res = _query(
        f"""
        SELECT
            toDayOfWeek(timestamp) AS dow,
            toHour(timestamp) AS hour,
            sum(bytes) AS bytes,
            count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY dow, hour
        ORDER BY dow, hour
        """,
        {"start": start, "end": end, **extra_params},
    )
    cells = [
        {"dow": int(r[0]), "hour": int(r[1]), "bytes": int(r[2] or 0), "flows": int(r[3] or 0)}
        for r in res.result_rows
    ]
    max_bytes = max((c["bytes"] for c in cells), default=0)
    return {"max_bytes": max_bytes, "cells": cells}


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


@router.get("/interfaces")
async def netflow_interfaces(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=5, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    exporter: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Top SNMP interfaces by traffic. Aggregates ingress (input_snmp) + egress (output_snmp) per (exporter, ifIndex). Joined with SNMP interface names from PG."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _exporter_filter(exporter)
    res = _query(
        f"""
        SELECT
            toString(exporter_ip) AS exporter_ip,
            ifindex,
            sum(in_bytes) AS in_bytes,
            sum(out_bytes) AS out_bytes,
            sum(in_packets) AS in_packets,
            sum(out_packets) AS out_packets,
            sum(in_flows) AS in_flows,
            sum(out_flows) AS out_flows
        FROM (
            SELECT exporter_ip, input_snmp AS ifindex,
                   sum(bytes) AS in_bytes, 0 AS out_bytes,
                   sum(packets) AS in_packets, 0 AS out_packets,
                   count() AS in_flows, 0 AS out_flows
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
              AND input_snmp != 0
            GROUP BY exporter_ip, input_snmp
            UNION ALL
            SELECT exporter_ip, output_snmp AS ifindex,
                   0 AS in_bytes, sum(bytes) AS out_bytes,
                   0 AS in_packets, sum(packets) AS out_packets,
                   0 AS in_flows, count() AS out_flows
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
              AND output_snmp != 0
            GROUP BY exporter_ip, output_snmp
        )
        GROUP BY exporter_ip, ifindex
        ORDER BY (in_bytes + out_bytes) DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    iface_meta = await _resolve_interface_names(res.result_rows, db)
    out = []
    for r in res.result_rows:
        key = (r[0], int(r[1]))
        meta = iface_meta.get(key, {})
        out.append({
            "exporter_ip": r[0],
            "ifindex": int(r[1]),
            "if_name": meta.get("if_name"),
            "if_descr": meta.get("if_descr"),
            "if_alias": meta.get("if_alias"),
            "if_speed": meta.get("if_speed"),
            "device_hostname": meta.get("device_hostname"),
            "display_name": _interface_display_name(meta, int(r[1])),
            "in_bytes": int(r[2] or 0),
            "out_bytes": int(r[3] or 0),
            "bytes": int((r[2] or 0) + (r[3] or 0)),
            "in_packets": int(r[4] or 0),
            "out_packets": int(r[5] or 0),
            "packets": int((r[4] or 0) + (r[5] or 0)),
            "in_flows": int(r[6] or 0),
            "out_flows": int(r[7] or 0),
            "flows": int((r[6] or 0) + (r[7] or 0)),
        })
    return out


# ─────────────────────────────────────────────────────────────────
# DSCP / QoS distribution
# ─────────────────────────────────────────────────────────────────

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


@router.get("/dscp")
async def netflow_dscp(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Group flows by IP ToS DSCP code-point (top 6 bits of ToS)."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Don't apply the dscp filter to itself, otherwise the chart collapses to one bar.
    scope_for_dscp = {**scope, "dscp_filter": None}
    extra_sql, extra_params = _scope(**scope_for_dscp)
    res = _query(
        f"""
        SELECT bitShiftRight(tos, 2) AS dscp, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY dscp
        ORDER BY bytes DESC
        """,
        {"start": start, "end": end, **extra_params},
    )
    return [
        {"dscp": int(r[0]), "label": _dscp_label(r[0]), "bytes": int(r[1] or 0), "packets": int(r[2] or 0), "flows": int(r[3] or 0)}
        for r in res.result_rows
    ]


# ─────────────────────────────────────────────────────────────────
# TCP-flag breakdown (scan / flood detection)
# ─────────────────────────────────────────────────────────────────

@router.get("/tcp-flags")
async def netflow_tcp_flags(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Bucket TCP flows by which control bits ever fired. Useful for spotting scans (SYN-only) and floods."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Don't apply the tcp_flag filter to itself.
    scope_for_flags = {**scope, "tcp_flag": None, "protocol": None}
    extra_sql, extra_params = _scope(**scope_for_flags)
    res = _query(
        f"""
        SELECT
            countIf(bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0) AS syn_only,
            countIf(bitAnd(tcp_flags, 16) != 0 AND bitAnd(tcp_flags, 2) = 0) AS ack_only,
            countIf(bitAnd(tcp_flags, 4) != 0) AS rst,
            countIf(bitAnd(tcp_flags, 1) != 0) AS fin,
            countIf(bitAnd(tcp_flags, 8) != 0) AS psh,
            countIf(bitAnd(tcp_flags, 32) != 0) AS urg,
            countIf(tcp_flags = 0) AS no_flags,
            count() AS total
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND protocol = 6
        """,
        {"start": start, "end": end, **extra_params},
    ).result_rows[0]
    return {
        "total_tcp": int(res[7] or 0),
        "syn_only": int(res[0] or 0),
        "ack_only": int(res[1] or 0),
        "rst": int(res[2] or 0),
        "fin": int(res[3] or 0),
        "psh": int(res[4] or 0),
        "urg": int(res[5] or 0),
        "no_flags": int(res[6] or 0),
    }


# ─────────────────────────────────────────────────────────────────
# Network-class distribution (RFC1918 / public / multicast / loopback)
# ─────────────────────────────────────────────────────────────────

@router.get("/network-classes")
async def netflow_network_classes(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Classify the source/destination address space — Private / Public / Multicast / Loopback / Link-local."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Don't filter the breakdown by itself.
    scope_for_nc = {**scope, "net_class": None}
    extra_sql, extra_params = _scope(**scope_for_nc)
    case_expr = """
        CASE
          WHEN isIPAddressInRange(toString(addr), '10.0.0.0/8') THEN 'Private 10/8'
          WHEN isIPAddressInRange(toString(addr), '172.16.0.0/12') THEN 'Private 172.16/12'
          WHEN isIPAddressInRange(toString(addr), '192.168.0.0/16') THEN 'Private 192.168/16'
          WHEN isIPAddressInRange(toString(addr), '127.0.0.0/8') THEN 'Loopback'
          WHEN isIPAddressInRange(toString(addr), '169.254.0.0/16') THEN 'Link-local'
          WHEN isIPAddressInRange(toString(addr), '224.0.0.0/4') THEN 'Multicast'
          WHEN isIPAddressInRange(toString(addr), '100.64.0.0/10') THEN 'CGNAT'
          ELSE 'Public'
        END
    """
    res = _query(
        f"""
        SELECT
            klass,
            sum(bytes) AS bytes,
            sum(packets) AS packets,
            count() AS flows
        FROM (
            SELECT src_addr AS addr, bytes, packets, 'src' AS dir FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            UNION ALL
            SELECT dst_addr AS addr, bytes, packets, 'dst' AS dir FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        )
        ARRAY JOIN [{case_expr.strip()}] AS klass
        GROUP BY klass
        ORDER BY bytes DESC
        """,
        {"start": start, "end": end, **extra_params},
    )
    return [
        {"name": r[0], "bytes": int(r[1] or 0), "packets": int(r[2] or 0), "flows": int(r[3] or 0)}
        for r in res.result_rows
    ]


# ─────────────────────────────────────────────────────────────────
# Forensics: raw flow record search
# ─────────────────────────────────────────────────────────────────

@router.get("/forensics")
async def netflow_forensics(
    hours: int = Query(default=1, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=2000),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    src: str | None = Query(default=None, description="Source IP or CIDR"),
    dst: str | None = Query(default=None, description="Destination IP or CIDR"),
    proto: int | None = Query(default=None),
    src_port: int | None = Query(default=None),
    dst_port: int | None = Query(default=None),
    min_bytes: int | None = Query(default=None),
    forensics_dscp: int | None = Query(default=None, alias="dscp_eq"),
    sort: str = Query(default="timestamp", regex="^(timestamp|bytes|packets|src_port|dst_port)$"),
    order: str = Query(default="desc", regex="^(asc|desc)$"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Raw-flow forensic search. Returns un-aggregated records with full multi-field filtering."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    where = [f"timestamp BETWEEN %(start)s AND %(end)s{extra_sql}"]
    params: dict = {"start": start, "end": end, "limit": limit, **extra_params}
    if src:
        try:
            ipaddress.ip_network(src, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid src CIDR: {src}")
        where.append("isIPAddressInRange(toString(src_addr), %(src_cidr)s)")
        params["src_cidr"] = src if "/" in src else f"{src}/32"
    if dst:
        try:
            ipaddress.ip_network(dst, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid dst CIDR: {dst}")
        where.append("isIPAddressInRange(toString(dst_addr), %(dst_cidr)s)")
        params["dst_cidr"] = dst if "/" in dst else f"{dst}/32"
    if proto is not None:
        where.append("protocol = %(proto)s"); params["proto"] = int(proto)
    if src_port is not None:
        where.append("src_port = %(sp)s"); params["sp"] = int(src_port)
    if dst_port is not None:
        where.append("dst_port = %(dp)s"); params["dp"] = int(dst_port)
    if min_bytes is not None:
        where.append("bytes >= %(min_bytes)s"); params["min_bytes"] = int(min_bytes)
    if forensics_dscp is not None:
        where.append("bitShiftRight(tos, 2) = %(fdscp)s"); params["fdscp"] = int(forensics_dscp)

    sort_col = sort
    res = _query(
        f"""
        SELECT
            timestamp,
            toString(exporter_ip) AS exporter_ip,
            toString(src_addr) AS src,
            toString(dst_addr) AS dst,
            src_port,
            dst_port,
            protocol,
            tcp_flags,
            tos,
            input_snmp,
            output_snmp,
            packets,
            bytes,
            (last_switched_ms - first_switched_ms) AS duration_ms
        FROM zenplus.flow_records
        WHERE {' AND '.join(where)}
        ORDER BY {sort_col} {order.upper()}
        LIMIT %(limit)s
        """,
        params,
    )
    iface_keys = []
    for r in res.result_rows:
        exporter_ip = str(r[1])
        if int(r[9] or 0) != 0:
            iface_keys.append((exporter_ip, int(r[9])))
        if int(r[10] or 0) != 0:
            iface_keys.append((exporter_ip, int(r[10])))
    iface_meta = await _resolve_interface_names(iface_keys, db)
    return [
        {
            "timestamp": _to_iso_utc(r[0]),
            "exporter_ip": r[1],
            "src": r[2],
            "dst": r[3],
            "src_port": int(r[4]),
            "dst_port": int(r[5]),
            "protocol": int(r[6]),
            "protocol_name": _protocol_name(r[6]),
            "tcp_flags": int(r[7]),
            "dscp": int(r[8]) >> 2,
            "input_snmp": int(r[9]),
            "output_snmp": int(r[10]),
            "input_interface": _interface_payload(r[1], int(r[9]), iface_meta.get((r[1], int(r[9])))) if int(r[9]) != 0 else None,
            "output_interface": _interface_payload(r[1], int(r[10]), iface_meta.get((r[1], int(r[10])))) if int(r[10]) != 0 else None,
            "packets": int(r[11]),
            "bytes": int(r[12]),
            "duration_ms": int(r[13]),
            "service": _port_name(r[5], r[6]),
        }
        for r in res.result_rows
    ]


# ─────────────────────────────────────────────────────────────────
# Capacity planning — 95th-percentile + utilization vs ifSpeed
# ─────────────────────────────────────────────────────────────────


@router.get("/capacity")
async def netflow_capacity(
    hours: int = Query(default=168, ge=1, le=720),
    limit: int = Query(default=20, ge=1, le=100),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    exporter: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-interface 95th-percentile + average + max bps, with utilization vs SNMP ifSpeed."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _exporter_filter(exporter)
    # Note: capacity always covers the whole window, ignoring detail-level filters by design.
    bucket = 300  # 5-minute slots
    res = _query(
        f"""
        SELECT
            exporter_ip_str,
            ifindex,
            quantile(0.95)(slot_bps) AS p95,
            avg(slot_bps) AS avg_bps,
            max(slot_bps) AS max_bps,
            sum(slot_bytes) AS total_bytes,
            sum(slot_packets) AS total_packets
        FROM (
            SELECT
                toString(exporter_ip) AS exporter_ip_str,
                ifindex,
                toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS slot,
                sum(byte_count) AS slot_bytes,
                sum(packet_count) AS slot_packets,
                sum(byte_count) * 8.0 / {bucket} AS slot_bps
            FROM (
                SELECT timestamp, exporter_ip, input_snmp AS ifindex,
                       bytes AS byte_count, packets AS packet_count
                FROM zenplus.flow_records
                WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
                  AND input_snmp != 0
                UNION ALL
                SELECT timestamp, exporter_ip, output_snmp AS ifindex,
                       bytes AS byte_count, packets AS packet_count
                FROM zenplus.flow_records
                WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
                  AND output_snmp != 0
            )
            GROUP BY exporter_ip, ifindex, slot
        )
        GROUP BY exporter_ip_str, ifindex
        ORDER BY p95 DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    rows = res.result_rows
    iface_meta = await _resolve_interface_names(rows, db)
    out = []
    for r in rows:
        key = (r[0], int(r[1]))
        meta = iface_meta.get(key, {})
        speed = meta.get("if_speed") or 0
        p95 = float(r[2] or 0)
        avg = float(r[3] or 0)
        mx = float(r[4] or 0)
        out.append({
            "exporter_ip": r[0],
            "ifindex": int(r[1]),
            "if_name": meta.get("if_name"),
            "if_descr": meta.get("if_descr"),
            "if_alias": meta.get("if_alias"),
            "if_speed": speed,
            "device_hostname": meta.get("device_hostname"),
            "display_name": _interface_display_name(meta, int(r[1])),
            "p95_bps": p95,
            "avg_bps": avg,
            "max_bps": mx,
            "utilization_p95_pct": round((p95 / speed * 100), 2) if speed else None,
            "utilization_max_pct": round((mx / speed * 100), 2) if speed else None,
            "total_bytes": int(r[5] or 0),
            "total_packets": int(r[6] or 0),
        })
    return out


# ─────────────────────────────────────────────────────────────────
# Anomaly / security detection — Plixer-FA-style algorithms
# ─────────────────────────────────────────────────────────────────


@router.get("/anomalies")
async def netflow_anomalies(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    exporter: str | None = Query(default=None),
    user: User = Depends(get_current_user),
):
    """Run a battery of flow-derived security detectors and return any matches."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _exporter_filter(exporter)
    # Anomaly detection always runs unfiltered by drill-down dimensions.
    base_params = {"start": start, "end": end, **extra_params}
    findings: list[dict] = []

    # 1. SYN scan: hosts originating SYN-only flows to many distinct destinations
    syn_scan = _query(
        f"""
        SELECT
            toString(src_addr) AS src,
            uniqExact(dst_addr) AS dst_count,
            uniqExact(dst_port) AS port_count,
            count() AS flows,
            sum(bytes) AS bytes
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND protocol = 6
          AND bitAnd(tcp_flags, 2) != 0
          AND bitAnd(tcp_flags, 16) = 0
        GROUP BY src
        HAVING dst_count >= 100
        ORDER BY dst_count DESC
        LIMIT 8
        """,
        base_params,
    )
    for r in syn_scan.result_rows:
        sev = "critical" if r[1] > 5000 else "warning"
        findings.append({
            "id": f"syn-scan-{r[0]}",
            "kind": "syn_scan",
            "severity": sev,
            "title": "TCP SYN scan",
            "description": f"{r[0]} initiated SYN-only probes to {int(r[1]):,} distinct destinations across {int(r[2]):,} ports",
            "metric": int(r[1]),
            "metric_label": "destinations",
            "src": r[0],
            "flows": int(r[3]),
            "bytes": int(r[4]),
        })

    # 2. RST flood: high-RST emitters
    rst_flood = _query(
        f"""
        SELECT
            toString(src_addr) AS src,
            countIf(bitAnd(tcp_flags, 4) != 0) AS rst,
            count() AS total
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND protocol = 6
        GROUP BY src
        HAVING total >= 1000 AND rst / total >= 0.5
        ORDER BY rst DESC
        LIMIT 5
        """,
        base_params,
    )
    for r in rst_flood.result_rows:
        findings.append({
            "id": f"rst-flood-{r[0]}",
            "kind": "rst_flood",
            "severity": "warning",
            "title": "Elevated RST ratio",
            "description": f"{r[0]} produced {int(r[1]):,} RSTs out of {int(r[2]):,} TCP flows ({float(r[1])/float(r[2])*100:.0f}%)",
            "metric": int(r[1]),
            "metric_label": "RSTs",
            "src": r[0],
        })

    # 3. RFC1918 leakage: private addresses appearing as dst on flows leaving via internet-facing iface
    rfc1918_leak = _query(
        f"""
        SELECT
            toString(src_addr) AS src,
            toString(dst_addr) AS dst,
            sum(bytes) AS bytes,
            count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND (isIPAddressInRange(toString(src_addr), '10.0.0.0/8')
               OR isIPAddressInRange(toString(src_addr), '172.16.0.0/12')
               OR isIPAddressInRange(toString(src_addr), '192.168.0.0/16'))
          AND NOT (isIPAddressInRange(toString(dst_addr), '10.0.0.0/8')
                   OR isIPAddressInRange(toString(dst_addr), '172.16.0.0/12')
                   OR isIPAddressInRange(toString(dst_addr), '192.168.0.0/16')
                   OR isIPAddressInRange(toString(dst_addr), '127.0.0.0/8')
                   OR isIPAddressInRange(toString(dst_addr), '224.0.0.0/4'))
          AND dst_port IN (22, 23, 3389, 445, 1433, 3306, 5432)
        GROUP BY src, dst
        ORDER BY bytes DESC
        LIMIT 5
        """,
        base_params,
    )
    for r in rfc1918_leak.result_rows:
        findings.append({
            "id": f"sensitive-egress-{r[0]}-{r[1]}",
            "kind": "sensitive_egress",
            "severity": "critical",
            "title": "Sensitive port egress to public",
            "description": f"{r[0]} reached {r[1]} on a privileged service port from inside the private network",
            "src": r[0],
            "dst": r[1],
            "bytes": int(r[2]),
            "flows": int(r[3]),
        })

    # 4. ICMP flood
    icmp = _query(
        f"""
        SELECT toString(src_addr) AS src, count() AS flows, sum(packets) AS pkts, sum(bytes) AS bytes
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND protocol = 1
        GROUP BY src
        HAVING flows >= 5000
        ORDER BY pkts DESC
        LIMIT 3
        """,
        base_params,
    )
    for r in icmp.result_rows:
        findings.append({
            "id": f"icmp-flood-{r[0]}",
            "kind": "icmp_flood",
            "severity": "warning",
            "title": "ICMP flood candidate",
            "description": f"{r[0]} generated {int(r[1]):,} ICMP flows ({int(r[2]):,} packets)",
            "src": r[0],
            "flows": int(r[1]),
            "bytes": int(r[3]),
        })

    # 5. Top loud hosts (volumetric outliers via mean+3σ proxy)
    volume = _query(
        f"""
        SELECT
            toString(src_addr) AS src,
            sum(bytes) AS bytes,
            count() AS flows
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY src
        ORDER BY bytes DESC
        LIMIT 5
        """,
        base_params,
    )
    rows = volume.result_rows
    if rows:
        total = sum(int(r[1]) for r in rows)
        if rows and int(rows[0][1]) > total * 0.5 and int(rows[0][1]) > 10_000_000_000:
            r = rows[0]
            findings.append({
                "id": f"loud-host-{r[0]}",
                "kind": "volumetric_outlier",
                "severity": "info",
                "title": "Volumetric outlier",
                "description": f"{r[0]} alone produced more than half of all traffic in this window",
                "src": r[0],
                "bytes": int(r[1]),
                "flows": int(r[2]),
            })

    return {
        "total": len(findings),
        "findings": findings,
        "generated_at": _to_iso_utc(datetime.now(timezone.utc)),
    }


# ─────────────────────────────────────────────────────────────────
# Saved Views CRUD
# ─────────────────────────────────────────────────────────────────


class SavedViewIn(BaseModel):
    name: str = Field(..., max_length=120)
    description: str | None = Field(None, max_length=500)
    query: dict
    pinned: bool = False


class SavedViewOut(BaseModel):
    id: str
    name: str
    description: str | None
    query: dict
    pinned: bool
    created_at: str
    updated_at: str


@router.get("/saved-views", response_model=list[SavedViewOut])
async def list_saved_views(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(NetflowSavedView).order_by(NetflowSavedView.pinned.desc(), NetflowSavedView.updated_at.desc()))).scalars().all()
    return [
        SavedViewOut(
            id=str(v.id),
            name=v.name,
            description=v.description,
            query=v.query,
            pinned=v.pinned,
            created_at=v.created_at.isoformat(),
            updated_at=v.updated_at.isoformat(),
        )
        for v in rows
    ]


@router.post("/saved-views", response_model=SavedViewOut)
async def create_saved_view(
    body: SavedViewIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    view = NetflowSavedView(name=body.name, description=body.description, query=body.query, pinned=body.pinned, owner_id=user.id)
    db.add(view)
    await db.commit()
    await db.refresh(view)
    return SavedViewOut(
        id=str(view.id),
        name=view.name,
        description=view.description,
        query=view.query,
        pinned=view.pinned,
        created_at=view.created_at.isoformat(),
        updated_at=view.updated_at.isoformat(),
    )


@router.delete("/saved-views/{view_id}")
async def delete_saved_view(
    view_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(NetflowSavedView).where(NetflowSavedView.id == view_id))
    view = res.scalar_one_or_none()
    if not view:
        raise HTTPException(status_code=404, detail="Saved view not found")
    await db.delete(view)
    await db.commit()
    return {"deleted": view_id}


@router.get("/exporters")
async def netflow_exporters(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts)
    # Always show every exporter, regardless of which one is currently selected.
    scope_for_exp = {**scope, "exporter": None}
    extra_sql, extra_params = _scope(**scope_for_exp)
    res = _query(
        f"""
        SELECT
            toString(exporter_ip) AS exporter_ip,
            sum(bytes) AS bytes,
            sum(packets) AS packets,
            count() AS flows,
            max(received_at) AS last_seen
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY exporter_ip
        ORDER BY bytes DESC
        LIMIT 100
        """,
        {"start": start, "end": end, **extra_params},
    )
    return [
        {"exporter_ip": r[0], "bytes": int(r[1]), "packets": int(r[2]), "flows": int(r[3]), "last_seen": _to_iso_utc(r[4])}
        for r in res.result_rows
    ]
