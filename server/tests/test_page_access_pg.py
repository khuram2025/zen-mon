"""Custom page-access roles on the real schema (disposable PostgreSQL, migrate-185).

Real routes through a plain ``TestClient`` with ``get_db`` pointed at the
private ``pg_fixture`` database and the real role lookup (not the conftest
shim), so the role rows decide what each account may do. Never the appliance
database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import security
from app.core.database import get_db
from app.core.security import get_current_user, get_role_permissions, invalidate_role_cache
from app.main import app
from pg_fixture import pg_cluster, pg_db  # noqa: F401

HELPDESK = ["dashboard.view", "devices.view", "devices.manage", "alerts.view", "alerts.acknowledge",
            "servers.view", "maps.view", "maps.manage", "reports.view"]


def add_user(db, role: str) -> SimpleNamespace:
    name = f"u-{uuid.uuid4().hex[:8]}"
    uid = db.scalar("INSERT INTO users (username, email, password_hash, role, full_name) "
                    "VALUES ($1, $2, 'x', $3, 'Test User') RETURNING id", name, f"{name}@example.test", role)
    return SimpleNamespace(id=uid, username=name, email=f"{name}@example.test", full_name="Test User", role=role,
                           is_active=True, auth_source="local", scope_tags=[], last_login=None, timezone=None,
                           must_change_password=False, mfa_enabled=False, token_version=0, password_hash="")


@pytest.fixture
def env(pg_db, monkeypatch):
    # The real lookup reads roles.permissions and roles.page_access.
    monkeypatch.setattr(security, "get_role_permissions", get_role_permissions)
    invalidate_role_cache()
    admin = add_user(pg_db, "admin")
    who = SimpleNamespace(user=admin)

    async def session():
        engine = create_async_engine(pg_db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                yield db
        finally:
            await engine.dispose()

    async def signed_in():
        return who.user

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield SimpleNamespace(client=TestClient(app), db=pg_db, admin=admin, who=who)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        invalidate_role_cache()


def as_user(env, user):
    env.who.user = user
    invalidate_role_cache()


def new_map(env, name):
    r = env.client.post("/api/v1/maps", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_role(env, name, perms, page_access, expect=201):
    r = env.client.post("/api/v1/roles", json={"name": name, "display_name": name.replace("_", " ").title(),
                                               "permissions": perms, "page_access": page_access})
    assert r.status_code == expect, r.text
    return r.json()


# ── migration ──────────────────────────────────────────────────────────────────

def test_migration_adds_a_null_column_so_no_existing_role_changes(pg_db):
    col = pg_db.query("SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                      "WHERE table_name = 'roles' AND column_name = 'page_access'")
    assert col == [("jsonb", "YES", None)]
    assert pg_db.scalar("SELECT count(*) FROM roles") >= 4
    assert pg_db.scalar("SELECT count(*) FROM roles WHERE page_access IS NOT NULL") == 0
    # Only objects (or NULL) are accepted.
    with pytest.raises(Exception):
        pg_db.execute("UPDATE roles SET page_access = '[]'::jsonb WHERE name = 'viewer'")
    # Re-running the migration is harmless.
    from pathlib import Path
    pg_db.psql_file(Path(__file__).resolve().parents[2] / "scripts" / "migrate-185-role-page-access.sql")
    assert pg_db.scalar("SELECT count(*) FROM roles WHERE page_access IS NOT NULL") == 0


@pytest.mark.asyncio
async def test_existing_roles_keep_exactly_their_permissions(pg_db, monkeypatch):
    monkeypatch.setattr(security, "get_role_permissions", get_role_permissions)
    invalidate_role_cache()
    engine = create_async_engine(pg_db.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as db:
            for name in ("operator", "viewer", "read_only"):
                stored = pg_db.scalar("SELECT permissions FROM roles WHERE name = $1", name)
                assert sorted(await get_role_permissions(db, name)) == sorted(stored), name
    finally:
        await engine.dispose()
        invalidate_role_cache()


# ── end to end ─────────────────────────────────────────────────────────────────

def test_page_limited_role_sees_only_its_pages_and_maps(env):
    map_a = new_map(env, "NOC wall A")
    map_b = new_map(env, "Core B")
    role = create_role(env, "helpdesk_wall", HELPDESK, {"pages": ["/devices", "/maps/manual"], "maps": [map_a]})
    assert role["page_access"] == {"pages": ["/devices", "/maps/manual"], "maps": [map_a]}
    assert role["permissions"] == sorted(HELPDESK), "the stored permissions are what the admin chose"
    member = add_user(env.db, "helpdesk_wall")
    as_user(env, member)
    c = env.client

    me = c.get("/api/v1/auth/me").json()
    assert me["page_access"] == {"pages": ["/devices", "/maps/manual"], "maps": [map_a]}
    assert sorted(me["permissions"]) == ["devices.manage", "devices.view", "maps.manage", "maps.view"]

    # Allowed page APIs work.
    assert c.get("/api/v1/devices/groups").status_code == 200
    # Only the listed map is listed or opened; no new maps.
    listed = c.get("/api/v1/maps").json()["data"]
    assert [m["id"] for m in listed] == [map_a]
    assert c.get(f"/api/v1/maps/{map_a}").status_code == 200
    for path in (f"/api/v1/maps/{map_b}", f"/api/v1/maps/{map_b}/nodes-live", f"/api/v1/maps/{map_b}/versions"):
        assert c.get(path).status_code == 403, path
    assert c.put(f"/api/v1/maps/{map_b}", json={"name": "renamed"}).status_code == 403
    assert c.delete(f"/api/v1/maps/{map_b}").status_code == 403
    assert c.post("/api/v1/maps", json={"name": "mine"}).status_code == 403
    assert c.put(f"/api/v1/maps/{map_a}", json={"name": "NOC wall A2"}).status_code == 200, "maps.manage still applies to its map"

    # Hidden pages' APIs are refused even though the role's permission list holds them.
    for path in ("/api/v1/servers", "/api/v1/alerts", "/api/v1/alerts/summary", "/api/v1/reports/data/executive"):
        assert c.get(path).status_code == 403, path
    # Cannot reach role or user management either.
    assert c.get("/api/v1/roles").status_code == 403

    # Lifting the limit restores the role's full permission list at once.
    as_user(env, env.admin)
    r = c.put(f"/api/v1/roles/{role['id']}", json={"page_access": None})
    assert r.status_code == 200, r.text
    assert r.json()["page_access"] is None
    as_user(env, member)
    assert c.get("/api/v1/servers").status_code != 403
    assert {m["id"] for m in c.get("/api/v1/maps").json()["data"]} == {map_a, map_b}
    me = c.get("/api/v1/auth/me").json()
    assert me["page_access"] is None and sorted(me["permissions"]) == sorted(HELPDESK)


def test_wall_role_limited_to_one_dashboard(env):
    role = create_role(env, "noc_wall", HELPDESK, {"pages": ["/dashboards/servers"], "maps": None})
    member = add_user(env.db, "noc_wall")
    as_user(env, member)
    me = env.client.get("/api/v1/auth/me").json()
    # The Server Team dashboard reads servers and alerts; it changes nothing.
    assert sorted(me["permissions"]) == ["alerts.view", "dashboard.view", "servers.view"]
    assert env.client.get("/api/v1/maps").status_code == 403
    assert env.client.get("/api/v1/devices/groups").status_code == 403
    assert role["page_access"]["maps"] is None


def test_history_and_audit_record_the_page_limit(env):
    role = create_role(env, "audit_me", HELPDESK, {"pages": ["/devices"], "maps": None})
    r = env.client.put(f"/api/v1/roles/{role['id']}", json={"page_access": {"pages": ["/devices", "/alerts"], "maps": None}})
    assert r.status_code == 200, r.text
    changes = env.db.scalar("SELECT changes FROM role_history WHERE role_id = $1 AND action = 'update' "
                            "ORDER BY revision DESC LIMIT 1", uuid.UUID(role["id"]))
    assert changes["page_access"]["to"]["pages"] == ["Alerts & Events > Active Alerts", "Network > Devices"]
    # Saving the same value again changes nothing.
    r = env.client.put(f"/api/v1/roles/{role['id']}", json={"page_access": {"pages": ["/alerts", "/devices"]}})
    assert r.json()["revision"] == 2


def test_validation(env):
    create_role(env, "bad_page", ["devices.view"], {"pages": ["/nope"]}, expect=400)
    create_role(env, "bad_map", ["maps.view"], {"maps": [str(uuid.uuid4())]}, expect=400)
    r = create_role(env, "full_admin_limited", ["system.admin"], {"pages": ["/devices"]}, expect=400)
    assert "Full administration" in r["detail"]
    # Granting Full administration to a limited role is refused too, until the limit is removed.
    role = create_role(env, "later_admin", ["devices.view"], {"pages": ["/devices"]})
    r = env.client.put(f"/api/v1/roles/{role['id']}", json={"permissions": ["system.admin"]})
    assert r.status_code == 400, r.text
    r = env.client.put(f"/api/v1/roles/{role['id']}", json={"permissions": ["system.admin"], "page_access": None})
    assert r.status_code == 200, r.text
    # Omitting page_access leaves it unchanged.
    role = create_role(env, "keep_me", ["devices.view"], {"pages": ["/devices"]})
    r = env.client.put(f"/api/v1/roles/{role['id']}", json={"description": "changed"})
    assert r.json()["page_access"] == {"pages": ["/devices"], "maps": None}


def test_page_limited_manager_cannot_hand_out_wider_access(env):
    perms = ["users.view", "users.manage", "roles.view", "roles.manage", "devices.view", "alerts.view"]
    create_role(env, "desk_lead", perms, {"pages": ["/devices", "/alerts", "/settings/general?tab=users"]})
    create_role(env, "desk", ["devices.view"], {"pages": ["/devices"]})
    create_role(env, "open_devices", ["devices.view"], None)
    lead = add_user(env.db, "desk_lead")
    as_user(env, lead)
    c = env.client
    assignable = {r["name"] for r in c.get("/api/v1/roles/assignable").json()}
    assert "desk" in assignable and "open_devices" not in assignable
    # Creating an unlimited role, or one with pages beyond the lead's own, is refused.
    r = c.post("/api/v1/roles", json={"name": "sneaky", "display_name": "Sneaky", "permissions": ["devices.view"]})
    assert r.status_code == 403, r.text
    r = c.post("/api/v1/roles", json={"name": "sneaky2", "display_name": "Sneaky 2", "permissions": ["devices.view"],
                                      "page_access": {"pages": ["/devices", "/link-utilization"]}})
    assert r.status_code == 403, r.text
    r = c.post("/api/v1/roles", json={"name": "desk2", "display_name": "Desk 2", "permissions": ["devices.view"],
                                      "page_access": {"pages": ["/devices"]}})
    assert r.status_code == 201, r.text
    desk2 = r.json()
    r = c.put(f"/api/v1/roles/{desk2['id']}", json={"page_access": None})
    assert r.status_code == 403, "a page-limited manager cannot lift a limit"
