"""Custom page-access roles (migrate-185): the restriction model, the role
lookup and delegation, without a database.

The restriction vectors are the same as dashboard/scripts/test-page-access.mjs,
so the sidebar preview and the API agree on what a page-limited role keeps.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core import delegation, page_access, security
from app.core.page_access import PageAccessError, restrict_permissions, validate, within
from app.core.permissions import ALL_PERMISSIONS
from app.core.security import get_role_page_access, get_role_permissions, invalidate_role_cache


def limited(*pages, maps=None):
    return {"pages": list(pages), "maps": maps}


# ── catalog ────────────────────────────────────────────────────────────────────

def test_catalog_is_the_navigation_tree():
    pages = page_access.pages()
    assert len(pages) > 50
    for key in ("/", "/devices", "/alerts", "/maps/manual", "/netflow/forensics", "/dashboards/servers",
                "/settings/general?tab=users"):
        assert key in pages, key
    assert {k for k, p in pages.items() if p.personal} == {
        "/settings/general?tab=appearance", "/settings/general?tab=support", "/settings/general?tab=profile"}
    known = set(ALL_PERMISSIONS)
    for p in pages.values():
        for any_of, all_of in p.gates:
            assert set(any_of) | set(all_of) <= known, p.key
        assert set(p.uses) <= known and all(u.endswith(".view") for u in p.uses), p.key
    # Dashboards only display data.
    assert all(pages[k].view_only for k in ("/", "/dashboards/network", "/dashboards/servers", "/dashboards/apps"))
    assert not pages["/devices"].view_only


# ── restriction vectors (mirrored in test-page-access.mjs) ─────────────────────

HELPDESK = ["dashboard.view", "devices.view", "devices.manage", "alerts.view", "alerts.acknowledge",
            "servers.view", "apm.view", "reports.view", "ncm.view", "audit.view"]


def r(perms, pages):
    return sorted(restrict_permissions(perms, None if pages is None else {"pages": pages, "maps": None}))


def test_no_limit_keeps_every_permission():
    assert r(HELPDESK, None) == sorted(HELPDESK)
    assert restrict_permissions(HELPDESK, None) == HELPDESK
    assert restrict_permissions(HELPDESK, {"pages": None, "maps": ["x"]}) == HELPDESK  # a map limit alone


def test_pages_keep_what_they_need_and_what_extends_it():
    assert r(HELPDESK, ["/devices", "/alerts"]) == ["alerts.acknowledge", "alerts.view", "devices.manage", "devices.view"]


def test_a_view_only_dashboard_keeps_read_access_for_its_widgets_only():
    assert r(HELPDESK, ["/"]) == ["alerts.view", "apm.view", "dashboard.view", "devices.view", "reports.view"]


def test_listed_pages_the_permissions_cannot_open_contribute_nothing():
    assert r(["devices.view"], ["/servers", "/devices"]) == ["devices.view"]
    assert r(["servers.view"], ["/devices"]) == []


def test_personal_pages_and_empty_lists_keep_nothing():
    assert r(HELPDESK, ["/settings/general?tab=profile"]) == []
    assert r(HELPDESK, []) == []


def test_child_pages_carry_their_parent_gate_and_dependencies():
    assert r(["discovery.view", "discovery.run", "devices.manage", "devices.view"], ["/discovery/credentials"]) == [
        "devices.manage", "devices.view", "discovery.run", "discovery.view"]
    assert r(["devices.manage", "devices.view"], ["/discovery/credentials"]) == []


def test_full_administration_is_never_limited():
    assert r(["system.admin"], ["/devices"]) == ["system.admin"]


def test_unknown_page_keys_open_nothing():
    assert r(HELPDESK, ["/no-such-page"]) == []


# ── validation ─────────────────────────────────────────────────────────────────

def test_validate_cleans_and_refuses():
    assert validate(None) is None
    assert validate({"pages": None, "maps": None}) is None
    mid = str(uuid4())
    assert validate({"pages": ["/devices", "/alerts", "/devices", "/settings/general?tab=profile"],
                     "maps": [mid.upper()]}) == {"pages": ["/alerts", "/devices"], "maps": [mid]}
    assert validate({"pages": []}) == {"pages": [], "maps": None}
    for bad in ({"pages": ["/nope"]}, {"pages": "/devices"}, {"maps": ["not-a-uuid"]}, {"extra": 1}, ["x"],
                {"pages": ["/devices"] * 201}):
        with pytest.raises(PageAccessError):
            validate(bad)


def test_within():
    a = str(uuid4())
    b = str(uuid4())
    assert within(None, None) and within(limited("/devices"), None)
    assert not within(None, limited("/devices"))
    assert within(limited("/devices"), limited("/devices", "/alerts"))
    assert not within(limited("/devices", "/servers"), limited("/devices"))
    assert within(limited("/settings/general?tab=profile"), limited("/devices")), "personal pages are always open"
    assert within({"pages": None, "maps": [a]}, {"pages": None, "maps": [a, b]})
    assert not within({"pages": None, "maps": None}, {"pages": None, "maps": [a]})
    assert within(limited("/devices", maps=[a]), {"pages": None, "maps": [a]})


# ── role lookup (security.get_role_permissions) ────────────────────────────────

class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Res:
    def __init__(self, row): self.row = row
    def first(self): return self.row


class RolesDB:
    """Answers the two role lookups; ``page`` may be an exception."""

    def __init__(self, perms, page):
        self.perms, self.page = perms, page

    def begin_nested(self): return Tx()

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT permissions FROM roles" in sql:
            return Res((self.perms,))
        if "SELECT page_access FROM roles" in sql:
            if isinstance(self.page, Exception):
                raise self.page
            return Res((self.page,))
        raise AssertionError(sql)


class UndefinedColumnError(Exception):
    pass


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch):
    import conftest  # noqa: F401
    monkeypatch.setattr(security, "get_role_permissions", get_role_permissions)
    invalidate_role_cache()
    yield
    invalidate_role_cache()


@pytest.mark.asyncio
async def test_lookup_narrows_permissions_of_a_page_limited_role():
    db = RolesDB(HELPDESK, {"pages": ["/devices"], "maps": None})
    assert sorted(await get_role_permissions(db, "helpdesk")) == ["devices.manage", "devices.view"]
    assert await get_role_page_access(db, "helpdesk") == {"pages": ["/devices"], "maps": None}


@pytest.mark.asyncio
async def test_unlimited_role_keeps_its_list_unchanged():
    assert await get_role_permissions(RolesDB(HELPDESK, None), "plain") == HELPDESK


@pytest.mark.asyncio
async def test_page_lookup_failure_fails_closed():
    assert await get_role_permissions(RolesDB(HELPDESK, RuntimeError("connection reset")), "x") == []
    invalidate_role_cache()
    assert await get_role_page_access(RolesDB(HELPDESK, RuntimeError("down")), "x") == {"pages": [], "maps": []}


@pytest.mark.asyncio
async def test_before_the_migration_there_are_no_page_limits():
    db = RolesDB(HELPDESK, UndefinedColumnError('column "page_access" does not exist'))
    assert await get_role_permissions(db, "x") == HELPDESK
    assert await get_role_page_access(db, "x") is None


@pytest.mark.asyncio
async def test_page_lookup_failure_keeps_the_last_value(monkeypatch):
    assert await get_role_page_access(RolesDB([], {"pages": ["/alerts"]}), "x") == {"pages": ["/alerts"], "maps": None}
    monkeypatch.setattr(security, "ROLE_POLICY_MAX_STALENESS_SECONDS", -1)
    assert await get_role_page_access(RolesDB([], RuntimeError("down")), "x") == {"pages": ["/alerts"], "maps": None}


# ── route policy: hidden pages' APIs are refused ───────────────────────────────

@pytest.mark.asyncio
async def test_route_policy_refuses_the_module_of_an_unlisted_page():
    from app.core import route_policy

    db = RolesDB(HELPDESK, {"pages": ["/devices"], "maps": None})
    perms = await get_role_permissions(db, "helpdesk")
    assert route_policy._grants(perms, ("devices.view",))
    for module in ("servers", "alerts", "apm", "reports", "ncm"):
        read, write = route_policy._POLICIES[module]
        assert not route_policy._grants(perms, read), module
    # Shared reference data stays readable (any *.view).
    assert route_policy._grants(perms, (route_policy.ANY_VIEW,))


# ── delegation ─────────────────────────────────────────────────────────────────

def _actor(role):
    return SimpleNamespace(id=uuid4(), role=role, scope_tags=[], username="a")


@pytest.fixture
def table(monkeypatch):
    perms = {"lead": ["users.manage", "users.view", "roles.view", "roles.manage", "devices.view", "alerts.view"],
             "admin": ["system.admin"]}
    access = {"lead": {"pages": ["/devices", "/alerts", "/settings/general?tab=users"], "maps": None}}

    async def get_perms(db, role): return perms.get(role, [])
    async def get_access(db, role): return access.get(role)
    async def vis(db, user): return None
    monkeypatch.setattr(delegation, "get_role_permissions", get_perms)
    monkeypatch.setattr(delegation, "get_role_page_access", get_access)
    monkeypatch.setattr(delegation, "visible_tags", vis)
    return access


def _role(name, access=None, perms=("devices.view",)):
    return SimpleNamespace(name=name, permissions=list(perms), scope_tags=[], page_access=access)


@pytest.mark.asyncio
async def test_page_limited_manager_only_assigns_roles_within_their_pages(table):
    lead = _actor("lead")
    roles = [_role("open"), _role("desk", {"pages": ["/devices"], "maps": None}),
             _role("wide", {"pages": ["/devices", "/servers"], "maps": None})]
    assert await delegation.assignable_role_names(None, lead, roles) == {"desk"}
    await delegation.assert_can_assign_role(None, lead, roles[1])
    for bad in (roles[0], roles[2]):
        with pytest.raises(HTTPException) as e:
            await delegation.assert_can_assign_role(None, lead, bad)
        assert e.value.status_code == 403
    # Full administration is not limited by anything.
    assert await delegation.assignable_role_names(None, _actor("admin"), roles) == {"open", "desk", "wide"}


@pytest.mark.asyncio
async def test_page_limited_manager_cannot_lift_or_widen_a_page_limit(table):
    lead = _actor("lead")
    await delegation.validate_role_page_access(None, lead, None, {"pages": ["/devices"], "maps": None})
    await delegation.validate_role_page_access(None, lead, {"pages": ["/devices"]}, {"pages": ["/alerts"]})
    for before, after in (({"pages": ["/devices"]}, None),
                          (None, {"pages": ["/servers"]}),
                          ({"pages": ["/servers"]}, {"pages": ["/devices"]})):
        with pytest.raises(HTTPException) as e:
            await delegation.validate_role_page_access(None, lead, before, after)
        assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        await delegation.validate_role_page_access(None, lead, None, None, creating=True)   # a new unlimited role
    await delegation.validate_role_page_access(None, lead, None, {"pages": ["/alerts"]}, creating=True)
    # Unchanged is always fine (editing the name of a role that is already within limits).
    await delegation.validate_role_page_access(None, lead, None, None)
    await delegation.validate_role_page_access(None, _actor("admin"), {"pages": ["/servers"]}, None)
