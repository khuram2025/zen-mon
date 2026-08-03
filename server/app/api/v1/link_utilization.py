"""Fleet-wide link / interface utilization — SNMP traffic with optional NetFlow overlay."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

logger = logging.getLogger("zenplus.link_utilization")

router = APIRouter(prefix="/link-utilization", tags=["Link Utilization"])


def _clean_ip(ip: str | None) -> str:
    if not ip:
        return ""
    return str(ip).split("/")[0]


def _naive_utc(dt: datetime) -> datetime:
    """ClickHouse stores naive UTC; strip the tzinfo FastAPI parsed."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _window_filter(
    hours: int, from_time: datetime | None, to_time: datetime | None, params: dict,
) -> str:
    """ClickHouse WHERE fragment for the active window, filling `params`.

    Presets stay relative (now() - N hours) so auto-refresh tracks the clock.
    An explicit from/to pins the window — previously both endpoints ignored it,
    so the Custom range picker silently showed "the last N hours" instead of
    the chosen window.
    """
    if from_time is not None and to_time is not None:
        params["from_ts"] = _naive_utc(from_time)
        params["to_ts"] = _naive_utc(to_time)
        return "timestamp >= %(from_ts)s AND timestamp < %(to_ts)s"
    params["hours"] = hours
    return "timestamp >= now() - INTERVAL %(hours)s HOUR"


def _bucket_seconds(hours: int) -> int:
    if hours <= 6:
        return 0
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 1800
    return 7200


def _effective_speed(row: dict) -> int:
    manual = row.get("configured_speed_bps")
    if manual and int(manual) > 0:
        return int(manual)
    return int(row.get("if_speed") or 0)


def _util_pct(in_bps: float, out_bps: float, speed: int) -> float | None:
    if speed <= 0:
        return None
    return round(max(in_bps, out_bps) / speed * 100.0, 1)


@router.get("")
async def list_links(
    hours: int = Query(default=24, ge=1, le=720),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=500),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(up|down)$"),
    min_util: float | None = Query(default=None, ge=0, le=200),
    sort: str = Query(default="util", pattern="^(util|peak|traffic|in|out|name)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rank monitored interfaces by SNMP utilization across the fleet."""
    from app.core.database import get_clickhouse_client

    client = get_clickhouse_client()

    # Latest SNMP stats per interface in the window.
    ch_params: dict = {}
    window = _window_filter(hours, from_time, to_time, ch_params)
    try:
        ch = client.query(
            f"""
            SELECT
                device_id,
                if_index,
                avg(in_bps) AS avg_in,
                avg(out_bps) AS avg_out,
                max(in_bps) AS max_in,
                max(out_bps) AS max_out,
                argMax(in_bps, timestamp) AS cur_in,
                argMax(out_bps, timestamp) AS cur_out,
                argMax(oper_status, timestamp) AS oper_status
            FROM zenplus.snmp_if_metrics
            WHERE {window}
            GROUP BY device_id, if_index
            """,
            parameters=ch_params,
        )
    except Exception as e:
        raise HTTPException(500, f"clickhouse query failed: {e}")

    metrics: dict[tuple[str, int], dict] = {}
    for r in ch.result_rows:
        did, idx = str(r[0]), int(r[1])
        metrics[(did, idx)] = {
            "avg_in_bps": float(r[2] or 0),
            "avg_out_bps": float(r[3] or 0),
            "max_in_bps": float(r[4] or 0),
            "max_out_bps": float(r[5] or 0),
            "in_bps": float(r[6] or 0),
            "out_bps": float(r[7] or 0),
            "oper_status_ch": int(r[8] or 0),
        }

    if not metrics:
        return {"items": [], "summary": {"total": 0, "high_util": 0, "with_netflow": 0, "avg_util": None}}

    # Inventory from Postgres.
    #
    # Scope by device, not by (device, interface) pair. Enumerating every pair
    # meant one placeholder per interface — ~6.3k bound parameters for a 35
    # device fleet — and the device_id::text cast blocked the index, costing
    # 3.2s of a 3.6s response. Filtering on the ~35 device UUIDs is 0.05s; the
    # extra interfaces it returns are dropped by the metrics lookup below.
    device_ids = sorted({k[0] for k in metrics})

    inv_rows = (await db.execute(text("""
        SELECT di.device_id::text, di.if_index, di.if_name, di.if_descr, di.if_alias,
               di.if_speed, di.configured_speed_bps, di.oper_status, di.admin_status,
               di.monitored, d.hostname, d.ip_address::text, d.id::text
        FROM device_interfaces di
        JOIN devices d ON d.id = di.device_id
        WHERE di.device_id = ANY(CAST(:device_ids AS uuid[]))
          AND di.monitored = TRUE
    """), {"device_ids": device_ids})).all()

    # NetFlow-enabled interfaces in the same window.
    nf_set: set[tuple[str, int]] = set()
    nf_params: dict = {}
    nf_window = _window_filter(hours, from_time, to_time, nf_params)
    try:
        nf = client.query(
            f"""
            SELECT DISTINCT toString(exporter_ip) AS ip, ifindex
            FROM (
                SELECT exporter_ip, input_snmp AS ifindex
                FROM zenplus.flow_records
                WHERE {nf_window} AND input_snmp != 0
                UNION ALL
                SELECT exporter_ip, output_snmp AS ifindex
                FROM zenplus.flow_records
                WHERE {nf_window} AND output_snmp != 0
            )
            """,
            parameters=nf_params,
        )
        for r in nf.result_rows:
            nf_set.add((str(r[0]), int(r[1])))
    except Exception:
        logger.exception("netflow interface lookup failed")

    items = []
    for row in inv_rows:
        did, idx = row[0], int(row[1])
        m = metrics.get((did, idx))
        # Device-scoped inventory also returns interfaces with no samples in
        # the window; they cannot be ranked, so leave them out as before.
        if m is None:
            continue
        speed = _effective_speed({"configured_speed_bps": row[6], "if_speed": row[5]})
        in_bps = m.get("in_bps", 0.0)
        out_bps = m.get("out_bps", 0.0)
        util = _util_pct(in_bps, out_bps, speed)
        peak_util = _util_pct(m.get("max_in_bps", 0), m.get("max_out_bps", 0), speed)
        oper = row[7] or ("up" if m.get("oper_status_ch") == 1 else "down")
        # SPAN/mirror destination ports (and some LAG members) report
        # ifOperStatus=down while forwarding real traffic — Cisco marks SPAN
        # destinations "down (monitoring)". Moving counters are ground truth:
        # a genuinely down port cannot produce a non-zero rate in its latest
        # sample, so present these as up instead of the contradictory
        # "Down + 240 Mbps".
        if oper != "up" and (in_bps > 0 or out_bps > 0):
            oper = "up"
        # ip_address is inet, so ::text yields "192.168.100.102/32", but the
        # flow exporter IPs from ClickHouse carry no prefix. Comparing the raw
        # values never matched, so has_netflow was always False.
        ip = _clean_ip(row[11])
        has_nf = (ip, idx) in nf_set

        item = {
            "device_id": did,
            "hostname": row[10],
            "device_ip": ip,
            "if_index": idx,
            "if_name": row[2],
            "if_descr": row[3],
            "if_alias": row[4],
            "if_speed": row[5],
            "configured_speed_bps": row[6],
            "effective_speed_bps": speed,
            "oper_status": oper,
            "admin_status": row[8],
            "in_bps": in_bps,
            "out_bps": out_bps,
            "total_bps": in_bps + out_bps,
            "avg_in_bps": m.get("avg_in_bps", 0),
            "avg_out_bps": m.get("avg_out_bps", 0),
            "max_in_bps": m.get("max_in_bps", 0),
            "max_out_bps": m.get("max_out_bps", 0),
            "util_pct": util,
            "peak_util_pct": peak_util,
            "has_netflow": has_nf,
        }
        items.append(item)

    # Filters
    q = (search or "").strip().lower()
    if q:
        items = [
            i for i in items
            if q in (i["hostname"] or "").lower()
            or q in (i["if_name"] or "").lower()
            or q in (i["if_descr"] or "").lower()
            or q in (i["if_alias"] or "").lower()
            or q in (i["device_ip"] or "").lower()
        ]
    if status == "up":
        items = [i for i in items if i["oper_status"] == "up"]
    elif status == "down":
        items = [i for i in items if i["oper_status"] != "up"]
    if min_util is not None:
        # Filter on the window's peak, not the instantaneous sample: a bursty
        # uplink that hit 64% five minutes ago but idles at 4% right now is
        # exactly what a ">= 50%" filter is trying to find.
        items = [
            i for i in items
            if max(i["util_pct"] or 0, i["peak_util_pct"] or 0) >= min_util
        ]

    # Sort
    if sort == "name":
        items.sort(key=lambda x: (x["hostname"] or "", x["if_name"] or ""))
    elif sort == "in":
        items.sort(key=lambda x: x["in_bps"], reverse=True)
    elif sort == "out":
        items.sort(key=lambda x: x["out_bps"], reverse=True)
    elif sort == "traffic":
        items.sort(key=lambda x: x["total_bps"], reverse=True)
    elif sort == "peak":
        items.sort(key=lambda x: (x["peak_util_pct"] or 0), reverse=True)
    else:
        items.sort(key=lambda x: (x["util_pct"] or 0), reverse=True)

    # Summarise the whole filtered set, not just the page. These drive KPIs
    # labelled "Fleet avg util" and "interfaces with flow data"; computing them
    # after the cut reported the top-N slice as if it were the fleet.
    utils = [i["util_pct"] for i in items if i["util_pct"] is not None]
    summary = {
        "total": len(items),
        "high_util": sum(1 for u in utils if u >= 80),
        "warning_util": sum(1 for u in utils if 50 <= u < 80),
        "with_netflow": sum(1 for i in items if i["has_netflow"]),
        "avg_util": round(sum(utils) / len(utils), 1) if utils else None,
        "returned": min(len(items), limit),
    }

    items = items[:limit]
    return {"items": items, "summary": summary, "hours": hours}


@router.get("/{device_id}/{if_index}")
async def link_detail(
    device_id: UUID,
    if_index: int,
    hours: int = Query(default=24, ge=1, le=720),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-link drill-down: SNMP traffic/errors + optional NetFlow overlay."""
    from app.core.database import get_clickhouse_client

    inv = (await db.execute(text("""
        SELECT di.if_name, di.if_descr, di.if_alias, di.if_speed, di.configured_speed_bps,
               di.oper_status, di.admin_status, di.mac_address,
               d.hostname, d.ip_address::text, d.id::text
        FROM device_interfaces di
        JOIN devices d ON d.id = di.device_id
        WHERE di.device_id = :did AND di.if_index = :idx
    """), {"did": device_id, "idx": if_index})).first()
    if not inv:
        raise HTTPException(404, "interface not found")

    speed = _effective_speed({"configured_speed_bps": inv[4], "if_speed": inv[3]})
    client = get_clickhouse_client()
    bucket = _bucket_seconds(hours)
    did = str(device_id)

    params: dict = {"id": did, "if": if_index}
    window = _window_filter(hours, from_time, to_time, params)

    # SNMP traffic series
    if bucket == 0:
        snmp_sql = f"""
            SELECT toUnixTimestamp64Milli(timestamp) AS ts,
                   in_bps, out_bps, in_errors, out_errors, in_discards, out_discards,
                   in_bps AS in_peak, out_bps AS out_peak
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
            ORDER BY timestamp
        """
    else:
        # Carry the per-bucket peak alongside the average: the chart plots the
        # average, but a summary built from averages hides the real peaks.
        # Errors/discards are cumulative counters, so the in-bucket increase is
        # max-min — summing the counter readings produced nonsense totals.
        # Aliases must not reuse the source column names: `avg(in_bps) AS in_bps`
        # makes a later max(in_bps) resolve to the alias, which ClickHouse
        # rejects as a nested aggregate (ILLEGAL_AGGREGATION).
        snmp_sql = f"""
            SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)) * 1000 AS ts,
                   avg(in_bps) AS avg_in, avg(out_bps) AS avg_out,
                   max(in_errors) - min(in_errors) AS d_in_err,
                   max(out_errors) - min(out_errors) AS d_out_err,
                   max(in_discards) - min(in_discards) AS d_in_disc,
                   max(out_discards) - min(out_discards) AS d_out_disc,
                   max(in_bps) AS peak_in, max(out_bps) AS peak_out
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
            GROUP BY ts ORDER BY ts
        """

    try:
        snmp_res = client.query(snmp_sql, parameters=params)
    except Exception as e:
        raise HTTPException(500, f"snmp query failed: {e}")

    traffic = []
    errors = []
    for r in snmp_res.result_rows:
        traffic.append({
            "ts": int(r[0]),
            "in_bps": float(r[1]), "out_bps": float(r[2]),
            # Per-bucket peak. Equals the average on unbucketed ranges; on wider
            # windows it's what keeps the chart's ceiling honest against IN MAX.
            "in_peak_bps": float(r[7] or 0), "out_peak_bps": float(r[8] or 0),
        })
        errors.append({
            "ts": int(r[0]),
            "in_errors": int(r[3] or 0), "out_errors": int(r[4] or 0),
            "in_discards": int(r[5] or 0), "out_discards": int(r[6] or 0),
        })

    # Summary comes from the raw samples, never from the plotted buckets.
    # Deriving max/current from bucket averages understated this interface's
    # peak by 27% over 24h and disagreed with the fleet table, which aggregates
    # raw. Both now use the same aggregation, so the row and the panel match.
    summary: dict = {}
    try:
        agg = client.query(
            f"""
            SELECT avg(in_bps), max(in_bps), argMax(in_bps, timestamp),
                   avg(out_bps), max(out_bps), argMax(out_bps, timestamp),
                   max(in_errors) - min(in_errors), max(out_errors) - min(out_errors),
                   max(in_discards) - min(in_discards), max(out_discards) - min(out_discards),
                   count()
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
            """,
            parameters=params,
        )
        row = agg.result_rows[0] if agg.result_rows else None
    except Exception as e:
        raise HTTPException(500, f"snmp summary query failed: {e}")

    if row and int(row[10] or 0) > 0:
        in_avg, in_max, in_cur = float(row[0] or 0), float(row[1] or 0), float(row[2] or 0)
        out_avg, out_max, out_cur = float(row[3] or 0), float(row[4] or 0), float(row[5] or 0)
        summary = {
            "in_avg_bps": in_avg,
            "in_max_bps": in_max,
            "in_current_bps": in_cur,
            "out_avg_bps": out_avg,
            "out_max_bps": out_max,
            "out_current_bps": out_cur,
            "util_pct": _util_pct(in_cur, out_cur, speed),
            "peak_util_pct": _util_pct(in_max, out_max, speed),
            "total_errors": max(0, int(row[6] or 0)) + max(0, int(row[7] or 0)),
            "total_discards": max(0, int(row[8] or 0)) + max(0, int(row[9] or 0)),
            "samples": int(row[10]),
        }

    # Strip the inet prefix before querying ClickHouse — flow_records stores
    # bare exporter IPs, so "192.168.41.105/32" matched nothing and every
    # interface reported "no flow records".
    exporter_ip = _clean_ip(inv[9])
    netflow = {"has_flows": False, "timeseries": [], "top_talkers": [], "protocols": []}

    if exporter_ip:
        nf_bucket = 60 if hours <= 6 else (300 if hours <= 48 else 1800)
        nf_params: dict = {"ip": exporter_ip, "if": if_index}
        nf_window = _window_filter(hours, from_time, to_time, nf_params)
        try:
            # The inner aliases must not shadow `bytes`: aliasing sum(bytes) AS
            # bytes made the outer sum() resolve to sum(sum(bytes)), which
            # ClickHouse rejects with ILLEGAL_AGGREGATION.
            nf_ts = client.query(
                f"""
                SELECT ts, sum(bucket_in_bps) AS in_bps, sum(bucket_out_bps) AS out_bps,
                       sum(bucket_bytes) AS bytes
                FROM (
                    SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {nf_bucket} SECOND)) * 1000 AS ts,
                           sum(bytes) * 8.0 / {nf_bucket} AS bucket_in_bps,
                           0 AS bucket_out_bps,
                           sum(bytes) AS bucket_bytes
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND input_snmp = %(if)s
                      AND {nf_window}
                    GROUP BY ts
                    UNION ALL
                    SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {nf_bucket} SECOND)) * 1000 AS ts,
                           0 AS bucket_in_bps,
                           sum(bytes) * 8.0 / {nf_bucket} AS bucket_out_bps,
                           sum(bytes) AS bucket_bytes
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND output_snmp = %(if)s
                      AND {nf_window}
                    GROUP BY ts
                )
                GROUP BY ts ORDER BY ts
                """,
                parameters=nf_params,
            )
            ts_map: dict[int, dict] = {}
            for r in nf_ts.result_rows:
                ts = int(r[0])
                ts_map.setdefault(ts, {"ts": ts, "in_bps": 0.0, "out_bps": 0.0, "bytes": 0})
                ts_map[ts]["in_bps"] += float(r[1] or 0)
                ts_map[ts]["out_bps"] += float(r[2] or 0)
                ts_map[ts]["bytes"] += int(r[3] or 0)
            netflow["timeseries"] = sorted(ts_map.values(), key=lambda x: x["ts"])
            netflow["has_flows"] = len(netflow["timeseries"]) > 0

            talkers = client.query(
                f"""
                SELECT ip, sum(bytes) AS total_bytes, sum(packets) AS total_packets, count() AS flows
                FROM (
                    SELECT toString(src_addr) AS ip, bytes, packets
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                      AND {nf_window}
                    UNION ALL
                    SELECT toString(dst_addr) AS ip, bytes, packets
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                      AND {nf_window}
                )
                GROUP BY ip ORDER BY total_bytes DESC LIMIT 10
                """,
                parameters=nf_params,
            )
            netflow["top_talkers"] = [
                {"ip": r[0], "bytes": int(r[1]), "packets": int(r[2]), "flows": int(r[3])}
                for r in talkers.result_rows
            ]

            protos = client.query(
                f"""
                SELECT protocol, sum(bytes) AS total_bytes
                FROM zenplus.flow_records
                WHERE exporter_ip = %(ip)s
                  AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                  AND {nf_window}
                GROUP BY protocol ORDER BY total_bytes DESC LIMIT 8
                """,
                parameters=nf_params,
            )
            netflow["protocols"] = [
                {"protocol": int(r[0]), "bytes": int(r[1])} for r in protos.result_rows
            ]
        except Exception:
            # NetFlow is an optional overlay, so a failure here must not break
            # the SNMP view — but log it. Swallowing silently hid a malformed
            # query that made every interface report "no flow records".
            logger.exception("netflow overlay failed for %s/%s", exporter_ip, if_index)

    # Same SPAN/mirror-port correction as the list endpoint: ifOperStatus says
    # down, but the latest sample carries traffic — the port is forwarding.
    oper_status = inv[5]
    if oper_status != "up" and (
        summary.get("in_current_bps", 0) > 0 or summary.get("out_current_bps", 0) > 0
    ):
        oper_status = "up"

    return {
        "device_id": did,
        "if_index": if_index,
        "hostname": inv[8],
        "device_ip": exporter_ip,
        "if_name": inv[0],
        "if_descr": inv[1],
        "if_alias": inv[2],
        "if_speed": inv[3],
        "configured_speed_bps": inv[4],
        "effective_speed_bps": speed,
        "oper_status": oper_status,
        "admin_status": inv[6],
        "mac_address": inv[7],
        "hours": hours,
        # Buckets wider than the poll interval mean the plotted line is an
        # average; the client says so rather than presenting it as raw usage.
        "bucket_seconds": bucket,
        "traffic": traffic,
        "errors": errors,
        "summary": summary,
        "netflow": netflow,
    }
