"""
Live JSON data builders for the on-screen Reports section.

The PDF/Excel/CSV exporters live in `report_service.py` and `export_service.py`.
This module is the live-UI counterpart: each function returns a plain dict
suitable for JSON serialization to the dashboard.

Reuses the data fetchers and aggregation helpers from `report_service.py`
to stay consistent with the exported reports.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_clickhouse_client
from app.services.report_service import (
    _fetch_devices,
    _fetch_service_checks,
    _fetch_alerts,
    _fetch_ping_metrics,
    _fetch_service_metrics,
    _fetch_service_status_log,
    _device_uptime_pct,
    _device_rtt_stats,
    _ping_outage_episodes,
    _service_uptime_pct,
    _mttr_seconds,
)


def _safe_device_status_log(start: datetime, end: datetime) -> list[dict]:
    """
    Wrap status-log fetch — the table may not exist in older deployments.
    Returns an empty list if the table is missing.
    """
    try:
        from app.services.report_service import _fetch_device_status_log
        return _fetch_device_status_log(start, end)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Time-range helpers
# ---------------------------------------------------------------------------

def _normalise_window(
    from_time: Optional[datetime],
    to_time: Optional[datetime],
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = to_time or now
    start = from_time or (end - timedelta(hours=24))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _prior_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    delta = end - start
    return start - delta, start


def _trend_bucket(start: datetime, end: datetime) -> tuple[str, str]:
    """
    Return (clickhouse_interval, label) appropriate for the window length.
    Used for availability trend bucketing.
    """
    span = end - start
    if span <= timedelta(hours=6):
        return "5 MINUTE", "5m"
    if span <= timedelta(days=2):
        return "30 MINUTE", "30m"
    if span <= timedelta(days=14):
        return "1 HOUR", "1h"
    if span <= timedelta(days=60):
        return "6 HOUR", "6h"
    return "1 DAY", "1d"


def _delta(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    if curr is None or prev is None:
        return None
    return round(curr - prev, 2)


# ---------------------------------------------------------------------------
# Internal aggregation helpers
# ---------------------------------------------------------------------------

def _availability_pct(ping_rows: list[dict]) -> Optional[float]:
    if not ping_rows:
        return None
    up = sum(1 for r in ping_rows if r.get("is_up"))
    return round((up / len(ping_rows)) * 100, 2)


def _availability_trend(start: datetime, end: datetime) -> list[dict]:
    """One ClickHouse query buckets is_up over time across all devices."""
    interval, _ = _trend_bucket(start, end)
    client = get_clickhouse_client()
    q = f"""
        SELECT
            toStartOfInterval(timestamp, INTERVAL {interval}) AS ts,
            avg(is_up) * 100 AS availability_pct
        FROM zenplus.ping_metrics
        WHERE timestamp >= %(start)s AND timestamp <= %(end)s
        GROUP BY ts
        ORDER BY ts
    """
    try:
        rs = client.query(q, parameters={"start": start, "end": end})
        return [
            {
                "ts": (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]).isoformat(),
                "availability_pct": round(float(r[1]), 2) if r[1] is not None else None,
            }
            for r in rs.result_rows
        ]
    except Exception:
        return []


def _alert_volume_by_severity(start: datetime, end: datetime, alerts: list[dict]) -> list[dict]:
    """Bucket alert counts per (day, severity)."""
    if not alerts:
        return []
    span = end - start
    fmt = "%Y-%m-%dT%H:00:00" if span <= timedelta(days=2) else "%Y-%m-%d"
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"critical": 0, "warning": 0, "info": 0})
    for a in alerts:
        t = a.get("triggered_at")
        if not t:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        key = t.strftime(fmt)
        sev = (a.get("severity") or "info").lower()
        if sev not in buckets[key]:
            buckets[key][sev] = 0
        buckets[key][sev] += 1
    return [
        {"ts": k + ("Z" if "T" in k else "T00:00:00Z"), **v}
        for k, v in sorted(buckets.items())
    ]


def _outage_intervals(status_log: list[dict], device_map: dict[str, dict]) -> list[dict]:
    """Convert status-log rows into outage intervals (down→up duration)."""
    out: list[dict] = []
    for entry in status_log:
        ns = (entry.get("new_status") or "").lower()
        if ns not in ("down", "offline"):
            continue
        did = str(entry["device_id"])
        ts = entry.get("timestamp")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        dur = entry.get("duration_sec") or 0
        d = device_map.get(did, {})
        out.append({
            "device_id": did,
            "hostname": d.get("hostname", "Unknown"),
            "started_at": ts.isoformat() if ts else None,
            "duration_minutes": round(dur / 60, 1) if dur else 0,
            "kind": "device",
        })
    return out


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

async def build_executive(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
) -> dict:
    start, end = _normalise_window(from_time, to_time)
    prev_start, prev_end = _prior_window(start, end)

    devices = await _fetch_devices(db)
    alerts = await _fetch_alerts(db, start, end)
    prev_alerts = await _fetch_alerts(db, prev_start, prev_end)
    ping_rows = _fetch_ping_metrics(start, end)
    prev_pings = _fetch_ping_metrics(prev_start, prev_end)
    status_log = _safe_device_status_log(start, end)

    device_map = {str(d["id"]): d for d in devices}

    avail = _availability_pct(ping_rows)
    prev_avail = _availability_pct(prev_pings)

    active_critical = sum(
        1 for a in alerts
        if (a.get("status") or "").lower() == "active"
        and (a.get("severity") or "").lower() == "critical"
    )

    mttr_s = _mttr_seconds(alerts)
    prev_mttr_s = _mttr_seconds(prev_alerts)
    mttr_min = round(mttr_s / 60, 1) if mttr_s else None
    prev_mttr_min = round(prev_mttr_s / 60, 1) if prev_mttr_s else None

    incidents = len(alerts)
    prev_incidents = len(prev_alerts)

    sla_target = 99.9
    sla_attained = avail if avail is not None else 0

    # Top issues — combine alert count with downtime
    alert_count_by_dev: dict[str, int] = defaultdict(int)
    for a in alerts:
        if a.get("device_id"):
            alert_count_by_dev[str(a["device_id"])] += 1

    downtime_by_dev: dict[str, float] = defaultdict(float)
    for entry in status_log:
        if (entry.get("new_status") or "").lower() in ("down", "offline"):
            downtime_by_dev[str(entry["device_id"])] += float(entry.get("duration_sec") or 0)

    scored: list[tuple[str, float, float]] = []
    for did, d in device_map.items():
        score = alert_count_by_dev[did] * 10 + downtime_by_dev[did] / 60
        if score > 0:
            scored.append((did, score, downtime_by_dev[did]))
    scored.sort(key=lambda x: x[1], reverse=True)

    top_issues = []
    for did, _score, downtime_s in scored[:5]:
        d = device_map.get(did, {})
        sev = "critical" if downtime_s >= 300 else ("warning" if downtime_s > 0 else "info")
        top_issues.append({
            "device_id": did,
            "hostname": d.get("hostname", "Unknown"),
            "issue": "down" if downtime_s > 0 else "noisy",
            "duration_minutes": round(downtime_s / 60, 1),
            "alert_count": alert_count_by_dev[did],
            "severity": sev,
        })

    # Location summary
    loc_buckets: dict[str, dict] = defaultdict(lambda: {"devices": 0, "down": 0})
    for d in devices:
        loc = d.get("location") or "Unknown"
        loc_buckets[loc]["devices"] += 1
        if (d.get("status") or "").lower() in ("down", "offline"):
            loc_buckets[loc]["down"] += 1
    location_summary = [
        {
            "location": loc,
            "devices": v["devices"],
            "down": v["down"],
            "availability_pct": round((v["devices"] - v["down"]) / v["devices"] * 100, 1) if v["devices"] else 0,
        }
        for loc, v in sorted(loc_buckets.items(), key=lambda x: -x[1]["devices"])
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "kpis": {
            "availability_pct": avail,
            "availability_delta_pct": _delta(avail, prev_avail),
            "active_critical_count": active_critical,
            "mttr_minutes": mttr_min,
            "mttr_delta_minutes": _delta(mttr_min, prev_mttr_min),
            "devices_monitored": len(devices),
            "sla_target_pct": sla_target,
            "sla_attained_pct": sla_attained,
            "incidents_count": incidents,
            "incidents_delta": incidents - prev_incidents,
        },
        "availability_trend": _availability_trend(start, end),
        "top_issues": top_issues,
        "location_summary": location_summary,
        "outage_timeline": _outage_intervals(status_log[:50], device_map),
    }


async def build_technical(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
) -> dict:
    start, end = _normalise_window(from_time, to_time)

    devices = await _fetch_devices(db)
    alerts = await _fetch_alerts(db, start, end)
    ping_rows = _fetch_ping_metrics(start, end)
    status_log = _safe_device_status_log(start, end)

    device_map = {str(d["id"]): d for d in devices}

    # Top-10 worst availability devices
    per_device_uptime: list[tuple[str, float, dict, int]] = []
    for d in devices:
        did = str(d["id"])
        rows = [r for r in ping_rows if str(r["device_id"]) == did]
        if not rows:
            continue
        up_pct = sum(1 for r in rows if r.get("is_up")) / len(rows) * 100
        rtt_stats = _device_rtt_stats(ping_rows, did)
        outage_count = _ping_outage_episodes(ping_rows, did)
        per_device_uptime.append((did, up_pct, rtt_stats, outage_count))

    per_device_uptime.sort(key=lambda x: x[1])
    worst_devices = []
    for did, up_pct, rtt_stats, outage_count in per_device_uptime[:10]:
        d = device_map.get(did, {})
        worst_devices.append({
            "device_id": did,
            "hostname": d.get("hostname", "Unknown"),
            "ip": str(d.get("ip_address") or ""),
            "availability_pct": round(up_pct, 2),
            "outage_count": outage_count,
            "avg_rtt_ms": round(rtt_stats["avg"], 1) if rtt_stats["avg"] else None,
            "p95_rtt_ms": round(rtt_stats["p95"], 1) if rtt_stats["p95"] else None,
        })

    # Top-10 noisy alerts
    per_rule_count: dict[tuple, dict] = defaultdict(lambda: {"alert_count": 0, "device_id": None, "hostname": None})
    for a in alerts:
        rule_id = str(a.get("rule_id") or "")
        key = rule_id or f"msg:{(a.get('message') or '')[:40]}"
        per_rule_count[key]["alert_count"] += 1
        if a.get("device_id") and not per_rule_count[key]["device_id"]:
            per_rule_count[key]["device_id"] = str(a["device_id"])
            per_rule_count[key]["hostname"] = a.get("hostname")
            per_rule_count[key]["sample_message"] = a.get("message")
            per_rule_count[key]["severity"] = a.get("severity")

    noisy_alerts = sorted(per_rule_count.items(), key=lambda x: -x[1]["alert_count"])[:10]
    noisy_alerts_out = [
        {
            "rule_key": k,
            "alert_count": v["alert_count"],
            "device_id": v["device_id"],
            "hostname": v["hostname"],
            "sample_message": v.get("sample_message"),
            "severity": v.get("severity"),
        }
        for k, v in noisy_alerts
    ]

    # Top bandwidth interfaces — query snmp_if_metrics (in_bps/out_bps already in bits/sec)
    top_bw = []
    try:
        client = get_clickhouse_client()
        q = """
            SELECT device_id, if_index,
                   avg(in_bps) AS in_bps_avg,
                   avg(out_bps) AS out_bps_avg
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(start)s AND timestamp <= %(end)s
            GROUP BY device_id, if_index
            ORDER BY (in_bps_avg + out_bps_avg) DESC
            LIMIT 10
        """
        rs = client.query(q, parameters={"start": start, "end": end})
        # Lookup interface names
        if_names: dict[tuple, dict] = {}
        if rs.result_rows:
            id_pairs = [(str(r[0]), int(r[1])) for r in rs.result_rows]
            placeholders = ",".join(f"(:d{i}, :i{i})" for i in range(len(id_pairs)))
            params = {}
            for i, (did, idx) in enumerate(id_pairs):
                params[f"d{i}"] = did
                params[f"i{i}"] = idx
            sql = text(f"""
                SELECT device_id, if_index, if_name, if_speed
                FROM device_interfaces
                WHERE (device_id, if_index) IN ({placeholders})
            """)
            res = await db.execute(sql, params)
            for row in res.fetchall():
                if_names[(str(row[0]), int(row[1]))] = {"if_name": row[2], "if_speed": row[3]}

        for r in rs.result_rows:
            did, idx = str(r[0]), int(r[1])
            in_bps, out_bps = float(r[2] or 0), float(r[3] or 0)
            meta = if_names.get((did, idx), {})
            speed = meta.get("if_speed") or 0
            util = round(max(in_bps, out_bps) / speed * 100, 1) if speed else None
            d = device_map.get(did, {})
            top_bw.append({
                "device_id": did,
                "hostname": d.get("hostname", "Unknown"),
                "if_index": idx,
                "if_name": meta.get("if_name", f"#{idx}"),
                "in_bps_avg": in_bps,
                "out_bps_avg": out_bps,
                "utilization_pct": util,
            })
    except Exception:
        # Interface metrics table may not exist yet — return empty list
        top_bw = []

    # Interface error/discard table — best-effort
    iface_errors = []
    try:
        sql = text("""
            SELECT d.id::text AS device_id, d.hostname, di.if_index, di.if_name,
                   di.oper_status
            FROM device_interfaces di
            JOIN devices d ON d.id = di.device_id
            WHERE di.monitored = TRUE
            ORDER BY d.hostname, di.if_index
            LIMIT 50
        """)
        res = await db.execute(sql)
        for row in res.fetchall():
            iface_errors.append({
                "device_id": row[0],
                "hostname": row[1],
                "if_index": row[2],
                "if_name": row[3],
                "oper_status": row[4],
            })
    except Exception:
        iface_errors = []

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "worst_devices": worst_devices,
        "noisy_alerts": noisy_alerts_out,
        "top_bandwidth_interfaces": top_bw,
        "alert_volume_by_severity": _alert_volume_by_severity(start, end, alerts),
        "interface_errors": iface_errors,
        "outage_history": _outage_intervals(status_log, device_map),
    }


async def build_business(
    db: AsyncSession,
    from_time: Optional[datetime],
    to_time: Optional[datetime],
) -> dict:
    start, end = _normalise_window(from_time, to_time)

    services = await _fetch_service_checks(db)
    svc_ids = [str(s["id"]) for s in services]
    svc_rows = _fetch_service_metrics(start, end, svc_ids) if svc_ids else []

    # Fetch service groups for grouping
    group_map: dict[str, str] = {}
    try:
        sql = text("""
            SELECT sc.id::text AS service_id, scg.name AS group_name
            FROM service_checks sc
            LEFT JOIN service_check_groups scg ON scg.id = sc.group_id
        """)
        res = await db.execute(sql)
        for row in res.fetchall():
            group_map[row[0]] = row[1] or "Ungrouped"
    except Exception:
        pass

    # Per-service availability + response stats
    service_availability = []
    response_quantiles = []
    customer_impact_minutes = 0.0

    for s in services:
        sid = str(s["id"])
        rows = [r for r in svc_rows if str(r["service_check_id"]) == sid]
        total = len(rows)
        up = sum(1 for r in rows if r.get("is_up"))
        avail = round(up / total * 100, 2) if total else None
        failed = total - up

        resp = sorted(r["response_ms"] for r in rows if r.get("response_ms") and r.get("response_ms") > 0)
        if resp:
            n = len(resp)
            p50 = resp[int(n * 0.5)]
            p95 = resp[min(int(n * 0.95), n - 1)]
            p99 = resp[min(int(n * 0.99), n - 1)]
        else:
            p50 = p95 = p99 = None

        gname = group_map.get(sid, "Ungrouped")
        service_availability.append({
            "service_check_id": sid,
            "name": s.get("name", "Unknown"),
            "type": (s.get("check_type") or "").lower(),
            "group_name": gname,
            "status": (s.get("status") or "").lower(),
            "availability_pct": avail,
            "checks_total": total,
            "checks_failed": failed,
        })
        response_quantiles.append({
            "service_check_id": sid,
            "name": s.get("name", "Unknown"),
            "p50_ms": round(p50, 1) if p50 else None,
            "p95_ms": round(p95, 1) if p95 else None,
            "p99_ms": round(p99, 1) if p99 else None,
        })

        # Customer impact = downtime minutes for grouped services (heuristic)
        if gname != "Ungrouped" and total:
            customer_impact_minutes += (failed / total) * (end - start).total_seconds() / 60

    # TLS expiry warnings
    tls_warnings = []
    for s in services:
        days = s.get("tls_days_remaining")
        if days is None:
            continue
        if days < 30:
            severity = "critical" if days < 7 else "warning"
            expiry = s.get("tls_expiry_date")
            tls_warnings.append({
                "service_check_id": str(s["id"]),
                "name": s.get("name", "Unknown"),
                "tls_expiry_date": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
                "days_remaining": days,
                "severity": severity,
            })
    tls_warnings.sort(key=lambda x: x["days_remaining"])

    # Service outage history from status log
    svc_log = _fetch_service_status_log(start, end, svc_ids) if svc_ids else []
    svc_name_map = {str(s["id"]): s.get("name") or "Unknown" for s in services}
    service_outages = []
    for entry in svc_log:
        if (entry.get("new_status") or "").lower() not in ("down", "offline"):
            continue
        sid = str(entry["service_check_id"])
        ts = entry.get("timestamp")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        dur = entry.get("duration_sec") or 0
        service_outages.append({
            "service_check_id": sid,
            "name": svc_name_map.get(sid, "Unknown"),
            "started_at": ts.isoformat() if ts else None,
            "duration_minutes": round(dur / 60, 1) if dur else 0,
        })

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "service_availability": service_availability,
        "response_time_quantiles": response_quantiles,
        "tls_warnings": tls_warnings,
        "customer_impact_minutes": round(customer_impact_minutes, 1),
        "service_outages": service_outages,
    }


async def build_inventory(db: AsyncSession) -> dict:
    devices = await _fetch_devices(db)

    # Devices by type
    by_type: dict[str, int] = defaultdict(int)
    for d in devices:
        by_type[d.get("device_type") or "other"] += 1

    # Devices by vendor (from discovery)
    by_vendor: dict[str, int] = defaultdict(int)
    sql = text("SELECT COALESCE(vendor, 'Unknown') AS vendor, COUNT(*) FROM devices GROUP BY vendor ORDER BY 2 DESC")
    res = await db.execute(sql)
    for row in res.fetchall():
        by_vendor[row[0]] = int(row[1])

    # Devices by location
    by_location: dict[str, int] = defaultdict(int)
    for d in devices:
        by_location[d.get("location") or "Unknown"] += 1

    # Interface totals
    interface_totals = {"total": 0, "monitored": 0, "down": 0}
    try:
        sql = text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE monitored) AS monitored,
                COUNT(*) FILTER (WHERE oper_status = 'down') AS down
            FROM device_interfaces
        """)
        res = await db.execute(sql)
        row = res.fetchone()
        if row:
            interface_totals = {
                "total": int(row[0] or 0),
                "monitored": int(row[1] or 0),
                "down": int(row[2] or 0),
            }
    except Exception:
        pass

    # Sensors fleet
    sensors_out = []
    try:
        sql = text("""
            SELECT s.id::text, s.name, s.status, s.last_heartbeat_at, s.queue_depth,
                   s.queue_dropped_count, s.version, s.hostname,
                   si.name AS site_name
            FROM sensors s
            LEFT JOIN sites si ON si.id = s.site_id
            ORDER BY s.name
        """)
        res = await db.execute(sql)
        for row in res.fetchall():
            hb = row[3]
            if hb and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            sensors_out.append({
                "sensor_id": row[0],
                "name": row[1],
                "status": row[2],
                "last_heartbeat": hb.isoformat() if hb else None,
                "queue_depth": int(row[4] or 0),
                "queue_dropped_count": int(row[5] or 0),
                "version": row[6],
                "hostname": row[7],
                "site": row[8],
            })
    except Exception:
        sensors_out = []

    # Recently added devices
    recently_added = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    sql = text("""
        SELECT id::text, hostname, ip_address::text, device_type, location, vendor, model, created_at
        FROM devices
        WHERE created_at >= :cutoff
        ORDER BY created_at DESC
        LIMIT 20
    """)
    try:
        res = await db.execute(sql, {"cutoff": cutoff})
        for row in res.fetchall():
            ca = row[7]
            if ca and ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            recently_added.append({
                "device_id": row[0],
                "hostname": row[1],
                "ip": row[2],
                "device_type": row[3],
                "location": row[4],
                "vendor": row[5],
                "model": row[6],
                "added_at": ca.isoformat() if ca else None,
            })
    except Exception:
        recently_added = []

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "devices_by_type": [{"type": k, "count": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        "devices_by_vendor": [{"vendor": k, "count": v} for k, v in sorted(by_vendor.items(), key=lambda x: -x[1])],
        "devices_by_location": [{"location": k, "count": v} for k, v in sorted(by_location.items(), key=lambda x: -x[1])],
        "interface_totals": interface_totals,
        "sensors": sensors_out,
        "recently_added_devices": recently_added,
        "totals": {
            "devices": len(devices),
            "sensors": len(sensors_out),
            "interfaces": interface_totals["total"],
        },
    }
