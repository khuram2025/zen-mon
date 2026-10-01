"""Last-administrator invariant (RP-08, UA-16).

"At least one *active* account holds a role with Full administration" must
survive every change — user demote / disable / delete, role permission edits
and concurrent requests. The pattern is lock → mutate → verify:

    await lock_admin_invariant(db)
    before = await count_active_full_admins(db)
    ...apply the change and db.flush()...
    await assert_admin_survives(db, before)

The transaction-scoped advisory lock serialises every administrator-affecting
change fleet-wide, so two individually valid concurrent requests cannot both
pass a count-then-write check.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SUPERUSER_PERMISSION

_LOCK_KEY = 0x5A50_4144_4D49  # "ZPADMI"

_ADMIN_ROLE_PREDICATE = "permissions::jsonb @> CAST(:perm AS jsonb)"


async def lock_admin_invariant(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})


async def full_admin_role_names(db: AsyncSession) -> set[str]:
    rows = (await db.execute(
        text(f"SELECT name FROM roles WHERE {_ADMIN_ROLE_PREDICATE}"),
        {"perm": f'["{SUPERUSER_PERMISSION}"]'},
    )).scalars().all()
    return set(rows)


async def count_active_full_admins(db: AsyncSession) -> int:
    return int((await db.execute(
        text(
            "SELECT count(*) FROM users u JOIN roles r ON r.name = u.role "
            f"WHERE u.is_active IS TRUE AND r.{_ADMIN_ROLE_PREDICATE}"
        ),
        {"perm": f'["{SUPERUSER_PERMISSION}"]'},
    )).scalar() or 0)


async def assert_admin_survives(db: AsyncSession, before: int) -> None:
    """Raise 409 when the change just flushed removed the last active full
    administrator. Installations that already had none are not blocked."""
    if before <= 0:
        return
    after = await count_active_full_admins(db)
    if after <= 0:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This change would leave the appliance without an active full administrator",
        )
