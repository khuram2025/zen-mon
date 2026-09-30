"""NetFlow forensics: raw flow record search (JSON or CSV export)."""

from __future__ import annotations

import csv
import io
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

from .common import (
    _interface_payload,
    _port_name,
    _protocol_name,
    _query,
    _resolve_interface_names,
    _resolve_window,
    _scope,
    _to_iso_utc,
    scope_params,
)

router = APIRouter()


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
    fmt: str = Query(default="json", alias="format", regex="^(json|csv)$"),
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
    res = await _query(
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
    rows = [
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
    if fmt == "csv":
        cols = [
            "timestamp", "exporter_ip", "src", "src_port", "dst", "dst_port",
            "protocol", "protocol_name", "service", "tcp_flags", "dscp",
            "input_snmp", "output_snmp", "bytes", "packets", "duration_ms",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        fname = f"netflow-forensics-{start:%Y%m%dT%H%M%S}-{end:%Y%m%dT%H%M%S}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    return rows
