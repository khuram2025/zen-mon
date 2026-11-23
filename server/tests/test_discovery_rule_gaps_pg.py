"""Discovery rules: server site, "Add to group", custom fields and per-rule
schedules (migrate-213) on a disposable PostgreSQL.

* a server rule sets the site and adds the server to a static group; undo
  restores both; a dynamic group is refused when the rule is saved;
* a custom field is a condition (custom.<key>, typed like the field) and an
  action (coerced to the field's type, first matching rule wins per field);
  a hand edit under set-once is kept; the "changed" pass notices a hand edit
  of a custom field; a field a rule uses cannot be deleted;
* a rule with its own schedule runs at its own slots only, once per slot,
  independent of the shared engine schedule; a one-time run runs once.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.discovery_rules import reconcile, runner
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

BASE = "/api/v1/discovery-v2/rules"
MANAGER = ["discovery.view", "discovery.run", "discovery.manage_rules", "devices.view", "devices.manage",
           "servers.view", "servers.manage", "devices.manage_policies"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def empty_inventory(pg_db):
    pg_db.execute("TRUNCATE devices, servers, sites, device_groups, custom_field_defs, discovery_engine_rules,"
                  " discovery_engine_runs, discovery_engine_assignments, discovery_engine_asset_state CASCADE")


def enable_engine(db, **kw):
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'discovery_rules'",
               {**runner.DEFAULT_SETTINGS, "enabled": True, **kw})


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "rules-manager"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, **kw)


def add_server(db, name, *, os_version="10.0.17763", tags=()):
    return str(db.scalar("INSERT INTO servers (display_name, hostname, os_type, os_name, os_version, tags)"
                         " VALUES ($1, $1, 'windows', 'Windows Server 2019', $2, $3::jsonb) RETURNING id",
                         name, os_version, list(tags)))


def add_device(db, name, ip, *, vendor="Cisco"):
    return str(db.scalar("INSERT INTO devices (hostname, ip_address, vendor) VALUES ($1, $2::inet, $3) RETURNING id",
                         name, ip, vendor))


def add_group(db, name, *, kinds=("device", "server"), dynamic=False):
    if dynamic:
        return str(db.scalar(
            "INSERT INTO device_groups (name, kind, condition, asset_kinds) VALUES ($1, 'dynamic',"
            " '{\"op\": \"all\", \"conditions\": []}'::jsonb, $2) RETURNING id", name, list(kinds)))
    return str(db.scalar("INSERT INTO device_groups (name, asset_kinds) VALUES ($1, $2) RETURNING id",
                         name, list(kinds)))


def add_field(db, key, ftype, *, choices=None, kinds=("device", "server")):
    db.execute("INSERT INTO custom_field_defs (key, label, field_type, choices, asset_kinds)"
               " VALUES ($1, $2, $3, $4::jsonb, $5)", key, key.replace("_", " ").title(), ftype, choices, list(kinds))


def rule(name, condition, actions, **kw):
    return {"name": name, "mode": kw.pop("mode", "active"), "asset_kinds": kw.pop("kinds", ["server"]),
            "triggers": kw.pop("triggers", ["created"]), "condition": {"op": "all", "conditions": condition},
            "actions": actions, **kw}


def create(c, body, status=201):
    r = c.post(BASE, json=body)
    assert r.status_code == status, r.text
    return r.json() if status == 201 else r.json()["detail"]


def run_manual(c, kinds=("server",)):
    body = {"targets": {"kinds": list(kinds)}}
    pre = c.post(f"{BASE}/inventory/preview", json=body)
    assert pre.status_code == 200, pre.text
    r = c.post(f"{BASE}/inventory/apply", json={**body, "digest": pre.json()["digest"]})
    assert r.status_code == 200, r.text
    return pre.json(), r.json()


OLD_WINDOWS = [{"field": "os_version", "operator": "version_lt", "value": "10.0.20348"}]


# ── server site and group ───────────────────────────────────────────────────

def test_server_site_and_group_set_by_os_version_and_undone(pg_db):
    old = add_server(pg_db, "app-01")
    new = add_server(pg_db, "app-02", os_version="10.0.20348")
    site = str(pg_db.scalar("INSERT INTO sites (name) VALUES ('Legacy hall') RETURNING id"))
    group = add_group(pg_db, "Windows 2019")
    with manager(pg_db) as c:
        create(c, rule("Old Windows", OLD_WINDOWS, [{"key": "site", "value": site},
                                                    {"key": "add_group", "value": group},
                                                    {"key": "tags", "value": ["win2019"]}]))
        pre, out = run_manual(c)
        assert pre["summary"]["by_action"] == {"site": 1, "add_group": 1, "tags": 1}
        assert out["applied"] == 3
        assert pg_db.scalar("SELECT site_id::text FROM servers WHERE id = $1", old) == site
        assert pg_db.scalar("SELECT site_id FROM servers WHERE id = $1", new) is None
        assert pg_db.query("SELECT asset_kind, asset_id::text, source FROM device_group_members"
                           " WHERE group_id = $1", group) == [("server", old, "static")]
        assert pg_db.scalar("SELECT member_count FROM device_groups WHERE id = $1", group) == 1
        # a second run changes nothing
        assert run_manual(c)[0]["summary"]["changes"] == 0
        undo = c.post(f"{BASE}/history/{out['engine_run_id']}/undo")
        assert undo.status_code == 200, undo.text
        assert undo.json()["reverted"] == 3
    assert pg_db.scalar("SELECT site_id FROM servers WHERE id = $1", old) is None
    assert pg_db.scalar("SELECT count(*) FROM device_group_members WHERE group_id = $1", group) == 0


def test_group_action_refuses_dynamic_groups_and_needs_manage_policies(pg_db):
    dyn = add_group(pg_db, "Computed", dynamic=True)
    static = add_group(pg_db, "Hand-picked")
    with manager(pg_db) as c:
        assert "Unknown device group" in create(c, rule("Dyn", OLD_WINDOWS, [{"key": "add_group", "value": dyn}]),
                                                status=422)
    perms = [p for p in MANAGER if p != "devices.manage_policies"]
    with manager(pg_db, role="no-policies", permissions=perms) as c:
        detail = create(c, rule("Static", OLD_WINDOWS, [{"key": "add_group", "value": static}]), status=403)
        assert "devices.manage_policies" in detail


def test_group_member_removed_by_hand_is_kept_out_under_set_once(pg_db):
    sid = add_server(pg_db, "app-01")
    group = add_group(pg_db, "Windows 2019")
    with manager(pg_db) as c:
        create(c, rule("Old Windows", OLD_WINDOWS, [{"key": "add_group", "value": group}]))
        run_manual(c)
        pg_db.execute("DELETE FROM device_group_members WHERE group_id = $1", group)
        pre, _ = run_manual(c)
        assert [ch["outcome"] for ch in pre["changes"]] == ["kept_manual_edit"]
    assert pg_db.scalar("SELECT count(*) FROM device_group_members WHERE asset_id = $1", sid) == 0


def test_group_holding_devices_only_is_reported_not_written_for_a_server(pg_db):
    add_server(pg_db, "app-01")
    group = add_group(pg_db, "Switches", kinds=("device",))
    with manager(pg_db) as c:
        create(c, rule("Old Windows", OLD_WINDOWS, [{"key": "add_group", "value": group}]))
        _, out = run_manual(c)
        assert out["applied"] == 0 and out.get("error") == 1
        items = c.get(f"{BASE}/history/{out['engine_run_id']}/items").json()
        rows = items["data"] if isinstance(items, dict) else items
        assert "holds device only" in rows[0]["detail"]


# ── custom fields ───────────────────────────────────────────────────────────

def test_custom_field_condition_and_action_typed_and_first_rule_wins(pg_db):
    add_field(pg_db, "role", "choice", choices=["Core", "Edge", "Access"])
    add_field(pg_db, "tier", "number")
    core = add_device(pg_db, "core-1", "10.0.0.1")
    edge = add_device(pg_db, "edge-1", "10.0.0.2")
    pg_db.execute("INSERT INTO custom_field_values (field_id, asset_kind, asset_id, value_text)"
                  " SELECT id, 'device', $1, 'Core' FROM custom_field_defs WHERE key = 'role'", core)
    with manager(pg_db) as c:
        # "core" is spelled as the choice; tier "1" is stored as the number 1
        r1 = create(c, rule("Core tier", [{"field": "custom.role", "operator": "is", "value": "Core"}],
                            [{"key": "custom_field", "value": {"field": "tier", "value": "1"}}], kinds=["device"]))
        assert r1["condition"]["conditions"][0]["type"] == "enum"
        assert r1["actions"][0]["value"] == {"field": "tier", "value": 1}
        # a catch-all: its tier is shadowed by "Core tier" on the core device
        create(c, rule("Everyone else", [], [{"key": "custom_field", "value": {"field": "tier", "value": 3}}],
                       kinds=["device"]))
        # a missing role "is not Core"
        create(c, rule("No role yet", [{"field": "custom.role", "operator": "is_not", "value": "Core"}],
                       [{"key": "custom_field", "value": {"field": "role", "value": "edge"}}], kinds=["device"]))
        pre, out = run_manual(c, kinds=("device",))
        got = {(ch["asset_id"], ch["item_key"]): (ch["before"], ch["after"]) for ch in pre["changes"]}
        assert got == {(core, "tier"): (None, "1"), (edge, "tier"): (None, "3"), (edge, "role"): (None, "Edge")}
        assert out["applied"] == 3
        vals = dict(pg_db.query(
            "SELECT d.hostname || '.' || f.key, COALESCE(v.value_text, v.value_num::text) FROM custom_field_values v"
            " JOIN custom_field_defs f ON f.id = v.field_id JOIN devices d ON d.id = v.asset_id"))
        assert vals == {"core-1.role": "Core", "core-1.tier": "1", "edge-1.tier": "3", "edge-1.role": "Edge"}
        # a hand edit under set-once is kept
        pg_db.execute("UPDATE custom_field_values SET value_num = 2 WHERE asset_id = $1"
                      " AND field_id = (SELECT id FROM custom_field_defs WHERE key = 'tier')", edge)
        pre, _ = run_manual(c, kinds=("device",))
        assert [(ch["item_key"], ch["outcome"]) for ch in pre["changes"]] == [("tier", "kept_manual_edit")]
        # the field a rule uses cannot be deleted
        assert c.delete("/api/v1/custom-fields/tier").status_code == 409


def test_custom_field_rules_are_validated(pg_db):
    add_field(pg_db, "tier", "number", kinds=("server",))
    add_field(pg_db, "warranty_end", "date")
    with manager(pg_db) as c:
        assert "Unknown field" in create(c, rule("x", [{"field": "custom.nope", "operator": "is_empty"}],
                                                 [{"key": "tags", "value": ["a"]}]), status=422)
        assert "not a number" in create(c, rule("x", [], [{"key": "custom_field",
                                                            "value": {"field": "tier", "value": "high"}}]), status=422)
        assert "does not apply" in create(c, rule("x", [], [{"key": "custom_field",
                                                             "value": {"field": "tier", "value": 1}}],
                                                  kinds=["device"]), status=422)
        assert "set twice" in create(c, rule("x", [], [{"key": "custom_field", "value": {"field": "tier", "value": 1}},
                                                       {"key": "custom_field", "value": {"field": "tier", "value": 2}}]),
                                     status=422)
        assert "YYYY-MM-DD" in create(c, rule("x", [{"field": "custom.warranty_end", "operator": "before",
                                                     "value": "31/12/2027"}], [{"key": "tags", "value": ["a"]}]),
                                      status=422)
        cat = c.get(f"{BASE}/catalog").json()
        tier = next(f for f in cat["fields"] if f["key"] == "custom.tier")
        assert tier["custom"] and tier["applies_to"] == ["server"] and tier["type"] == "number"
        assert {f["key"] for f in cat["custom_fields"]} == {"tier", "warranty_end"}


@pytest.mark.asyncio
async def test_changed_pass_notices_a_custom_field_edit(pg_db):
    add_field(pg_db, "role", "text")
    sid = add_server(pg_db, "db-01")
    with manager(pg_db) as c:
        create(c, rule("Databases", [{"field": "custom.role", "operator": "equals", "value": "database"}],
                       [{"key": "tags", "value": ["db"]}], triggers=["changed"]))
    enable_engine(pg_db)
    async with session(pg_db) as s:
        await reconcile.reconcile_changed(s)
    assert pg_db.scalar("SELECT tags FROM servers WHERE id = $1", sid) == []
    pg_db.execute("INSERT INTO custom_field_values (field_id, asset_kind, asset_id, value_text)"
                  " SELECT id, 'server', $1, 'Database' FROM custom_field_defs WHERE key = 'role'", sid)
    async with session(pg_db) as s:
        out = await reconcile.reconcile_changed(s)
    assert out.get("applied") == 1
    assert pg_db.scalar("SELECT tags FROM servers WHERE id = $1", sid) == ["db"]


# ── per-rule schedules ──────────────────────────────────────────────────────

def test_schedules_are_validated_and_described(pg_db):
    with manager(pg_db) as c:
        weekly = {"kind": "weekly", "days": [0, 1, 2, 3, 4], "time": "02:00", "timezone": "Asia/Riyadh"}
        assert "needs the" in create(c, rule("x", [], [{"key": "tags", "value": ["a"]}], schedule=weekly), status=422)
        r = create(c, rule("Weekdays", [], [{"key": "tags", "value": ["a"]}], triggers=["scheduled"], schedule=weekly))
        assert r["schedule_text"] == "Weekdays at 02:00 (Asia/Riyadh)" and r["next_run_at"]
        assert r["schedule_last_slot"] is not None             # starting point: the next slot runs
        past = {"kind": "once", "at": "2020-01-01T10:00", "timezone": "UTC"}
        assert "future" in create(c, rule("y", [], [{"key": "tags", "value": ["a"]}], triggers=["scheduled"],
                                          schedule=past), status=422)
        bad = {"kind": "monthly", "day": 31, "time": "02:00", "timezone": "UTC"}
        assert "1 to 28" in create(c, rule("z", [], [{"key": "tags", "value": ["a"]}], triggers=["scheduled"],
                                           schedule=bad), status=422)
        status = c.get(f"{BASE}/schedule").json()
        assert [o["name"] for o in status["own_schedules"]] == ["Weekdays"] and status["rules"] == 0


@pytest.mark.asyncio
async def test_own_schedule_runs_at_its_slots_only_and_once_per_slot(pg_db):
    a = add_device(pg_db, "r1", "10.1.0.1")
    with manager(pg_db) as c:
        own = create(c, rule("Mon-Wed-Fri owner", [], [{"key": "owner", "value": "NOC"}], kinds=["device"],
                             triggers=["scheduled"],
                             schedule={"kind": "weekly", "days": [0, 2, 4], "time": "02:00", "timezone": "UTC"}))
        create(c, rule("Shared location", [], [{"key": "location", "value": "Riyadh"}], kinds=["device"],
                       triggers=["scheduled"]))
    enable_engine(pg_db)                                    # the shared schedule stays off
    mon = datetime(2026, 11, 30, 1, 0, tzinfo=timezone.utc)  # a Monday, 01:00
    pg_db.execute("UPDATE discovery_engine_rules SET schedule_last_slot = $1 WHERE id = $2",
                  mon - timedelta(days=3) + timedelta(hours=1), own["id"])   # Friday 02:00 already ran
    async with session(pg_db) as s:
        out = await reconcile.scheduled_pass(s, now=mon)
        assert out["skipped"] == "schedule_off" and "own_schedules" not in out
        out = await reconcile.scheduled_pass(s, now=mon + timedelta(hours=1, minutes=5))
        assert out["own_schedules"]["rules"] == ["Mon-Wed-Fri owner"] and out["own_schedules"]["applied"] == 1
        again = await reconcile.scheduled_pass(s, now=mon + timedelta(hours=2))
        assert "own_schedules" not in again
        # Tuesday is not a slot; Wednesday is
        assert "own_schedules" not in await reconcile.scheduled_pass(s, now=mon + timedelta(days=1, hours=3))
        pg_db.execute("UPDATE devices SET owner = NULL WHERE id = $1", a)
        pg_db.execute("DELETE FROM discovery_engine_assignments")
        wed = await reconcile.scheduled_pass(s, now=mon + timedelta(days=2, hours=1, minutes=1))
        assert wed["own_schedules"]["due"] == 1
    # the rule on the shared (off) schedule never ran
    assert pg_db.scalar("SELECT location FROM devices WHERE id = $1", a) is None
    assert pg_db.scalar("SELECT owner FROM devices WHERE id = $1", a) == "NOC"


@pytest.mark.asyncio
async def test_one_time_run_runs_once(pg_db):
    add_device(pg_db, "r1", "10.1.0.1")
    at = datetime.now(timezone.utc) + timedelta(days=2)
    with manager(pg_db) as c:
        create(c, rule("Once", [], [{"key": "owner", "value": "Migration"}], kinds=["device"], triggers=["scheduled"],
                       schedule={"kind": "once", "at": at.strftime("%Y-%m-%dT%H:%M"), "timezone": "UTC"}))
    enable_engine(pg_db)
    async with session(pg_db) as s:
        assert "own_schedules" not in await reconcile.scheduled_pass(s, now=at - timedelta(hours=1))
        out = await reconcile.scheduled_pass(s, now=at + timedelta(minutes=1))
        assert out["own_schedules"]["applied"] == 1
        assert "own_schedules" not in await reconcile.scheduled_pass(s, now=at + timedelta(days=30))
