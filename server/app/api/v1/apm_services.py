"""APM service registry, RED analytics, and service map (AM-E3).

Reads RED rollups from ClickHouse `apm_span_metrics_5m` / `_1h` (request/error
counts, tdigest latency, apdex buckets) and the dependency graph from the
pre-aggregated `apm_service_graph` (see services/apm_service_graph.py), with a
raw parent/child join only over the few minutes that rollup has not covered yet.
A background loop upserts `apm_services` in Postgres with denormalised last-seen
RED + health.

RED is measured on **entry spans** (SERVER/CONSUMER) — internal/client spans are
not counted as inbound requests.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import get_ch_client, get_db, AsyncSessionLocal
from app.core.security import get_current_user
from app.models.user import User
from app.services.apm_rollup import align_ms_window, bucket_for, table_for

logger = logging.getLogger("zenplus.apm.services")
router = APIRouter(prefix="/apm", tags=["APM services"])

ENTRY_KINDS = "('SERVER','CONSUMER')"


# ── schemas ──────────────────────────────────────────────────────────────────

class ServiceRED(BaseModel):
    name: str
    envs: list[str]
    health: str
    request_count: int
    rps: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    apdex: float


class ServiceListResponse(BaseModel):
    services: list[ServiceRED]
    facets: dict[str, dict[str, int]]
    window_seconds: int


class REDPoint(BaseModel):
    timestamp: datetime
    rps: float
    error_rate: float
    p50_ms: float
    p95_ms: float


class OperationRED(BaseModel):
    operation: str
    request_count: int
    rps: float
    error_rate: float
    p95_ms: float


class MapNode(BaseModel):
    name: str
    health: str
    rps: float
    error_rate: float
    p95_ms: float


class MapEdge(BaseModel):
    client: str
    server: str
    calls: int
    error_rate: float
    #: Mean call latency. The graph rollup stores sum+count (SummingMergeTree),
    #: which cannot reconstruct a percentile — service-level p95 lives on the
    #: node, per-edge latency is an average by construction.
    avg_ms: float


class ServiceMapResponse(BaseModel):
    nodes: list[MapNode]
    edges: list[MapEdge]


# ── helpers ──────────────────────────────────────────────────────────────────

RANGE_MS = {
    "15m": 15 * 60_000, "1h": 3_600_000, "6h": 6 * 3_600_000,
    "24h": 24 * 3_600_000, "7d": 7 * 24 * 3_600_000,
}


def _window(from_ms: Optional[int], to_ms: Optional[int], range_: Optional[str]) -> tuple[int, int]:
    """Requested window, bucket-aligned so no rollup bucket is silently dropped.

    Rollup rows are labelled with their bucket start, so `timestamp >= from` on
    an unaligned boundary excludes the bucket that covers most of the window.
    Only the lower bound moves; see apm_rollup.align_ms_window.
    """
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if from_ms is None or to_ms is None:
        from_ms = now - RANGE_MS.get(range_ or "1h", 3_600_000)
        to_ms = now
    aligned_from, aligned_to, _covered = align_ms_window(from_ms, to_ms)
    return aligned_from, aligned_to


def _covered_seconds(frm: int, to: int) -> float:
    """Seconds the aligned window actually spans — the divisor for req/s."""
    return max((to - frm) / 1000.0, 1.0)


def _health(error_rate: float, p95_ms: float, reqs: int) -> str:
    if reqs == 0:
        return "no_data"
    if error_rate >= 0.05 or p95_ms >= 1000:
        return "critical"
    if error_rate >= 0.01 or p95_ms >= 500:
        return "degraded"
    return "healthy"


def _ch():
    return get_ch_client()


def _query_services(frm: int, to: int, env: Optional[str]) -> list[dict]:
    win_s = _covered_seconds(frm, to)
    params = {"frm": frm, "to": to}
    env_cond = ""
    if env:
        env_cond = "AND env = {env:String}"; params["env"] = env
    sql = f"""
        SELECT
            service_name,
            groupUniqArray(env)                                              AS envs,
            sum(request_count)                                               AS reqs,
            sum(error_count)                                                 AS errs,
            arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),1) AS p50,
            arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),2) AS p95,
            arrayElement(quantilesTDigestMerge(0.5,0.95,0.99)(duration_state),3) AS p99,
            sum(satisfied_count)                                             AS sat,
            sum(tolerating_count)                                            AS tol
        FROM zenplus.{table_for(int(win_s))}
        WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
          AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
          AND span_kind IN {ENTRY_KINDS} {env_cond}
        GROUP BY service_name
        ORDER BY reqs DESC
    """
    out = []
    for r in _ch().query(sql, parameters=params).result_rows:
        reqs, errs = int(r[2]), int(r[3])
        p50, p95, p99 = float(r[4] or 0), float(r[5] or 0), float(r[6] or 0)
        sat, tol = int(r[7]), int(r[8])
        err_rate = (errs / reqs) if reqs else 0.0
        apdex = ((sat + tol / 2) / reqs) if reqs else 0.0
        out.append({
            "name": r[0], "envs": list(r[1]), "request_count": reqs,
            "rps": round(reqs / win_s, 3), "error_rate": round(err_rate, 5),
            "p50_ms": round(p50, 2), "p95_ms": round(p95, 2), "p99_ms": round(p99, 2),
            "apdex": round(apdex, 3), "health": _health(err_rate, p95, reqs),
        })
    return out


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/services", response_model=ServiceListResponse)
async def list_services(
    env: Optional[str] = None,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    frm, to = _window(from_ms, to_ms, range_)
    rows = await asyncio.to_thread(_query_services, frm, to, env)

    def facets():
        sql = f"""
            SELECT env, uniqExact(service_name)
            FROM zenplus.{table_for(int(_covered_seconds(frm, to)))}
            WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind IN {ENTRY_KINDS}
            GROUP BY env
        """
        return {r[0]: int(r[1]) for r in _ch().query(sql, parameters={"frm": frm, "to": to}).result_rows}

    env_facet = await asyncio.to_thread(facets)
    health_facet: dict[str, int] = {}
    for s in rows:
        health_facet[s["health"]] = health_facet.get(s["health"], 0) + 1

    return ServiceListResponse(
        services=[ServiceRED(**s) for s in rows],
        facets={"env": env_facet, "health": health_facet},
        window_seconds=int((to - frm) / 1000),
    )


@router.get("/services/{name}", response_model=ServiceRED)
async def get_service(
    name: str, env: Optional[str] = None,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None, to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    frm, to = _window(from_ms, to_ms, range_)
    rows = await asyncio.to_thread(_query_services, frm, to, env)
    match = next((s for s in rows if s["name"] == name), None)
    if not match:
        # service exists but no entry-span RED in window -> empty shell
        return ServiceRED(name=name, envs=[env] if env else [], health="no_data",
                          request_count=0, rps=0, error_rate=0, p50_ms=0, p95_ms=0,
                          p99_ms=0, apdex=0)
    return ServiceRED(**match)


@router.get("/services/{name}/red", response_model=list[REDPoint])
async def service_red_timeseries(
    name: str, env: Optional[str] = None,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None, to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    frm, to = _window(from_ms, to_ms, range_)
    params = {"frm": frm, "to": to, "svc": name}
    env_cond = ""
    if env:
        env_cond = "AND env = {env:String}"; params["env"] = env
    # Long ranges read the hourly rollup: a 7-day range off the 5-minute table
    # is 2016 points, which is neither chartable nor cheap.
    span_s = _covered_seconds(frm, to)
    table = table_for(int(span_s))
    bucket_s = float(bucket_for(int(span_s)))

    def run():
        sql = f"""
            SELECT timestamp,
                   sum(request_count) AS reqs,
                   sum(error_count)   AS errs,
                   arrayElement(quantilesTDigestMerge(0.5)(duration_state),1)  AS p50,
                   arrayElement(quantilesTDigestMerge(0.95)(duration_state),1) AS p95
            FROM zenplus.{table}
            WHERE service_name = {{svc:String}}
              AND timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind IN {ENTRY_KINDS} {env_cond}
            GROUP BY timestamp ORDER BY timestamp
        """
        pts = []
        for r in _ch().query(sql, parameters=params).result_rows:
            reqs, errs = int(r[1]), int(r[2])
            pts.append(REDPoint(
                timestamp=r[0], rps=round(reqs / bucket_s, 3),
                error_rate=round((errs / reqs) if reqs else 0.0, 5),
                p50_ms=round(float(r[3] or 0), 2), p95_ms=round(float(r[4] or 0), 2),
            ))
        return pts

    return await asyncio.to_thread(run)


@router.get("/services/{name}/operations", response_model=list[OperationRED])
async def service_operations(
    name: str, env: Optional[str] = None,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None, to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    frm, to = _window(from_ms, to_ms, range_)
    win_s = _covered_seconds(frm, to)
    params = {"frm": frm, "to": to, "svc": name}
    env_cond = ""
    if env:
        env_cond = "AND env = {env:String}"; params["env"] = env

    def run():
        sql = f"""
            SELECT operation,
                   sum(request_count) AS reqs, sum(error_count) AS errs,
                   arrayElement(quantilesTDigestMerge(0.95)(duration_state),1) AS p95
            FROM zenplus.{table_for(int(win_s))}
            WHERE service_name = {{svc:String}}
              AND timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
              AND timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
              AND span_kind IN {ENTRY_KINDS} {env_cond}
            GROUP BY operation ORDER BY reqs DESC LIMIT 25
        """
        ops = []
        for r in _ch().query(sql, parameters=params).result_rows:
            reqs, errs = int(r[1]), int(r[2])
            ops.append(OperationRED(
                operation=r[0], request_count=reqs, rps=round(reqs / win_s, 3),
                error_rate=round((errs / reqs) if reqs else 0.0, 5),
                p95_ms=round(float(r[3] or 0), 2),
            ))
        return ops

    return await asyncio.to_thread(run)


@router.get("/service-map", response_model=ServiceMapResponse)
async def service_map(
    env: Optional[str] = None,
    range_: Optional[str] = Query("1h", alias="range"),
    from_ms: Optional[int] = None, to_ms: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    frm, to = _window(from_ms, to_ms, range_)
    nodes = await asyncio.to_thread(_query_services, frm, to, env)

    def edges():
        """Edges from the pre-aggregated graph, plus a live tail.

        `apm_service_graph` is filled per closed 5-minute bucket by
        services/apm_service_graph.py. Everything after its watermark has not
        been aggregated yet, so the last few minutes still come from the raw
        parent/child join — but over a bounded tail, not the whole range, which
        is what made the old map the first query to fall over at volume.
        """
        params = {"frm": frm, "to": to}
        env_cond = "AND env = {env:String}" if env else ""
        if env:
            params["env"] = env

        watermark_row = _ch().query(
            "SELECT toUnixTimestamp64Milli(max(timestamp)) FROM zenplus.apm_service_graph"
        ).result_rows
        watermark = int(watermark_row[0][0] or 0) if watermark_row else 0
        # Aggregated rows are bucket-start stamped and cover BUCKET..BUCKET+5m.
        aggregated_to = min(to, watermark + 300_000) if watermark else frm

        agg: dict[tuple[str, str], list[float]] = {}
        if aggregated_to > frm:
            params["agg_to"] = aggregated_to
            for r in _ch().query(f"""
                SELECT client_service, server_service,
                       sum(request_count), sum(error_count), sum(duration_sum_ms)
                FROM zenplus.apm_service_graph
                WHERE timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
                  AND timestamp <  fromUnixTimestamp64Milli({{agg_to:Int64}}) {env_cond}
                GROUP BY client_service, server_service
            """, parameters=params).result_rows:
                agg[(r[0], r[1])] = [float(r[2] or 0), float(r[3] or 0), float(r[4] or 0)]

        # Live tail: raw join, but only over the un-aggregated window.
        tail_from = max(frm, aggregated_to)
        if tail_from < to:
            tail_params = {"frm": tail_from, "to": to}
            tail_env = ""
            if env:
                tail_env = "AND child.env = {env:String}"; tail_params["env"] = env
            for r in _ch().query(f"""
                SELECT parent.service_name AS client, child.service_name AS server,
                       count(), countIf(child.has_error = 1),
                       sum(child.duration_nano) / 1e6
                FROM zenplus.apm_spans AS child
                INNER JOIN zenplus.apm_spans AS parent
                  ON child.parent_span_id = parent.span_id AND child.trace_id = parent.trace_id
                WHERE child.timestamp >= fromUnixTimestamp64Milli({{frm:Int64}})
                  AND child.timestamp <  fromUnixTimestamp64Milli({{to:Int64}})
                  AND parent.timestamp >= fromUnixTimestamp64Milli({{frm:Int64}}) - 300000
                  AND parent.service_name != child.service_name {tail_env}
                GROUP BY client, server
            """, parameters=tail_params).result_rows:
                slot = agg.setdefault((r[0], r[1]), [0.0, 0.0, 0.0])
                slot[0] += float(r[2] or 0)
                slot[1] += float(r[3] or 0)
                slot[2] += float(r[4] or 0)

        out = []
        for (client, server), (calls, errs, dur_sum) in agg.items():
            n = int(calls)
            if n <= 0:
                continue
            out.append(MapEdge(
                client=client, server=server, calls=n,
                error_rate=round(errs / n, 5),
                avg_ms=round(dur_sum / n, 2),
            ))
        out.sort(key=lambda e: e.calls, reverse=True)
        return out

    edge_list = await asyncio.to_thread(edges)
    node_names = {n["name"] for n in nodes}
    # include client/server-only services that have no entry-span RED as bare nodes
    for e in edge_list:
        for svc in (e.client, e.server):
            if svc not in node_names:
                node_names.add(svc)
                nodes.append({"name": svc, "health": "no_data", "rps": 0,
                              "error_rate": 0, "p95_ms": 0})
    return ServiceMapResponse(
        nodes=[MapNode(name=n["name"], health=n["health"], rps=n.get("rps", 0),
                       error_rate=n.get("error_rate", 0), p95_ms=n.get("p95_ms", 0)) for n in nodes],
        edges=edge_list,
    )


# ── background: service registry upsert + health denormalisation ─────────────

# A registered service silent for this long is DOWN from APM's perspective:
# the health freeze at last-known-good was audit finding #1 (a dead service
# looked healthy forever). 10 minutes = two rollup buckets of grace.
NO_DATA_AFTER_S = 600


async def _decay_silent_services(db) -> int:
    """Flip services that stopped reporting to no_data (once, idempotent)."""
    res = await db.execute(text("""
        UPDATE apm_services
        SET health = 'no_data', last_rps = 0, updated_at = NOW()
        WHERE health != 'no_data'
          AND last_seen_at IS NOT NULL
          AND last_seen_at < NOW() - make_interval(secs => :cutoff)
    """), {"cutoff": NO_DATA_AFTER_S})
    return res.rowcount or 0


async def apm_service_registry_loop(interval_s: int = 60):
    """Upsert apm_services from recent entry-span RED; denormalise health."""
    await asyncio.sleep(15)
    while True:
        try:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            rows = await asyncio.to_thread(_query_services, now - 10 * 60_000, now, None)
            async with AsyncSessionLocal() as db:
                decayed = await _decay_silent_services(db)
                if decayed:
                    logger.warning("apm registry: %d service(s) stopped reporting -> no_data", decayed)
                await db.commit()
            if rows:
                async with AsyncSessionLocal() as db:
                    # env name -> id map
                    env_map = {r[0]: r[1] for r in (await db.execute(
                        text("SELECT name, id FROM apm_environments"))).all()}
                    for s in rows:
                        env_name = s["envs"][0] if s["envs"] else None
                        env_id = env_map.get(env_name)
                        if env_id is None:
                            # ON CONFLICT (name, env_id) can't dedupe NULL env_id
                            # (NULLS DISTINCT); skip unknown-env services here —
                            # they still show in the live RED list/map from CH.
                            continue
                        await db.execute(text("""
                            INSERT INTO apm_services
                                (name, env_id, health, last_seen_at, last_rps,
                                 last_error_rate, last_p95_ms, last_apdex, updated_at)
                            VALUES (:n, :e, :h, NOW(), :rps, :er, :p95, :ap, NOW())
                            ON CONFLICT (name, env_id) DO UPDATE SET
                                health = EXCLUDED.health,
                                last_seen_at = NOW(),
                                last_rps = EXCLUDED.last_rps,
                                last_error_rate = EXCLUDED.last_error_rate,
                                last_p95_ms = EXCLUDED.last_p95_ms,
                                last_apdex = EXCLUDED.last_apdex,
                                updated_at = NOW()
                        """), {"n": s["name"], "e": env_id, "h": s["health"],
                               "rps": s["rps"], "er": s["error_rate"],
                               "p95": s["p95_ms"], "ap": s["apdex"]})
                    await db.commit()
        except Exception:
            logger.exception("apm service registry loop error")
        await asyncio.sleep(interval_s)
