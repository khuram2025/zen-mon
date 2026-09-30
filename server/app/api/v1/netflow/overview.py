"""NetFlow overview endpoints: headline totals, time series, device status,
the day-of-week/hour heatmap and the exporter list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.models.user import User

from .common import (
    QOS_ROLLUP_TABLE,
    ROLLUP_TABLE,
    _floor_hour,
    _protocol_name,
    _qos_rollup_eligible,
    _query,
    _resolve_window,
    _rollup_eligible,
    _scope,
    _to_iso_utc,
    _window_seconds,
    scope_params,
)

router = APIRouter()


@router.get("/overview")
async def netflow_overview(
    hours: int = Query(default=24, ge=1, le=720),
    minutes: int | None = Query(default=None, ge=1, le=43200),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts, minutes)
    rate_window = min(300, _window_seconds(start, end))
    if _rollup_eligible(start, end, scope):
        # Long windows: totals from the hourly rollup; the live-rate figure
        # still comes from raw, but only over the last few minutes.
        extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h")
        raw_extra_sql, raw_extra_params = _scope(**scope)
        params = {"start": _floor_hour(start), "end": end, **extra_params}
        summary = list((await _query(
            f"""
            SELECT
                sum(bytes) AS total_bytes,
                sum(packets) AS total_packets,
                sum(flows) AS total_flows,
                uniqExact(exporter_ip) AS exporters,
                uniqExact(src_addr) AS src_hosts,
                uniqExact(dst_addr) AS dst_hosts,
                max(received_at) AS last_flow
            FROM {ROLLUP_TABLE}
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            """,
            params,
        )).result_rows[0])
        summary.append((await _query(
            f"""
            SELECT sum(bytes) * 8 / %(rate_window)s
            FROM zenplus.flow_records
            WHERE timestamp BETWEEN %(end)s - toIntervalSecond(%(rate_window)s) AND %(end)s{raw_extra_sql}
            """,
            {"end": end, "rate_window": rate_window, **raw_extra_params},
        )).result_rows[0][0])
    else:
        extra_sql, extra_params = _scope(**scope)
        params = {"start": start, "end": end, "rate_window": rate_window, **extra_params}
        summary = (await _query(
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
        )).result_rows[0]

    proto_table = ROLLUP_TABLE if _rollup_eligible(start, end, scope) else "zenplus.flow_records"
    top_proto = (await _query(
        f"""
        SELECT protocol, sum(bytes) AS total_bytes
        FROM {proto_table}
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        GROUP BY protocol
        ORDER BY total_bytes DESC
        LIMIT 1
        """,
        params,
    )).result_rows

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
    minutes: int | None = Query(default=None, ge=1, le=43200),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    scope: dict = Depends(scope_params),
    user: User = Depends(get_current_user),
):
    start, end = _resolve_window(hours, from_ts, to_ts, minutes)
    span_hours = _window_seconds(start, end) / 3600
    bucket = 60 if span_hours <= 6 else 300 if span_hours <= 48 else 1800 if span_hours <= 168 else 7200
    # Hour-aligned buckets on a long window can come from the hourly rollup.
    if bucket % 3600 == 0 and _rollup_eligible(start, end, scope):
        extra_sql, extra_params = _scope(**scope, table="flow_conversations_1h")
        res = await _query(
            f"""
            SELECT
                toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)) * 1000 AS ts,
                sum(bytes) * 8 / {bucket} AS bps,
                sum(bytes) AS total_bytes,
                sum(packets) AS total_packets,
                sum(flows) AS total_flows
            FROM {ROLLUP_TABLE}
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            GROUP BY ts
            ORDER BY ts
            """,
            {"start": _floor_hour(start), "end": end, **extra_params},
        )
    else:
        extra_sql, extra_params = _scope(**scope)
        res = await _query(
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
    if _qos_rollup_eligible(start, end, scope):
        extra_sql, extra_params = _scope(**scope, table="flow_qos_1h")
        res = (await _query(
            f"""
            SELECT
                sum(flows) AS total_flows,
                sum(packets) AS total_packets,
                sum(bytes) AS total_bytes,
                sum(duration_ms) / sum(flows) AS avg_duration_ms,
                sum(empty_flows) AS agg_empty_flows,
                sumIf(flows, bitAnd(toUInt8(tcp_flags), 4) = 4) AS rst_flows,
                uniqExact(exporter_ip) AS exporter_count,
                max(received_at) AS last_flow
            FROM {QOS_ROLLUP_TABLE}
            WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
            """,
            {"start": _floor_hour(start), "end": end, **extra_params},
        )).result_rows[0]
    else:
        extra_sql, extra_params = _scope(**scope)
        res = (await _query(
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
        )).result_rows[0]

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
    slots_seen = (await _query(
        f"""
        SELECT countDistinct(toStartOfFiveMinutes(timestamp))
        FROM zenplus.flow_records
        WHERE timestamp BETWEEN %(start)s AND %(end)s{extra_sql}
        """,
        {"start": start, "end": end, **extra_params},
    )).result_rows[0][0]
    uptime_pct = round(min(100, (int(slots_seen or 0) / slots_total) * 100), 1)

    return {
        # BUG-04: these are flow-derived HEURISTICS, not real device telemetry:
        #   flow_duration_ms    = avg(last_switched - first_switched) per flow
        #   rst_ratio_pct       = TCP-RST flows / total flows (not packet loss)
        #   flow_continuity_pct = fraction of 5-min slots that carried any flow
        # Real latency / loss / uptime require ICMP/SNMP correlation (ZenPlus
        # already collects ping RTT — join on the exporter device).
        "flow_derived": True,
        "flow_duration_ms": latency_ms,
        "rst_ratio_pct": loss_pct,
        "flow_continuity_pct": uptime_pct,
        # Deprecated aliases kept for backward compatibility only; the UI must
        # not present these as real latency / packet-loss / uptime.
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
    res = await _query(
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
    res = await _query(
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
