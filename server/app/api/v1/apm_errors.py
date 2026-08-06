"""APM error tracking / issues inbox (AM-E4).

Occurrence data (counts, trends, stacks, linked traces) comes from ClickHouse
`apm_exceptions`; triage state (status/assignee) lives in Postgres
`apm_error_issues`. Issues are grouped by the `group_id` fingerprint produced at
ingest (see apm_ingest.exception_group_id).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/apm", tags=["APM errors"])

VALID_STATUS = {"unresolved", "resolved", "resolved_in_version", "ignored"}


# ── schemas ──────────────────────────────────────────────────────────────────

class ErrorIssue(BaseModel):
    group_id: str
    exception_type: str
    message: str
    service: str
    services: list[str]
    occurrences: int
    traces: int
    first_seen: datetime
    last_seen: datetime
    versions: list[str]
    http_route: str
    status: str = "unresolved"
    assignee: Optional[str] = None
    resolved_in_version: Optional[str] = None


class ErrorListResponse(BaseModel):
    issues: list[ErrorIssue]
    counts: dict[str, int]


class Occurrence(BaseModel):
    timestamp: datetime
    trace_id: str
    span_id: str
    service: str
    message: str


class TrendPoint(BaseModel):
    timestamp: datetime
    count: int


class ErrorDetail(ErrorIssue):
    sample_stack: str
    representative_trace_id: str
    per_service: list[dict]
    occurrences_recent: list[Occurrence]
    trend: list[TrendPoint]


class TriageUpdate(BaseModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    resolved_in_version: Optional[str] = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _window_ms(range_: str) -> tuple[int, int]:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    spans = {"1h": 3_600_000, "6h": 6 * 3_600_000, "24h": 24 * 3_600_000, "7d": 7 * 24 * 3_600_000}
    return now - spans.get(range_, 24 * 3_600_000), now


def _ch():
    return get_ch_client()


def _s(v):
    """ClickHouse FixedString columns (group_id, trace_id) come back as bytes."""
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def _query_groups(frm: int, to: int, service: Optional[str], env: Optional[str]) -> list[dict]:
    params = {"frm": frm, "to": to}
    cond = ""
    if service:
        cond += " AND service_name = {svc:String}"; params["svc"] = service
    if env:
        cond += " AND env = {env:String}"; params["env"] = env
    sql = f"""
        SELECT group_id,
               any(exception_type)                 AS etype,
               argMax(exception_message, timestamp) AS msg,
               any(service_name)                   AS service,
               groupUniqArray(service_name)        AS services,
               count()                             AS occurrences,
               uniqExact(trace_id)                 AS traces,
               min(timestamp)                      AS first_seen,
               max(timestamp)                      AS last_seen,
               groupUniqArray(service_version)     AS versions,
               any(http_route)                     AS route
        FROM zenplus.apm_exceptions
        WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
          AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}}) {cond}
        GROUP BY group_id
        ORDER BY last_seen DESC
        LIMIT 200
    """
    out = []
    for r in _ch().query(sql, parameters=params).result_rows:
        out.append({
            "group_id": _s(r[0]), "exception_type": r[1], "message": r[2] or "",
            "service": r[3], "services": list(r[4]), "occurrences": int(r[5]),
            "traces": int(r[6]), "first_seen": r[7], "last_seen": r[8],
            "versions": [v for v in r[9] if v], "http_route": r[10] or "",
        })
    return out


async def _triage_map(db: AsyncSession, group_ids: list[str]) -> dict[str, dict]:
    """group_id -> triage row.

    ``apm_error_issues`` is keyed on (group_id, service_id), so a fingerprint
    seen in two services has two rows while the inbox shows one issue. Collapse
    deterministically on the most recently touched row rather than letting the
    dict-build pick an arbitrary one — otherwise the displayed status flipped
    between refreshes for any cross-service error.
    """
    if not group_ids:
        return {}
    rows = (await db.execute(text(
        "SELECT DISTINCT ON (group_id) group_id, status, assignee, resolved_in_version "
        "FROM apm_error_issues WHERE group_id = ANY(:gids) "
        "ORDER BY group_id, updated_at DESC NULLS LAST, last_seen_at DESC NULLS LAST"
    ), {"gids": group_ids})).mappings().all()
    return {r["group_id"]: dict(r) for r in rows}


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/errors", response_model=ErrorListResponse)
async def list_errors(
    range_: str = "24h",
    service: Optional[str] = None,
    env: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    frm, to = _window_ms(range_)
    groups = await asyncio.to_thread(_query_groups, frm, to, service, env)
    triage = await _triage_map(db, [g["group_id"] for g in groups])

    issues = []
    # Every status gets a key even at zero, so the filter chips render a stable
    # row of counts instead of appearing and disappearing as triage changes.
    counts: dict[str, int] = {s: 0 for s in VALID_STATUS}
    counts["all"] = len(groups)
    for g in groups:
        t = triage.get(g["group_id"], {})
        st = t.get("status") or "unresolved"
        counts[st] = counts.get(st, 0) + 1
        if status and st != status:
            continue
        issues.append(ErrorIssue(**g, status=st, assignee=t.get("assignee"),
                                  resolved_in_version=t.get("resolved_in_version")))
    return ErrorListResponse(issues=issues, counts=counts)


@router.get("/errors/{group_id}", response_model=ErrorDetail)
async def get_error(
    group_id: str, range_: str = "24h",
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    frm, to = _window_ms(range_)

    def fetch():
        p = {"gid": group_id, "frm": frm, "to": to}
        win = ("timestamp >= fromUnixTimestamp64Milli({frm:Int64}) "
               "AND timestamp < fromUnixTimestamp64Milli({to:Int64})")
        base = _query_groups(frm, to, None, None)
        summary = next((g for g in base if g["group_id"] == group_id), None)
        stack = _ch().query(
            f"SELECT exception_stack FROM zenplus.apm_exceptions WHERE group_id={{gid:String}} "
            f"AND exception_stack != '' AND {win} ORDER BY timestamp DESC LIMIT 1", parameters=p
        ).result_rows
        rep = _ch().query(
            f"SELECT trace_id FROM zenplus.apm_exceptions WHERE group_id={{gid:String}} AND {win} "
            f"ORDER BY timestamp DESC LIMIT 1", parameters=p
        ).result_rows
        per_svc = _ch().query(
            f"SELECT service_name, count() FROM zenplus.apm_exceptions WHERE group_id={{gid:String}} "
            f"AND {win} GROUP BY service_name ORDER BY count() DESC", parameters=p
        ).result_rows
        occ = _ch().query(
            f"SELECT timestamp, trace_id, span_id, service_name, exception_message "
            f"FROM zenplus.apm_exceptions WHERE group_id={{gid:String}} AND {win} "
            f"ORDER BY timestamp DESC LIMIT 25", parameters=p
        ).result_rows
        trend = _ch().query(
            f"SELECT toStartOfInterval(timestamp, INTERVAL 1 hour) AS t, count() "
            f"FROM zenplus.apm_exceptions WHERE group_id={{gid:String}} AND {win} "
            f"GROUP BY t ORDER BY t", parameters=p
        ).result_rows
        return summary, stack, rep, per_svc, occ, trend

    summary, stack, rep, per_svc, occ, trend = await asyncio.to_thread(fetch)
    if not summary:
        raise HTTPException(404, "Error issue not found")
    triage = await _triage_map(db, [group_id])
    t = triage.get(group_id, {})

    return ErrorDetail(
        **summary, status=t.get("status") or "unresolved", assignee=t.get("assignee"),
        resolved_in_version=t.get("resolved_in_version"),
        sample_stack=(stack[0][0] if stack else ""),
        representative_trace_id=(_s(rep[0][0]) if rep else ""),
        per_service=[{"service": r[0], "count": int(r[1])} for r in per_svc],
        occurrences_recent=[Occurrence(timestamp=r[0], trace_id=_s(r[1]), span_id=r[2], service=r[3], message=r[4] or "") for r in occ],
        trend=[TrendPoint(timestamp=r[0], count=int(r[1])) for r in trend],
    )


@router.patch("/errors/{group_id}", response_model=ErrorIssue)
async def triage_error(
    group_id: str, body: TriageUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    if body.status and body.status not in VALID_STATUS:
        raise HTTPException(400, f"Invalid status; must be one of {sorted(VALID_STATUS)}")

    # resolve the group's dominant service + env, register the service if needed
    def ch_meta():
        r = _ch().query(
            "SELECT any(service_name), any(env), min(timestamp), max(timestamp) "
            "FROM zenplus.apm_exceptions WHERE group_id={gid:String}", parameters={"gid": group_id}
        ).result_rows
        return r[0] if r else None
    meta = await asyncio.to_thread(ch_meta)
    if not meta or not meta[0]:
        raise HTTPException(404, "Error issue not found")
    svc_name, env_name, first_seen, last_seen = meta

    env_id = None
    if env_name:
        row = (await db.execute(text("SELECT id FROM apm_environments WHERE name=:n"), {"n": env_name})).first()
        env_id = row[0] if row else None
    svc = (await db.execute(text(
        "SELECT id FROM apm_services WHERE name=:n AND env_id IS NOT DISTINCT FROM :e"
    ), {"n": svc_name, "e": env_id})).first()
    if svc:
        service_id = svc[0]
    else:
        service_id = (await db.execute(text(
            "INSERT INTO apm_services (name, env_id) VALUES (:n, :e) RETURNING id"
        ), {"n": svc_name, "e": env_id})).scalar()

    await db.execute(text("""
        INSERT INTO apm_error_issues
            (group_id, service_id, status, assignee, resolved_in_version, first_seen_at, last_seen_at)
        VALUES (:g, :sid, COALESCE(:st,'unresolved'), :asg, :riv, :fs, :ls)
        ON CONFLICT (group_id, service_id) DO UPDATE SET
            status = COALESCE(EXCLUDED.status, apm_error_issues.status),
            assignee = COALESCE(EXCLUDED.assignee, apm_error_issues.assignee),
            resolved_in_version = COALESCE(EXCLUDED.resolved_in_version, apm_error_issues.resolved_in_version),
            last_seen_at = EXCLUDED.last_seen_at,
            updated_at = NOW()
    """), {"g": group_id, "sid": service_id, "st": body.status, "asg": body.assignee,
           "riv": body.resolved_in_version, "fs": first_seen, "ls": last_seen})
    await db.commit()

    frm, to = _window_ms("7d")
    groups = await asyncio.to_thread(_query_groups, frm, to, None, None)
    g = next((x for x in groups if x["group_id"] == group_id), None)
    triage = await _triage_map(db, [group_id])
    t = triage.get(group_id, {})
    if not g:
        raise HTTPException(404, "Error issue not found")
    return ErrorIssue(**g, status=t.get("status") or "unresolved", assignee=t.get("assignee"),
                      resolved_in_version=t.get("resolved_in_version"))
