"""Appliance default and individual session policy. -1 preserves the configured fixed JWT lifetime."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.audit_service import AuditUnavailable, write_audit_log


async def effective_at_login(db: AsyncSession, uid) -> int:
    # Serialize policy selection and session insertion with administrative changes.
    default = (await db.execute(text(
        "SELECT minutes FROM session_timeout_default WHERE singleton FOR SHARE"))).scalar_one()
    override = (await db.execute(text(
        "SELECT session_timeout_minutes FROM users WHERE id=:id"), {"id": uid})).scalar_one()
    return default if override is None else override


async def snapshot(db: AsyncSession) -> dict:
    default = (await db.execute(text("SELECT minutes FROM session_timeout_default"))).scalar_one()
    rows = (await db.execute(text(
        "SELECT id, username, session_timeout_minutes AS minutes FROM users ORDER BY lower(username)"))).mappings()
    return {"minutes": default, "fixed_minutes": get_settings().JWT_EXPIRE_MINUTES,
            "users": [dict(row) for row in rows]}


async def change(db: AsyncSession, *, user_id: UUID | None, minutes: int | None, actor):
    default = (await db.execute(text(
        "SELECT minutes FROM session_timeout_default WHERE singleton FOR UPDATE"))).scalar_one()
    params = {"minutes": minutes, "id": user_id, "actor": actor.username}
    if user_id is None:
        previous = default
        if minutes == previous:
            return
        await db.execute(text("UPDATE session_timeout_default SET minutes=:minutes"), params)
        affected = "user_id IN (SELECT id FROM users WHERE session_timeout_minutes IS NULL)"
    else:
        row = (await db.execute(text(
            "SELECT session_timeout_minutes FROM users WHERE id=:id FOR UPDATE"), params)).first()
        if row is None:
            raise HTTPException(404, "User no longer exists")
        previous = row[0]
        if minutes == previous:
            return
        await db.execute(text("UPDATE users SET session_timeout_minutes=:minutes WHERE id=:id"), params)
        affected = "user_id=:id"
    if (previous if previous is not None else default) != (minutes if minutes is not None else default):
        await db.execute(text(
            "UPDATE auth_sessions SET revoked_at=NOW(), revoked_by=:actor, revoke_reason='session timeout changed' "
            "WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at>NOW()) AND " + affected), params)
    try:
        await write_audit_log(db, actor=actor, action="security.session_timeout.update",
                              resource_type="user" if user_id else "system_setting",
                              resource_id=str(user_id or "session_timeout"), required=True,
                              metadata={"previous_minutes": previous, "minutes": minutes})
    except AuditUnavailable as exc:
        await db.rollback()
        raise HTTPException(503, "Session timeout change could not be audited") from exc
    await db.commit()
