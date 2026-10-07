from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core import delegation


def actor(role="custom", scope=None):
    return SimpleNamespace(id=uuid4(), role=role, scope_tags=scope or [], username="a")


@pytest.fixture
def perms(monkeypatch):
    table = {"um": ["users.view", "users.manage", "devices.view"], "admin": ["system.admin"],
             "viewer": ["devices.view"], "scoped": ["users.manage", "devices.view"]}
    async def get(db, role): return table.get(role, [])
    monkeypatch.setattr(delegation, "get_role_permissions", get)
    async def vis(db, user): return sorted(user.scope_tags) or None
    monkeypatch.setattr(delegation, "visible_tags", vis)
    return table


@pytest.mark.asyncio
async def test_grant_ceiling(perms):
    um = actor("um")
    await delegation.assert_can_grant(None, um, ["devices.view"])
    for bad in (["system.admin"], ["roles.manage"], ["devices.view", "alerts.manage"]):
        with pytest.raises(HTTPException) as e:
            await delegation.assert_can_grant(None, um, bad)
        assert e.value.status_code == 403
    await delegation.assert_can_grant(None, actor("admin"), ["system.admin", "users.manage"])


@pytest.mark.asyncio
async def test_target_boundary_protects_higher_roles_and_accounts(perms):
    um = actor("um")
    with pytest.raises(HTTPException):
        await delegation.assert_can_manage_role_target(None, um, ["system.admin"])
    await delegation.assert_can_manage_role_target(None, um, ["devices.view"])
    victim = SimpleNamespace(id=uuid4(), scope_tags=[])
    with pytest.raises(HTTPException):
        await delegation.assert_can_manage_user(None, um, victim, ["system.admin"])
    await delegation.assert_can_manage_user(None, um, victim, ["devices.view"])
    await delegation.assert_can_manage_user(None, um, SimpleNamespace(id=um.id, scope_tags=[]), ["system.admin"])  # self


@pytest.mark.asyncio
async def test_scoped_actor_cannot_widen_scope_or_manage_unrestricted_accounts(perms):
    s = actor("scoped", scope=["web"])
    with pytest.raises(HTTPException):
        await delegation.validate_assigned_scope(None, s, [])                 # would create an unrestricted account
    with pytest.raises(HTTPException):
        await delegation.validate_assigned_scope(None, s, ["web", "db"])      # outside own scope
    assert await delegation.validate_assigned_scope(None, s, ["web"]) == ["web"]
    with pytest.raises(HTTPException):
        await delegation.assert_can_manage_user(None, s, SimpleNamespace(id=uuid4(), scope_tags=[]), ["devices.view"])
    await delegation.assert_can_manage_user(None, s, SimpleNamespace(id=uuid4(), scope_tags=["web"]), ["devices.view"])
    assert await delegation.validate_assigned_scope(None, actor("admin"), []) == []


# ── role scope (migrate-154) ─────────────────────────────────────────────────

def role(name, perms=("devices.view",), scope=()):
    return SimpleNamespace(name=name, permissions=list(perms), scope_tags=list(scope))


@pytest.mark.asyncio
async def test_scoped_actor_only_assigns_roles_scoped_within_their_own(perms):
    s = actor("scoped", scope=["web"])
    roles = [role("plain"), role("web_team", scope=["Web"]), role("db_team", scope=["db"]),
             role("mixed", scope=["web", "db"])]
    assert await delegation.assignable_role_names(None, s, roles) == {"plain", "web_team"}
    await delegation.assert_can_assign_role(None, s, roles[1])
    for bad in roles[2:]:
        with pytest.raises(HTTPException) as e:
            await delegation.assert_can_assign_role(None, s, bad)
        assert e.value.status_code == 403
    # An unscoped actor is not limited by role scopes.
    assert await delegation.assignable_role_names(None, actor("um"), roles) == {r.name for r in roles}


@pytest.mark.asyncio
async def test_scoped_actor_cannot_widen_or_lift_a_role_scope(perms):
    s = actor("scoped", scope=["web"])
    assert await delegation.validate_role_scope(None, s, [], ["web"]) == ["web"]
    assert await delegation.validate_role_scope(None, s, ["web"], ["WEB"]) == ["WEB"]   # unchanged
    with pytest.raises(HTTPException):
        await delegation.validate_role_scope(None, s, [], ["db"])            # outside own scope
    with pytest.raises(HTTPException):
        await delegation.validate_role_scope(None, s, ["web"], [])           # lifting widens members
    with pytest.raises(HTTPException):
        await delegation.validate_role_scope(None, s, ["db"], ["web"])       # another team's role
    assert await delegation.validate_role_scope(None, actor("admin"), ["db"], []) == []


@pytest.mark.asyncio
async def test_role_scope_satisfies_the_scoped_actor_account_rule(perms):
    s = actor("scoped", scope=["web"])
    # No tags of its own, but the role already limits the account to "web".
    assert await delegation.validate_assigned_scope(None, s, [], role_scope=["web"]) == []
    await delegation.assert_effective_scope_within(None, s, [], ["web"])
    with pytest.raises(HTTPException):
        await delegation.assert_effective_scope_within(None, s, [], [])      # would be unrestricted
    with pytest.raises(HTTPException):
        await delegation.assert_effective_scope_within(None, s, ["web"], ["db"])
    await delegation.assert_effective_scope_within(None, actor("admin"), [], [])
