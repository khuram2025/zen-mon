"""APM usage analytics — traffic, pages, operations and users.

Answers "who uses what, how much": request volumes and trends, top pages
(``http_route``), top operations, and per-user statistics attributed from the
OTel ``enduser.id`` / ``user.id`` span attributes when the instrumented app
provides them.

Reads raw ``apm_spans`` (typed attribute maps, bloom-indexed) scoped to entry
spans, so the window is bounded by the raw-span TTL (7 days). RED/performance
views stay on the rollups; usage analytics needs the attribute detail that
rollups intentionally drop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/apm/usage", tags=["APM usage analytics"])

ENTRY_KINDS = "('SERVER','CONSUMER')"
MAX_HOURS = 168  # raw-span TTL is 7 days

# enduser.id is the OTel semconv attribute; user.id is a common alternative.
USER_EXPR = ("if(attributes_string['enduser.id'] != '', "
             "attributes_string['enduser.id'], attributes_string['user.id'])")


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=min(hours, MAX_HOURS))
            ).strftime("%Y-%m-%d %H:%M:%S")


def _scope(service: Optional[str], env: Optional[str], params: dict) -> str:
    cond = ""
    if service:
        cond += " AND service_name = %(svc)s"
        params["svc"] = service
    if env:
        cond += " AND env = %(env)s"
        params["env"] = env
    return cond


def _bucket_s(hours: int) -> int:
    if hours <= 3:
        return 300
    if hours <= 24:
        return 1800
    return 7200


@router.get("/summary")
async def usage_summary(
    hours: int = Query(24, ge=1, le=MAX_HOURS),
    service: Optional[str] = None,
    env: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    params: dict = {"since": _since(hours), "bucket": _bucket_s(hours)}
    scope = _scope(service, env, params)

    def _fetch() -> dict:
        client = get_ch_client()
        totals = client.query(f"""
            SELECT count()                                   AS requests,
                   uniqIf({USER_EXPR}, {USER_EXPR} != '')    AS users,
                   uniqIf(http_route, http_route != '')      AS pages,
                   uniq(service_name)                        AS services,
                   countIf(has_error = 1)                    AS errors,
                   avg(duration_nano) / 1e6                  AS avg_ms,
                   quantile(0.95)(duration_nano) / 1e6       AS p95_ms
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}{scope}
        """, parameters=params).result_rows[0]

        series = client.query(f"""
            SELECT toStartOfInterval(timestamp, INTERVAL %(bucket)s SECOND) AS t,
                   count()                                AS requests,
                   uniqIf({USER_EXPR}, {USER_EXPR} != '') AS users,
                   countIf(has_error = 1)                 AS errors
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}{scope}
            GROUP BY t ORDER BY t
        """, parameters=params).result_rows

        return {
            "requests": int(totals[0]), "unique_users": int(totals[1]),
            "pages": int(totals[2]), "services": int(totals[3]),
            "errors": int(totals[4]),
            "error_rate": round(int(totals[4]) / int(totals[0]), 5) if totals[0] else 0,
            "avg_ms": round(float(totals[5] or 0), 2),
            "p95_ms": round(float(totals[6] or 0), 2),
            "series": [
                {"t": r[0].isoformat(), "requests": int(r[1]),
                 "users": int(r[2]), "errors": int(r[3])}
                for r in series
            ],
        }

    return await asyncio.to_thread(_fetch)


@router.get("/pages")
async def usage_pages(
    hours: int = Query(24, ge=1, le=MAX_HOURS),
    service: Optional[str] = None,
    env: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    params: dict = {"since": _since(hours), "lim": limit}
    scope = _scope(service, env, params)

    def _fetch() -> list[dict]:
        rows = get_ch_client().query(f"""
            SELECT http_route,
                   anyHeavy(service_name)                    AS service,
                   count()                                   AS hits,
                   uniqIf({USER_EXPR}, {USER_EXPR} != '')    AS users,
                   countIf(has_error = 1)                    AS errors,
                   quantile(0.95)(duration_nano) / 1e6       AS p95_ms,
                   max(timestamp)                            AS last_hit
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}
              AND http_route != ''{scope}
            GROUP BY http_route ORDER BY hits DESC LIMIT %(lim)s
        """, parameters=params).result_rows
        return [
            {"route": r[0], "service": r[1], "hits": int(r[2]), "users": int(r[3]),
             "errors": int(r[4]),
             "error_rate": round(int(r[4]) / int(r[2]), 5) if r[2] else 0,
             "p95_ms": round(float(r[5] or 0), 2),
             "last_hit": r[6].isoformat() if isinstance(r[6], datetime) else None}
            for r in rows
        ]

    return {"pages": await asyncio.to_thread(_fetch)}


@router.get("/operations")
async def usage_operations(
    hours: int = Query(24, ge=1, le=MAX_HOURS),
    service: Optional[str] = None,
    env: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    params: dict = {"since": _since(hours), "lim": limit}
    scope = _scope(service, env, params)

    def _fetch() -> list[dict]:
        rows = get_ch_client().query(f"""
            SELECT service_name, name,
                   count()                                   AS hits,
                   uniqIf({USER_EXPR}, {USER_EXPR} != '')    AS users,
                   countIf(has_error = 1)                    AS errors,
                   quantile(0.95)(duration_nano) / 1e6       AS p95_ms
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}{scope}
            GROUP BY service_name, name ORDER BY hits DESC LIMIT %(lim)s
        """, parameters=params).result_rows
        return [
            {"service": r[0], "operation": r[1], "hits": int(r[2]),
             "users": int(r[3]), "errors": int(r[4]),
             "error_rate": round(int(r[4]) / int(r[2]), 5) if r[2] else 0,
             "p95_ms": round(float(r[5] or 0), 2)}
            for r in rows
        ]

    return {"operations": await asyncio.to_thread(_fetch)}


@router.get("/users")
async def usage_users(
    hours: int = Query(24, ge=1, le=MAX_HOURS),
    service: Optional[str] = None,
    env: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    params: dict = {"since": _since(hours), "lim": limit}
    scope = _scope(service, env, params)

    def _fetch() -> dict:
        client = get_ch_client()
        rows = client.query(f"""
            SELECT {USER_EXPR}                               AS uid,
                   count()                                   AS requests,
                   countIf(has_error = 1)                    AS errors,
                   uniq(service_name)                        AS services,
                   uniqIf(http_route, http_route != '')      AS pages,
                   min(timestamp)                            AS first_seen,
                   max(timestamp)                            AS last_seen
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}
              AND {USER_EXPR} != ''{scope}
            GROUP BY uid ORDER BY requests DESC LIMIT %(lim)s
        """, parameters=params).result_rows
        attributed = client.query(f"""
            SELECT countIf({USER_EXPR} != ''), count()
            FROM zenplus.apm_spans
            WHERE timestamp >= %(since)s AND span_kind_str IN {ENTRY_KINDS}{scope}
        """, parameters=params).result_rows[0]
        return {
            "users": [
                {"user_id": r[0], "requests": int(r[1]), "errors": int(r[2]),
                 "services": int(r[3]), "pages": int(r[4]),
                 "first_seen": r[5].isoformat() if isinstance(r[5], datetime) else None,
                 "last_seen": r[6].isoformat() if isinstance(r[6], datetime) else None}
                for r in rows
            ],
            "attributed_requests": int(attributed[0]),
            "total_requests": int(attributed[1]),
        }

    return await asyncio.to_thread(_fetch)
