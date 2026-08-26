"""APM trace explorer + single-trace waterfall (AM-E2).

Read paths over the ClickHouse `apm_spans` table:
  * GET /api/v1/apm/traces            — trace search (filters + live/indexed window)
  * GET /api/v1/apm/traces/{trace_id} — full ordered span tree for one trace

Trace-id lookup is served by the `idx_trace_id` bloom skip index, not a scan.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.database import get_ch_client
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/apm", tags=["APM traces"])


# ── schemas ──────────────────────────────────────────────────────────────────

class TraceSummary(BaseModel):
    trace_id: str
    root_service: str
    root_operation: str
    start_time: datetime
    duration_ms: float
    span_count: int
    error_count: int
    has_error: bool
    services: list[str]


class TraceListResponse(BaseModel):
    traces: list[TraceSummary]
    mode: str
    count: int


class SpanNode(BaseModel):
    span_id: str
    parent_span_id: str
    name: str
    service_name: str
    span_kind: str
    status_code: str
    status_message: str
    has_error: bool
    depth: int
    start_offset_ms: float
    duration_ms: float
    http_method: str
    http_route: str
    http_status_code: int
    db_system: str
    db_operation: str
    db_statement: str
    rpc_method: str
    attributes: dict
    events: list[dict]


class TraceDetailResponse(BaseModel):
    trace_id: str
    start_time: datetime
    duration_ms: float
    span_count: int
    services: list[str]
    spans: list[SpanNode]


# ── helpers ──────────────────────────────────────────────────────────────────

def _ch():
    return get_ch_client()


def _build_trace_list(
    *, frm_ms: int, to_ms: int, service: Optional[str], operation: Optional[str],
    errors_only: bool, http_route: Optional[str], env: Optional[str],
    min_duration_ms: Optional[float], limit: int,
    http_status_code: Optional[int] = None, service_version: Optional[str] = None,
    status_code: Optional[str] = None,
) -> list[dict]:
    # Window applies to both stages (a trace's spans are short-lived, so this
    # keeps partition pruning while still summarising the *whole* matched trace).
    win = ["timestamp >= fromUnixTimestamp64Milli({frm:Int64})",
           "timestamp <  fromUnixTimestamp64Milli({to:Int64})"]
    params: dict = {"frm": frm_ms, "to": to_ms, "lim": limit}
    match_conds = list(win)
    if service:
        match_conds.append("service_name = {svc:String}"); params["svc"] = service
    if operation:
        match_conds.append("name = {op:String}"); params["op"] = operation
    if errors_only:
        match_conds.append("has_error = 1")
    if http_route:
        match_conds.append("http_route = {rt:String}"); params["rt"] = http_route
    if env:
        match_conds.append("env = {env:String}"); params["env"] = env
    if http_status_code is not None:
        match_conds.append("http_status_code = {hsc:UInt16}"); params["hsc"] = int(http_status_code)
    if service_version:
        match_conds.append("service_version = {ver:String}"); params["ver"] = service_version
    if status_code:
        match_conds.append("status_code = {st:String}"); params["st"] = status_code
    match_where = " AND ".join(match_conds)
    win_where = " AND ".join(win)

    having = ""
    if min_duration_ms is not None:
        having = "HAVING duration_ms >= {mindur:Float64}"
        params["mindur"] = float(min_duration_ms)

    # Stage 1: trace_ids whose spans match the filters. Stage 2: summarise ALL
    # spans of those traces (true root service/op, total spans, full duration).
    sql = f"""
        WITH matched AS (
            SELECT trace_id
            FROM zenplus.apm_spans
            WHERE {match_where}
            GROUP BY trace_id
            ORDER BY max(timestamp) DESC
            LIMIT {{lim:UInt32}}
        )
        SELECT
            trace_id,
            argMin(service_name, timestamp)                                   AS root_service,
            argMin(name, timestamp)                                           AS root_operation,
            min(timestamp)                                                    AS start_time,
            (max(toUnixTimestamp64Nano(timestamp) + duration_nano)
                - min(toUnixTimestamp64Nano(timestamp))) / 1e6                AS duration_ms,
            count()                                                           AS span_count,
            countIf(has_error = 1)                                            AS error_count,
            max(has_error)                                                    AS trace_error,
            arrayDistinct(groupArray(service_name))                           AS services
        FROM zenplus.apm_spans
        WHERE trace_id IN (SELECT trace_id FROM matched) AND {win_where}
        GROUP BY trace_id
        {having}
        ORDER BY start_time DESC
    """
    res = _ch().query(sql, parameters=params)
    out = []
    for r in res.result_rows:
        out.append({
            "trace_id": r[0], "root_service": r[1], "root_operation": r[2],
            "start_time": r[3], "duration_ms": round(float(r[4]), 3),
            "span_count": int(r[5]), "error_count": int(r[6]),
            "has_error": bool(r[7]), "services": list(r[8]),
        })
    return out


def _fetch_trace_spans(trace_id: str) -> list[dict]:
    sql = """
        SELECT
            span_id, parent_span_id, name, service_name, span_kind_str,
            status_code, status_message, has_error,
            toUnixTimestamp64Nano(timestamp) AS start_ns, duration_nano,
            http_method, http_route, http_status_code,
            db_system, db_operation, db_statement, rpc_method,
            attributes_string, attributes_number, attributes_bool,
            events_ts, events_name, events_attrs
        FROM zenplus.apm_spans
        WHERE trace_id = {tid:String}
        ORDER BY start_ns
        LIMIT 5000
    """
    res = _ch().query(sql, parameters={"tid": trace_id})
    cols = res.column_names
    return [dict(zip(cols, row)) for row in res.result_rows]


def _assemble_tree(spans: list[dict]) -> tuple[list[dict], float, list[str]]:
    """Return (ordered SpanNode dicts with depth+offset, trace_duration_ms, services)."""
    if not spans:
        return [], 0.0, []
    start_ns = min(s["start_ns"] for s in spans)
    end_ns = max(s["start_ns"] + int(s["duration_nano"]) for s in spans)
    trace_dur_ms = (end_ns - start_ns) / 1e6
    services = sorted({s["service_name"] for s in spans})

    by_id = {s["span_id"]: s for s in spans}
    children: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for s in spans:
        p = s["parent_span_id"]
        if p and p in by_id:
            children.setdefault(p, []).append(s)
        else:
            roots.append(s)
    # stable ordering by start time
    roots.sort(key=lambda s: s["start_ns"])
    for lst in children.values():
        lst.sort(key=lambda s: s["start_ns"])

    ordered: list[dict] = []

    def walk(span: dict, depth: int):
        attrs = {}
        for m in ("attributes_string", "attributes_number", "attributes_bool"):
            attrs.update(span.get(m) or {})
        ev = []
        ev_ts = span.get("events_ts") or []
        ev_name = span.get("events_name") or []
        ev_attrs = span.get("events_attrs") or []
        for i in range(len(ev_ts)):
            t_ns = int(ev_ts[i].timestamp() * 1e9) if hasattr(ev_ts[i], "timestamp") else 0
            ev.append({
                "name": ev_name[i] if i < len(ev_name) else "",
                "offset_ms": round((t_ns - start_ns) / 1e6, 3) if t_ns else 0.0,
                "attributes": ev_attrs[i] if i < len(ev_attrs) else "",
            })
        ordered.append({
            "span_id": span["span_id"], "parent_span_id": span["parent_span_id"],
            "name": span["name"], "service_name": span["service_name"],
            "span_kind": span["span_kind_str"], "status_code": span["status_code"],
            "status_message": span["status_message"], "has_error": bool(span["has_error"]),
            "depth": depth,
            "start_offset_ms": round((span["start_ns"] - start_ns) / 1e6, 3),
            "duration_ms": round(int(span["duration_nano"]) / 1e6, 3),
            "http_method": span["http_method"], "http_route": span["http_route"],
            "http_status_code": int(span["http_status_code"]),
            "db_system": span["db_system"], "db_operation": span["db_operation"],
            "db_statement": span["db_statement"], "rpc_method": span["rpc_method"],
            "attributes": attrs, "events": ev,
        })
        for c in children.get(span["span_id"], []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    return ordered, trace_dur_ms, services


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/traces", response_model=TraceListResponse)
async def list_traces(
    mode: str = Query("live", pattern="^(live|indexed)$"),
    service: Optional[str] = None,
    operation: Optional[str] = None,
    errors_only: bool = False,
    http_route: Optional[str] = None,
    env: Optional[str] = None,
    min_duration_ms: Optional[float] = None,
    http_status_code: Optional[int] = None,
    service_version: Optional[str] = None,
    status_code: Optional[str] = None,
    from_ms: Optional[int] = Query(None, description="window start, unix ms"),
    to_ms: Optional[int] = Query(None, description="window end, unix ms"),
    limit: int = Query(100, ge=1, le=1000),
    user: User = Depends(get_current_user),
):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if mode == "live":
        # recent rolling window (default last 15m); ignores from/to
        to = now_ms
        frm = now_ms - 15 * 60 * 1000
    else:
        to = to_ms if to_ms is not None else now_ms
        frm = from_ms if from_ms is not None else now_ms - 60 * 60 * 1000

    rows = await asyncio.to_thread(
        _build_trace_list, frm_ms=frm, to_ms=to, service=service, operation=operation,
        errors_only=errors_only, http_route=http_route, env=env,
        min_duration_ms=min_duration_ms, limit=limit,
        http_status_code=http_status_code, service_version=service_version,
        status_code=status_code,
    )
    return TraceListResponse(
        traces=[TraceSummary(**r) for r in rows], mode=mode, count=len(rows)
    )


@router.get("/traces/{trace_id}", response_model=TraceDetailResponse)
async def get_trace(trace_id: str, user: User = Depends(get_current_user)):
    if not trace_id or len(trace_id) > 64:
        raise HTTPException(400, "Invalid trace id")
    spans = await asyncio.to_thread(_fetch_trace_spans, trace_id)
    if not spans:
        raise HTTPException(404, "Trace not found")
    ordered, dur_ms, services = _assemble_tree(spans)
    return TraceDetailResponse(
        trace_id=trace_id,
        start_time=min(datetime.fromtimestamp(s["start_ns"] / 1e9, tz=timezone.utc) for s in spans),
        duration_ms=round(dur_ms, 3),
        span_count=len(spans),
        services=services,
        spans=[SpanNode(**s) for s in ordered],
    )
