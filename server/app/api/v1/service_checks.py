from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.service_check import (
    ServiceCheckCreate,
    ServiceCheckUpdate,
    ServiceCheckResponse,
    ServiceCheckSummary,
    ServiceMetricResponse,
    ServiceCheckGroupCreate,
    ServiceCheckGroupUpdate,
    ServiceCheckGroupResponse,
    ServiceMaintenanceCreate,
    ServiceMaintenanceResponse,
    ServiceCheckTemplateCreate,
    ServiceCheckTemplateUpdate,
    ServiceCheckTemplateResponse,
    ServiceCheckTemplateApply,
    ServiceCheckTemplateApplyResult,
)
from app.services import service_check_service, service_metric_service

router = APIRouter(prefix="/service-checks", tags=["Service Checks"])
groups_router = APIRouter(prefix="/service-check-groups", tags=["Service Check Groups"])
maintenance_router = APIRouter(prefix="/service-check-maintenance", tags=["Service Check Maintenance"])
templates_router = APIRouter(prefix="/service-check-templates", tags=["Service Check Templates"])


@templates_router.get("", response_model=list[ServiceCheckTemplateResponse])
async def list_service_check_templates(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.list_templates(db)


@templates_router.post("", response_model=ServiceCheckTemplateResponse, status_code=201)
async def create_service_check_template(
    data: ServiceCheckTemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.create_template(db, data, current_user.id)


@templates_router.get("/{template_id}", response_model=ServiceCheckTemplateResponse)
async def get_service_check_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    t = await service_check_service.get_template(db, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@templates_router.put("/{template_id}", response_model=ServiceCheckTemplateResponse)
async def update_service_check_template(
    template_id: UUID,
    data: ServiceCheckTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    t = await service_check_service.update_template(db, template_id, data)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@templates_router.delete("/{template_id}", status_code=204)
async def delete_service_check_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = await service_check_service.delete_template(db, template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return None


@templates_router.post("/{template_id}/apply", response_model=ServiceCheckTemplateApplyResult)
async def apply_service_check_template(
    template_id: UUID,
    data: ServiceCheckTemplateApply,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return await service_check_service.apply_template(db, template_id, data, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@maintenance_router.get("", response_model=list[ServiceMaintenanceResponse])
async def list_service_check_maintenance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.list_maintenance(db)


@maintenance_router.post("", response_model=ServiceMaintenanceResponse, status_code=201)
async def create_service_check_maintenance(
    data: ServiceMaintenanceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.create_maintenance(db, data, current_user.id)


@maintenance_router.delete("/{maintenance_id}", status_code=204)
async def delete_service_check_maintenance(
    maintenance_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = await service_check_service.delete_maintenance(db, maintenance_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    return None


@router.get("")
async def list_service_checks(
    device_id: UUID | None = None,
    check_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    group_id: UUID | None = None,
    tag: str | None = None,
    level: int | None = Query(default=None, ge=1, le=3),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.get_service_checks(
        db, device_id=device_id, check_type=check_type, status=status,
        search=search, group_id=group_id, tag=tag, level=level,
        skip=skip, limit=limit,
    )


@router.get("/tags", response_model=list[str])
async def list_service_check_tags(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.list_tags(db)


# ── Groups (mounted at /service-check-groups) ───────────────────────────────

@groups_router.get("", response_model=list[ServiceCheckGroupResponse])
async def list_service_check_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.list_groups(db)


@groups_router.post("", response_model=ServiceCheckGroupResponse, status_code=201)
async def create_service_check_group(
    data: ServiceCheckGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.create_group(db, data)


@groups_router.put("/{group_id}", response_model=ServiceCheckGroupResponse)
async def update_service_check_group(
    group_id: UUID,
    data: ServiceCheckGroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    out = await service_check_service.update_group(db, group_id, data)
    if not out:
        raise HTTPException(status_code=404, detail="Group not found")
    return out


@groups_router.delete("/{group_id}", status_code=204)
async def delete_service_check_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = await service_check_service.delete_group(db, group_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Group not found")
    return None


@router.get("/summary", response_model=ServiceCheckSummary)
async def service_check_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.get_service_check_summary(db)


@router.get("/uptime-stats")
async def service_check_uptime_stats(
    hours: int = Query(default=24, ge=1, le=8760),
    current_user: User = Depends(get_current_user),
):
    """Get uptime percentages per service check over a time range from ClickHouse metrics."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    to_time = datetime.utcnow()
    from_time = to_time - timedelta(hours=hours)

    tables_to_try = []
    if hours <= 6:
        tables_to_try = ["service_metrics"]
    elif hours <= 168:
        tables_to_try = ["service_metrics_5m", "service_metrics"]
    else:
        tables_to_try = ["service_metrics_5m", "service_metrics"]

    uptime_map = {}
    for table in tables_to_try:
        if table == "service_metrics":
            query = f"""
                SELECT service_check_id,
                       countIf(is_up = 1) AS up_count,
                       count() AS total_count
                FROM zenplus.{table}
                WHERE timestamp >= %(from)s AND timestamp <= %(to)s
                GROUP BY service_check_id
            """
        else:
            query = f"""
                SELECT service_check_id,
                       sum(uptime_pct * sample_count) AS weighted_up,
                       sum(sample_count) AS total_count
                FROM zenplus.{table}
                WHERE timestamp >= %(from)s AND timestamp <= %(to)s
                GROUP BY service_check_id
            """
        try:
            result = client.query(query, parameters={"from": from_time, "to": to_time})
            if len(result.result_rows) > 0:
                for row in result.result_rows:
                    check_id = str(row[0])
                    up = row[1]
                    total = row[2]
                    if table == "service_metrics":
                        uptime_map[check_id] = round((up / total * 100) if total > 0 else 0, 2)
                    else:
                        uptime_map[check_id] = round((up / total * 100) if total > 0 else 0, 2)
                break
        except Exception:
            continue

    return {"hours": hours, "from": from_time.isoformat(), "to": to_time.isoformat(), "checks": uptime_map}


@router.post("", response_model=ServiceCheckResponse, status_code=201)
async def create_service_check(
    data: ServiceCheckCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.create_service_check(db, data, current_user.id)


@router.get("/{check_id}", response_model=ServiceCheckResponse)
async def get_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sc = await service_check_service.get_service_check(db, check_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.put("/{check_id}", response_model=ServiceCheckResponse)
async def update_service_check(
    check_id: UUID,
    data: ServiceCheckUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sc = await service_check_service.update_service_check(db, check_id, data)
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.delete("/{check_id}", status_code=204)
async def delete_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await service_check_service.delete_service_check(db, check_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Service check not found")


class BulkDeleteRequest(BaseModel):
    check_ids: list[UUID]


@router.post("/bulk-delete")
async def bulk_delete_service_checks(
    data: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await service_check_service.bulk_delete_service_checks(db, data.check_ids)
    return {"deleted": count}


@router.get("/export/json")
async def export_service_checks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    checks = await service_check_service.export_service_checks(db)
    return JSONResponse(content=checks)


@router.get("/{check_id}/metrics", response_model=ServiceMetricResponse)
async def get_service_check_metrics(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="auto"),
    current_user: User = Depends(get_current_user),
):
    return service_metric_service.get_service_metrics(
        check_id, from_time=from_time, to_time=to_time, granularity=granularity,
    )


@router.get("/{check_id}/status-history")
async def get_service_check_status_history(
    check_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
):
    return service_metric_service.get_service_status_history(
        check_id, from_time=from_time, to_time=to_time, limit=limit,
    )


@router.get("/{check_id}/hourly-uptime")
async def get_service_check_hourly_uptime(
    check_id: UUID,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hourly uptime % for the last N days (default 30). Powers the calendar."""
    return await service_check_service.get_hourly_uptime(db, check_id, days)


@router.get("/{check_id}/sla")
async def get_service_check_sla(
    check_id: UUID,
    hours: int = Query(default=720, ge=1, le=8760),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SLA stats: uptime %, MTTR, MTBF, incident count over a time window.

    Default window is 30 days (720h). Valid range 1h..1y.
    """
    out = await service_check_service.get_service_sla(db, check_id, hours)
    if out.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Service check not found")
    return out


@router.post("/{check_id}/test")
async def test_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run an on-demand test of a service check and return the result."""
    import socket
    import ssl
    import time
    import httpx

    check = await service_check_service.get_service_check(db, check_id)
    if not check:
        raise HTTPException(status_code=404, detail="Service check not found")

    result = {
        "check_id": str(check_id),
        "check_type": check.check_type,
        "target": check.target_url or f"{check.target_host}:{check.target_port}",
        "status": "unknown",
        "response_time_ms": 0,
        "error": "",
        "details": {},
    }

    start = time.monotonic()

    try:
        if check.check_type == "http":
            url = check.target_url or f"http://{check.target_host}:{check.target_port or 80}"
            async with httpx.AsyncClient(
                timeout=check.timeout,
                follow_redirects=check.http_follow_redirects,
                verify=False,
            ) as client:
                method = (check.http_method or "GET").upper()
                resp = await client.request(method, url)

            elapsed = (time.monotonic() - start) * 1000
            result["response_time_ms"] = round(elapsed, 1)
            result["details"]["status_code"] = resp.status_code
            result["details"]["headers"] = dict(resp.headers)
            result["details"]["body_length"] = len(resp.content)

            expected = check.http_expected_status or 200
            if resp.status_code == expected:
                result["status"] = "up"
            else:
                result["status"] = "down"
                result["error"] = f"Expected HTTP {expected}, got {resp.status_code}"

            # Content match
            if check.http_content_match:
                body = resp.text
                if check.http_content_match in body:
                    result["details"]["content_match"] = True
                else:
                    result["status"] = "down"
                    result["error"] = f"Content match failed: '{check.http_content_match}' not found"
                    result["details"]["content_match"] = False

        elif check.check_type == "tcp":
            host = check.target_host
            port = check.target_port or 80
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(check.timeout)
            sock.connect((host, port))
            elapsed = (time.monotonic() - start) * 1000
            sock.close()
            result["response_time_ms"] = round(elapsed, 1)
            result["status"] = "up"
            result["details"]["connected"] = True

        elif check.check_type == "tls":
            host = check.target_host
            port = check.target_port or 443
            ctx = ssl.create_default_context()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(check.timeout)
            wrapped = ctx.wrap_socket(sock, server_hostname=host)
            wrapped.connect((host, port))
            elapsed = (time.monotonic() - start) * 1000
            cert = wrapped.getpeercert()
            wrapped.close()
            result["response_time_ms"] = round(elapsed, 1)
            result["status"] = "up"
            result["details"]["connected"] = True
            if cert:
                result["details"]["subject"] = dict(x[0] for x in cert.get("subject", []))
                result["details"]["issuer"] = dict(x[0] for x in cert.get("issuer", []))
                result["details"]["expires"] = cert.get("notAfter", "")
                result["details"]["serial"] = cert.get("serialNumber", "")

        elif check.check_type == "icmp":
            # ICMP ping via subprocess (the Python `ping3` / raw-socket routes need
            # CAP_NET_RAW that uvicorn doesn't have). The `ping` binary works fine.
            import subprocess
            count = int((check.config or {}).get("count", 1)) or 1
            count = max(1, min(count, 10))
            proc = subprocess.run(
                ["ping", "-c", str(count), "-W", str(max(1, check.timeout)), check.target_host],
                capture_output=True, text=True, timeout=check.timeout + 5,
            )
            elapsed = (time.monotonic() - start) * 1000
            out = proc.stdout + proc.stderr
            # Parse average RTT from ping output: rtt min/avg/max/mdev = 0.3/0.5/0.7/0.1 ms
            import re
            m = re.search(r"min/avg/max/\w+\s*=\s*[\d.]+/([\d.]+)/[\d.]+", out)
            if proc.returncode == 0:
                rtt = float(m.group(1)) if m else elapsed
                result["response_time_ms"] = round(rtt, 1)
                result["status"] = "up"
                result["details"]["packets"] = count
            else:
                result["response_time_ms"] = round(elapsed, 1)
                result["status"] = "down"
                loss_m = re.search(r"(\d+)%\s*packet loss", out)
                result["error"] = f"Ping failed ({loss_m.group(1)}% packet loss)" if loss_m else "Ping failed"

        elif check.check_type == "dns":
            # DNS resolution check. Config: record_type (A|AAAA|CNAME|MX|TXT), expected (optional substring).
            import socket as _sock
            cfg = check.config or {}
            record_type = str(cfg.get("record_type", "A")).upper()
            expected = cfg.get("expected") or None

            try:
                import dns.resolver  # type: ignore
                resolver = dns.resolver.Resolver()
                resolver.lifetime = check.timeout
                resolver.timeout = check.timeout
                answers = resolver.resolve(check.target_host, record_type)
                values = [str(a).rstrip('.') for a in answers]
            except ImportError:
                # Fallback: basic A-record resolution with stdlib when dnspython isn't present.
                if record_type not in ("A", "AAAA"):
                    raise RuntimeError("dnspython not installed; only A/AAAA fallback supported")
                family = _sock.AF_INET if record_type == "A" else _sock.AF_INET6
                infos = _sock.getaddrinfo(check.target_host, None, family=family)
                values = sorted({info[4][0] for info in infos})

            elapsed = (time.monotonic() - start) * 1000
            result["response_time_ms"] = round(elapsed, 1)
            result["details"]["record_type"] = record_type
            result["details"]["values"] = values
            if not values:
                result["status"] = "down"
                result["error"] = "No DNS records returned"
            elif expected and not any(expected in v for v in values):
                result["status"] = "down"
                result["error"] = f"Expected '{expected}' not in answers {values}"
            else:
                result["status"] = "up"

    except (socket.timeout, TimeoutError):
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = f"Connection timed out after {check.timeout}s"
    except ConnectionRefusedError:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = "Connection refused"
    except ssl.SSLError as e:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = f"TLS error: {e}"
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = str(e)

    # Update the service check status in PostgreSQL so the UI reflects the test result
    from sqlalchemy import update as sql_update
    from app.models.service_check import ServiceCheck as ServiceCheckModel
    now_ts = datetime.utcnow()
    update_values = {
        "status": result["status"],
        "last_check_at": now_ts,
        "last_response_ms": result["response_time_ms"],
        "last_error": result["error"] if result["error"] else None,
    }
    # Update TLS fields if available
    if check.check_type == "tls" and result["details"].get("expires"):
        update_values["tls_subject"] = str(result["details"].get("subject", {}).get("commonName", ""))
        update_values["tls_issuer"] = str(result["details"].get("issuer", {}).get("organizationName", ""))

    await db.execute(
        sql_update(ServiceCheckModel)
        .where(ServiceCheckModel.id == check_id)
        .values(**update_values)
    )
    await db.commit()

    # Write metric to ClickHouse for chart / timeline data
    try:
        from app.core.database import get_clickhouse_client
        ch = get_clickhouse_client()
        is_up_val = 1 if result["status"] == "up" else 0
        status_code = result["details"].get("status_code")
        ch.insert(
            "service_metrics",
            [[
                str(check_id),
                str(check.device_id) if check.device_id else None,
                now_ts,
                check.check_type,
                is_up_val,
                result["response_time_ms"],
                status_code,
                None,  # tls_days_remaining
                None,  # tls_valid
                None,  # content_matched
                result["error"] if result["error"] else None,
                "api-test",
            ]],
            column_names=[
                "service_check_id", "device_id", "timestamp", "check_type",
                "is_up", "response_ms", "status_code", "tls_days_remaining",
                "tls_valid", "content_matched", "error_message", "poller_id",
            ],
        )
        ch.close()
    except Exception:
        pass  # Don't fail the test response if CH write fails

    return result
