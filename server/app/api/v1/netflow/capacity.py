"""NetFlow capacity planning: 95th-percentile and utilization vs ifSpeed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

from .common import (
    _exporter_filter,
    _interface_display_name,
    _query,
    _resolve_interface_names,
    _resolve_window,
)

router = APIRouter()


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
    res = await _query(
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
