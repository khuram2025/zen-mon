"""Admin servers / agent-fleet / agent-policies API.

Dashboard-facing CRUD for monitored servers, the agents installed on them,
the policies that drive collection, and the install-token flow.

All routes require an authenticated dashboard user (JWT bearer).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.services.filesystem_monitoring import pg_capacity_filter
from app.models.user import User
from app.schemas.agent import (
    AgentBulkAction,
    AgentPackageDownloadRequest,
    AgentPolicyCreate,
    AgentPolicyResponse,
    AgentPolicyUpdate,
    AgentResponse,
    BaselineCreate,
    BaselineResponse,
    BaselineRuleResponse,
    BaselineUpdate,
    InstallTokenCreate,
    InstallTokenResponse,
    MetricPoint,
    MetricSeries,
    ServerBulkAction,
    ServerCreate,
    ServerMetricsResponse,
    ServerResponse,
    ServerUpdate,
)
from app.services.host_metric_service import (
    query_fleet_latest_metrics,
    query_process_history,
    query_server_memory_total,
    query_server_metrics,
    query_top_pressure,
)

router = APIRouter(prefix="/servers", tags=["Servers"])
policies_router = APIRouter(prefix="/agent-policies", tags=["Agent Policies"])
fleet_router = APIRouter(prefix="/agent-fleet", tags=["Agent Fleet"])
overview_router = APIRouter(prefix="/server-monitoring", tags=["Server Monitoring"])
baselines_router = APIRouter(prefix="/server-baselines", tags=["Software Baselines"])

logger = logging.getLogger("zenplus.servers")

TOKEN_PREFIX = "zpa_enr_"
DEFAULT_TOKEN_TTL_HOURS = 24


# ── Helpers ─────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_enrollment_token() -> tuple[str, str, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(24)
    return raw, _sha256(raw), raw[:12]


async def _server_url(request: Request, db: AsyncSession) -> str:
    """Base URL that *target servers* must be able to reach.

    Resolution order: APP_BASE_URL from .env, then the company system-setting
    ``base_url``, then the request's Host header. The header fallback is wrong
    behind dev proxies (it yields localhost), so deployments should set one of
    the first two.
    """
    base = (get_settings().APP_BASE_URL or "").strip()
    if not base:
        row = (await db.execute(
            text("SELECT value->>'base_url' FROM system_settings WHERE key = 'company'"),
        )).first()
        base = (row[0] or "").strip() if row and row[0] else ""
    if base:
        return base.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _json_list(v: Any) -> list:
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return list(parsed) if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _server_row_to_response(row: dict) -> ServerResponse:
    tags = _json_list(row.get("tags"))
    return ServerResponse(
        id=str(row["id"]),
        display_name=row["display_name"],
        boot_time=row.get("boot_time"),
        hostname=row.get("hostname"),
        fqdn=row.get("fqdn"),
        primary_ip=str(row["primary_ip"]) if row.get("primary_ip") else None,
        site_id=str(row["site_id"]) if row.get("site_id") else None,
        site_name=row.get("site_name"),
        device_id=str(row["device_id"]) if row.get("device_id") else None,
        os_type=row.get("os_type") or "unknown",
        os_name=row.get("os_name"),
        os_version=row.get("os_version"),
        kernel_or_build=row.get("kernel_or_build"),
        architecture=row.get("architecture"),
        collection_mode=row.get("collection_mode") or "agent",
        status=row.get("status") or "unknown",
        environment=row.get("environment"),
        owner=row.get("owner"),
        tags=list(tags) if isinstance(tags, (list, tuple)) else [],
        windows_credential_id=str(row["windows_credential_id"]) if row.get("windows_credential_id") else None,
        snmp_credential_id=str(row["snmp_credential_id"]) if row.get("snmp_credential_id") else None,
        ncm_credential_id=str(row["ncm_credential_id"]) if row.get("ncm_credential_id") else None,
        last_seen=row.get("last_seen"),
        description=row.get("description"),
        status_reasons=[str(r) for r in _json_list(row.get("status_reasons"))],
        agent_id=str(row["agent_id"]) if row.get("agent_id") else None,
        agent_status=row.get("agent_status"),
        agent_version=row.get("agent_version"),
        agent_last_heartbeat_at=row.get("agent_last_heartbeat_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _policy_row(row: dict) -> AgentPolicyResponse:
    def _arr(field: str) -> list:
        v = row.get(field)
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                return list(json.loads(v))
            except Exception:
                return []
        return []

    def _obj(field: str) -> dict:
        v = row.get(field)
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return dict(json.loads(v))
            except Exception:
                return {}
        return {}

    return AgentPolicyResponse(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        platform=row["platform"],
        metric_interval_s=row["metric_interval_s"],
        upload_interval_s=row["upload_interval_s"],
        process_top_n=row["process_top_n"],
        service_watchlist=_arr("service_watchlist"),
        process_watchlist=_arr("process_watchlist"),
        event_log_filters=_arr("event_log_filters"),
        disk_ignore=_arr("disk_ignore"),
        network_ignore=_arr("network_ignore"),
        cardinality_limits=_obj("cardinality_limits"),
        update_ring=row["update_ring"],
        feature_flags=_obj("feature_flags"),
        config_version=row["config_version"],
        is_builtin=bool(row["is_builtin"]),
        agent_count=row.get("agent_count") or 0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Servers CRUD ────────────────────────────────────────────────────

@router.get("")
async def list_servers(
    site_id: Optional[UUID] = None,
    os_type: Optional[str] = None,
    collection_mode: Optional[str] = None,
    status: Optional[str] = None,
    environment: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "display_name",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    offset = (page - 1) * page_size

    sort_map = {
        "display_name": "s.display_name",
        "hostname": "s.hostname",
        "status": "s.status",
        "os_type": "s.os_type",
        "environment": "s.environment",
        "last_seen": "s.last_seen",
        "created_at": "s.created_at",
    }
    sort_col = sort_map.get(sort, "s.display_name")
    sort_dir = "DESC" if order.lower() == "desc" else "ASC"

    where = ["1=1"]
    params: dict[str, Any] = {"limit": page_size, "offset": offset}
    if site_id:
        where.append("s.site_id = :site"); params["site"] = site_id
    if os_type:
        where.append("s.os_type = :os"); params["os"] = os_type
    if collection_mode:
        where.append("s.collection_mode = :cm"); params["cm"] = collection_mode
    if status:
        where.append("s.status = :st"); params["st"] = status
    if environment:
        where.append("s.environment = :env"); params["env"] = environment
    if tag:
        # Repeatable filter: ?tag=prod (servers whose tags contain "prod")
        where.append("s.tags @> CAST(:tag_json AS jsonb)")
        params["tag_json"] = json.dumps([tag])
    if q:
        where.append("""(s.display_name ILIKE :q OR s.hostname ILIKE :q OR s.fqdn ILIKE :q
                         OR host(s.primary_ip)::text ILIKE :q OR s.owner ILIKE :q
                         OR s.tags::text ILIKE :q)""")
        params["q"] = f"%{q}%"

    where_sql = " AND ".join(where)
    sql_total = f"SELECT COUNT(*) FROM servers s WHERE {where_sql}"
    total_row = (await db.execute(text(sql_total), params)).first()
    total = total_row[0] if total_row else 0

    sql = f"""
        SELECT s.*, st.name AS site_name,
               a.id AS agent_id, a.status AS agent_status, a.version AS agent_version,
               a.last_heartbeat_at AS agent_last_heartbeat_at
        FROM servers s
        LEFT JOIN sites st ON st.id = s.site_id
        LEFT JOIN LATERAL (
            SELECT id, status, version, last_heartbeat_at FROM agents
            WHERE server_id = s.id
            ORDER BY last_heartbeat_at DESC NULLS LAST LIMIT 1
        ) a ON TRUE
        WHERE {where_sql}
        ORDER BY {sort_col} {sort_dir} NULLS LAST
        LIMIT :limit OFFSET :offset
    """
    rows = (await db.execute(text(sql), params)).mappings().all()

    return {
        "items": [_server_row_to_response(dict(r)) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/facets")
async def server_facets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Distinct filter values + counts for the inventory list sidebar."""
    async def _group(col: str) -> list[dict]:
        rows = (await db.execute(text(
            f"""SELECT {col} AS value, COUNT(*) AS count FROM servers
                WHERE {col} IS NOT NULL AND {col}::text != ''
                GROUP BY {col} ORDER BY count DESC, value"""
        ))).all()
        return [{"value": r[0], "count": int(r[1])} for r in rows]

    tags = (await db.execute(text(
        """SELECT t.tag AS value, COUNT(*) AS count
           FROM servers s, jsonb_array_elements_text(s.tags) AS t(tag)
           GROUP BY t.tag ORDER BY count DESC, value LIMIT 100"""
    ))).all()

    sites = (await db.execute(text(
        """SELECT st.id, st.name, COUNT(*) AS count
           FROM servers s JOIN sites st ON st.id = s.site_id
           GROUP BY st.id, st.name ORDER BY count DESC"""
    ))).all()

    return {
        "status": await _group("status"),
        "os_type": await _group("os_type"),
        "collection_mode": await _group("collection_mode"),
        "environment": await _group("environment"),
        "tags": [{"value": r[0], "count": int(r[1])} for r in tags],
        "sites": [{"id": str(r[0]), "name": r[1], "count": int(r[2])} for r in sites],
    }


@router.get("/latest-metrics")
async def servers_latest_metrics(
    window_minutes: int = Query(10, ge=1, le=120),
    user: User = Depends(get_current_user),
):
    """Current cpu/mem/disk/net per server (keyed by server id) for the list view."""
    return {"servers": query_fleet_latest_metrics(window_minutes)}


@router.post("/bulk")
async def bulk_server_action(
    data: ServerBulkAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if not data.server_ids:
        raise HTTPException(400, "server_ids required")
    ids = [str(s) for s in data.server_ids]
    affected = 0

    if data.action in ("add_tags", "remove_tags"):
        if not data.tags:
            raise HTTPException(400, "tags required for tag actions")
        rows = (await db.execute(
            text("SELECT id, tags FROM servers WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )).all()
        for sid, tags in rows:
            current = _json_list(tags)
            if data.action == "add_tags":
                merged = current + [t for t in data.tags if t not in current]
            else:
                merged = [t for t in current if t not in data.tags]
            if merged != current:
                await db.execute(
                    text("UPDATE servers SET tags = CAST(:tags AS jsonb), updated_at = NOW() WHERE id = :id"),
                    {"tags": json.dumps(merged), "id": sid},
                )
                affected += 1
    elif data.action == "set_environment":
        res = await db.execute(
            text("UPDATE servers SET environment = :env, updated_at = NOW() WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"env": data.environment, "ids": ids},
        )
        affected = res.rowcount or 0
    elif data.action == "decommission":
        res = await db.execute(
            text("UPDATE servers SET status = 'disabled', updated_at = NOW() WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        await db.execute(
            text("UPDATE agents SET status = 'disabled', updated_at = NOW() WHERE server_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        affected = res.rowcount or 0
    elif data.action == "delete":
        await db.execute(
            text("DELETE FROM agents WHERE server_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        res = await db.execute(
            text("DELETE FROM servers WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        affected = res.rowcount or 0

    await db.commit()

    # Tag changes can move servers in/out of tag-scoped baselines.
    if data.action in ("add_tags", "remove_tags"):
        from app.services.baseline_service import evaluate_server
        for sid in data.server_ids:
            try:
                await evaluate_server(db, str(sid), commit=False)
            except Exception:
                logger.exception("baseline re-eval failed for %s", sid)
        await db.commit()

    return {"ok": True, "affected": affected}


@router.post("", response_model=ServerResponse)
async def create_server(
    data: ServerCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("""INSERT INTO servers (display_name, hostname, fqdn, primary_ip, site_id, device_id,
                                      os_type, collection_mode, environment, owner, description,
                                      tags, windows_credential_id, snmp_credential_id, ncm_credential_id, created_by)
                VALUES (:dn, :hn, :fqdn, :ip, :site, :dev,
                        :os, :cm, :env, :own, :desc,
                        COALESCE(:tags, '[]'::jsonb), :wcred, :scred, :ncred, :cb)
                RETURNING *"""),
        {
            "dn": data.display_name, "hn": data.hostname, "fqdn": data.fqdn,
            "ip": data.primary_ip, "site": data.site_id, "dev": data.device_id,
            "os": data.os_type, "cm": data.collection_mode,
            "env": data.environment, "own": data.owner, "desc": data.description,
            "tags": json.dumps(data.tags or []),
            "wcred": data.windows_credential_id,
            "scred": data.snmp_credential_id,
            "ncred": data.ncm_credential_id,
            "cb": user.id,
        },
    )).mappings().first()
    await db.commit()
    return _server_row_to_response(dict(row))


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""SELECT s.*, st.name AS site_name,
                       a.id AS agent_id, a.status AS agent_status, a.version AS agent_version,
                       a.last_heartbeat_at AS agent_last_heartbeat_at,
                       a.last_metric_at AS agent_last_metric_at,
                       a.clock_skew_s AS agent_clock_skew_s
                FROM servers s
                LEFT JOIN sites st ON st.id = s.site_id
                LEFT JOIN LATERAL (
                    SELECT id, status, version, last_heartbeat_at, last_metric_at,
                           clock_skew_s
                    FROM agents
                    WHERE server_id = s.id
                    ORDER BY last_heartbeat_at DESC NULLS LAST LIMIT 1
                ) a ON TRUE
                WHERE s.id = :id"""),
        {"id": server_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Server not found")
    return _server_row_to_response(dict(row))


@router.patch("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: UUID,
    data: ServerUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    sets = []
    params: dict[str, Any] = {"id": server_id}
    for field in ["display_name", "hostname", "fqdn", "primary_ip", "site_id", "device_id",
                  "os_type", "collection_mode", "status", "environment", "owner", "description",
                  "windows_credential_id", "snmp_credential_id", "ncm_credential_id"]:
        v = getattr(data, field)
        if v is not None:
            sets.append(f"{field} = :{field}")
            params[field] = v
    if data.tags is not None:
        sets.append("tags = :tags")
        params["tags"] = json.dumps(data.tags)
    if not sets:
        return await get_server(server_id, db, user)
    sql = f"UPDATE servers SET {', '.join(sets)}, updated_at = NOW() WHERE id = :id RETURNING *"
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Server not found")
    await db.commit()
    return await get_server(server_id, db, user)


@router.delete("/{server_id}")
async def delete_server(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    # Remove the server's agents first: the FK is ON DELETE SET NULL, which
    # would leave an orphan agent authenticating forever with no server
    # binding (every upload 400s). Deleting the row lets the host re-enroll.
    await db.execute(text("DELETE FROM agents WHERE server_id = :id"), {"id": server_id})
    res = await db.execute(text("DELETE FROM servers WHERE id = :id"), {"id": server_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Server not found")
    return {"ok": True}


@router.post("/{server_id}/decommission", response_model=ServerResponse)
async def decommission_server(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    await db.execute(
        text("UPDATE servers SET status = 'disabled', updated_at = NOW() WHERE id = :id"),
        {"id": server_id},
    )
    await db.execute(
        text("UPDATE agents SET status = 'disabled', updated_at = NOW() WHERE server_id = :id"),
        {"id": server_id},
    )
    await db.commit()
    return await get_server(server_id, db, user)


# ── Server detail companions ────────────────────────────────────────

@router.get("/{server_id}/metrics", response_model=ServerMetricsResponse)
async def server_metrics(
    server_id: UUID,
    metrics: str = Query("cpu_total_pct,memory_used_pct,network_rx_bps,network_tx_bps"),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    interval_s: int = 60,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not to:
        to = datetime.now(timezone.utc)
    if not from_:
        from_ = to - timedelta(hours=6)
    metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
    series = query_server_metrics(str(server_id), from_, to, metric_list)
    return ServerMetricsResponse(
        server_id=str(server_id),
        **{"from": from_},
        to=to,
        interval_s=interval_s,
        series=series,
    )


@router.get("/{server_id}/processes")
async def server_processes(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Only return processes refreshed recently so exited processes don't show
    # as live. Window matches PROCESS_STALE_SECONDS in host_metric_service.py.
    rows = (await db.execute(
        text("""SELECT pid, name, cmdline, user_name, cpu_pct, memory_bytes, started_at, updated_at
                FROM server_process_inventory
                WHERE server_id = :id
                  AND updated_at >= NOW() - make_interval(secs => :ttl)
                ORDER BY cpu_pct DESC NULLS LAST LIMIT 200"""),
        {"id": server_id, "ttl": 300},
    )).mappings().all()
    return {
        "items": [dict(r) for r in rows],
        "mem_total_bytes": query_server_memory_total(str(server_id)),
    }


@router.get("/{server_id}/processes/history", response_model=ServerMetricsResponse)
async def server_process_history(
    server_id: UUID,
    name: str = Query(..., min_length=1, max_length=255),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = None,
    interval_s: int = 60,
    user: User = Depends(get_current_user),
):
    """CPU/memory trend for all processes sharing a name (aggregated, PID-agnostic)."""
    if not to:
        to = datetime.now(timezone.utc)
    if not from_:
        from_ = to - timedelta(hours=6)
    series = query_process_history(str(server_id), name, from_, to)
    return ServerMetricsResponse(
        server_id=str(server_id),
        **{"from": from_},
        to=to,
        interval_s=interval_s,
        series=series,
    )


@router.get("/{server_id}/services")
async def server_services(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""SELECT service_name, display_name, start_mode, state, pid, description,
                       updated_at,
                       updated_at < NOW() - INTERVAL '15 minutes' AS is_stale
                FROM server_service_inventory
                WHERE server_id = :id
                ORDER BY service_name"""),
        {"id": server_id},
    )).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/{server_id}/filesystems")
async def server_filesystems(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text(f"""SELECT mount, fs_type, device, total_bytes, used_bytes, free_bytes,
                        used_pct, updated_at,
                        updated_at < NOW() - INTERVAL '15 minutes' AS is_stale
                 FROM server_filesystem_inventory
                 WHERE server_id = :id
                   AND {pg_capacity_filter()}
                 ORDER BY mount"""),
        {"id": server_id},
    )).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/{server_id}/network")
async def server_network(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""SELECT if_name, mac_address, ip_addresses, speed_mbps, is_up, mtu, updated_at
                FROM server_network_interface_inventory
                WHERE server_id = :id
                ORDER BY if_name"""),
        {"id": server_id},
    )).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/{server_id}/events")
async def server_events(
    server_id: UUID,
    limit: int = Query(200, ge=1, le=1000),
    hours: int = Query(24, ge=1, le=720),
    level: Optional[str] = Query(None, regex="^(critical|error|warning|information|verbose)$"),
    include_empty: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return Event Log summary aggregated from ClickHouse."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    try:
        conditions = ["server_id = %(sid)s", "timestamp >= now() - INTERVAL %(hrs)s HOUR"]
        params: dict[str, Any] = {"sid": str(server_id), "lim": limit, "hrs": hours}
        if not include_empty:
            conditions.append("event_count > 0")
        if level:
            conditions.append("level = %(lvl)s")
            params["lvl"] = level
        res = client.query(
            f"""SELECT timestamp, log_name, level, event_count
                FROM zenplus.host_event_log_summary
                WHERE {' AND '.join(conditions)}
                ORDER BY timestamp DESC LIMIT %(lim)s""",
            parameters=params,
        ).result_rows
        items = [{"timestamp": r[0], "log_name": r[1], "level": r[2], "count": int(r[3])} for r in res]
        # Which channels the agent actually checked, so "nothing to report"
        # can be distinguished from "nothing was collected".
        checked = client.query(
            """SELECT DISTINCT log_name FROM zenplus.host_event_log_summary
               WHERE server_id = %(sid)s AND timestamp >= now() - INTERVAL %(hrs)s HOUR""",
            parameters={"sid": str(server_id), "hrs": hours},
        ).result_rows
    except Exception as exc:
        logger.warning("event log query failed: %s", exc)
        # A store outage must not render as a clean host.
        raise HTTPException(503, f"Event log store unavailable: {exc}")
    return {"items": items, "channels": sorted(r[0] for r in checked),
            "hours": hours, "truncated": len(items) >= limit}


@router.get("/{server_id}/software")
async def server_software(
    server_id: UUID,
    limit: int = Query(5000, ge=1, le=20000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""SELECT package_name, version, vendor, install_date, updated_at
                FROM server_software_inventory
                WHERE server_id = :id
                ORDER BY lower(package_name)
                LIMIT :lim"""),
        {"id": server_id, "lim": limit},
    )).mappings().all()
    total = (await db.execute(
        text("SELECT COUNT(*) FROM server_software_inventory WHERE server_id = :id"),
        {"id": server_id},
    )).scalar_one()
    return {"items": [dict(r) for r in rows], "total": int(total),
            "truncated": int(total) > len(rows)}


@router.get("/{server_id}/compliance")
async def server_compliance(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Baseline evaluation results for one server, violations first."""
    rows = (await db.execute(
        text("""SELECT res.rule_id, res.baseline_id, res.status, res.found_package,
                       res.found_version, res.expected, res.severity,
                       res.first_failed_at, res.evaluated_at,
                       b.name AS baseline_name,
                       br.rule_type, br.package_match, br.match_type, br.min_version, br.notes
                FROM server_baseline_results res
                JOIN software_baselines b ON b.id = res.baseline_id
                JOIN software_baseline_rules br ON br.id = res.rule_id
                WHERE res.server_id = :sid
                ORDER BY (res.status = 'compliant'), b.name, br.package_match"""),
        {"sid": server_id},
    )).mappings().all()
    items = [dict(r) for r in rows]
    summary = {"total": len(items), "compliant": 0, "missing": 0, "outdated": 0, "prohibited": 0}
    for it in items:
        summary[it["status"]] = summary.get(it["status"], 0) + 1
    return {"items": items, "summary": summary}


@router.post("/{server_id}/evaluate-baselines")
async def server_evaluate_baselines(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    from app.services.baseline_service import evaluate_server
    summary = await evaluate_server(db, str(server_id))
    return {"ok": True, **summary}


@router.get("/{server_id}/commands")
async def server_commands(
    server_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Command history across this server's agents (for the Agent tab)."""
    rows = (await db.execute(
        text("""SELECT c.id, c.command, c.params, c.status, c.created_at, c.sent_at,
                       c.completed_at, c.expires_at,
                       r.success, r.error_message,
                       u.username AS requested_by_name
                FROM agent_commands c
                JOIN agents a ON a.id = c.agent_id
                LEFT JOIN agent_command_results r ON r.command_id = c.id
                LEFT JOIN users u ON u.id = c.requested_by
                WHERE a.server_id = :sid
                ORDER BY c.created_at DESC
                LIMIT :lim"""),
        {"sid": server_id, "lim": limit},
    )).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/{server_id}/agent")
async def server_agent(
    server_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""SELECT a.*, p.name AS policy_name, st.name AS site_name
                FROM agents a
                LEFT JOIN agent_policies p ON p.id = a.policy_id
                LEFT JOIN sites st ON st.id = a.site_id
                WHERE a.server_id = :sid
                ORDER BY a.last_heartbeat_at DESC NULLS LAST LIMIT 1"""),
        {"sid": server_id},
    )).mappings().first()
    if not row:
        return None
    return _agent_response(dict(row))


def _agent_response(row: dict) -> AgentResponse:
    tags = row.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    return AgentResponse(
        id=str(row["id"]),
        server_id=str(row["server_id"]) if row.get("server_id") else None,
        server_name=row.get("server_name"),
        site_id=str(row["site_id"]) if row.get("site_id") else None,
        site_name=row.get("site_name"),
        agent_uid=row["agent_uid"],
        hostname=row.get("hostname"),
        platform=row["platform"],
        version=row.get("version"),
        status=row["status"],
        api_key_prefix=row.get("api_key_prefix"),
        last_heartbeat_at=row.get("last_heartbeat_at"),
        last_metric_at=row.get("last_metric_at"),
        last_config_hash=row.get("last_config_hash"),
        queue_depth=row.get("queue_depth") or 0,
        spool_bytes=row.get("spool_bytes") or 0,
        clock_skew_s=row.get("clock_skew_s") or 0,
        update_ring=row["update_ring"],
        desired_version=row.get("desired_version"),
        current_version=row.get("current_version"),
        certificate_expires_at=row.get("certificate_expires_at"),
        last_ip=str(row["last_ip"]) if row.get("last_ip") else None,
        policy_id=str(row["policy_id"]) if row.get("policy_id") else None,
        policy_name=row.get("policy_name"),
        config_apply_error=row.get("config_apply_error"),
        tags=list(tags) if isinstance(tags, (list, tuple)) else [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Install token flow ──────────────────────────────────────────────

@router.post("/{server_id}/install-token", response_model=InstallTokenResponse)
async def server_install_token(
    server_id: UUID,
    request: Request,
    data: Optional[InstallTokenCreate] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    server_row = (await db.execute(
        text("SELECT site_id, hostname FROM servers WHERE id = :id"),
        {"id": server_id},
    )).first()
    if not server_row:
        raise HTTPException(404, "Server not found")

    raw, hashed, prefix = _new_enrollment_token()
    ttl_hours = (data.ttl_hours if data else DEFAULT_TOKEN_TTL_HOURS)
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    site_id = (data.site_id if data else None) or server_row[0]
    policy_id = (data.policy_id if data else None)
    platform = (data.platform if data else "windows")
    max_uses = (data.max_uses if data else 1)
    tags_json = json.dumps((data.tags if data else []) or [])

    row = (await db.execute(
        text("""INSERT INTO agent_enrollment_tokens
                  (token_hash, token_prefix, platform, site_id, policy_id, server_id,
                   hostname_hint, tags, expires_at, max_uses, created_by)
                VALUES (:h, :p, :pl, :site, :pol, :sid, :hint, :tags, :exp, :mu, :cb)
                RETURNING id, expires_at"""),
        {
            "h": hashed, "p": prefix, "pl": platform,
            "site": site_id, "pol": policy_id, "sid": server_id,
            "hint": (data.hostname_hint if data else server_row[1]),
            "tags": tags_json, "exp": expires, "mu": max_uses, "cb": user.id,
        },
    )).first()
    await db.commit()

    server_url = await _server_url(request, db)
    msi_url = await _msi_download_url(platform, db, server_url)
    install_cmd = _msi_install_command(server_url, raw, platform)

    return InstallTokenResponse(
        token_id=str(row[0]),
        enrollment_token=raw,
        token_prefix=prefix,
        expires_at=row[1],
        max_uses=max_uses,
        server_url=server_url,
        platform=platform,
        site_id=str(site_id) if site_id else None,
        policy_id=str(policy_id) if policy_id else None,
        install_command=install_cmd,
        msi_download_url=msi_url,
    )


@router.post("/install-token", response_model=InstallTokenResponse)
async def standalone_install_token(
    request: Request,
    data: InstallTokenCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Generate an install token not bound to an existing server.

    The enrolling agent's hostname becomes the new server's display name.
    """
    raw, hashed, prefix = _new_enrollment_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=data.ttl_hours)
    tags_json = json.dumps(data.tags or [])

    row = (await db.execute(
        text("""INSERT INTO agent_enrollment_tokens
                  (token_hash, token_prefix, platform, site_id, policy_id,
                   hostname_hint, tags, expires_at, max_uses, created_by)
                VALUES (:h, :p, :pl, :site, :pol, :hint, :tags, :exp, :mu, :cb)
                RETURNING id, expires_at"""),
        {
            "h": hashed, "p": prefix, "pl": data.platform,
            "site": data.site_id, "pol": data.policy_id,
            "hint": data.hostname_hint, "tags": tags_json,
            "exp": expires, "mu": data.max_uses, "cb": user.id,
        },
    )).first()
    await db.commit()

    server_url = await _server_url(request, db)
    msi_url = await _msi_download_url(data.platform, db, server_url)
    install_cmd = _msi_install_command(server_url, raw, data.platform)

    return InstallTokenResponse(
        token_id=str(row[0]),
        enrollment_token=raw,
        token_prefix=prefix,
        expires_at=row[1],
        max_uses=data.max_uses,
        server_url=server_url,
        platform=data.platform,
        site_id=str(data.site_id) if data.site_id else None,
        policy_id=str(data.policy_id) if data.policy_id else None,
        install_command=install_cmd,
        msi_download_url=msi_url,
    )


async def _msi_download_url(platform: str, db: AsyncSession, server_url: str) -> Optional[str]:
    row = (await db.execute(
        text("""SELECT download_path FROM agent_packages
                WHERE platform = :p AND is_latest = TRUE
                ORDER BY released_at DESC LIMIT 1"""),
        {"p": platform},
    )).first()
    if not row:
        return None
    path = row[0]
    if path.startswith("http"):
        return path
    return f"{server_url}{path}"


def _msi_install_command(server_url: str, token: str, platform: str) -> str:
    if platform == "windows":
        # Elevated PowerShell one-liner: fetch the installer script, which
        # downloads the MSI, verifies its SHA-256 against the manifest, and
        # installs silently with enrollment properties.
        ps = (
            f"$s=Join-Path $env:TEMP 'zenplus-agent-install.ps1'; "
            f"Invoke-WebRequest -UseBasicParsing '{server_url}/api/v1/agents/install.ps1' -OutFile $s; "
            f"& $s -ControllerUrl '{server_url}' -EnrollmentToken '{token}'"
        )
        return f"powershell -NoProfile -ExecutionPolicy Bypass -Command \"{ps}\""
    elif platform == "linux":
        install_url = shlex.quote(f"{server_url}/api/v1/agents/install.sh")
        return (
            f"curl -fsSL {install_url} | sudo env "
            f"ZENPLUS_CONTROLLER_URL={shlex.quote(server_url)} "
            f"ZENPLUS_ENROLLMENT_TOKEN={shlex.quote(token)} "
            "bash"
        )
    return (f"# No installer is published for platform={platform} yet — "
            "register the server and install the agent manually")


# ── Enrollment token lifecycle ──────────────────────────────────────

@router.get("/enrollment-tokens/list")
async def list_enrollment_tokens(
    include_expired: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recent enrollment tokens (prefix only — raw tokens are never stored)."""
    where = "1=1" if include_expired else "(t.expires_at > NOW() AND t.revoked_at IS NULL AND t.uses < t.max_uses)"
    rows = (await db.execute(
        text(f"""SELECT t.id, t.token_prefix, t.platform, t.hostname_hint, t.tags,
                        t.expires_at, t.max_uses, t.uses, t.consumed_at, t.consumed_ip,
                        t.revoked_at, t.created_at, t.server_id, t.policy_id,
                        u.username AS created_by_name, p.name AS policy_name,
                        s.display_name AS server_name
                 FROM agent_enrollment_tokens t
                 LEFT JOIN users u ON u.id = t.created_by
                 LEFT JOIN agent_policies p ON p.id = t.policy_id
                 LEFT JOIN servers s ON s.id = t.server_id
                 WHERE {where}
                 ORDER BY t.created_at DESC LIMIT :lim"""),
        {"lim": limit},
    )).mappings().all()
    return {"items": [{
        "id": str(r["id"]),
        "token_prefix": r["token_prefix"],
        "platform": r["platform"],
        "hostname_hint": r["hostname_hint"],
        "tags": _json_list(r["tags"]),
        "expires_at": r["expires_at"],
        "max_uses": r["max_uses"],
        "uses": r["uses"],
        "consumed_at": r["consumed_at"],
        "consumed_ip": str(r["consumed_ip"]) if r["consumed_ip"] else None,
        "revoked_at": r["revoked_at"],
        "created_at": r["created_at"],
        "created_by_name": r["created_by_name"],
        "policy_id": str(r["policy_id"]) if r["policy_id"] else None,
        "policy_name": r["policy_name"],
        "server_id": str(r["server_id"]) if r["server_id"] else None,
        "server_name": r["server_name"],
    } for r in rows]}


@router.post("/enrollment-tokens/{token_id}/revoke")
async def revoke_enrollment_token(
    token_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(
        text("""UPDATE agent_enrollment_tokens SET revoked_at = NOW()
                WHERE id = :id AND revoked_at IS NULL"""),
        {"id": token_id},
    )
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Token not found or already revoked")
    return {"ok": True}


# ── Agent policies CRUD ─────────────────────────────────────────────

@policies_router.get("")
async def list_policies(
    platform: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    params: dict[str, Any] = {}
    where = "1=1"
    if platform:
        where = "p.platform = :p"
        params["p"] = platform
    rows = (await db.execute(
        text(f"""SELECT p.*, COALESCE(a.cnt, 0) AS agent_count
                 FROM agent_policies p
                 LEFT JOIN (
                     SELECT policy_id, COUNT(*) AS cnt FROM agents
                     WHERE policy_id IS NOT NULL GROUP BY policy_id
                 ) a ON a.policy_id = p.id
                 WHERE {where}
                 ORDER BY p.is_builtin DESC, p.name"""),
        params,
    )).mappings().all()
    return {"items": [_policy_row(dict(r)) for r in rows]}


@policies_router.post("", response_model=AgentPolicyResponse)
async def create_policy(
    data: AgentPolicyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("""INSERT INTO agent_policies
                  (name, description, platform, metric_interval_s, upload_interval_s, process_top_n,
                   service_watchlist, process_watchlist, event_log_filters,
                   disk_ignore, network_ignore, cardinality_limits,
                   update_ring, feature_flags, created_by)
                VALUES (:n, :d, :pl, :mi, :ui, :ptn,
                        :sw, :pw, :elf,
                        :di, :ni, :cl,
                        :ur, :ff, :cb)
                RETURNING *"""),
        {
            "n": data.name, "d": data.description, "pl": data.platform,
            "mi": data.metric_interval_s, "ui": data.upload_interval_s,
            "ptn": data.process_top_n,
            "sw": json.dumps(data.service_watchlist),
            "pw": json.dumps(data.process_watchlist),
            "elf": json.dumps(data.event_log_filters),
            "di": json.dumps(data.disk_ignore),
            "ni": json.dumps(data.network_ignore),
            "cl": json.dumps(data.cardinality_limits),
            "ur": data.update_ring, "ff": json.dumps(data.feature_flags),
            "cb": user.id,
        },
    )).mappings().first()
    await db.commit()
    return _policy_row(dict(row))


@policies_router.get("/{policy_id}", response_model=AgentPolicyResponse)
async def get_policy(
    policy_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""SELECT p.*, COALESCE(a.cnt, 0) AS agent_count
                FROM agent_policies p
                LEFT JOIN (
                    SELECT policy_id, COUNT(*) AS cnt FROM agents
                    WHERE policy_id IS NOT NULL GROUP BY policy_id
                ) a ON a.policy_id = p.id
                WHERE p.id = :id"""),
        {"id": policy_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Policy not found")
    return _policy_row(dict(row))


@policies_router.patch("/{policy_id}", response_model=AgentPolicyResponse)
async def update_policy(
    policy_id: UUID,
    data: AgentPolicyUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    sets = []
    params: dict[str, Any] = {"id": policy_id}
    plain_fields = ["name", "description", "platform", "metric_interval_s",
                    "upload_interval_s", "process_top_n", "update_ring"]
    json_fields = ["service_watchlist", "process_watchlist", "event_log_filters",
                   "disk_ignore", "network_ignore", "cardinality_limits", "feature_flags"]
    for field in plain_fields:
        v = getattr(data, field)
        if v is not None:
            sets.append(f"{field} = :{field}")
            params[field] = v
    for field in json_fields:
        v = getattr(data, field)
        if v is not None:
            sets.append(f"{field} = :{field}")
            params[field] = json.dumps(v)
    if not sets:
        return await get_policy(policy_id, db, user)
    sets.append("config_version = config_version + 1")
    sql = f"UPDATE agent_policies SET {', '.join(sets)}, updated_at = NOW() WHERE id = :id RETURNING *"
    row = (await db.execute(text(sql), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Policy not found")
    await db.commit()
    return await get_policy(policy_id, db, user)


@policies_router.delete("/{policy_id}")
async def delete_policy(
    policy_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("SELECT is_builtin FROM agent_policies WHERE id = :id"),
        {"id": policy_id},
    )).first()
    if not row:
        raise HTTPException(404, "Policy not found")
    if row[0]:
        raise HTTPException(400, "Cannot delete a built-in policy")
    await db.execute(
        text("UPDATE agents SET policy_id = NULL WHERE policy_id = :id"),
        {"id": policy_id},
    )
    await db.execute(text("DELETE FROM agent_policies WHERE id = :id"), {"id": policy_id})
    await db.commit()
    return {"ok": True}


# ── Agent fleet ─────────────────────────────────────────────────────

@fleet_router.get("")
async def list_fleet(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    site_id: Optional[UUID] = None,
    policy_id: Optional[UUID] = None,
    update_ring: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "last_heartbeat_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    page = max(1, page); page_size = max(1, min(500, page_size))
    offset = (page - 1) * page_size

    sort_map = {
        "hostname": "a.hostname",
        "status": "a.status",
        "version": "a.version",
        "last_heartbeat_at": "a.last_heartbeat_at",
        "queue_depth": "a.queue_depth",
        "spool_bytes": "a.spool_bytes",
        "update_ring": "a.update_ring",
        "platform": "a.platform",
    }
    sort_col = sort_map.get(sort, "a.last_heartbeat_at")
    sort_dir = "DESC" if order.lower() == "desc" else "ASC"

    where = ["1=1"]
    params: dict[str, Any] = {"limit": page_size, "offset": offset}
    if status:
        where.append("a.status = :st"); params["st"] = status
    if platform:
        where.append("a.platform = :pl"); params["pl"] = platform
    if site_id:
        where.append("a.site_id = :site"); params["site"] = site_id
    if policy_id:
        where.append("a.policy_id = :pol"); params["pol"] = policy_id
    if update_ring:
        where.append("a.update_ring = :ur"); params["ur"] = update_ring
    if q:
        where.append("(a.hostname ILIKE :q OR a.agent_uid ILIKE :q OR a.version ILIKE :q)")
        params["q"] = f"%{q}%"

    where_sql = " AND ".join(where)
    total = (await db.execute(text(f"SELECT COUNT(*) FROM agents a WHERE {where_sql}"), params)).first()[0]

    rows = (await db.execute(
        text(f"""SELECT a.*, p.name AS policy_name, st.name AS site_name,
                        s.display_name AS server_name
                 FROM agents a
                 LEFT JOIN agent_policies p ON p.id = a.policy_id
                 LEFT JOIN sites st ON st.id = a.site_id
                 LEFT JOIN servers s ON s.id = a.server_id
                 WHERE {where_sql}
                 ORDER BY {sort_col} {sort_dir} NULLS LAST
                 LIMIT :limit OFFSET :offset"""),
        params,
    )).mappings().all()
    summary_row = (await db.execute(text(
        """SELECT COUNT(*) FILTER (WHERE status = 'online')   AS online,
                  COUNT(*) FILTER (WHERE status = 'stale')    AS stale,
                  COUNT(*) FILTER (WHERE status = 'offline')  AS offline,
                  COUNT(*) FILTER (WHERE status = 'disabled') AS disabled,
                  COUNT(*)                                    AS total,
                  COALESCE(SUM(queue_depth), 0)               AS queue_depth,
                  COALESCE(SUM(spool_bytes), 0)               AS spool_bytes
           FROM agents"""
    ))).mappings().first()

    return {
        "items": [_agent_response(dict(r)) for r in rows],
        "total": total, "page": page, "page_size": page_size,
        "summary": dict(summary_row) if summary_row else {},
    }


# ── Agent packages (register before /{agent_id} so the literal path wins) ──

@fleet_router.get("/packages")
async def list_agent_packages(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        text("""SELECT id, platform, arch, version, channel, file_name, file_size,
                       sha256, is_latest, released_at
                FROM agent_packages ORDER BY platform, released_at DESC"""),
    )).mappings().all()
    return {"items": [dict(r) | {"id": str(r["id"])} for r in rows]}


@fleet_router.post("/packages/publish")
async def publish_agent_packages(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Scan the on-disk package store and (re)register packages.

    Layout: /opt/zenplus/artifacts/agents/<platform>/zenplus-agent-<version>.<ext>
    (.msi for windows, .tar.gz for linux/macos). The newest version per
    platform is flagged is_latest and served by the download endpoint.
    """
    from app.api.v1.agents import AGENT_PKG_DIR

    def _parse_version(name: str) -> Optional[str]:
        import re
        m = re.match(r"^zenplus-agent[-_](\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)?)\.(msi|tar\.gz)$", name)
        return m.group(1) if m else None

    published: list[dict] = []
    skipped: list[str] = []
    for platform in ("windows", "linux", "macos"):
        pdir = AGENT_PKG_DIR / platform
        if not pdir.is_dir():
            continue
        candidates = []
        for f in sorted(pdir.iterdir()):
            if not f.is_file():
                continue
            version = _parse_version(f.name)
            if not version:
                skipped.append(f"{platform}/{f.name}")
                continue
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            candidates.append((version, f.name, f.stat().st_size, digest))
        if not candidates:
            continue
        # Highest version (numeric-aware) becomes latest.
        def _vkey(v: str):
            return [int(p) if p.isdigit() else 0 for p in v.split("-")[0].split(".")]
        candidates.sort(key=lambda c: _vkey(c[0]))
        latest_version = candidates[-1][0]
        for version, fname, fsize, digest in candidates:
            await db.execute(
                text("""INSERT INTO agent_packages
                          (platform, arch, version, channel, file_name, file_size, sha256,
                           download_path, is_latest)
                        VALUES (:pl, 'amd64', :v, 'stable', :fn, :fs, :sha, :dp, :latest)
                        ON CONFLICT (platform, arch, version, channel) DO UPDATE SET
                          file_name = EXCLUDED.file_name,
                          file_size = EXCLUDED.file_size,
                          sha256 = EXCLUDED.sha256,
                          download_path = EXCLUDED.download_path,
                          is_latest = EXCLUDED.is_latest"""),
                {"pl": platform, "v": version, "fn": fname, "fs": fsize, "sha": digest,
                 "dp": f"/api/v1/agents/packages/{platform}/latest",
                 "latest": version == latest_version},
            )
            published.append({"platform": platform, "version": version, "file_name": fname,
                              "is_latest": version == latest_version})
        await db.execute(
            text("""UPDATE agent_packages SET is_latest = FALSE
                    WHERE platform = :pl AND channel = 'stable' AND version != :v"""),
            {"pl": platform, "v": latest_version},
        )
    await db.commit()
    return {"published": published, "skipped": skipped,
            "package_dir": str(AGENT_PKG_DIR)}


# ── Pre-configured package download ─────────────────────────────────
#
# The published MSI ships with a fixed-width placeholder as the default value
# of its ENROLLMENT_TOKEN property. Because every enrollment token is exactly
# the same length ("zpa_enr_" + 32 chars), the placeholder can be rewritten
# byte-for-byte in the packaged file: the compound-file layout, stream sizes
# and string-pool offsets all stay valid, so no MSI rebuild is needed and the
# operator gets a package that installs on N hosts with nothing to type.

# Must match config.PlaceholderEnrollmentToken in the agent source.
PKG_TOKEN_PLACEHOLDER = b"zpa_enr_PLACEHOLDERTOKENPLACEHOLDERTOKEN"


def _stamp_enrollment_token(blob: bytes, raw_token: str) -> bytes:
    """Rewrite the package's placeholder token in place.

    Length equality is what makes this safe; it is asserted rather than
    assumed, because a mismatch would silently corrupt the package.
    """
    token = raw_token.encode()
    if len(token) != len(PKG_TOKEN_PLACEHOLDER):
        raise HTTPException(
            500,
            f"Enrollment token is {len(token)} bytes but the package placeholder is "
            f"{len(PKG_TOKEN_PLACEHOLDER)}; refusing to produce a corrupt package.",
        )
    occurrences = blob.count(PKG_TOKEN_PLACEHOLDER)
    if occurrences == 0:
        raise HTTPException(
            409,
            "The published package predates pre-configured downloads (no token "
            "placeholder found). Rebuild and republish the agent package.",
        )
    return blob.replace(PKG_TOKEN_PLACEHOLDER, token)


@fleet_router.post("/packages/download")
async def download_preconfigured_package(
    data: AgentPackageDownloadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Return the published package with a freshly minted token stamped in.

    The token's max_uses equals the requested server count, so one download
    covers exactly that many hosts. The raw token is never persisted — only
    its hash is stored, and the only copy leaves in the response body.
    """
    from app.api.v1.agents import AGENT_PKG_DIR

    pkg = await _latest_package_for_download(data.platform, db)
    if not pkg:
        raise HTTPException(
            404,
            f"No {data.platform} agent package is published yet. Drop the package into "
            f"{AGENT_PKG_DIR}/{data.platform}/ and publish it from this page.",
        )
    file_path = (AGENT_PKG_DIR / data.platform / pkg["file_name"]).resolve()
    if not str(file_path).startswith(str(AGENT_PKG_DIR)) or not file_path.is_file():
        raise HTTPException(410, "Published package file is missing on disk")

    raw, hashed, prefix = _new_enrollment_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=data.ttl_hours)
    tags = list(data.tags or [])
    if data.label:
        tags.append(data.label)

    blob = _stamp_enrollment_token(file_path.read_bytes(), raw)

    row = (await db.execute(
        text("""INSERT INTO agent_enrollment_tokens
                  (token_hash, token_prefix, platform, site_id, policy_id,
                   hostname_hint, tags, expires_at, max_uses, created_by)
                VALUES (:h, :p, :pl, :site, :pol, :hint, :tags, :exp, :mu, :cb)
                RETURNING id, expires_at"""),
        {
            "h": hashed, "p": prefix,
            # 'any' lets one package cover mixed hosts; the package itself is
            # already platform-specific.
            "pl": "any",
            "site": data.site_id, "pol": data.policy_id,
            "hint": data.label or f"pre-configured {data.platform} package",
            "tags": json.dumps(tags), "exp": expires,
            "mu": data.server_count, "cb": user.id,
        },
    )).first()
    await db.commit()

    logger.info(
        "pre-configured %s package downloaded by user=%s token=%s max_uses=%d",
        data.platform, user.id, prefix, data.server_count,
    )

    filename = pkg["file_name"]
    media = "application/x-msi" if filename.endswith(".msi") else "application/octet-stream"
    return Response(
        content=blob,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(blob)),
            # Read by the dashboard to show what the download is good for.
            # Deliberately no raw token here — it lives only inside the file.
            "X-Token-Id": str(row[0]),
            "X-Token-Prefix": prefix,
            "X-Token-Expires-At": row[1].isoformat(),
            "X-Token-Max-Uses": str(data.server_count),
            "X-Package-Version": pkg["version"],
            "Access-Control-Expose-Headers": (
                "Content-Disposition,X-Token-Id,X-Token-Prefix,"
                "X-Token-Expires-At,X-Token-Max-Uses,X-Package-Version"
            ),
        },
    )


async def _latest_package_for_download(platform: str, db: AsyncSession) -> Optional[dict]:
    row = (await db.execute(
        text("""SELECT * FROM agent_packages
                WHERE platform = :p AND channel = 'stable' AND is_latest = TRUE
                ORDER BY released_at DESC LIMIT 1"""),
        {"p": platform},
    )).mappings().first()
    return dict(row) if row else None


@fleet_router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (await db.execute(
        text("""SELECT a.*, p.name AS policy_name, st.name AS site_name,
                       s.display_name AS server_name
                FROM agents a
                LEFT JOIN agent_policies p ON p.id = a.policy_id
                LEFT JOIN sites st ON st.id = a.site_id
                LEFT JOIN servers s ON s.id = a.server_id
                WHERE a.id = :id"""),
        {"id": agent_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Agent not found")
    return _agent_response(dict(row))


@fleet_router.post("/{agent_id}/rotate-certificate")
async def rotate_certificate(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    await db.execute(
        text("""INSERT INTO agent_commands (agent_id, command, requested_by)
                VALUES (:aid, 'rotate_certificate', :u)"""),
        {"aid": agent_id, "u": user.id},
    )
    await db.commit()
    return {"ok": True}


@fleet_router.post("/{agent_id}/request-diagnostics")
async def request_diagnostics(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("""INSERT INTO agent_commands (agent_id, command, requested_by)
                VALUES (:aid, 'upload_diagnostics', :u) RETURNING id"""),
        {"aid": agent_id, "u": user.id},
    )).first()
    await db.execute(
        text("""INSERT INTO agent_diagnostics (agent_id, requested_by, status)
                VALUES (:aid, :u, 'requested')"""),
        {"aid": agent_id, "u": user.id},
    )
    await db.commit()
    return {"ok": True, "command_id": str(row[0])}


# ── On-demand agent commands ────────────────────────────────────────
# The agent implements collect_now and refresh_config but nothing could
# queue them, so an operator had to wait out the collection interval to see
# a change. Only commands the agent actually implements are accepted —
# queueing an unsupported one just produces a failed row in its history.

AGENT_ON_DEMAND_COMMANDS = {
    "collect_now": "Collect now",
    "refresh_config": "Refresh config",
    "status": "Status report",
}


@fleet_router.post("/{agent_id}/commands/{command}")
async def queue_agent_command(
    agent_id: UUID,
    command: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if command not in AGENT_ON_DEMAND_COMMANDS:
        raise HTTPException(
            400,
            f"Unsupported command '{command}'. Supported: "
            + ", ".join(sorted(AGENT_ON_DEMAND_COMMANDS)),
        )
    agent = (await db.execute(
        text("SELECT id, status FROM agents WHERE id = :id"), {"id": agent_id},
    )).mappings().first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent["status"] == "disabled":
        raise HTTPException(409, "Agent is disabled; enable it before sending commands")

    # Don't stack duplicates the agent hasn't picked up yet.
    pending = (await db.execute(
        text("""SELECT id FROM agent_commands
                WHERE agent_id = :aid AND command = :cmd
                  AND status IN ('queued', 'sent')
                LIMIT 1"""),
        {"aid": agent_id, "cmd": command},
    )).first()
    if pending:
        return {"ok": True, "queued": False, "command": command,
                "detail": f"{AGENT_ON_DEMAND_COMMANDS[command]} is already queued"}

    await db.execute(
        text("""INSERT INTO agent_commands (agent_id, command, requested_by)
                VALUES (:aid, :cmd, :u)"""),
        {"aid": agent_id, "cmd": command, "u": user.id},
    )
    await db.commit()
    return {"ok": True, "queued": True, "command": command,
            "detail": f"{AGENT_ON_DEMAND_COMMANDS[command]} queued"}


@fleet_router.post("/{agent_id}/set-update-ring")
async def set_update_ring(
    agent_id: UUID,
    ring: str = Query(..., regex="^(canary|beta|stable|pinned)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    await db.execute(
        text("UPDATE agents SET update_ring = :r, updated_at = NOW() WHERE id = :id"),
        {"r": ring, "id": agent_id},
    )
    await db.commit()
    return {"ok": True}


@fleet_router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Remove an agent record (offline leftovers, decommissioned hosts).

    The uninstalled/retired agent's key stops authenticating immediately;
    a live host can re-enroll with a fresh token. The server row is kept.
    """
    res = await db.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Agent not found")
    return {"ok": True}


@fleet_router.post("/bulk")
async def fleet_bulk_action(
    data: AgentBulkAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if not data.agent_ids:
        return {"ok": True, "affected": 0}
    ids = list(data.agent_ids)
    if data.action == "change_policy":
        if not data.policy_id:
            raise HTTPException(400, "policy_id required")
        await db.execute(
            text("UPDATE agents SET policy_id = :p, updated_at = NOW() WHERE id = ANY(:ids)"),
            {"p": data.policy_id, "ids": ids},
        )
    elif data.action == "change_update_ring":
        if not data.update_ring:
            raise HTTPException(400, "update_ring required")
        await db.execute(
            text("UPDATE agents SET update_ring = :r, updated_at = NOW() WHERE id = ANY(:ids)"),
            {"r": data.update_ring, "ids": ids},
        )
    elif data.action == "trigger_upgrade":
        await db.execute(
            text("""UPDATE agents SET desired_version = COALESCE(:v, desired_version), updated_at = NOW()
                    WHERE id = ANY(:ids)"""),
            {"v": data.target_version, "ids": ids},
        )
        for aid in ids:
            await db.execute(
                text("""INSERT INTO agent_commands (agent_id, command, params, requested_by)
                        VALUES (:aid, 'upgrade_agent', :p, :u)"""),
                {"aid": aid, "p": json.dumps({"version": data.target_version}), "u": user.id},
            )
    elif data.action == "request_diagnostics":
        for aid in ids:
            await db.execute(
                text("""INSERT INTO agent_commands (agent_id, command, requested_by)
                        VALUES (:aid, 'upload_diagnostics', :u)"""),
                {"aid": aid, "u": user.id},
            )
            await db.execute(
                text("""INSERT INTO agent_diagnostics (agent_id, requested_by, status)
                        VALUES (:aid, :u, 'requested')"""),
                {"aid": aid, "u": user.id},
            )
    elif data.action == "rotate_certificate":
        for aid in ids:
            await db.execute(
                text("""INSERT INTO agent_commands (agent_id, command, requested_by)
                        VALUES (:aid, 'rotate_certificate', :u)"""),
                {"aid": aid, "u": user.id},
            )
    elif data.action == "disable":
        await db.execute(
            text("UPDATE agents SET status = 'disabled', updated_at = NOW() WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
    elif data.action == "enable":
        await db.execute(
            text("""UPDATE agents SET status = 'enrolling', updated_at = NOW()
                    WHERE id = ANY(:ids) AND status = 'disabled'"""),
            {"ids": ids},
        )
    else:
        raise HTTPException(400, "Unknown action")
    await db.commit()
    return {"ok": True, "affected": len(ids)}


# ── Server monitoring overview KPIs ─────────────────────────────────

@overview_router.get("/overview")
async def overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    counts = (await db.execute(
        text("""SELECT status, COUNT(*) FROM servers GROUP BY status""")
    )).all()
    status_counts = {r[0]: r[1] for r in counts}
    total_row = (await db.execute(text("SELECT COUNT(*) FROM servers"))).first()
    total = total_row[0] if total_row else 0

    os_counts_rows = (await db.execute(
        text("SELECT os_type, COUNT(*) FROM servers GROUP BY os_type")
    )).all()
    os_counts = {r[0]: r[1] for r in os_counts_rows}

    agent_counts_rows = (await db.execute(
        text("SELECT status, COUNT(*) FROM agents GROUP BY status")
    )).all()
    agent_counts = {r[0]: r[1] for r in agent_counts_rows}

    sites_rows = (await db.execute(
        text("""SELECT s.id, s.name, COUNT(srv.id) AS cnt
                FROM sites s
                LEFT JOIN servers srv ON srv.site_id = s.id
                GROUP BY s.id, s.name
                ORDER BY cnt DESC LIMIT 10""")
    )).all()
    sites = [{"id": str(r[0]), "name": r[1], "server_count": r[2]} for r in sites_rows]

    top_cpu = query_top_pressure("cpu", 5)
    top_memory = query_top_pressure("memory", 5)
    top_disk = query_top_pressure("disk", 5)
    top_network = query_top_pressure("network", 5)

    # Enrich with display names
    async def _hydrate(items: list[dict]) -> list[dict]:
        if not items:
            return []
        ids = [it["server_id"] for it in items]
        rows = (await db.execute(
            text("SELECT id, display_name, hostname FROM servers WHERE id = ANY(:ids)"),
            {"ids": ids},
        )).mappings().all()
        by_id = {str(r["id"]): r for r in rows}
        out = []
        for it in items:
            r = by_id.get(it["server_id"])
            if r:
                out.append({
                    **it,
                    "display_name": r["display_name"],
                    "hostname": r.get("hostname"),
                })
            else:
                out.append(it)
        return out

    return {
        "total": total,
        "status_counts": status_counts,
        "os_counts": os_counts,
        "agent_counts": agent_counts,
        "sites": sites,
        "top_cpu": await _hydrate(top_cpu),
        "top_memory": await _hydrate(top_memory),
        "top_disk": await _hydrate(top_disk),
        "top_network": await _hydrate(top_network),
    }


# ── Software baselines (compliance) ──────────────────────────────────

def _baseline_rule_row(row: dict) -> BaselineRuleResponse:
    return BaselineRuleResponse(
        id=str(row["id"]),
        baseline_id=str(row["baseline_id"]),
        rule_type=row["rule_type"],
        package_match=row["package_match"],
        match_type=row["match_type"],
        min_version=row.get("min_version"),
        severity=row["severity"],
        notes=row.get("notes"),
        created_at=row["created_at"],
    )


def _baseline_row(row: dict, rules: list[dict] | None = None) -> BaselineResponse:
    return BaselineResponse(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        enabled=bool(row["enabled"]),
        os_type=row.get("os_type"),
        site_id=str(row["site_id"]) if row.get("site_id") else None,
        site_name=row.get("site_name"),
        match_tags=[str(t) for t in _json_list(row.get("match_tags"))],
        alerting=bool(row.get("alerting", True)),
        rule_count=row.get("rule_count") or 0,
        servers_evaluated=row.get("servers_evaluated") or 0,
        servers_compliant=row.get("servers_compliant") or 0,
        violations=row.get("violations") or 0,
        rules=[_baseline_rule_row(r) for r in (rules or [])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_BASELINE_STATS_SQL = """
    SELECT b.*, st.name AS site_name,
           (SELECT COUNT(*) FROM software_baseline_rules r WHERE r.baseline_id = b.id) AS rule_count,
           (SELECT COUNT(DISTINCT res.server_id) FROM server_baseline_results res
             WHERE res.baseline_id = b.id) AS servers_evaluated,
           (SELECT COUNT(DISTINCT res.server_id) FROM server_baseline_results res
             WHERE res.baseline_id = b.id
               AND res.server_id NOT IN (
                   SELECT server_id FROM server_baseline_results
                   WHERE baseline_id = b.id AND status != 'compliant')) AS servers_compliant,
           (SELECT COUNT(*) FROM server_baseline_results res
             WHERE res.baseline_id = b.id AND res.status != 'compliant') AS violations
    FROM software_baselines b
    LEFT JOIN sites st ON st.id = b.site_id
"""


async def _load_baseline(db: AsyncSession, baseline_id: UUID) -> BaselineResponse:
    row = (await db.execute(
        text(_BASELINE_STATS_SQL + " WHERE b.id = :id"), {"id": baseline_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Baseline not found")
    rules = (await db.execute(
        text("SELECT * FROM software_baseline_rules WHERE baseline_id = :id ORDER BY package_match"),
        {"id": baseline_id},
    )).mappings().all()
    return _baseline_row(dict(row), [dict(r) for r in rules])


@baselines_router.get("")
async def list_baselines(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(text(_BASELINE_STATS_SQL + " ORDER BY b.name"))).mappings().all()
    items = []
    for row in rows:
        rules = (await db.execute(
            text("SELECT * FROM software_baseline_rules WHERE baseline_id = :id ORDER BY package_match"),
            {"id": row["id"]},
        )).mappings().all()
        items.append(_baseline_row(dict(row), [dict(r) for r in rules]))
    return {"items": items}


async def _insert_rules(db: AsyncSession, baseline_id: str, rules) -> None:
    for r in rules:
        await db.execute(
            text("""INSERT INTO software_baseline_rules
                        (baseline_id, rule_type, package_match, match_type, min_version, severity, notes)
                    VALUES (:bid, :rt, :pm, :mt, :mv, :sev, :notes)"""),
            {"bid": baseline_id, "rt": r.rule_type, "pm": r.package_match,
             "mt": r.match_type, "mv": r.min_version, "sev": r.severity, "notes": r.notes},
        )


@baselines_router.post("", response_model=BaselineResponse)
async def create_baseline(
    data: BaselineCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    dup = (await db.execute(
        text("SELECT 1 FROM software_baselines WHERE name = :n"), {"n": data.name},
    )).first()
    if dup:
        raise HTTPException(409, "A baseline with this name already exists")

    row = (await db.execute(
        text("""INSERT INTO software_baselines
                    (name, description, enabled, os_type, site_id, match_tags, alerting, created_by)
                VALUES (:n, :d, :en, :os, :site, CAST(:tags AS jsonb), :al, :cb)
                RETURNING id"""),
        {"n": data.name, "d": data.description, "en": data.enabled, "os": data.os_type,
         "site": data.site_id, "tags": json.dumps(data.match_tags or []),
         "al": data.alerting, "cb": user.id},
    )).first()
    baseline_id = str(row[0])
    await _insert_rules(db, baseline_id, data.rules)
    await db.commit()

    from app.services.baseline_service import evaluate_baseline
    await evaluate_baseline(db, baseline_id)
    return await _load_baseline(db, UUID(baseline_id))


@baselines_router.get("/{baseline_id}", response_model=BaselineResponse)
async def get_baseline(
    baseline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _load_baseline(db, baseline_id)


@baselines_router.patch("/{baseline_id}", response_model=BaselineResponse)
async def update_baseline(
    baseline_id: UUID,
    data: BaselineUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    sets, params = [], {"id": baseline_id}
    for field in ("name", "description", "enabled", "alerting"):
        v = getattr(data, field)
        if v is not None:
            sets.append(f"{field} = :{field}")
            params[field] = v
    if data.os_type is not None or data.clear_os_type:
        sets.append("os_type = :os_type")
        params["os_type"] = None if data.clear_os_type else data.os_type
    if data.site_id is not None or data.clear_site:
        sets.append("site_id = :site_id")
        params["site_id"] = None if data.clear_site else data.site_id
    if data.match_tags is not None:
        sets.append("match_tags = CAST(:match_tags AS jsonb)")
        params["match_tags"] = json.dumps(data.match_tags)

    if sets:
        res = await db.execute(
            text(f"UPDATE software_baselines SET {', '.join(sets)}, updated_at = NOW() WHERE id = :id"),
            params,
        )
        if res.rowcount == 0:
            raise HTTPException(404, "Baseline not found")

    if data.rules is not None:
        # Replace-all: cascade clears old results; alerts resolve on re-eval.
        await db.execute(
            text("DELETE FROM software_baseline_rules WHERE baseline_id = :id"),
            {"id": baseline_id},
        )
        await _insert_rules(db, str(baseline_id), data.rules)
    await db.commit()

    from app.services.baseline_service import evaluate_baseline
    await evaluate_baseline(db, str(baseline_id))
    return await _load_baseline(db, baseline_id)


@baselines_router.delete("/{baseline_id}")
async def delete_baseline(
    baseline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    res = await db.execute(
        text("DELETE FROM software_baselines WHERE id = :id"), {"id": baseline_id},
    )
    if res.rowcount == 0:
        raise HTTPException(404, "Baseline not found")
    # Close any alerts this baseline raised.
    await db.execute(
        text("""UPDATE alerts SET status = 'resolved', resolved_at = NOW()
                WHERE status = 'active' AND metadata->>'baseline_id' = :bid"""),
        {"bid": str(baseline_id)},
    )
    await db.commit()
    return {"ok": True}


@baselines_router.post("/{baseline_id}/evaluate")
async def evaluate_baseline_now(
    baseline_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    from app.services.baseline_service import evaluate_baseline
    n = await evaluate_baseline(db, str(baseline_id))
    return {"ok": True, "servers_evaluated": n}


@baselines_router.get("/{baseline_id}/results")
async def baseline_results(
    baseline_id: UUID,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-server outcomes for one baseline, violations first."""
    where = "res.baseline_id = :bid"
    params: dict[str, Any] = {"bid": baseline_id}
    if status:
        where += " AND res.status = :st"
        params["st"] = status
    rows = (await db.execute(
        text(f"""SELECT res.server_id, res.rule_id, res.status, res.found_package,
                        res.found_version, res.expected, res.severity,
                        res.first_failed_at, res.evaluated_at,
                        s.display_name AS server_name, s.hostname, s.os_type,
                        br.rule_type, br.package_match, br.match_type, br.min_version
                 FROM server_baseline_results res
                 JOIN servers s ON s.id = res.server_id
                 JOIN software_baseline_rules br ON br.id = res.rule_id
                 WHERE {where}
                 ORDER BY (res.status = 'compliant'), s.display_name, br.package_match"""),
        params,
    )).mappings().all()
    return {"items": [dict(r) for r in rows]}
