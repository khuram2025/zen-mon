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
