"""Discovery rules phase 2 on a disposable PostgreSQL (migrate-191): the acceptance.

* running a rule on 300 existing devices shows a preview; applying changes
  exactly the previewed set; a second run changes nothing;
* a value edited by hand after a set-once rule set it is left alone and
  reported as a kept manual edit; the same rule in enforce mode restores it;
* undo restores every value still equal to what the rule set, skips values
  changed since, and lists both;
* a rule in Simulating mode records what it would do and writes nothing;
* a device added outside discovery gets the rules with the "when added"
  trigger, once; while the engine is off new devices are only marked as seen;
* the device's explain view shows which rule set its group, template and tags;
* tag-scoped users cannot run rules and see only in-scope assets in history;
* a preview that went stale is refused; servers take owner/environment/tags.
"""
from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.discovery_rules import inventory, runner
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

BASE = "/api/v1/discovery-v2/rules"
MANAGER = ["discovery.view", "discovery.run", "discovery.manage_rules", "devices.view", "devices.manage",
           "servers.view", "servers.manage"]


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
    pg_db.execute("TRUNCATE devices, servers CASCADE")


def enable_engine(db, **kw):
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'discovery_rules'",
               {**runner.DEFAULT_SETTINGS, "enabled": True, **kw})


def add_group(db, name):
    return str(db.scalar("INSERT INTO device_groups (name) VALUES ($1) RETURNING id", name))


def add_template(db, name):
    return str(db.scalar("INSERT INTO device_profiles (name, vendor) VALUES ($1, 'Fortinet') RETURNING id", name))


def add_devices(db, n, *, prefix="10.51", vendor="Fortinet", tags=("site-a",)):
    db.execute(
        "INSERT INTO devices (hostname, ip_address, vendor, tags, status, created_at)"
        " SELECT 'dev-' || g, ($1 || '.' || (g / 250) || '.' || (g % 250 + 1))::inet, $2, $3::jsonb, 'up',"
        " now() - interval '1 day' FROM generate_series(0, $4 - 1) g", prefix, vendor, list(tags), n)
    return [str(r[0]) for r in db.query("SELECT id FROM devices WHERE ip_address <<= ($1 || '.0.0/16')::inet"
                                         " ORDER BY hostname", prefix)]


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "rules-manager"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, **kw)


def vendor_rule(group, template=None, *, name="Fortinet devices", enforce=False, mode="active", tags=("firewall",),
                triggers=("discovery",), kinds=("device",)):
    acts = [{"key": "group", "value": group}, {"key": "tags", "value": list(tags)},
            {"key": "location", "value": "Riyadh DC"}]
    if template:
        acts.append({"key": "template", "value": template})
    return {"name": name, "mode": mode, "asset_kinds": list(kinds), "triggers": list(triggers),
            "enforcement": "enforce" if enforce else "set_once",
            "condition": {"op": "all", "conditions": [{"field": "vendor", "operator": "equals", "value": "Fortinet"}]},
            "actions": acts}


def preview(c, **body):
    r = c.post(f"{BASE}/inventory/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def apply(c, pre, **body):
    r = c.post(f"{BASE}/inventory/apply", json={**body, "digest": pre["digest"]})
    assert r.status_code == 200, r.text
    return r.json()


# ── acceptance ──────────────────────────────────────────────────────────────

def test_preview_then_apply_exactly_that_and_a_second_run_changes_nothing(pg_db):
    group, template = add_group(pg_db, "Firewalls"), add_template(pg_db, "FortiGate")
    add_devices(pg_db, 300)
    add_devices(pg_db, 5, prefix="10.60", vendor="Cisco")
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group, template)).raise_for_status()
        pre = preview(c, targets={"kinds": ["device"]})
        assert pre["summary"]["assets"] == 305 and pre["summary"]["matched"] == 300
        assert pre["summary"]["changes"] == 1200 and pre["summary"]["assets_changed"] == 300
        assert pre["summary"]["by_action"] == {"group": 300, "template": 300, "location": 300, "tags": 300}
        out = apply(c, pre, targets={"kinds": ["device"]})
        assert out["applied"] == 1200 and out["skipped_changed"] == 0 and out["status"] == "completed"
        again = preview(c, targets={"kinds": ["device"]})
        assert again["summary"]["changes"] == 0 and again["changes"] == []
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE group_id = $1 AND profile_id = $2"
                        " AND location = 'Riyadh DC' AND tags @> '[\"firewall\", \"site-a\"]'", group, template) == 300
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE vendor = 'Cisco' AND group_id IS NULL") == 5
    assert pg_db.scalar("SELECT count(DISTINCT profile_source) FROM devices WHERE vendor = 'Fortinet'") == 1
    assert pg_db.scalar("SELECT profile_source FROM devices WHERE vendor = 'Fortinet' LIMIT 1") == "rule"
    assert pg_db.scalar("SELECT count(*) FROM discovery_engine_assignments") == 1200
    applied = {(str(a), k, i or "") for a, k, i in pg_db.query(
        "SELECT asset_id, action_key, item_key FROM discovery_engine_run_items WHERE outcome = 'applied'")}
    assert applied == {(c_["asset_id"], c_["action_key"], c_["item_key"]) for c_ in pre["changes"]}
    actions = {a for (a,) in pg_db.query("SELECT action FROM audit_logs WHERE action = 'discovery.rules.run'")}
    assert actions == {"discovery.rules.run"}


def test_a_stale_preview_is_refused(pg_db):
    group = add_group(pg_db, "G")
    ids = add_devices(pg_db, 3)
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group)).raise_for_status()
        pre = preview(c, targets={"kinds": ["device"]})
        pg_db.execute("UPDATE devices SET location = 'Elsewhere' WHERE id = $1", ids[0])
        r = c.post(f"{BASE}/inventory/apply", json={"targets": {"kinds": ["device"]}, "digest": pre["digest"]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "preview_stale"
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE group_id IS NOT NULL") == 0


def test_hand_edits_are_kept_by_set_once_and_restored_by_enforce(pg_db):
    g1, g2 = add_group(pg_db, "G1"), add_group(pg_db, "G2")
    ids = add_devices(pg_db, 2)
    with manager(pg_db) as c:
        rid = c.post(BASE, json=vendor_rule(g1)).json()["id"]
        apply(c, preview(c, targets={"kinds": ["device"]}), targets={"kinds": ["device"]})
        pg_db.execute("UPDATE devices SET group_id = $1, tags = '[\"site-a\"]'::jsonb WHERE id = $2", g2, ids[0])
        pre = preview(c, targets={"kinds": ["device"]})
        assert {(c_["action_key"], c_["outcome"]) for c_ in pre["changes"]} == {
            ("group", "kept_manual_edit"), ("tags", "kept_manual_edit")}
        out = apply(c, pre, targets={"kinds": ["device"]})
        assert out["applied"] == 0 and out["kept_manual_edit"] == 2
        assert pg_db.scalar("SELECT group_id::text FROM devices WHERE id = $1", ids[0]) == g2

        body = {**vendor_rule(g1, enforce=True), "expected_revision": 1}
        c.put(f"{BASE}/{rid}", json=body).raise_for_status()
        pre = preview(c, targets={"kinds": ["device"]})
        assert {(c_["action_key"], c_["outcome"]) for c_ in pre["changes"]} == {("group", "apply"), ("tags", "apply")}
        apply(c, pre, targets={"kinds": ["device"]})
    assert pg_db.scalar("SELECT group_id::text FROM devices WHERE id = $1", ids[0]) == g1
    assert pg_db.scalar("SELECT tags FROM devices WHERE id = $1", ids[0]) == ["site-a", "firewall"]
    kept = pg_db.scalar("SELECT count(*) FROM discovery_engine_run_items WHERE outcome = 'kept_manual_edit'")
    assert kept == 2


def test_undo_restores_unchanged_values_and_lists_the_rest(pg_db):
    group = add_group(pg_db, "G")
    ids = add_devices(pg_db, 3)
    pg_db.execute("UPDATE devices SET location = 'Old site'")
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group)).raise_for_status()
        run_id = apply(c, preview(c, targets={"kinds": ["device"]}), targets={"kinds": ["device"]})["engine_run_id"]
        pg_db.execute("UPDATE devices SET location = 'Hand edit' WHERE id = $1", ids[0])
        out = c.post(f"{BASE}/history/{run_id}/undo").json()
        assert out["reverted"] == 8 and out["skipped_changed"] == 1     # 3 devices x 3 values, 1 edited and out["imported_devices"] == []
        assert c.post(f"{BASE}/history/{run_id}/undo").status_code == 409            # once
        items = c.get(f"{BASE}/history/{out['engine_run_id']}/items").json()
        assert {i["outcome"] for i in items} == {"reverted", "skipped_changed"}
    assert pg_db.query("SELECT location, group_id, tags FROM devices ORDER BY hostname") == [
        ("Hand edit", None, ["site-a"]), ("Old site", None, ["site-a"]), ("Old site", None, ["site-a"])]
    assert pg_db.scalar("SELECT count(*) FROM discovery_engine_assignments") == 1    # the kept hand edit's
    assert pg_db.scalar("SELECT status FROM discovery_engine_runs WHERE id = $1", run_id) == "undone"


@pytest.mark.asyncio
async def test_new_devices_get_created_rules_once_and_simulating_rules_write_nothing(pg_db):
    group = add_group(pg_db, "G")
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group, triggers=("created",))).raise_for_status()
        c.post(BASE, json={**vendor_rule(group, name="Sim", mode="simulate", triggers=("created",), tags=("sim",)),
                           "actions": [{"key": "tags", "value": ["sim"]}, {"key": "owner", "value": "NOC"}]}
               ).raise_for_status()
        c.post(BASE, json=vendor_rule(group, name="Scan only", tags=("scan",))).raise_for_status()
    # Added while the engine is off: marked as seen, never evaluated later.
    pg_db.execute("INSERT INTO devices (hostname, ip_address, vendor, tags) VALUES ('early', '10.9.0.1', 'Fortinet', '[]')")
    async with session(pg_db) as s:
        assert (await inventory.sweep_created(s))["enabled"] is False
    enable_engine(pg_db)
    pg_db.execute("INSERT INTO devices (hostname, ip_address, vendor, tags) VALUES ('new-fw', '10.9.0.2', 'Fortinet', '[]')")
    async with session(pg_db) as s:
        out = await inventory.sweep_created(s)
    assert out["assets"] == 1 and out["applied"] == 3 and out["simulated"] == 2
    async with session(pg_db) as s:
        assert (await inventory.sweep_created(s)) == {"assets": 0}                    # once
    assert pg_db.query("SELECT hostname, group_id::text, tags, owner FROM devices ORDER BY hostname") == [
        ("early", None, [], None), ("new-fw", group, ["firewall"], None)]
    sim = pg_db.query("SELECT i.action_key, i.outcome FROM discovery_engine_run_items i"
                      " JOIN discovery_engine_runs r ON r.id = i.run_id WHERE r.mode = 'simulate' ORDER BY 1")
    assert sim == [("owner", "simulated"), ("tags", "simulated")]


def test_explain_shows_what_rules_set_and_kept_edits(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    ids = add_devices(pg_db, 1)
    with manager(pg_db) as c:
        rid = c.post(BASE, json=vendor_rule(group, template)).json()["id"]
        run_id = apply(c, preview(c, targets={"device_ids": [ids[0]], "kinds": ["device"]}),
                       targets={"device_ids": [ids[0]], "kinds": ["device"]})["engine_run_id"]
        pg_db.execute("UPDATE devices SET location = 'Hand edit' WHERE id = $1", ids[0])
    with api_client(pg_db, role="dev-viewer", permissions=["devices.view"]) as c:
        out = c.get(f"{BASE}/assets/device/{ids[0]}").json()
    by_key = {(s["action_key"], s["item_key"]): s for s in out["settings"]}
    assert by_key[("group", None)]["state"] == "rule" and by_key[("group", None)]["rule_id"] == rid
    assert by_key[("group", None)]["run_id"] == run_id
    assert by_key[("template", None)]["value"] == template
    assert by_key[("tags", "firewall")]["state"] == "rule"
    assert by_key[("location", None)]["state"] == "manual_edit"
    assert out["matching_rules"][0]["rule_id"] == rid and out["pending"][0]["outcome"] == "kept_manual_edit"
    assert {h["run_id"] for h in out["history"]} == {run_id}


def test_servers_take_owner_environment_and_tags(pg_db):
    sid = str(pg_db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status, os_type, tags)"
                           " VALUES ('app01', 'app01', 'agent', 'healthy', 'linux', '[]') RETURNING id"))
    with manager(pg_db) as c:
        c.post(BASE, json={"name": "Linux servers", "mode": "active", "asset_kinds": ["server"],
                           "condition": {"op": "all", "conditions": [
                               {"field": "os_type", "operator": "equals", "value": "linux"}]},
                           "actions": [{"key": "environment", "value": "production"},
                                       {"key": "owner", "value": "Systems team"},
                                       {"key": "tags", "value": ["linux"]}]}).raise_for_status()
        apply(c, preview(c, targets={"kinds": ["server"]}), targets={"kinds": ["server"]})
    assert pg_db.query("SELECT environment, owner, tags FROM servers WHERE id = $1", sid) == [
        ("production", "Systems team", ["linux"])]


def test_run_needs_manage_rules_and_the_action_permissions(pg_db):
    group = add_group(pg_db, "G")
    add_devices(pg_db, 1)
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group)).raise_for_status()
    with api_client(pg_db, role="disc-runner", permissions=["discovery.view", "discovery.run"]) as c:
        assert c.post(f"{BASE}/inventory/preview", json={}).status_code == 403
    with api_client(pg_db, role="rules-only", permissions=["discovery.view", "discovery.manage_rules"]) as c:
        pre = preview(c, targets={"kinds": ["device"]})
        r = c.post(f"{BASE}/inventory/apply", json={"targets": {"kinds": ["device"]}, "digest": pre["digest"]})
        assert r.status_code == 403 and "devices.manage" in r.json()["detail"]
    with manager(pg_db, role="scoped-mgr", scope_tags=("site-a",)) as c:
        assert c.post(f"{BASE}/inventory/preview", json={}).status_code == 403
    with manager(pg_db) as c:
        r = c.post(f"{BASE}/inventory/preview", json={"rule_ids": ["00000000-0000-0000-0000-000000000001"]})
        assert r.status_code == 422


def test_scoped_viewers_see_only_their_assets_in_history(pg_db):
    group = add_group(pg_db, "G")
    add_devices(pg_db, 2, tags=("team-a",))
    add_devices(pg_db, 2, prefix="10.52", tags=("team-b",))
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(group)).raise_for_status()
        run_id = apply(c, preview(c, targets={"kinds": ["device"]}), targets={"kinds": ["device"]})["engine_run_id"]
    with api_client(pg_db, role="team-a-viewer", permissions=["discovery.view", "devices.view"],
                    scope_tags=("team-a",)) as c:
        items = c.get(f"{BASE}/history/{run_id}/items").json()
        hosts = {h for (h,) in pg_db.query("SELECT id::text FROM devices WHERE tags @> '[\"team-a\"]'")}
        assert items and {i["asset_id"] for i in items} <= hosts
        other = pg_db.scalar("SELECT id::text FROM devices WHERE tags @> '[\"team-b\"]' LIMIT 1")
        assert c.get(f"{BASE}/assets/device/{other}").status_code == 404


def test_conflicts_report(pg_db):
    g1, g2 = add_group(pg_db, "G1"), add_group(pg_db, "G2")
    add_devices(pg_db, 4)
    with manager(pg_db) as c:
        c.post(BASE, json=vendor_rule(g1, name="Vendor")).raise_for_status()
        c.post(BASE, json={**vendor_rule(g2, name="Subnet", tags=("riyadh",)), "condition": {"op": "all", "conditions": [
            {"field": "ip_address", "operator": "in_subnet", "value": ["10.51.0.0/16"]}]}}).raise_for_status()
        c.post(BASE, json={**vendor_rule(g2, name="Nothing"), "condition": {"op": "all", "conditions": [
            {"field": "vendor", "operator": "equals", "value": "Nobody"}]}}).raise_for_status()
        out = c.get(f"{BASE}/conflicts").json()
    assert out["assets"] == 4
    assert [(o["rule_name"], o["winner_rule_name"], o["action_key"], o["assets"]) for o in out["overridden"]] == [
        ("Subnet", "Vendor", "group", 4), ("Subnet", "Vendor", "location", 4)]
    assert [r["rule_name"] for r in out["never_match"]] == ["Nothing"]
    assert out["never_win"] == []      # Subnet still adds its tag


@pytest.mark.asyncio
async def test_undo_of_a_scan_lists_imported_devices_and_never_deletes_them(pg_db):
    profile = str(pg_db.scalar("INSERT INTO discovery_profiles (name, import_mode) VALUES ('p', 'auto_match')"
                               " RETURNING id"))
    run = str(pg_db.scalar("INSERT INTO discovery_runs (profile_id, trigger_type, status, phase)"
                           " VALUES ($1, 'manual', 'completed', 'done') RETURNING id", profile))
    pg_db.execute("INSERT INTO discovery_results_v2 (run_id, profile_id, ip_address, hostname, vendor, status,"
                  " import_ready, credential_status, raw_data) VALUES ($1, $2, '10.70.0.1', 'fw-x', 'Fortinet',"
                  " 'new', true, 'valid', '{}'::jsonb)", run, profile)
    with manager(pg_db) as c:
        c.post(BASE, json={"name": "Import Fortinet", "mode": "active",
                           "condition": {"op": "all", "conditions": [
                               {"field": "vendor", "operator": "equals", "value": "Fortinet"}]},
                           "actions": [{"key": "decision", "value": "auto_import"}]}).raise_for_status()
        c.post(BASE, json={"name": "Watch", "mode": "simulate", "condition": {"op": "all", "conditions": []},
                           "actions": [{"key": "tags", "value": ["watched"]}]}).raise_for_status()
    enable_engine(pg_db)
    async with session(pg_db) as s:
        summary = await runner.evaluate_discovery_run(s, run, trigger="discovery")
    assert summary["imported"] == 1 and summary["simulated"] == 1
    assert pg_db.scalar("SELECT tags FROM devices") == []                     # the simulating rule wrote nothing
    with manager(pg_db) as c:
        out = c.post(f"{BASE}/history/{summary['engine_run_id']}/undo").json()
    assert out["reverted"] == 0 and [d["hostname"] for d in out["imported_devices"]] == ["fw-x"]
    assert pg_db.scalar("SELECT count(*) FROM devices") == 1
