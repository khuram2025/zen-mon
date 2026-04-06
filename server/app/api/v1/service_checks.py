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
)
from app.services import service_check_service, service_metric_service

router = APIRouter(prefix="/service-checks", tags=["Service Checks"])


@router.get("")
async def list_service_checks(
    device_id: UUID | None = None,
    check_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await service_check_service.get_service_checks(
        db, device_id=device_id, check_type=check_type, status=status,
        search=search, skip=skip, limit=limit,
    )


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

    client.close()
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

    return result
