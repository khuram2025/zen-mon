"""Delegation boundaries (RP-01, RP-03, UA-06).

An actor may only hand out, or touch, access that does not exceed their own:

* **grant ceiling** - a role / account may only be given permissions the
  actor holds themselves (Full administration holds everything);
* **target boundary** - an actor may only modify, reset, disable or delete an
  account or role whose current access is within their own ceiling;
* **scope ceiling** - a tag-scoped actor can only assign (and manage accounts
  restricted to) tags inside their own scope, and can never create an
  unrestricted account. The same holds for role scopes (roles.scope_tags,
  which every member inherits): a scoped actor cannot assign or edit a role
  scoped outside their own tags, nor lift a role's scope.

These checks are the server-side policy; the UI only mirrors them by listing
the assignable roles returned from ``GET /roles/assignable``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SUPERUSER_PERMISSION, exceeds
from app.core.scoping import visible_tags
from app.core.security import get_role_permissions
from app.models.user import User


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


async def actor_permissions(db: AsyncSession, actor: User) -> list[str]:
    return await get_role_permissions(db, actor.role)


async def is_full_admin(db: AsyncSession, actor: User) -> bool:
    return SUPERUSER_PERMISSION in await actor_permissions(db, actor)


async def assert_can_grant(db: AsyncSession, actor: User, permissions) -> None:
    """The actor may put ``permissions`` on a role / account."""
    missing = exceeds(permissions, await actor_permissions(db, actor))
    if missing:
        shown = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
        raise _forbidden(
            f"You cannot grant access you do not hold yourself ({shown}). "
            "Ask a user with that access, or a full administrator."
        )


async def assert_can_manage_role_target(db: AsyncSession, actor: User, role_permissions) -> None:
    """The actor may edit / rename / delete a role that currently grants
    ``role_permissions`` (a delegated manager must not be able to alter a role
    more privileged than themselves, e.g. by emptying its permission list)."""
    missing = exceeds(role_permissions, await actor_permissions(db, actor))
    if missing:
        raise _forbidden(
            "This role grants access beyond your own, so you cannot change or delete it"
        )


def _lower(tags) -> set[str]:
    return {str(t).strip().lower() for t in (tags or []) if str(t).strip()}


async def assignable_role_names(db: AsyncSession, actor: User, roles) -> set[str]:
    """Names of ``roles`` (rows with .name/.permissions/.scope_tags) the actor may assign."""
    ceiling = await actor_permissions(db, actor)
    scope = await visible_tags(db, actor)
    return {
        r.name for r in roles
        if not exceeds(r.permissions or [], ceiling)
        and (scope is None or _lower(getattr(r, "scope_tags", None)) <= set(scope))
    }


async def assert_can_assign_role(db: AsyncSession, actor: User, role) -> None:
    ceiling = await actor_permissions(db, actor)
    if exceeds(role.permissions or [], ceiling):
        raise _forbidden(
            f"You cannot assign the '{role.name}' role: it grants access beyond your own"
        )
    scope = await visible_tags(db, actor)
    if scope is not None:
        outside = sorted(_lower(getattr(role, "scope_tags", None)) - set(scope))
        if outside:
            raise _forbidden(
                f"You cannot assign the '{role.name}' role: its visibility scope includes tags "
                f"outside your own ({', '.join(outside)})"
            )


async def validate_role_scope(db: AsyncSession, actor: User, before, after: list[str]) -> list[str]:
    """Scope the actor may put on a role. A tag-scoped actor may only set tags
    inside their own scope, may not change the scope of a role scoped outside
    theirs, and may not lift a role's scope (an empty scope would widen every
    member who has no scope of their own to unrestricted)."""
    scope = await visible_tags(db, actor)
    if scope is None:
        return after
    old, new = _lower(before), _lower(after)
    if old == new:
        return after
    if old and not old <= set(scope):
        raise _forbidden("This role's visibility scope includes tags outside your own, so you cannot change it")
    if old and not new:
        raise _forbidden("You are limited to a visibility scope, so you cannot remove a role's scope")
    outside = sorted(new - set(scope))
    if outside:
        raise _forbidden(f"You cannot grant visibility of tags outside your own scope: {', '.join(outside)}")
    return after


async def assert_can_manage_user(db: AsyncSession, actor: User, target: User,
                                 target_role_permissions) -> None:
    """The actor may modify / reset / disable / delete ``target``."""
    if actor.id == target.id:
        return
    ceiling = await actor_permissions(db, actor)
    if exceeds(target_role_permissions, ceiling):
        raise _forbidden("This account has more access than you do, so you cannot change it")
    scope = await visible_tags(db, actor)
    if scope is not None:
        # The target's effective scope: their own tags plus their role's.
        target_scope = await visible_tags(db, target)
        if not target_scope or not set(target_scope) <= set(scope):
            raise _forbidden("This account can see resources outside your own visibility scope")


async def validate_assigned_scope(db: AsyncSession, actor: User, requested: list[str],
                                  role_scope=None) -> list[str]:
    """Scope the actor is allowed to put on an account. A tag-scoped actor
    cannot widen visibility: the new scope must be inside theirs, and non-empty
    unless the account's role (``role_scope``) already limits it."""
    scope = await visible_tags(db, actor)
    if scope is None:
        return requested
    wanted = {str(t).lower() for t in requested}
    if not wanted and not _lower(role_scope):
        raise _forbidden("You are limited to a visibility scope, so accounts you manage must be scoped too")
    outside = sorted(wanted - set(scope))
    if outside:
        raise _forbidden(f"You cannot grant visibility of tags outside your own scope: {', '.join(outside)}")
    return requested


async def assert_effective_scope_within(db: AsyncSession, actor: User, user_tags, role_scope) -> None:
    """After a change, the account's effective scope (its own tags plus its
    role's) must stay non-empty and inside a tag-scoped actor's scope — e.g.
    moving a user from a scoped role to an unscoped one must not leave them
    unrestricted."""
    scope = await visible_tags(db, actor)
    if scope is None:
        return
    effective = _lower(user_tags) | _lower(role_scope)
    if not effective:
        raise _forbidden("You are limited to a visibility scope, so accounts you manage must be scoped too")
    outside = sorted(effective - set(scope))
    if outside:
        raise _forbidden(f"You cannot grant visibility of tags outside your own scope: {', '.join(outside)}")
