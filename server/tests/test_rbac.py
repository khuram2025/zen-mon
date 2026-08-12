"""RBAC: permission catalog, role management guards, and permission deps."""

from __future__ import annotations

import pytest

from app.core.security import get_current_user, invalidate_role_cache
from app.core.permissions import (
    ALL_PERMISSIONS,
    LEGACY_ROLE_PERMISSIONS,
    SUPERUSER_PERMISSION,
    has_permission,
)
from app.main import app
from app.services.external_auth import map_ldap_role, map_radius_role

from conftest import FakeDB, make_user


class RoleRow:
    """Row shim for `SELECT permissions FROM roles WHERE name = :name`."""

    def __init__(self, perms):
        self.perms = perms

    def __getitem__(self, idx):
        return self.perms


class EmptyScalars:
    def scalars(self):
        return self

    def all(self):
        return []

    def scalar(self):
        return None

    def first(self):
        return None


class RbacFakeDB(FakeDB):
    """FakeDB that can answer the role-permission lookup."""

    def __init__(self, role_perms: dict[str, list[str]] | None = None):
        self.role_perms = role_perms or {}

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT permissions FROM roles" in sql:
            perms = self.role_perms.get((params or {}).get("name"))
            if perms is not None:
                from conftest import RowResult
                return RowResult(RoleRow(perms))
        if "FROM users" in sql and "SELECT users." in sql:
            return EmptyScalars()
        return await super().execute(statement, params)

    async def rollback(self):
        return None


@pytest.fixture(autouse=True)
def fresh_role_cache():
    invalidate_role_cache()
    yield
    invalidate_role_cache()


def use_db(role_perms=None):
    async def fake_db():
        yield RbacFakeDB(role_perms)
    return fake_db


# ── permission primitives ─────────────────────────────────────────────────────

def test_superuser_implies_everything():
    for perm in ALL_PERMISSIONS:
        assert has_permission([SUPERUSER_PERMISSION], perm)


def test_legacy_roles_cover_expected_surfaces():
    assert SUPERUSER_PERMISSION in LEGACY_ROLE_PERMISSIONS["admin"]
    assert "devices.manage" in LEGACY_ROLE_PERMISSIONS["operator"]
    assert SUPERUSER_PERMISSION not in LEGACY_ROLE_PERMISSIONS["operator"]
    assert "devices.manage" not in LEGACY_ROLE_PERMISSIONS["viewer"]
    assert "alerts.acknowledge" not in LEGACY_ROLE_PERMISSIONS["read_only"]


# ── role management API guards ────────────────────────────────────────────────

def test_viewer_cannot_manage_roles(client, as_viewer):
    r = client.post("/api/v1/roles", json={"display_name": "Helpdesk", "permissions": []})
    assert r.status_code == 403
    r = client.put("/api/v1/roles/00000000-0000-0000-0000-000000000001", json={"description": "x"})
    assert r.status_code == 403
    r = client.delete("/api/v1/roles/00000000-0000-0000-0000-000000000001")
    assert r.status_code == 403


def test_viewer_cannot_read_auth_providers(client, as_viewer):
    assert client.get("/api/v1/system/auth/providers").status_code == 403


def test_create_role_rejects_unknown_permission(client, as_admin):
    r = client.post(
        "/api/v1/roles",
        json={"display_name": "Helpdesk", "permissions": ["devices.fly"]},
    )
    assert r.status_code == 400
    assert "Unknown permissions" in r.json()["detail"]


def test_create_role_rejects_bad_name(client, as_admin):
    r = client.post(
        "/api/v1/roles",
        json={"name": "Bad Name!", "display_name": "Helpdesk", "permissions": []},
    )
    assert r.status_code == 400


def test_catalog_lists_modules(client, as_admin):
    r = client.get("/api/v1/roles/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["superuser_permission"] == SUPERUSER_PERMISSION
    ids = [p["id"] for m in body["modules"] for p in m["permissions"]]
    assert ids == ALL_PERMISSIONS


# ── DB-backed custom roles drive the permission dependency ────────────────────

def test_custom_role_grants_mapped_permissions(client):
    async def override():
        return make_user("helpdesk")

    app.dependency_overrides[get_current_user] = override
    from app.core.database import get_db
    app.dependency_overrides[get_db] = use_db({"helpdesk": ["users.view", "users.manage"]})
    try:
        # users.manage → may list users
        assert client.get("/api/v1/users").status_code == 200
        # but not an admin surface (require_admin_user needs system.admin)
        assert client.get("/api/v1/system/auth/providers").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_custom_role_without_grants_is_denied(client):
    async def override():
        return make_user("intern")

    app.dependency_overrides[get_current_user] = override
    from app.core.database import get_db
    app.dependency_overrides[get_db] = use_db({"intern": ["dashboard.view"]})
    try:
        assert client.get("/api/v1/users").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_custom_role_with_system_admin_passes_admin_gate(client):
    async def override():
        return make_user("superops")

    app.dependency_overrides[get_current_user] = override
    from app.core.database import get_db
    app.dependency_overrides[get_db] = use_db({"superops": [SUPERUSER_PERMISSION]})
    try:
        assert client.get("/api/v1/system/auth/providers").status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── external auth role mapping ────────────────────────────────────────────────

def test_map_ldap_role_matches_dn_and_cn():
    cfg = {
        "group_mappings": [
            {"group": "CN=Net Admins,OU=Groups,DC=corp,DC=local", "role": "admin"},
            {"group": "helpdesk", "role": "viewer"},
        ],
        "default_role": "",
    }
    assert map_ldap_role(cfg, ["cn=net admins,ou=groups,dc=corp,dc=local"]) == "admin"
    assert map_ldap_role(cfg, ["CN=Helpdesk,OU=Groups,DC=corp,DC=local"]) == "viewer"
    assert map_ldap_role(cfg, ["CN=Unrelated,DC=corp,DC=local"]) is None


def test_map_ldap_role_falls_back_to_default():
    cfg = {"group_mappings": [], "default_role": "read_only"}
    assert map_ldap_role(cfg, []) == "read_only"


def test_map_radius_role():
    cfg = {
        "class_mappings": [{"value": "netops", "role": "operator"}],
        "default_role": "viewer",
    }
    assert map_radius_role(cfg, ["NetOps"]) == "operator"
    assert map_radius_role(cfg, ["other"]) == "viewer"
    assert map_radius_role({"class_mappings": [], "default_role": ""}, ["x"]) is None
