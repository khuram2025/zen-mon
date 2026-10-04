"""Sign-in policy (system_settings key ``auth.policy``): multi-factor
requirement, failed-login lockout. Cached briefly so the per-request check in
the auth dependency costs nothing."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SUPERUSER_PERMISSION

POLICY_KEY = "auth.policy"
_CACHE_TTL = 15.0
_cache: tuple[float, dict] | None = None

DEFAULT_POLICY: dict[str, Any] = {
    # off | admins (every account whose role has Full administration) | all_local
    "mfa_required": "off",
    "lockout_threshold": 10,        # consecutive failures before the account locks
    "lockout_minutes": 15,
    "revision": 0,
}

MFA_MODES = ("off", "admins", "all_local")


def normalize(raw: dict | None) -> dict:
    policy = dict(DEFAULT_POLICY)
    if isinstance(raw, dict):
        for key in DEFAULT_POLICY:
            if key in raw:
                policy[key] = raw[key]
    if policy["mfa_required"] not in MFA_MODES:
        policy["mfa_required"] = "off"
    policy["lockout_threshold"] = max(3, min(int(policy["lockout_threshold"] or 10), 100))
    policy["lockout_minutes"] = max(1, min(int(policy["lockout_minutes"] or 15), 24 * 60))
    return policy


def invalidate() -> None:
    global _cache
    _cache = None


async def get_policy(db: AsyncSession) -> dict:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    try:
        async with db.begin_nested():
            row = (await db.execute(
                text("SELECT value FROM system_settings WHERE key = :k"), {"k": POLICY_KEY}
            )).first()
        policy = normalize(row[0] if row else None)
    except Exception:
        policy = normalize(None)
    _cache = (now, policy)
    return policy


async def mfa_required_for(db: AsyncSession, user) -> bool:
    mode = (await get_policy(db))["mfa_required"]
    if mode == "off" or getattr(user, "auth_source", "local") != "local":
        return False
    if mode == "all_local":
        return True
    from app.core.security import get_role_permissions
    return SUPERUSER_PERMISSION in await get_role_permissions(db, user.role)
