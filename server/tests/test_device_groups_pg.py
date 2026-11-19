"""Device Templates phase 2: groups you can use (migrate-192) on a disposable PostgreSQL.

Acceptance (docs/plans/device-templates-plan.md, phase 2):

* groups can be created, renamed, moved and deleted; a group in use is not
  deleted (the database would cascade-delete its alert rules);
* a dynamic group shows its members before saving and picks up a device that
  starts matching on the next pass;
* an alert rule scoped to a group covers its members, including dynamic and
  subgroup members, on the SNMP, status, trap, syslog and Device Tracker
  scope checks, and maintenance on a group covers them too;
* the affected count shown for a rule is the set the evaluators use;
* rules that scoped by devices.group_id before cover the same devices after;
* custom fields: CSV import, list column values, group condition, placeholder;
* a tag-scoped user sees only in-scope members and cannot change groups;
* a dynamic group can never be a device's own group.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

MANAGER = ["devices.view", "devices.manage", "devices.manage_policies", "alerts.view", "servers.view", "servers.manage"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def run(db, fn):
    async def go():
        async with session(db) as s:
            return await fn(s)
    return asyncio.run(go())


def add_device(db, name, ip, *, vendor=None, dtype="other", group=None, tags=(), snmp=True):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, vendor, device_type, group_id, tags, snmp_enabled, snmp_poll_interval)"
        " VALUES ($1, $2::inet, $3, $4, $5, $6::jsonb, $7, 60) RETURNING id",
        name, ip, vendor, dtype, group, list(tags), snmp))


def add_server(db, name, *, tags=()):
    return str(db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status, tags)"
                         " VALUES ($1, $1, 'agent', 'healthy', $2::jsonb) RETURNING id", name, list(tags)))


def add_group(db, name, parent=None):
    return str(db.scalar("INSERT INTO device_groups (name, parent_id) VALUES ($1, $2) RETURNING id", name, parent))


@pytest.fixture
def world(pg_db):
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM device_maintenance; "
               "DELETE FROM custom_field_defs; DELETE FROM devices; DELETE FROM servers; DELETE FROM device_groups")
    return db


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "group-admin"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, username=kw.pop("username", "noc-lead"), **kw)


FORTI_FW = {"op": "all", "conditions": [
    {"field": "vendor", "operator": "contains", "value": "fortinet"},
    {"field": "device_type", "operator": "is", "value": "firewall"},
    {"field": "ip_address", "operator": "in_subnet", "value": ["10.51.0.0/16"]},
]}


# ── Migration and primary membership ───────────────────────────────────────

def test_primary_group_membership_follows_devices_group_id(world):
    db = world
    core = add_group(db, "Core")
    sub = add_group(db, "Core / DC1", parent=core)
    d = add_device(db, "sw-1", "10.0.0.1", group=sub)
    closure = {r[0] for r in db.query(
        "SELECT g.name FROM device_group_member_closure c JOIN device_groups g ON g.id = c.group_id WHERE c.asset_id = $1", d)}
    assert closure == {"Core", "Core / DC1"}
    db.execute("UPDATE devices SET group_id = NULL WHERE id = $1", d)
    assert db.scalar("SELECT count(*) FROM device_group_members WHERE asset_id = $1", d) == 0


# ── Group CRUD ──────────────────────────────────────────────────────────────

def test_create_rename_move_and_delete_with_in_use_guard(world):
    db = world
    with manager(db) as c:
        a = c.post("/api/v1/device-groups", json={"name": "Branches"}).json()
        b = c.post("/api/v1/device-groups", json={"name": "Riyadh branches", "parent_id": a["id"]}).json()
        assert b["parent_id"] == a["id"]
        assert c.post("/api/v1/device-groups", json={"name": "branches"}).status_code == 409
        assert c.put(f"/api/v1/device-groups/{a['id']}", json={"parent_id": b["id"]}).status_code == 422   # cycle
        assert c.put(f"/api/v1/device-groups/{b['id']}", json={"name": "Riyadh"}).json()["name"] == "Riyadh"
        rule = str(db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, severity, group_id)"
                             " VALUES ('Branch down', 'ping_status', 'eq', 0, 'critical', $1) RETURNING id", b["id"]))
        r = c.delete(f"/api/v1/device-groups/{b['id']}")
        assert r.status_code == 409 and "alert rule" in r.json()["detail"]
        assert db.scalar("SELECT count(*) FROM alert_rules WHERE id = $1", rule) == 1
        assert c.delete(f"/api/v1/device-groups/{a['id']}").status_code == 409            # has a subgroup
        used = c.get(f"/api/v1/device-groups/{b['id']}").json()["used_by"]
        assert [x["name"] for x in used["alert_rules"]] == ["Branch down"]
        db.execute("DELETE FROM alert_rules WHERE id = $1", rule)
        assert c.delete(f"/api/v1/device-groups/{b['id']}").status_code == 204
        assert c.delete(f"/api/v1/device-groups/{a['id']}").status_code == 204


def test_depth_is_limited(world):
    db = world
    with manager(db) as c:
        parent = None
        for i in range(5):
            parent = c.post("/api/v1/device-groups", json={"name": f"L{i}", "parent_id": parent}).json()["id"]
        assert c.post("/api/v1/device-groups", json={"name": "L5", "parent_id": parent}).status_code == 422


# ── Dynamic groups ──────────────────────────────────────────────────────────

def test_dynamic_group_preview_create_and_join_on_next_pass(world):
    db = world
    fw1 = add_device(db, "fw-1", "10.51.1.1", vendor="Fortinet", dtype="firewall")
    add_device(db, "fw-elsewhere", "10.60.1.1", vendor="Fortinet", dtype="firewall")
    late = add_device(db, "fw-2", "10.51.2.1", vendor="Unknown", dtype="other")
    with manager(db) as c:
        pv = c.post("/api/v1/device-groups/preview", json={"condition": FORTI_FW}).json()
        assert pv["count"] == 1 and pv["sample"][0]["name"] == "fw-1"
        g = c.post("/api/v1/device-groups", json={"name": "Branch firewalls", "kind": "dynamic",
                                                   "condition": FORTI_FW}).json()
        assert g["kind"] == "dynamic" and g["member_count"] == 1
        members = c.get(f"/api/v1/device-groups/{g['id']}/members").json()["data"]
        assert [(m["id"], m["sources"]) for m in members] == [(fw1, ["rule"])]

        db.execute("UPDATE devices SET vendor = 'Fortinet', device_type = 'firewall' WHERE id = $1", late)
        from app.services.device_groups import membership
        assert run(db, membership.reconcile)["added"] == 1
        assert run(db, membership.reconcile).get("skipped") == "unchanged"         # second pass writes nothing
        names = {m["name"] for m in c.get(f"/api/v1/device-groups/{g['id']}/members").json()["data"]}
        assert names == {"fw-1", "fw-2"}

        # Static-only operations are refused on a dynamic group.
        assert c.post(f"/api/v1/device-groups/{g['id']}/members",
                      json={"asset_kind": "device", "asset_ids": [late]}).status_code == 422
        # A dynamic group is never a device's own group.
        assert c.put(f"/api/v1/devices/{late}", json={"group_id": g["id"]}).status_code == 422
        bad = c.post("/api/v1/device-groups", json={"name": "Bad", "kind": "dynamic",
                                                     "condition": {"field": "nope", "operator": "equals", "value": "x"}})
        assert bad.status_code == 422


def test_static_group_with_servers_and_extra_devices(world):
    db = world
    srv = add_server(db, "app-01")
    d = add_device(db, "sw-9", "10.9.9.9")
    with manager(db) as c:
        g = c.post("/api/v1/device-groups", json={"name": "Payments", "asset_kinds": ["device", "server"]}).json()
        assert c.post(f"/api/v1/device-groups/{g['id']}/members",
                      json={"asset_kind": "server", "asset_ids": [srv]}).json() == {"added": 1}
        assert c.post(f"/api/v1/device-groups/{g['id']}/members",
                      json={"asset_kind": "device", "asset_ids": [d]}).json() == {"added": 1}
        kinds = sorted(m["asset_kind"] for m in c.get(f"/api/v1/device-groups/{g['id']}/members").json()["data"])
        assert kinds == ["device", "server"]
        assert c.post(f"/api/v1/device-groups/{g['id']}/members/remove",
                      json={"asset_kind": "server", "asset_ids": [srv]}).json() == {"removed": 1}


# ── Scope: evaluators, maintenance, affected count ─────────────────────────

def test_group_scope_covers_dynamic_and_subgroup_members_everywhere(world):
    db = world
    parent = add_group(db, "Security")
    member_primary = add_device(db, "fw-core", "10.51.9.1", vendor="Cisco", dtype="router", group=parent)
    dyn = None
    with manager(db) as c:
        dyn = c.post("/api/v1/device-groups", json={"name": "Branch firewalls", "kind": "dynamic",
                                                     "condition": FORTI_FW, "parent_id": parent}).json()["id"]
    fw = add_device(db, "fw-1", "10.51.1.1", vendor="Fortinet", dtype="firewall")
    other = add_device(db, "printer", "10.70.0.5", vendor="HP", dtype="printer")
    from app.services.device_groups import membership
    run(db, lambda s: membership.reconcile(s, force=True))
    rule = NS(device_id=None, group_id=parent, device_type=None, location=None, scope_tag=None)

    # SNMP metric rules (fleet loader + scope check).
    from app.services import network_alert_service as nas
    devs = run(db, nas._snmp_devices)
    assert {d for d in devs if nas._device_in_scope(rule, d, devs[d])} == {member_primary, fw}

    # Device status and flap notices.
    from app.api.v1 import alert_engine as ae
    for did, expected in ((fw, True), (member_primary, True), (other, False)):
        groups = run(db, lambda s, i=did: membership.groups_of(s, "device", i))
        assert ae._rule_in_device_scope(rule, device_id=did, group_id=None, device_type=None, location=None,
                                        device_tags=set(), group_ids=groups) is expected
        # Traps share in_group with the same membership.
        assert ae._in_trap_group(parent, groups, None) is expected

    # Device Tracker.
    from app.services import udt_alert_service as udt
    r = {"id": "r", "device_id": None, "group_id": parent, "device_type": None, "location": None}
    for did, expected in ((fw, True), (other, False)):
        groups = run(db, lambda s, i=did: membership.groups_of(s, "device", i))
        assert udt._rule_scope_ok(r, {"id": did, "group_id": None, "groups": groups}) is expected

    # Membership unavailable -> the primary group decides (old behaviour).
    assert membership.in_group(parent, None, parent) and not membership.in_group(parent, None, None)

    # The affected count is the set the evaluators use.
    from app.services import alert_rule_scope
    got = run(db, lambda s: alert_rule_scope.affected(s, {"group_id": parent, "metric": "cpu"}))
    assert got["count"] == 2

    # Maintenance on the parent group covers the dynamic subgroup member.
    db.execute("INSERT INTO device_maintenance (scope_type, scope_group_id, starts_at, ends_at, reason)"
               " VALUES ('group', $1, now() - interval '1 hour', now() + interval '1 hour', 'change')", parent)
    assert run(db, lambda s: ae._device_in_maintenance(s, fw)) is True
    assert run(db, lambda s: ae._device_in_maintenance(s, other)) is False
    assert dyn


def test_rules_on_primary_groups_cover_the_same_devices(world):
    db = world
    g = add_group(db, "Core")
    d = add_device(db, "core-1", "10.1.1.1", group=g)
    add_device(db, "edge-1", "10.1.1.2")
    from app.services import network_alert_service as nas
    rule = NS(device_id=None, group_id=g, device_type=None, location=None, scope_tag=None)
    devs = run(db, nas._snmp_devices)
    assert [x for x in devs if nas._device_in_scope(rule, x, devs[x])] == [d]


# ── Custom fields ───────────────────────────────────────────────────────────

def test_custom_fields_import_list_condition_and_placeholder(world):
    db = world
    a = add_device(db, "fw-1", "10.51.1.1")
    b = add_device(db, "fw-2", "10.51.1.2")
    with manager(db) as c:
        assert c.post("/api/v1/custom-fields", json={"key": "department", "label": "Department", "field_type": "choice",
                                                      "choices": ["Finance", "Retail"], "show_in_list": True}).status_code == 201
        assert c.post("/api/v1/custom-fields", json={"key": "rack_u", "label": "Rack U", "field_type": "number"}).status_code == 201
        assert c.post("/api/v1/custom-fields", json={"key": "Bad Key", "label": "x", "field_type": "text"}).status_code == 422
        csv_text = "ip_address,department,rack_u\n10.51.1.1,finance,12\n10.51.1.2,Retail,\n10.99.9.9,Retail,3\n10.51.1.2,Nope,4\n"
        dry = c.post("/api/v1/custom-fields/import", json={"csv": csv_text}).json()
        assert dry["applied"] is False and len(dry["rows"]) == 2 and len(dry["problems"]) == 2
        assert db.scalar("SELECT count(*) FROM custom_field_values") == 0
        done = c.post("/api/v1/custom-fields/import", json={"csv": csv_text, "apply": True}).json()
        assert done["applied"] is True
        lv = c.get("/api/v1/custom-fields/list-values").json()
        assert lv["fields"] == ["department"] and lv["values"][a] == {"department": "Finance"}
        assert c.get("/api/v1/custom-fields/values", params={"asset_kind": "device", "asset_id": a}).json()["values"] == \
            {"department": "Finance", "rack_u": 12}
        assert c.put("/api/v1/custom-fields/values", json={"asset_kind": "device", "asset_id": b,
                                                           "values": {"rack_u": "abc"}}).status_code == 422
        g = c.post("/api/v1/device-groups", json={"name": "Finance", "kind": "dynamic", "condition":
                   {"field": "custom.department", "operator": "is", "value": "Finance"}}).json()
        assert g["member_count"] == 1
        assert c.delete("/api/v1/custom-fields/department").status_code == 409              # used by a group
    from app.services import custom_fields as cf
    ph = run(db, lambda s: cf.placeholders(s, "device", a))
    assert ph == {"custom.department": "Finance", "custom.rack_u": 12}
    assert run(db, lambda s: cf.placeholders(s, "device", b))["custom.rack_u"] == ""


# ── Permissions and scope ───────────────────────────────────────────────────

def test_scoped_users_see_in_scope_members_and_cannot_change_groups(world):
    db = world
    g = add_group(db, "Mixed")
    add_device(db, "team-a-sw", "10.2.0.1", group=g, tags=("team-a",))
    add_device(db, "team-b-sw", "10.2.0.2", group=g, tags=("team-b",))
    with api_client(db, role="scoped-group-admin", permissions=MANAGER, username="scoped", scope_tags=("team-a",)) as c:
        names = [m["name"] for m in c.get(f"/api/v1/device-groups/{g}/members").json()["data"]]
        assert names == ["team-a-sw"]
        assert c.post("/api/v1/device-groups", json={"name": "Nope"}).status_code == 403
        assert c.put(f"/api/v1/device-groups/{g}", json={"name": "Renamed"}).status_code == 403
    with api_client(db, role="device-operator", permissions=["devices.view", "devices.manage"], username="op") as c:
        assert c.get("/api/v1/device-groups").status_code == 200
        assert c.post("/api/v1/device-groups", json={"name": "Nope"}).status_code == 403
        assert c.post("/api/v1/custom-fields", json={"key": "x", "label": "X", "field_type": "text"}).status_code == 403


def test_device_list_filter_by_group_uses_membership(world):
    db = world
    parent = add_group(db, "Region")
    child = add_group(db, "Region / Site", parent=parent)
    d = add_device(db, "site-sw", "10.3.0.1", group=child)
    add_device(db, "other", "10.3.0.2")
    with manager(db) as c:
        rows = c.get("/api/v1/devices", params={"group_id": parent}).json()["data"]
        assert [r["id"] for r in rows] == [d]
