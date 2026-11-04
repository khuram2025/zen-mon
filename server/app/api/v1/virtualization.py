from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.crypto import decrypt_config, encrypt_config
from app.core.database import get_db, get_ch_client
from app.core.scoping import entity_visible, visible_tags
from app.core.security import require_permission, require_admin_user
from app.models.user import User
from app.models.virtualization import VirtualizationConnector
from app.services.audit_service import write_audit_log
from app.services.virtualization.common import ConnectionConfig, ConnectorError
from app.services.virtualization_service import collect

class CredentialSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Pydantic model-level errors otherwise echo the complete input, including secrets.
                raise HTTPException(422, [{k: e[k] for k in ("loc", "msg", "type")}
                                          for e in exc.errors()]) from None
        return safe


router = APIRouter(prefix="/virtualization", tags=["Virtualization"], route_class=CredentialSafeRoute)
_test_slots = asyncio.Semaphore(2)


class ConnectorWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=150)
    connection: ConnectionConfig
    test_token: str
    interval_seconds: int = Field(default=300, ge=60, le=3600)
    tags: list[str] = Field(default_factory=list, max_length=50)


def fingerprint(config):
    value = json.dumps(config.private_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(get_settings().JWT_SECRET.encode(), value, hashlib.sha256).hexdigest()


def test_token(config, user):
    return jwt.encode(dict(sub=str(user.id), purpose="virtualization-test", fingerprint=fingerprint(config),
        exp=datetime.now(timezone.utc) + timedelta(minutes=10)), get_settings().JWT_SECRET, algorithm="HS256")


def require_test(payload, user):
    try:
        data = jwt.decode(payload.test_token, get_settings().JWT_SECRET, algorithms=["HS256"],
                          options={"require": ["exp", "sub", "purpose", "fingerprint"]})
        if (data["sub"] != str(user.id) or data["purpose"] != "virtualization-test"
                or not hmac.compare_digest(data["fingerprint"], fingerprint(payload.connection))):
            raise ValueError()
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(400, "Test this exact connection successfully before saving (test expires after 10 minutes)") from None


def public(row, *, detail=False):
    stale = (not row.enabled or row.status == "error" or not row.last_success_at or
             datetime.now(timezone.utc) - row.last_success_at > timedelta(seconds=row.interval_seconds * 2 + 30))
    out = {key: getattr(row, key) for key in ("id", "name", "provider", "endpoint", "tags", "enabled",
        "interval_seconds", "status", "last_error", "last_attempt_at", "last_success_at", "summary")}
    out["stale"] = stale
    if detail:
        out.update(row.snapshot or {})
    return out


async def accessible(db, user, connector_id, *, lock=False):
    statement = select(VirtualizationConnector).where(VirtualizationConnector.id == connector_id)
    if lock:
        statement = statement.with_for_update(nowait=True)
    try:
        row = (await db.execute(statement)).scalar_one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            await db.rollback()
            raise HTTPException(409, "Collection is in progress; retry this change when it completes") from None
        raise
    if not row or not entity_visible(row.tags, await visible_tags(db, user)):
        raise HTTPException(404, "Connector not found")
    return row


@router.post("/test")
async def test_connection(payload: ConnectionConfig, user: User = Depends(require_admin_user)):
    try:
        await asyncio.wait_for(_test_slots.acquire(), timeout=1)
    except TimeoutError:
        raise HTTPException(429, "Two connection tests are already running; try again shortly") from None
    try:
        result = await collect(payload, timeout=90)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        _test_slots.release()
    return {"success": True, "test_token": test_token(payload, user), **result.summary()}


@router.get("/connectors")
async def list_connectors(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("servers.view"))):
    scope = await visible_tags(db, user)
    rows = (await db.execute(select(VirtualizationConnector).order_by(VirtualizationConnector.name))).scalars().all()
    return [public(r) for r in rows if entity_visible(r.tags, scope)]


@router.post("/connectors", status_code=201)
async def create_connector(payload: ConnectorWrite, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    require_test(payload, user)
    row = VirtualizationConnector(name=payload.name, provider=payload.connection.provider,
        endpoint=payload.connection.endpoint, config=encrypt_config(payload.connection.private_dict()),
        interval_seconds=payload.interval_seconds, tags=payload.tags)
    db.add(row)
    await db.flush()
    await write_audit_log(db, actor=user, action="virtualization.create", resource_type="virtualization_connector",
                          resource_id=str(row.id), metadata={"provider": row.provider})
    await db.commit()
    await db.refresh(row)
    return public(row)


@router.get("/connectors/{connector_id}")
async def connector_detail(connector_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("servers.view"))):
    return public(await accessible(db, user, connector_id), detail=True)


@router.get("/connectors/{connector_id}/configuration")
async def configuration(connector_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    row = await accessible(db, user, connector_id)
    config = decrypt_config(row.config)
    config.pop("secret", None)
    return config


@router.put("/connectors/{connector_id}")
async def update_connector(connector_id: uuid.UUID, payload: ConnectorWrite, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    require_test(payload, user)
    row = await accessible(db, user, connector_id, lock=True)
    if row.provider != payload.connection.provider or row.endpoint != payload.connection.endpoint:
        raise HTTPException(400, "Create a new connector to change platform or endpoint; this preserves resource history identity")
    # A changed credential can have a narrower visibility scope.
    row.snapshot, row.summary, row.last_success_at = {}, {}, None
    row.name, row.provider, row.endpoint = payload.name, payload.connection.provider, payload.connection.endpoint
    row.config = encrypt_config(payload.connection.private_dict())
    row.interval_seconds, row.tags = payload.interval_seconds, payload.tags
    row.status, row.last_error = "pending", None
    row.next_poll_at = row.updated_at = datetime.now(timezone.utc)
    await write_audit_log(db, actor=user, action="virtualization.update", resource_type="virtualization_connector", resource_id=str(row.id))
    await db.commit()
    return public(row)


class EnabledWrite(BaseModel):
    enabled: bool


@router.patch("/connectors/{connector_id}")
async def set_enabled(connector_id: uuid.UUID, payload: EnabledWrite, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    row = await accessible(db, user, connector_id, lock=True)
    row.enabled = payload.enabled
    row.next_poll_at = datetime.now(timezone.utc)
    await write_audit_log(db, actor=user, action="virtualization.enable" if row.enabled else "virtualization.disable",
                         resource_type="virtualization_connector", resource_id=str(row.id))
    await db.commit()
    return public(row)


@router.post("/connectors/{connector_id}/poll", status_code=202)
async def poll_now(connector_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    row = await accessible(db, user, connector_id, lock=True)
    if not row.enabled:
        raise HTTPException(400, "Enable the connector before polling")
    row.next_poll_at = datetime.now(timezone.utc)
    await db.commit()
    return {"queued": True}


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(connector_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    row = await accessible(db, user, connector_id, lock=True)
    await db.delete(row)
    await write_audit_log(db, actor=user, action="virtualization.delete", resource_type="virtualization_connector", resource_id=str(row.id))
    await db.commit()


@router.get("/connectors/{connector_id}/metrics")
async def metric_history(connector_id: uuid.UUID, resource_id: str = Query(max_length=512),
        hours: int = Query(default=24, ge=1, le=720), db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("servers.view"))):
    row = await accessible(db, user, connector_id)
    if not any(r["id"] == resource_id for r in (row.snapshot or {}).get("resources", [])):
        raise HTTPException(404, "Resource not found in this connector's current inventory")
    def query():
        return get_ch_client().query("""SELECT toStartOfInterval(timestamp, toIntervalMinute({bucket:UInt16})) AS time,
            metric, avg(value) AS value FROM virtualization_metrics
            WHERE connector_id = {id:UUID} AND resource_id = {resource:String}
              AND timestamp >= now() - toIntervalHour({hours:UInt16})
            GROUP BY time, metric ORDER BY time LIMIT 50000""",
            parameters={"id": str(connector_id), "resource": resource_id, "hours": hours,
                        "bucket": 60 if hours > 72 else 5}).named_results()
    try:
        return await asyncio.to_thread(lambda: list(query()))
    except Exception:
        raise HTTPException(503, "Virtualization metric history is unavailable") from None
