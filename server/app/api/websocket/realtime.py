import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse
import redis.asyncio as aioredis

from app.core import scoping
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.security import require_permission_stream
from app.models.user import User

router = APIRouter(prefix="/stream", tags=["Real-time"])
settings = get_settings()
logger = logging.getLogger(__name__)

# How often a tag-scoped stream re-resolves the devices its user may see.
_SCOPE_REFRESH_SECONDS = 30.0


async def _visible_device_ids_for(user_id) -> Optional[set[str]]:
    """Device ids the account may see, or None when it is unrestricted.
    Uses its own short-lived session: the request session is not safe to hold
    for the lifetime of a stream."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            return set()
        scope = await scoping.visible_tags(db, user)
        if scope is None:
            return None
        return set(await scoping.visible_device_ids(db, scope))


def _event_in_scope(raw: str, allowed: Optional[set[str]]) -> bool:
    """Fail closed: for a scoped account an event is forwarded only when it names
    a device that is in scope."""
    if allowed is None:
        return True
    try:
        payload = json.loads(raw)
        device_id = payload.get("device_id") if isinstance(payload, dict) else None
    except (ValueError, TypeError):
        return False
    return bool(device_id) and str(device_id) in allowed


async def _subscribe_to_channel(request: Request, channel: str, event_type: str, user: User):
    """SSE generator that subscribes to a Redis pub/sub channel, forwarding only
    events the signed-in account is allowed to see."""
    user_id = user.id
    allowed = await _visible_device_ids_for(user_id)
    refreshed = time.monotonic()
    r = aioredis.from_url(settings.REDIS_URL)
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)

    try:
        while True:
            if await request.is_disconnected():
                break

            if allowed is not None and time.monotonic() - refreshed > _SCOPE_REFRESH_SECONDS:
                allowed = await _visible_device_ids_for(user_id)
                refreshed = time.monotonic()

            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                if _event_in_scope(data, allowed):
                    yield {
                        "event": event_type,
                        "data": data,
                    }
            else:
                # Send heartbeat to keep connection alive
                yield {"event": "heartbeat", "data": ""}
                await asyncio.sleep(1)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await r.close()


@router.get("/metrics")
async def stream_metrics(
    request: Request,
    user: User = Depends(require_permission_stream("devices.view")),
):
    """SSE stream of live ping metrics (needs Devices: View)."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:metrics", "metric", user)
    )


@router.get("/status")
async def stream_status(
    request: Request,
    user: User = Depends(require_permission_stream("devices.view")),
):
    """SSE stream of device status changes (needs Devices: View)."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:status_change", "status_change", user)
    )


@router.get("/alerts")
async def stream_alerts(
    request: Request,
    user: User = Depends(require_permission_stream("alerts.view")),
):
    """SSE stream of new alerts (needs Alerting: View)."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:alerts", "alert", user)
    )
