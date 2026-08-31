from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_admin_user, require_operator_user
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
    ServiceCredentialCreate,
    ServiceCredentialUpdate,
    ServiceCredentialResponse,
)
from app.services import audit_service, service_check_service, service_metric_service

def _status_matches(code: int, patterns: str) -> bool:
    """Match an HTTP status against a comma list of patterns: 200, 2xx, 200-299."""
    for raw in patterns.split(","):
        p = raw.strip().lower()
        if not p:
            continue
        if "-" in p:
            try:
                lo, hi = (int(x) for x in p.split("-", 1))
            except ValueError:
                continue
            if lo <= code <= hi:
                return True
        elif "x" in p:
            prefix = p.rstrip("x")
            try:
                lo = int(prefix.ljust(3, "0"))
                hi = int(prefix.ljust(3, "9"))
            except ValueError:
                continue
            if lo <= code <= hi:
                return True
        else:
            try:
                if int(p) == code:
                    return True
            except ValueError:
                continue
    return False


router = APIRouter(prefix="/service-checks", tags=["Service Checks"])
groups_router = APIRouter(prefix="/service-check-groups", tags=["Service Check Groups"])
maintenance_router = APIRouter(prefix="/service-check-maintenance", tags=["Service Check Maintenance"])
templates_router = APIRouter(prefix="/service-check-templates", tags=["Service Check Templates"])
credentials_router = APIRouter(prefix="/service-credentials", tags=["Service Credentials"])


@credentials_router.get("", response_model=list[ServiceCredentialResponse])
async def list_service_credentials(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List safe credential metadata. Secret material is never returned."""
    return await service_check_service.list_service_credentials(db)


@credentials_router.post("", response_model=ServiceCredentialResponse, status_code=201)
async def create_service_credential(
    data: ServiceCredentialCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    try:
        credential = await service_check_service.create_service_credential(db, data, current_user.id)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A service credential with this name already exists")
    await audit_service.write_audit_log(
        db,
        actor=current_user,
        action="create",
        resource_type="service_credential",
        resource_id=str(credential.id),
        metadata={"name": credential.name, "auth_type": credential.auth_type},
    )
    await db.commit()
    return credential


@credentials_router.put("/{credential_id}", response_model=ServiceCredentialResponse)
async def update_service_credential(
    credential_id: UUID,
    data: ServiceCredentialUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    try:
        credential = await service_check_service.update_service_credential(db, credential_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A service credential with this name already exists")
    if not credential:
        raise HTTPException(status_code=404, detail="Service credential not found")
    await audit_service.write_audit_log(
        db,
        actor=current_user,
        action="update",
        resource_type="service_credential",
        resource_id=str(credential.id),
        metadata={"name": credential.name, "auth_type": credential.auth_type, "secret_rotated": data.secret is not None},
    )
    await db.commit()
    return credential


@credentials_router.delete("/{credential_id}", status_code=204)
async def delete_service_credential(
    credential_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if not await service_check_service.delete_service_credential(db, credential_id):
        raise HTTPException(status_code=404, detail="Service credential not found")
    await audit_service.write_audit_log(
        db,
        actor=current_user,
        action="delete",
        resource_type="service_credential",
        resource_id=str(credential_id),
    )
    await db.commit()


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
    current_user: User = Depends(require_operator_user),
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
    current_user: User = Depends(require_operator_user),
):
    t = await service_check_service.update_template(db, template_id, data)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@templates_router.delete("/{template_id}", status_code=204)
async def delete_service_check_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
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
    current_user: User = Depends(require_operator_user),
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
    current_user: User = Depends(require_operator_user),
):
    return await service_check_service.create_maintenance(db, data, current_user.id)


@maintenance_router.delete("/{maintenance_id}", status_code=204)
async def delete_service_check_maintenance(
    maintenance_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
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
        visible_tags=await scoping.visible_tags(db, current_user),
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
    current_user: User = Depends(require_operator_user),
):
    return await service_check_service.create_group(db, data)


@groups_router.put("/{group_id}", response_model=ServiceCheckGroupResponse)
async def update_service_check_group(
    group_id: UUID,
    data: ServiceCheckGroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    out = await service_check_service.update_group(db, group_id, data)
    if not out:
        raise HTTPException(status_code=404, detail="Group not found")
    return out


@groups_router.delete("/{group_id}", status_code=204)
async def delete_service_check_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
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
    return await service_check_service.get_service_check_summary(
        db, visible_tags=await scoping.visible_tags(db, current_user))


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


@router.get("/daily-uptime")
async def service_check_daily_uptime(
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Daily uptime percentage per check, batched for every check at once.

    Powers the per-row uptime bar strips on the services list — one rollup scan
    instead of a request per check. Sample-less days are reconstructed from the
    status log so an ingestion gap does not read as "not monitored".
    """
    return await service_check_service.get_daily_uptime_all(db, days)


@router.post("", response_model=ServiceCheckResponse, status_code=201)
async def create_service_check(
    data: ServiceCheckCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    try:
        return await service_check_service.create_service_check(db, data, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/test-config")
async def test_service_check_configuration(
    data: ServiceCheckCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    """Test an unsaved HTTP configuration without persisting it or its response body."""
    if data.check_type != "http":
        raise HTTPException(status_code=400, detail="Pre-save testing currently supports HTTP(S) checks")
    try:
        runtime_credential = await service_check_service.get_runtime_service_credential(
            db, data.credential_id
        )
        if runtime_credential and runtime_credential.get("auth_type") == "form" and len(data.workflow_steps) < 2:
            raise ValueError("Form authentication requires a sign-in step followed by a protected-page navigation step")
        from app.services.service_workflow import execute_http_workflow

        # The workflow runner consumes the same dict-shaped steps stored in JSONB.
        check = SimpleNamespace(**data.model_dump(mode="python"))
        result = await execute_http_workflow(check, runtime_credential)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result.update({
        "check_id": None,
        "check_type": "http",
        "target": data.target_url,
        "preview": True,
    })
    return result


@router.get("/{check_id}", response_model=ServiceCheckResponse)
async def get_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sc = await service_check_service.get_service_check(db, check_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    if not scoping.entity_visible(sc.tags, await scoping.visible_tags(db, current_user)):
        # 404, not 403: out-of-scope ids must not be confirmable.
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.put("/{check_id}", response_model=ServiceCheckResponse)
async def update_service_check(
    check_id: UUID,
    data: ServiceCheckUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    try:
        sc = await service_check_service.update_service_check(db, check_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not sc:
        raise HTTPException(status_code=404, detail="Service check not found")
    return sc


@router.delete("/{check_id}", status_code=204)
async def delete_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
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
    current_user: User = Depends(require_operator_user),
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


@router.get("/{check_id}/related")
async def get_related_service_checks(
    check_id: UUID,
    limit: int = Query(default=16, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Parent, dependents, and checks that share this device, host, or group."""
    out = await service_check_service.get_related_service_checks(db, check_id, limit=limit)
    if out is None:
        raise HTTPException(status_code=404, detail="Service check not found")
    return out


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
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SLA stats: uptime %, MTTR, MTBF, incident count over a time window.

    Default window is the last 30 days (720h), valid range 1h..1y. Pass `from` and `to`
    together to score an explicit span instead — `hours` alone always ends at now, which
    is the wrong window for a historical range.
    """
    out = await service_check_service.get_service_sla(db, check_id, hours, from_time, to_time)
    if out.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Service check not found")
    return out


@router.post("/{check_id}/test")
async def test_service_check(
    check_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_operator_user),
):
    """Run an on-demand test of a service check and return the result."""
    import socket
    import ssl
    import time
    import httpx

    try:
        check, runtime_credential = await service_check_service.get_runtime_service_check(db, check_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not check:
        raise HTTPException(status_code=404, detail="Service check not found")

    result = {
        "check_id": str(check_id),
        "check_type": check.check_type,
        "target": check.target_url or f"{check.target_host}:{check.target_port}",
        "status": "unknown",
        "response_time_ms": 0,
        "error": "",
        "diagnosis": None,
        "details": {},
    }

    start = time.monotonic()

    try:
        if check.check_type == "http":
            from app.services.service_workflow import execute_http_workflow
            workflow_result = await execute_http_workflow(check, runtime_credential)
            result.update(workflow_result)

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
        result["diagnosis"] = "timeout"
    except ConnectionRefusedError:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = "Connection refused"
        result["diagnosis"] = "connection_refused"
    except socket.gaierror:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = "The hostname could not be resolved"
        result["diagnosis"] = "dns"
    except ssl.SSLError as e:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = f"TLS error: {e}"
        result["diagnosis"] = "tls"
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        result["response_time_ms"] = round(elapsed, 1)
        result["status"] = "down"
        result["error"] = str(e)
        result["diagnosis"] = "unreachable" if isinstance(e, OSError) else "request"

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
