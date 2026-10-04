"""get_role_permissions must fail closed (RP-02, RP-12)."""
from __future__ import annotations

import pytest

from app.core import security
from app.core.security import get_role_permissions, invalidate_role_cache


class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Row:
    def __init__(self, v): self.v = v
    def __getitem__(self, i): return self.v


class Res:
    def __init__(self, row): self.row = row
    def first(self): return self.row


class DB:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.rollbacks = 0
    def begin_nested(self): return Tx()
    async def rollback(self): self.rollbacks += 1
    async def execute(self, *a, **k):
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        return Res(b)


class UndefinedTableError(Exception):
    pass


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # use the real lookup, not the conftest shim
    import conftest  # noqa: F401
    monkeypatch.setattr(security, "get_role_permissions", get_role_permissions)
    invalidate_role_cache()
    yield
    invalidate_role_cache()


@pytest.mark.asyncio
async def test_role_without_a_row_has_no_permissions_whatever_its_name():
    for name in ("owner", "admin", "editor", "anything"):
        invalidate_role_cache()
        assert await get_role_permissions(DB(None), name) == []


@pytest.mark.asyncio
async def test_lookup_error_never_widens_access_and_does_not_roll_back_the_session():
    db = DB(RuntimeError("connection reset"))
    assert await get_role_permissions(db, "admin") == []
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_lookup_error_keeps_last_valid_policy(monkeypatch):
    perms = await get_role_permissions(DB(Row(["devices.view"])), "custom")
    assert perms == ["devices.view"]
    monkeypatch.setattr(security, "ROLE_POLICY_MAX_STALENESS_SECONDS", -1)   # force a refresh
    assert await get_role_permissions(DB(RuntimeError("db down")), "custom") == ["devices.view"]


@pytest.mark.asyncio
async def test_legacy_vocabulary_only_when_the_roles_table_is_missing():
    perms = await get_role_permissions(DB(UndefinedTableError('relation "roles" does not exist')), "operator")
    assert "devices.manage" in perms
    invalidate_role_cache()
    assert await get_role_permissions(DB(UndefinedTableError('relation "roles" does not exist')), "stranger") == []
