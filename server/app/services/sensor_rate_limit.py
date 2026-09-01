"""Small Redis-backed quotas for the WAN-exposed sensor API."""

from __future__ import annotations

from collections import defaultdict, deque
import time

from fastapi import HTTPException

from app.core.config import get_settings

_WINDOW_S = 60
_buckets: dict[str, deque[float]] = defaultdict(deque)
_redis = None
_redis_disabled_until = 0.0


async def _redis_client():
    global _redis, _redis_disabled_until
    if time.monotonic() < _redis_disabled_until:
        return None
    if _redis is None:
        try:
            import redis.asyncio as redis
            _redis = redis.from_url(
                get_settings().REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=0.15,
                socket_timeout=0.15,
            )
        except Exception:
            _redis_disabled_until = time.monotonic() + 30
            return None
    return _redis


def _fallback(key: str, amount: int, limit: int) -> None:
    now = time.monotonic()
    if len(_buckets) > 10_000:
        cutoff = now - _WINDOW_S
        for bucket_key, bucket in list(_buckets.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                _buckets.pop(bucket_key, None)
        while len(_buckets) > 8_000:
            _buckets.pop(next(iter(_buckets)), None)
    bucket = _buckets[key]
    cutoff = now - _WINDOW_S
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) + amount > limit:
        raise HTTPException(429, "Sensor API quota exceeded", headers={"Retry-After": "60"})
    bucket.extend([now] * amount)


async def enforce_sensor_quota(scope: str, identity: str, *, amount: int, limit: int) -> None:
    """Charge a fixed-window quota, with a bounded per-worker fallback."""
    global _redis_disabled_until
    amount = max(1, amount)
    key = f"zp:sensor:q:{scope}:{identity}"
    client = await _redis_client()
    if client is not None:
        try:
            count = await client.incrby(key, amount)
            if int(count) == amount:
                await client.expire(key, _WINDOW_S + 2)
            if int(count) > limit:
                raise HTTPException(429, "Sensor API quota exceeded", headers={"Retry-After": "60"})
            return
        except HTTPException:
            raise
        except Exception:
            _redis_disabled_until = time.monotonic() + 30
    _fallback(key, amount, limit)
