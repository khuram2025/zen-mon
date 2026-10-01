"""Server-side session inventory (migrate-150 ``auth_sessions``).

Access tokens stay stateless JWTs, but each carries a ``jti`` that maps to a
row here so an administrator (or the user) can see where an account is signed
in and revoke one session. Global revocation (``users.token_version``) keeps
working for legacy tokens that predate the table.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_TOUCH_INTERVAL = timedelta(seconds=60)


def _is_missing_table(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    return type(orig).__name__ == "UndefinedTableError" or (
        "auth_sessions" in str(orig) and "does not exist" in str(orig))


async def create_session(
    db: AsyncSession, user_id, *, ip: str | None, user_agent: str | None,
    auth_method: str, mfa_verified: bool, ttl: timedelta,
) -> tuple[str, datetime]:
    jti = uuid.uuid4()
    expires = datetime.now(timezone.utc) + ttl
    try:
        async with db.begin_nested():
            await db.execute(text(
                "INSERT INTO auth_sessions (id, user_id, expires_at, ip_address, user_agent, "
                "auth_method, mfa_verified) VALUES (:id, :uid, :exp, :ip, :ua, :method, :mfa)"),
                {"id": jti, "uid": user_id, "exp": expires, "ip": ip,
                 "ua": (user_agent or "")[:300] or None, "method": auth_method, "mfa": mfa_verified})
            # Housekeeping: drop sessions that expired more than a week ago.
            await db.execute(text(
                "DELETE FROM auth_sessions WHERE expires_at < NOW() - INTERVAL '7 days'"))
    except Exception as exc:
        if not _is_missing_table(exc):
            raise
    return str(jti), expires


async def assert_session_active(db: AsyncSession, jti: str, user_id) -> None:
    """401 when the session was revoked or does not exist; refreshes
    last_seen_at at most once a minute."""
    try:
        async with db.begin_nested():
            row = (await db.execute(text(
                "SELECT revoked_at, expires_at, last_seen_at FROM auth_sessions "
                "WHERE id = CAST(:id AS uuid) AND user_id = :uid"),
                {"id": jti, "uid": user_id})).first()
    except Exception as exc:
        if _is_missing_table(exc):
            return
        raise
    if row is None or row[0] is not None:
        raise HTTPException(401, "Session revoked; sign in again")
    now = datetime.now(timezone.utc)
    if row[1] is not None and row[1] < now:
        raise HTTPException(401, "Session expired; sign in again")
    if row[2] is None or now - row[2] > _TOUCH_INTERVAL:
        await db.execute(text(
            "UPDATE auth_sessions SET last_seen_at = NOW() WHERE id = CAST(:id AS uuid)"), {"id": jti})
        await db.commit()


async def list_sessions(db: AsyncSession, user_id, current_jti: Optional[str] = None) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT id, created_at, last_seen_at, expires_at, ip_address, user_agent, auth_method, "
        "mfa_verified, revoked_at, revoked_by FROM auth_sessions "
        "WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > NOW() "
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
