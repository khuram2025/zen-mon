"""APM deep-dive analytics: heatmap, exemplars, facets, deployments, hosts, logs, cardinality.

These endpoints sit beside the RED rollup APIs. Heatmaps / exemplars / facets /
logs / cardinality read raw ``apm_spans`` (7-day TTL). Hosts and recorded
deployments come from Postgres. Version markers also fall back to first-seen
``service_version`` on entry spans so charts still show change even when the
agent has not written ``apm_deployments``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.apm_services import _covered_seconds, _window
from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.apm_rollup import bucket_for

router = APIRouter(prefix="/apm", tags=["APM insights"])

ENTRY_KIND_STR = "('SERVER','CONSUMER')"

# Inclusive upper edges in milliseconds. Last bucket is "greater than last edge".
LATENCY_EDGES_MS: tuple[int, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


def latency_bucket_labels() -> list[str]:
    labels: list[str] = []
    prev = 0
    for edge in LATENCY_EDGES_MS:
        labels.append(_fmt_edge(prev, edge))
        prev = edge
    labels.append(f">{_fmt_ms(LATENCY_EDGES_MS[-1])}")
    return labels


def latency_bucket_expr(col: str = "(duration_nano / 1e6)") -> str:
    parts = [f"{col} < {edge}, {i}" for i, edge in enumerate(LATENCY_EDGES_MS)]
    return "multiIf(" + ", ".join(parts) + f", {len(LATENCY_EDGES_MS)})"


def latency_bucket_mid_ms(index: int) -> float:
    """Representative latency for exemplar search inside a histogram bucket."""
    if index <= 0:
        return LATENCY_EDGES_MS[0] / 2
    if index >= len(LATENCY_EDGES_MS):
        return LATENCY_EDGES_MS[-1] * 1.5
    lo, hi = LATENCY_EDGES_MS[index - 1], LATENCY_EDGES_MS[index]
    return (lo + hi) / 2


def _fmt_ms(ms: int) -> str:
    if ms >= 1000:
        v = ms / 1000
        return f"{int(v)}s" if v == int(v) else f"{v:g}s"
    return f"{ms}ms"


def _fmt_edge(lo: int, hi: int) -> str:
    if lo == 0:
        return f"<{_fmt_ms(hi)}"
    return f"{_fmt_ms(lo)}–{_fmt_ms(hi)}"


def _ch():
    return get_ch_client()


def _iso(ts) -> str:
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    return str(ts)


# ── schemas ──────────────────────────────────────────────────────────────────

class HeatmapResponse(BaseModel):
    buckets: list[str]
    points: list[dict]
    histogram: list[dict]
    window_seconds: int


class ExemplarResponse(BaseModel):
    found: bool
    trace_id: Optional[str] = None
    operation: Optional[str] = None
    duration_ms: Optional[float] = None
    has_error: Optional[bool] = None
    timestamp: Optional[str] = None
    http_status_code: Optional[int] = None


class FacetValue(BaseModel):
    value: str
    count: int


class TraceFacetsResponse(BaseModel):
    services: list[FacetValue]
    operations: list[FacetValue]
    envs: list[FacetValue]
    versions: list[FacetValue]
    http_status: list[FacetValue]
    status_code: list[FacetValue]
    http_route: list[FacetValue]


class DeploymentMarker(BaseModel):
    version: str
    first_seen: str
    last_seen: Optional[str] = None
    traces: int = 0
    source: str
    git_sha: Optional[str] = None


class HostProcess(BaseModel):
    id: str
    hostname: str
    server_name: str
    server_ip: Optional[str] = None
    pid: int
    runtime: str
    runtime_version: str
    exe_path: str
    service_name_guess: str
    instrumentation_state: str
    last_seen_at: Optional[str] = None
    last_trace_at: Optional[str] = None
    traces_15m: int = 0


class CardinalityKey(BaseModel):
    key: str
    distinct: int
    spans: int


class CardinalityResponse(BaseModel):
    hours: int
    spans: int
    services: int
    operations: int
    routes: int
    versions: int
    attributes: list[CardinalityKey]


class TraceLogLine(BaseModel):
    source: str
    timestamp: str
    service_name: str
    span_id: str
    name: str
    message: str
    level: str = "info"


# ── heatmap ──────────────────────────────────────────────────────────────────

@router.get("/services/{name}/heatmap", response_model=HeatmapResponse)
async def service_latency_heatmap(
    name: str,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    del user
    frm, to = _window(from_ms, to_ms, range_)
    win_s = int(_covered_seconds(frm, to))
    bucket_s = bucket_for(win_s)
    labels = latency_bucket_labels()
    n_buckets = len(labels)
    expr = latency_bucket_expr()
    params = {"frm": frm, "to": to, "svc": name, "bs": bucket_s}

    def run():
        sql = f"""
            SELECT
                toDateTime(intDiv(toUnixTimestamp(timestamp), {{bs:UInt32}}) * {{bs:UInt32}}) AS t,
                {expr} AS b,
                count() AS c
            FROM zenplus.apm_spans
            WHERE service_name = {{svc:String}}
              AND timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind_str IN {ENTRY_KIND_STR}
            GROUP BY t, b
            ORDER BY t, b
        """
        rows = _ch().query(sql, parameters=params).result_rows
        by_t: dict[str, list[int]] = {}
        hist = [0] * n_buckets
        for t, b, c in rows:
            key = _iso(t)
            slot = by_t.setdefault(key, [0] * n_buckets)
            idx = int(b)
            if 0 <= idx < n_buckets:
                n = int(c)
                slot[idx] += n
                hist[idx] += n
        points = [{"timestamp": t, "counts": counts} for t, counts in by_t.items()]
        histogram = [{"bucket": labels[i], "count": hist[i]} for i in range(n_buckets)]
        return points, histogram

    points, histogram = await asyncio.to_thread(run)
    return HeatmapResponse(
        buckets=labels, points=points, histogram=histogram, window_seconds=win_s,
    )


# ── exemplar ─────────────────────────────────────────────────────────────────

@router.get("/services/{name}/exemplar", response_model=ExemplarResponse)
async def service_exemplar(
    name: str,
    at_ms: int = Query(..., description="unix ms of the chart/heatmap cell"),
    metric: str = Query("p95", pattern="^(p95|p50|error|rps)$"),
    target_ms: Optional[float] = Query(None, description="latency to match, from heatmap bucket"),
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    del user
    frm, to = _window(from_ms, to_ms, range_)
    bucket_s = bucket_for(int(_covered_seconds(frm, to)))
    half = max(int(bucket_s * 500), 30_000)
    b_from = max(frm, at_ms - half)
    b_to = min(to, at_ms + half)
    if b_to <= b_from:
        b_from, b_to = frm, to

    extra = ""
    order = "abs((duration_nano / 1e6) - {target:Float64}) ASC, duration_nano DESC"
    target = float(target_ms) if target_ms is not None else (500.0 if metric != "p50" else 100.0)
    if metric == "error":
        extra = "AND has_error = 1"
        order = "timestamp DESC"
    elif metric == "rps":
        extra = ""
        order = "timestamp DESC"
    params = {"svc": name, "frm": b_from, "to": b_to, "target": target}

    def run():
        sql = f"""
            SELECT trace_id, name, duration_nano / 1e6, has_error, timestamp, http_status_code
            FROM zenplus.apm_spans
            WHERE service_name = {{svc:String}}
              AND timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind_str IN {ENTRY_KIND_STR}
              {extra}
            ORDER BY {order}
            LIMIT 1
        """
        rows = _ch().query(sql, parameters=params).result_rows
        if not rows:
            return None
        r = rows[0]
        return ExemplarResponse(
            found=True,
            trace_id=str(r[0]).rstrip("\x00"),
            operation=r[1],
            duration_ms=round(float(r[2] or 0), 3),
            has_error=bool(r[3]),
            timestamp=_iso(r[4]),
            http_status_code=int(r[5] or 0),
        )

    hit = await asyncio.to_thread(run)
    return hit or ExemplarResponse(found=False)


# ── facets ───────────────────────────────────────────────────────────────────

@router.get("/trace-facets", response_model=TraceFacetsResponse)
async def trace_facets(
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    service: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    del user
    frm, to = _window(from_ms, to_ms, range_)
    params: dict = {"frm": frm, "to": to}
    svc_cond = ""
    if service:
        svc_cond = "AND service_name = {svc:String}"
        params["svc"] = service

    def facet(col: str, empty: str = "(unset)", limit: int = 20, numeric: bool = False) -> list[FacetValue]:
        value_expr = (
            f"if({col} = 0, '{empty}', toString({col}))"
            if numeric else
            f"if({col} = '' OR {col} IS NULL, '{empty}', toString({col}))"
        )
        sql = f"""
            SELECT {value_expr} AS v, count() AS c
            FROM zenplus.apm_spans
            WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind_str IN {ENTRY_KIND_STR}
              {svc_cond}
            GROUP BY v
            ORDER BY c DESC
            LIMIT {int(limit)}
        """
        return [FacetValue(value=str(r[0]), count=int(r[1])) for r in _ch().query(sql, parameters=params).result_rows]

    def run():
        return TraceFacetsResponse(
            services=facet("service_name"),
            operations=facet("name"),
            envs=facet("env"),
            versions=facet("service_version"),
            http_status=facet("http_status_code", empty="0", numeric=True),
            status_code=facet("status_code"),
            http_route=facet("http_route"),
        )

    return await asyncio.to_thread(run)


# ── deployments ──────────────────────────────────────────────────────────────

@router.get("/services/{name}/deployments")
async def service_deployments(
    name: str,
    range_: Optional[str] = Query("7d", alias="range"),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    del user
    frm, to = _window(from_ms, to_ms, range_)
    items: list[DeploymentMarker] = []
    frm_dt = datetime.fromtimestamp(frm / 1000, tz=timezone.utc)
    to_dt = datetime.fromtimestamp(to / 1000, tz=timezone.utc)

    pg_rows = (await db.execute(text("""
        SELECT d.version, d.git_sha, d.deployed_at, d.metadata
        FROM apm_deployments d
        JOIN apm_services s ON s.id = d.service_id
        WHERE s.name = :name
          AND d.deployed_at >= :frm
          AND d.deployed_at <  :to
        ORDER BY d.deployed_at
    """), {"name": name, "frm": frm_dt, "to": to_dt})).mappings().all()
    for r in pg_rows:
        items.append(DeploymentMarker(
            version=r["version"] or "(unset)",
            first_seen=_iso(r["deployed_at"]),
            last_seen=_iso(r["deployed_at"]),
            traces=0,
            source="agent",
            git_sha=r["git_sha"],
        ))

    def from_spans():
        sql = """
            SELECT
                if(service_version = '', '(unset)', service_version) AS version,
                min(timestamp) AS first_seen,
                max(timestamp) AS last_seen,
                uniqExact(trace_id) AS traces
            FROM zenplus.apm_spans
            WHERE service_name = {svc:String}
              AND timestamp >= fromUnixTimestamp64Milli({frm:Int64})
              AND timestamp <  fromUnixTimestamp64Milli({to:Int64})
              AND span_kind_str IN ('SERVER','CONSUMER')
            GROUP BY version
            ORDER BY first_seen
        """
        out = []
        for r in _ch().query(sql, parameters={"svc": name, "frm": frm, "to": to}).result_rows:
            out.append(DeploymentMarker(
                version=str(r[0]),
                first_seen=_iso(r[1]),
                last_seen=_iso(r[2]),
                traces=int(r[3] or 0),
                source="spans",
            ))
        return out

    span_items = await asyncio.to_thread(from_spans)
    # Prefer recorded agent deployments; still surface span versions that have
    # no matching recorded row so charts keep a change marker.
    recorded = {(m.version, m.first_seen[:16]) for m in items}
    for m in span_items:
        key = (m.version, m.first_seen[:16])
        if m.version == "(unset)" and m.traces and not items:
            items.append(m)
        elif key not in recorded and m.version != "(unset)":
            items.append(m)
    items.sort(key=lambda m: m.first_seen)
    return {"items": [m.model_dump() for m in items]}


# ── hosts / processes ────────────────────────────────────────────────────────

@router.get("/services/{name}/hosts")
async def service_hosts(
    name: str,
    active_hours: int = Query(168, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    del user
    rows = (await db.execute(text("""
        SELECT p.id, p.pid, p.runtime, p.runtime_version, p.exe_path,
               p.service_name_guess, p.instrumentation_state, p.last_seen_at,
               p.iis_site, p.windows_service,
               a.hostname,
               COALESCE(NULLIF(s.display_name, ''), a.hostname) AS server_name,
               COALESCE(host(s.primary_ip), host(a.last_ip)) AS server_ip
        FROM apm_agent_processes p
        JOIN agents a ON a.id = p.agent_id
        LEFT JOIN servers s ON s.id = p.server_id
        WHERE p.last_seen_at >= NOW() - make_interval(hours => :hours)
          AND (
                p.service_name_guess = :name
             OR p.iis_site = :name
             OR p.windows_service = :name
             OR p.iis_app_pool = :name
          )
        ORDER BY p.last_seen_at DESC
        LIMIT 50
    """), {"name": name, "hours": active_hours})).mappings().all()

    def traces():
        sql = """
            SELECT max(timestamp), countIf(timestamp >= now() - INTERVAL 15 MINUTE)
            FROM zenplus.apm_spans
            WHERE service_name = {svc:String}
              AND timestamp >= now() - INTERVAL 24 HOUR
        """
        r = _ch().query(sql, parameters={"svc": name}).result_rows
        if not r:
            return None, 0
        return _iso(r[0][0]) if r[0][0] else None, int(r[0][1] or 0)

    last_trace, traces_15m = await asyncio.to_thread(traces)
    processes = [
        HostProcess(
            id=str(r["id"]),
            hostname=r["hostname"] or "",
            server_name=r["server_name"] or r["hostname"] or "",
            server_ip=r["server_ip"],
            pid=int(r["pid"] or 0),
            runtime=r["runtime"] or "other",
            runtime_version=r["runtime_version"] or "",
            exe_path=r["exe_path"] or "",
            service_name_guess=r["service_name_guess"] or "",
            instrumentation_state=r["instrumentation_state"] or "none",
            last_seen_at=_iso(r["last_seen_at"]),
            last_trace_at=last_trace,
            traces_15m=traces_15m,
        ).model_dump()
        for r in rows
    ]
    return {"processes": processes, "last_trace_at": last_trace, "traces_15m": traces_15m}


# ── correlated logs (span events + exceptions) ───────────────────────────────

@router.get("/traces/{trace_id}/logs")
async def trace_logs(trace_id: str, user: User = Depends(get_current_user)):
    del user
    if not trace_id or len(trace_id) > 64:
        raise HTTPException(400, "Invalid trace id")
    tid = trace_id.strip()

    def run():
        lines: list[TraceLogLine] = []
        span_sql = """
            SELECT service_name, span_id, timestamp, events_ts, events_name, events_attrs,
                   status_code, status_message, has_error, name
            FROM zenplus.apm_spans
            WHERE trace_id = {tid:String}
            LIMIT 5000
        """
        for r in _ch().query(span_sql, parameters={"tid": tid}).result_rows:
            service, span_id, ts, ev_ts, ev_name, ev_attrs, status, msg, has_err, name = r
            if has_err or (status or "") == "ERROR":
                lines.append(TraceLogLine(
                    source="span_status",
                    timestamp=_iso(ts),
                    service_name=service or "",
                    span_id=str(span_id or ""),
                    name=name or "span.error",
                    message=msg or "span marked ERROR",
                    level="error",
                ))
            names = list(ev_name or [])
            times = list(ev_ts or [])
            attrs = list(ev_attrs or [])
            for i, ename in enumerate(names):
                ets = times[i] if i < len(times) else ts
                eattr = attrs[i] if i < len(attrs) else ""
                lines.append(TraceLogLine(
                    source="span_event",
                    timestamp=_iso(ets) if not isinstance(ets, (int, float)) else _iso(ts),
                    service_name=service or "",
                    span_id=str(span_id or ""),
                    name=str(ename or "event"),
                    message=str(eattr or ""),
                    level="error" if "exception" in str(ename).lower() else "info",
                ))
        exc_sql = """
            SELECT timestamp, service_name, span_id, exception_type, exception_message
            FROM zenplus.apm_exceptions
            WHERE trace_id = {tid:String}
            ORDER BY timestamp
            LIMIT 200
        """
        try:
            for r in _ch().query(exc_sql, parameters={"tid": tid}).result_rows:
                lines.append(TraceLogLine(
                    source="exception",
                    timestamp=_iso(r[0]),
                    service_name=r[1] or "",
                    span_id=str(r[2] or ""),
                    name=r[3] or "Exception",
                    message=r[4] or "",
                    level="error",
                ))
        except Exception:
            pass
        lines.sort(key=lambda L: L.timestamp)
        return [L.model_dump() for L in lines[:500]]

    items = await asyncio.to_thread(run)
    return {
        "trace_id": tid,
        "items": items,
        "source": "span_events_and_exceptions",
        "note": "OTLP logs are not ingested yet; this view correlates span events and exception records for the trace.",
    }


# ── cardinality / ingest explorer ────────────────────────────────────────────

@router.get("/cardinality", response_model=CardinalityResponse)
async def ingest_cardinality(
    hours: int = Query(24, ge=1, le=168),
    service: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    del user
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    frm = now - hours * 3_600_000
    params: dict = {"frm": frm, "to": now}
    svc_cond = ""
    if service:
        svc_cond = "AND service_name = {svc:String}"
        params["svc"] = service

    def run():
        totals_sql = f"""
            SELECT
                count() AS spans,
                uniqExact(service_name) AS services,
                uniqExact(name) AS operations,
                uniqExact(http_route) AS routes,
                uniqExact(service_version) AS versions
            FROM zenplus.apm_spans
            WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              {svc_cond}
        """
        t = _ch().query(totals_sql, parameters=params).result_rows[0]
        attr_sql = f"""
            SELECT
                key,
                uniqHLL12(attributes_string[key]) AS distinct_vals,
                count() AS spans
            FROM zenplus.apm_spans
            ARRAY JOIN mapKeys(attributes_string) AS key
            WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              {svc_cond}
              AND key != ''
            GROUP BY key
            ORDER BY distinct_vals DESC
            LIMIT 40
        """
        attrs = [
            CardinalityKey(key=str(r[0]), distinct=int(r[1] or 0), spans=int(r[2] or 0))
            for r in _ch().query(attr_sql, parameters=params).result_rows
        ]
        return CardinalityResponse(
            hours=hours,
            spans=int(t[0] or 0),
            services=int(t[1] or 0),
            operations=int(t[2] or 0),
            routes=int(t[3] or 0),
            versions=int(t[4] or 0),
            attributes=attrs,
        )

    return await asyncio.to_thread(run)


# Re-export for tests (RANGE_MS imported so window math stays in one place).
__all__ = [
    "router", "LATENCY_EDGES_MS", "latency_bucket_labels", "latency_bucket_expr",
    "latency_bucket_mid_ms",
]
