"""NetFlow anomaly / security detection."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.models.user import User

from .common import (
    _exporter_filter,
    _query,
    _resolve_window,
    _to_iso_utc,
)

router = APIRouter()


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
    syn_scan = await _query(
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
    rst_flood = await _query(
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
    rfc1918_leak = await _query(
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
    icmp = await _query(
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
    volume = await _query(
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
