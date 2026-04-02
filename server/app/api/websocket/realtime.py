import asyncio
import json
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
import redis.asyncio as aioredis

from app.core.config import get_settings

router = APIRouter(prefix="/stream", tags=["Real-time"])
settings = get_settings()


async def _subscribe_to_channel(request: Request, channel: str, event_type: str):
    """SSE generator that subscribes to a Redis pub/sub channel."""
    r = aioredis.from_url(settings.REDIS_URL)
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)

    try:
        while True:
            if await request.is_disconnected():
                break

            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
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
async def stream_metrics(request: Request):
    """SSE stream of live ping metrics."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:metrics", "metric")
    )


@router.get("/status")
async def stream_status(request: Request):
    """SSE stream of device status changes."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:status_change", "status_change")
    )


@router.get("/alerts")
async def stream_alerts(request: Request):
    """SSE stream of new alerts."""
    return EventSourceResponse(
        _subscribe_to_channel(request, "zenplus:alerts", "alert")
    )
