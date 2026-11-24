"""Server-side session inventory (migrate-150 ``auth_sessions``).

Every interactive JWT names a database session. Its deadline is fixed, extended
by explicit dashboard activity, or absent for a no-timeout session. All three
modes enforce account status, token version and server-side revocation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_TOUCH_INTERVAL = timedelta(seconds=60)


async def create_session(
    db: AsyncSession, user_id, *, ip: str | None, user_agent: str | None,
    auth_method: str, mfa_verified: bool, ttl: timedelta | None, idle_minutes: int | None,
) -> tuple[str, datetime | None]:
    jti = uuid.uuid4()
    expires = datetime.now(timezone.utc) + ttl if ttl else None
    await db.execute(text(
        "INSERT INTO auth_sessions (id, user_id, expires_at, idle_minutes, ip_address, user_agent, "
        "auth_method, mfa_verified) VALUES (:id, :uid, :exp, :idle, :ip, :ua, :method, :mfa)"),
        {"id": jti, "uid": user_id, "exp": expires, "idle": idle_minutes, "ip": ip,
         "ua": (user_agent or "")[:300] or None, "method": auth_method, "mfa": mfa_verified})
    await db.execute(text("DELETE FROM auth_sessions WHERE LEAST(revoked_at, expires_at) < NOW() - INTERVAL '7 days'"))
    return str(jti), expires


async def assert_session_active(db: AsyncSession, jti: str, user_id) -> None:
    row = (await db.execute(text(
        "SELECT last_seen_at FROM auth_sessions WHERE id=CAST(:id AS uuid) AND user_id=:uid "
        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())"),
        {"id": jti, "uid": user_id})).first()
    if row is None:
        raise HTTPException(401, "Session expired or revoked; sign in again")
    if datetime.now(timezone.utc) - row[0] > _TOUCH_INTERVAL:
        # Inventory activity includes background polling; it never moves the idle deadline.
        await db.execute(text("UPDATE auth_sessions SET last_seen_at=NOW() WHERE id=CAST(:id AS uuid)"),
                         {"id": jti})
        await db.commit()


async def record_activity(db: AsyncSession, jti: str, user_id) -> None:
    result = await db.execute(text(
        "UPDATE auth_sessions SET expires_at=clock_timestamp() + make_interval(mins => idle_minutes) "
        "WHERE id=CAST(:id AS uuid) AND user_id=:uid AND idle_minutes IS NOT NULL "
        "AND revoked_at IS NULL AND expires_at>clock_timestamp()"),
        {"id": jti, "uid": user_id})
    if result.rowcount != 1:
        raise HTTPException(401, "Idle session is no longer active")
    await db.commit()


async def list_sessions(db: AsyncSession, user_id, current_jti: Optional[str] = None) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT id, created_at, last_seen_at, expires_at, ip_address, user_agent, auth_method, "
        "mfa_verified, revoked_at, revoked_by FROM auth_sessions "
        "WHERE user_id = :uid AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW()) "
        "ORDER BY last_seen_at DESC LIMIT 50"), {"uid": user_id})).all()
    return [{
        "id": str(r[0]), "created_at": r[1], "last_seen_at": r[2], "expires_at": r[3],
        "ip_address": r[4], "user_agent": r[5], "auth_method": r[6], "mfa_verified": r[7],
        "current": current_jti is not None and str(r[0]) == current_jti,
    } for r in rows]


async def revoke_session(db: AsyncSession, session_id: str, user_id, *, by: str, reason: str = "") -> bool:
    res = await db.execute(text(
        "UPDATE auth_sessions SET revoked_at = NOW(), revoked_by = :by, revoke_reason = :why "
        "WHERE id = CAST(:id AS uuid) AND user_id = :uid AND revoked_at IS NULL"),
        {"id": session_id, "uid": user_id, "by": by, "why": reason or None})
    return (res.rowcount or 0) > 0


async def revoke_all_sessions(db: AsyncSession, user_id, *, by: str, reason: str = "") -> int:
    """Mark every session revoked AND bump token_version so legacy (jti-less)
    tokens die too."""
    res = await db.execute(text(
        "UPDATE auth_sessions SET revoked_at = NOW(), revoked_by = :by, revoke_reason = :why "
        "WHERE user_id = :uid AND revoked_at IS NULL"),
        {"uid": user_id, "by": by, "why": reason or None})
    await db.execute(text("UPDATE users SET token_version = COALESCE(token_version, 0) + 1 WHERE id = :uid"),
                     {"uid": user_id})
    return res.rowcount or 0
