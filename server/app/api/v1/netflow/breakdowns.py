"""NetFlow top-N and distribution breakdowns: talkers, endpoints,
conversations, protocols, ports, applications, interfaces, DSCP, TCP flags,
network classes, countries and IP groups."""

from __future__ import annotations

import ipaddress
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.services import geoip
from app.models.device import Device
from app.models.user import User

from .common import (
    QOS_ROLLUP_TABLE,
    ROLLUP_TABLE,
    _NET_CLASS_RANGES,
    _PROTO_APPLICATIONS,
    _application_for_flow,
    _dscp_label,
    _enrich_hosts_async,
    _exporter_filter,
    _floor_hour,
    _group_predicate,
    _interface_display_name,
    _interface_payload,
    _load_ip_groups,
    _port_class,
    _port_name,
    _port_summary,
    _protocol_name,
    _qos_rollup_eligible,
    _query,
    _resolve_interface_names,
    _resolve_window,
    _rollup_eligible,
    _scope,
    _to_iso_utc,
    scope_params,
)

router = APIRouter()


async def _top_ips_detail(
    start: datetime,
    end: datetime,
    limit: int,
    extra_sql: str,
    extra_params: dict,
    rank: str = "bytes",
    use_rollup: bool = False,
) -> list[dict]:
    """Two-phase top-N endpoint IPs.

    Phase 1 ranks addresses with cheap sum-only aggregation; phase 2 computes
    the expensive per-IP detail (uniq arrays, first/last seen) only for the
    winners. On a 7-day window this is ~5x faster than one pass that builds
    heavy aggregate states for every address on the network.

    With use_rollup the same two phases run over the hourly conversation
    rollup (caller must have checked _rollup_eligible and passed a matching
    extra_sql): flow counts come from sum(flows) and seen-times from the
    pre-aggregated first/last columns.
    """
    rank_expr = {
        "bytes": "sum(bytes)",
        "src_bytes": "sumIf(bytes, role.2 = 1)",
        "dst_bytes": "sumIf(bytes, role.2 = 0)",
    }[rank]
    if use_rollup:
        table, qual = ROLLUP_TABLE, "flow_conversations_1h"
        flows_expr, flows_if = "sum(flows)", "sumIf(flows, role.2 = {r})"
        first_expr, last_expr = "min(first_seen)", "max(last_seen)"
        start = _floor_hour(start)
    else:
        table, qual = "zenplus.flow_records", "flow_records"
        flows_expr, flows_if = "count()", "countIf(role.2 = {r})"
        first_expr, last_expr = "min(timestamp)", "max(timestamp)"
    params = {"start": start, "end": end, "limit": limit, **extra_params}
    # ARRAY JOIN attributes each record to both its endpoints in ONE table scan
    # (the old UNION ALL form scanned the window twice).
    top = await _query(
        f"""
        SELECT toString(role.1) AS ip
        FROM {table}
        ARRAY JOIN [(src_addr, toUInt8(1)), (dst_addr, toUInt8(0))] AS role
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY ip
        ORDER BY {rank_expr} DESC
        LIMIT %(limit)s
        """,
        params,
    )
    ips = [r[0] for r in top.result_rows]
    if not ips:
        return []
    detail_params = {**params, "ips": ips}
    # role.1 is compared as native IPv4 — a toString() here would convert
    # every expanded row in the window before filtering (see top-conversations
    # for the latency cliff that caused).
    res = await _query(
        f"""
        SELECT
            toString(role.1) AS ip,
            sum({qual}.bytes) AS total_bytes,
            sum({qual}.packets) AS total_packets,
            {flows_expr} AS total_flows,
            sumIf({qual}.bytes, role.2 = 1) AS src_bytes,
            sumIf({qual}.bytes, role.2 = 0) AS dst_bytes,
            {flows_if.format(r=1)} AS src_flows,
            {flows_if.format(r=0)} AS dst_flows,
            {first_expr} AS agg_first_seen,
            {last_expr} AS agg_last_seen,
            groupUniqArray(8)(toString(exporter_ip)) AS exporters,
            groupUniqArray(8)(protocol) AS protocols,
            groupUniqArray(8)(dst_port) AS ports
        FROM {table}
        ARRAY JOIN [(src_addr, toUInt8(1)), (dst_addr, toUInt8(0))] AS role
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND role.1 IN %(ips)s
        GROUP BY ip
        """,
        detail_params,
    )
    by_ip = {
        r[0]: {
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
    }
    # Preserve phase-1 ranking order.
    return [by_ip[ip] for ip in ips if ip in by_ip]


@router.get("/top-talkers")
async def netflow_top_talkers(
    hours: int = Query(default=24, ge=1, le=720),
    minutes: int | None = Query(default=None, ge=1, le=43200),
    limit: int = Query(default=10, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    resolve: int = Query(default=0, description="1 = reverse-DNS resolve endpoint IPs"),
    by: str = Query(default="total", regex="^(total|src|dst)$", description="Ranking: total, bytes sent (src) or received (dst)"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts, minutes)
    use_rollup = _rollup_eligible(start, end, scope)
    extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h" if use_rollup else "flow_records")
    rank = {"total": "bytes", "src": "src_bytes", "dst": "dst_bytes"}[by]
    rows = await _top_ips_detail(start, end, limit, extra_sql, extra_params, rank, use_rollup=use_rollup)
    return await _enrich_hosts_async(rows, bool(resolve))


@router.get("/top-endpoints")
async def netflow_top_endpoints(
    hours: int = Query(default=1, ge=1, le=720),
    limit: int = Query(default=10, ge=1, le=100),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    resolve: int = Query(default=0, description="1 = reverse-DNS resolve endpoint IPs"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Top endpoint IPs with source/destination contribution split."""
    start, end = _resolve_window(hours, from_ts, to_ts)
    use_rollup = _rollup_eligible(start, end, scope)
    extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h" if use_rollup else "flow_records")
    rows = await _top_ips_detail(start, end, limit, extra_sql, extra_params, "bytes", use_rollup=use_rollup)
    return await _enrich_hosts_async(rows, bool(resolve))


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
    if _rollup_eligible(start, end, scope):
        # Long windows read the hourly rollup: per-pair sums are precomputed
        # and the detail arrays are merged from stored aggregate states.
        extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h")
        res = await _query(
            f"""
            SELECT
                toString(src_addr) AS src,
                toString(dst_addr) AS dst,
                protocol,
                dst_port,
                groupUniqArrayMerge(10)(src_ports) AS agg_src_ports,
                sum(bytes) AS total_bytes,
                sum(packets) AS total_packets,
                sum(flows) AS total_flows,
                groupUniqArray(10)(toString(exporter_ip)) AS agg_exporters,
                groupUniqArrayMerge(10)(input_snmp) AS agg_input_snmp,
                groupUniqArrayMerge(10)(output_snmp) AS agg_output_snmp,
                min(first_seen) AS agg_first_seen,
                max(last_seen) AS agg_last_seen,
                max(received_at) AS agg_received_at,
                sum(duration_ms) / sum(flows) AS avg_duration_ms,
                groupBitOr(tcp_flags) AS agg_tcp_flags,
                sum(bytes) / sum(flows) AS avg_bytes,
                sum(packets) / sum(flows) AS avg_packets
            FROM {ROLLUP_TABLE}
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
              AND (src_addr, dst_addr, protocol, dst_port) IN (
                SELECT src_addr, dst_addr, protocol, dst_port
                FROM {ROLLUP_TABLE}
                WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
                GROUP BY src_addr, dst_addr, protocol, dst_port
                ORDER BY sum(bytes) DESC
                LIMIT %(limit)s
              )
            GROUP BY src, dst, protocol, dst_port
            ORDER BY total_bytes DESC
            LIMIT %(limit)s
            """,
            {"start": _floor_hour(start), "end": end, "limit": limit, **extra_params},
        )
        return await _format_conversations(res, db)
    extra_sql, extra_params = _scope(**scope)
    # Rank conversation keys with sum-only aggregation (inner IN subquery),
    # then compute the heavy detail (uniq arrays, flag ORs, interface joins)
    # only for the winning pairs. The pair filter compares native IPv4 columns:
    # a toString() on both sides forced a per-row string conversion across the
    # whole window, which tripled the query time on 24h+ ranges.
    res = await _query(
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
              AND (src_addr, dst_addr, protocol, dst_port) IN (
                SELECT src_addr, dst_addr, protocol, dst_port
                FROM zenplus.flow_records
                WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
                GROUP BY src_addr, dst_addr, protocol, dst_port
                ORDER BY sum(bytes) DESC
                LIMIT %(limit)s
              )
        )
        GROUP BY src, dst, protocol, dst_port
        ORDER BY bytes DESC
        LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    return await _format_conversations(res, db)


async def _format_conversations(res, db: AsyncSession) -> list[dict]:
    """Resolve exporter/interface names and shape conversation rows for the UI.

    Both the raw and the rollup query produce the same 18 columns in the same
    order, so the enrichment is shared.
    """
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
            "application": _application_for_flow(int(r[3]), int(r[2])),
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
    minutes: int | None = Query(default=None, ge=1, le=43200),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts, minutes)
    if _rollup_eligible(start, end, scope):
        table = ROLLUP_TABLE
        extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h")
        flows_expr, first_expr, last_expr = "sum(flows)", "min(first_seen)", "max(last_seen)"
        start = _floor_hour(start)
    else:
        table = "zenplus.flow_records"
        extra_sql, extra_params = _scope(**scope)
        flows_expr, first_expr, last_expr = "count()", "min(timestamp)", "max(timestamp)"
    res = await _query(
        f"""
        SELECT protocol, sum(bytes) AS total_bytes, sum(packets) AS total_packets, {flows_expr} AS total_flows,
               {first_expr} AS agg_first_seen,
               {last_expr} AS agg_last_seen
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol
        ORDER BY total_bytes DESC
        LIMIT 20
        """,
        {"start": start, "end": end, **extra_params},
    )
    port_res = await _query(
        f"""
        SELECT protocol, dst_port, sum(bytes) AS total_bytes, sum(packets) AS total_packets, {flows_expr} AS total_flows
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND dst_port != 0
        GROUP BY protocol, dst_port
        ORDER BY total_bytes DESC
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
    if _rollup_eligible(start, end, scope):
        table, flows_expr = ROLLUP_TABLE, "sum(flows)"
        extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h")
        start = _floor_hour(start)
    else:
        table, flows_expr = "zenplus.flow_records", "count()"
        extra_sql, extra_params = _scope(**scope)
    res = await _query(
        f"""
        SELECT protocol, dst_port, sum(bytes) AS total_bytes, sum(packets) AS total_packets, {flows_expr} AS total_flows
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol, dst_port
        ORDER BY total_bytes DESC
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
    if _rollup_eligible(start, end, scope_for_apps):
        table, flows_expr = ROLLUP_TABLE, "sum(flows)"
        extra_sql, extra_params = _scope(**scope_for_apps, table="flow_conversations_1h")
        start = _floor_hour(start)
    else:
        table, flows_expr = "zenplus.flow_records", "count()"
        extra_sql, extra_params = _scope(**scope_for_apps)
    res = await _query(
        f"""
        SELECT dst_port, protocol, sum(bytes) AS total_bytes, sum(packets) AS total_packets, {flows_expr} AS total_flows
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY dst_port, protocol
        """,
        {"start": start, "end": end, **extra_params},
    )
    buckets: dict[str, dict] = {}
    for port, proto, bytes_, packets, flows in res.result_rows:
        name = _application_for_flow(int(port), int(proto))
        b = buckets.setdefault(name, {"name": name, "bytes": 0, "packets": 0, "flows": 0, "ports": []})
        b["bytes"] += int(bytes_ or 0)
        b["packets"] += int(packets or 0)
        b["flows"] += int(flows or 0)
        if int(port) != 0 and int(proto) not in _PROTO_APPLICATIONS:
            b["ports"].append(_port_summary(port, int(proto), bytes_, packets, flows))
    for bucket in buckets.values():
        bucket["ports"] = sorted(bucket["ports"], key=lambda p: p["bytes"], reverse=True)[:8]
    return sorted(buckets.values(), key=lambda r: r["bytes"], reverse=True)


@router.get("/interfaces")
async def netflow_interfaces(
    hours: int = Query(default=24, ge=1, le=720),
    minutes: int | None = Query(default=None, ge=1, le=43200),
    limit: int = Query(default=5, ge=1, le=50),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    exporter: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Top SNMP interfaces by traffic. Aggregates ingress (input_snmp) + egress (output_snmp) per (exporter, ifIndex). Joined with SNMP interface names from PG."""
    start, end = _resolve_window(hours, from_ts, to_ts, minutes)
    extra_sql, extra_params = _exporter_filter(exporter)
    res = await _query(
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
    if _qos_rollup_eligible(start, end, scope_for_dscp):
        table, flows_expr = QOS_ROLLUP_TABLE, "sum(flows)"
        extra_sql, extra_params = _scope(**scope_for_dscp, table="flow_qos_1h")
        start = _floor_hour(start)
    else:
        table, flows_expr = "zenplus.flow_records", "count()"
        extra_sql, extra_params = _scope(**scope_for_dscp)
    res = await _query(
        f"""
        SELECT bitShiftRight(tos, 2) AS dscp, sum(bytes) AS total_bytes, sum(packets) AS total_packets, {flows_expr} AS total_flows
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY dscp
        ORDER BY total_bytes DESC
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
    if _qos_rollup_eligible(start, end, scope_for_flags):
        table, cnt = QOS_ROLLUP_TABLE, "sumIf(flows, {c})"
        total_expr = "sum(flows)"
        extra_sql, extra_params = _scope(**scope_for_flags, table="flow_qos_1h")
        start = _floor_hour(start)
    else:
        table, cnt = "zenplus.flow_records", "countIf({c})"
        total_expr = "count()"
        extra_sql, extra_params = _scope(**scope_for_flags)
    res = (await _query(
        f"""
        SELECT
            {cnt.format(c="bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0")} AS syn_only,
            {cnt.format(c="bitAnd(tcp_flags, 16) != 0 AND bitAnd(tcp_flags, 2) = 0")} AS ack_only,
            {cnt.format(c="bitAnd(tcp_flags, 4) != 0")} AS rst,
            {cnt.format(c="bitAnd(tcp_flags, 1) != 0")} AS fin,
            {cnt.format(c="bitAnd(tcp_flags, 8) != 0")} AS psh,
            {cnt.format(c="bitAnd(tcp_flags, 32) != 0")} AS urg,
            {cnt.format(c="tcp_flags = 0")} AS no_flags,
            {total_expr} AS total
        FROM {table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
          AND protocol = 6
        """,
        {"start": start, "end": end, **extra_params},
    )).result_rows[0]
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
    if _rollup_eligible(start, end, scope_for_nc):
        table, flows_expr = ROLLUP_TABLE, "flows"
        extra_sql, extra_params = _scope(**scope_for_nc, table="flow_conversations_1h")
        start = _floor_hour(start)
    else:
        table, flows_expr = "zenplus.flow_records", "1"
        extra_sql, extra_params = _scope(**scope_for_nc)

    # One numeric multiIf per side, single table scan (the old version scanned
    # twice via UNION ALL and string-converted every address — ~7s on 7 days).
    def _class_expr(col: str) -> str:
        branches = []
        for label, cidr in _NET_CLASS_RANGES.items():
            net = ipaddress.ip_network(cidr)
            lo = int(net.network_address)
            hi = int(net.broadcast_address)
            branches.append(f"toUInt32({col}) BETWEEN {lo} AND {hi}, '{label}'")
        return "multiIf(" + ", ".join(branches) + ", 'Public')"

    res = await _query(
        f"""
        SELECT
            klass,
            sum(raw_bytes) AS total_bytes,
            sum(raw_packets) AS total_packets,
            sum(row_flows) AS total_flows
        FROM (
            SELECT {_class_expr('src_addr')} AS sk, {_class_expr('dst_addr')} AS dk,
                   bytes AS raw_bytes, packets AS raw_packets, {flows_expr} AS row_flows
            FROM {table}
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        )
        ARRAY JOIN [sk, dk] AS klass
        GROUP BY klass
        ORDER BY total_bytes DESC
        """,
        {"start": start, "end": end, **extra_params},
    )
    return [
        {"name": r[0], "bytes": int(r[1] or 0), "packets": int(r[2] or 0), "flows": int(r[3] or 0)}
        for r in res.result_rows
    ]


@router.get("/countries")
async def netflow_countries(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=600, ge=1, le=2000),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Traffic by country (Phase 2b GeoIP). Aggregates the top-`limit` endpoint
    IPs by volume and groups them by country, so it reflects the bulk of traffic
    (the long tail beyond `limit` is omitted). Bytes are counted per endpoint
    side (src + dst), so totals are per-endpoint like top-talkers. Returns [] if
    no GeoIP database is provisioned (see scripts/fetch-geoip.py)."""
    if not geoip.available():
        return []
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    res = await _query(
        f"""
        SELECT toString(addr) AS ip, sum(bytes) AS bytes, sum(packets) AS packets, count() AS flows
        FROM zenplus.flow_records
        ARRAY JOIN [src_addr, dst_addr] AS addr
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY ip ORDER BY bytes DESC LIMIT %(limit)s
        """,
        {"start": start, "end": end, "limit": limit, **extra_params},
    )
    agg: dict[str, dict] = {}
    for ip, b, p, f in res.result_rows:
        cc, name = geoip.country_of(str(ip))
        key = cc or "—"
        e = agg.setdefault(key, {
            "country": cc,
            "country_name": name or ("Private / Unknown" if not cc else None),
            "bytes": 0, "packets": 0, "flows": 0,
        })
        e["bytes"] += int(b or 0)
        e["packets"] += int(p or 0)
        e["flows"] += int(f or 0)
    return sorted(agg.values(), key=lambda x: x["bytes"], reverse=True)


@router.get("/ip-groups")
async def netflow_ip_groups(
    hours: int = Query(default=24, ge=1, le=720),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    """Traffic by user-defined IP group / site (Phase 2). A flow is attributed to
    a group when its source OR destination is in the group's CIDRs, so totals can
    exceed the network total (like top-talkers). Returns [] when no groups are
    configured in /opt/zenplus/data/netflow-ip-groups.json."""
    groups = _load_ip_groups()
    if not groups:
        return []
    start, end = _resolve_window(hours, from_ts, to_ts)
    extra_sql, extra_params = _scope(**scope)
    selected: list[tuple[str, list]] = []
    cols: list[str] = []
    for name, nets in groups:
        pred = _group_predicate(name)
        if not pred:
            continue
        idx = len(selected)
        cols.append(f"sumIf(bytes, {pred}) AS g{idx}_bytes")
        cols.append(f"sumIf(packets, {pred}) AS g{idx}_packets")
        cols.append(f"countIf({pred}) AS g{idx}_flows")
        selected.append((name, nets))
    if not cols:
        return []
    row = (await _query(
        f"""
        SELECT {", ".join(cols)}
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        """,
        {"start": start, "end": end, **extra_params},
    )).result_rows[0]
    out = []
    for idx, (name, nets) in enumerate(selected):
        base = idx * 3
        out.append({
            "group": name,
            "bytes": int(row[base] or 0),
            "packets": int(row[base + 1] or 0),
            "flows": int(row[base + 2] or 0),
            "cidrs": [str(n) for n in nets],
        })
    out.sort(key=lambda x: x["bytes"], reverse=True)
    return out
