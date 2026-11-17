"""Discovery rules phase 3 on a disposable PostgreSQL (migrate-194): the acceptance.

* after a device's firmware changes, rules with the "changed" trigger
  re-evaluate it in the next pass, and only that device is evaluated;
* a device that loses the tag a revert-on-unmatch rule depends on gets the
  rule's values restored, with the reason in history;
* rules on controller-managed children tag and group them; polling and
  credential actions are refused for children; the node count is unchanged;
* a rule edit that would change more than the cap waits for approval; nothing
  is applied until approved; a rejected plan is not proposed again;
* two workers never apply the same pass twice;
* rules place devices on a network map and add dependency parents, undo takes
  them off again;
* export / import round-trips rules by name; an unresolved name is reported;
  starter rules arrive as drafts, once;
* migrate-194 re-applies cleanly over existing phase 2 data.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.discovery_rules import reconcile, runner
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

BASE = "/api/v1/discovery-v2/rules"
MANAGER = ["discovery.view", "discovery.run", "discovery.manage_rules", "devices.view", "devices.manage",
           "servers.view", "servers.manage", "maps.view", "maps.manage"]
MIGRATION = Path(__file__).resolve().parents[2] / "scripts/migrate-194-discovery-rules-reconcile.sql"


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


def add_device(db, name, ip, *, vendor="Fortinet", tags=(), os_version="7.2.9", **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 6}" for i in range(len(cols)))
    return str(db.scalar(
        f"INSERT INTO devices (hostname, ip_address, vendor, tags, os_version{names})"
        f" VALUES ($1, $2::inet, $3, $4::jsonb, $5{vals}) RETURNING id",
        name, ip, vendor, list(tags), os_version, *cols.values()))


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "rules-manager"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, **kw)


def rule(name, condition, actions, **kw):
    return {"name": name, "mode": "active", "asset_kinds": kw.pop("kinds", ["device"]),
            "triggers": kw.pop("triggers", ["changed"]), "condition": {"op": "all", "conditions": condition},
            "actions": actions, **kw}


async def rpass(db):
    async with session(db) as s:
        return await reconcile.reconcile_changed(s)


# ── acceptance ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_firmware_change_reevaluates_only_that_device(pg_db):
    add_device(pg_db, "fw1", "10.1.0.1")
    fw2 = add_device(pg_db, "fw2", "10.1.0.2")
    add_device(pg_db, "fw3", "10.1.0.3")
    with manager(pg_db) as c:
        c.post(BASE, json=rule("Old FortiOS", [{"field": "os_version", "operator": "version_lt", "value": "7.4"}],
                               [{"key": "tags", "value": ["fortios-7.2"]}])).raise_for_status()
    enable_engine(pg_db)
    first = await rpass(pg_db)
    assert first["assets"] == 3 and first["applied"] == 3
    settle = await rpass(pg_db)          # the tags it added change the fingerprints once
    assert settle["assets"] == 3 and settle.get("applied", 0) == 0
    assert await rpass(pg_db) == {"assets": 0}
    pg_db.execute("UPDATE devices SET os_version = '7.4.3', status = 'down' WHERE id = $1", fw2)
    pg_db.execute("UPDATE devices SET status = 'down'")       # status flips never re-evaluate
    out = await rpass(pg_db)
    assert out["assets"] == 1 and out.get("applied", 0) == 0  # set once: the tag stays


@pytest.mark.asyncio
async def test_revert_on_unmatch_restores_the_previous_values(pg_db):
    dev = add_device(pg_db, "fw1", "10.1.0.1", tags=["dc-riyadh"], location="Old site")
    with manager(pg_db) as c:
        c.post(BASE, json=rule("Riyadh DC", [{"field": "tags", "operator": "has_any", "value": ["dc-riyadh"]}],
                               [{"key": "location", "value": "Riyadh DC"}, {"key": "owner", "value": "NOC"}],
                               on_unmatch="revert")).raise_for_status()
    enable_engine(pg_db)
    await rpass(pg_db)
    assert pg_db.query("SELECT location, owner FROM devices") == [("Riyadh DC", "NOC")]
    await rpass(pg_db)
    pg_db.execute("UPDATE devices SET tags = '[]'::jsonb WHERE id = $1", dev)
    out = await rpass(pg_db)
    assert out["reverted"] == 2
    assert pg_db.query("SELECT location, owner FROM devices") == [("Old site", None)]
    assert pg_db.scalar("SELECT count(*) FROM discovery_engine_assignments") == 0
    details = {d for (d,) in pg_db.query(
        "SELECT detail FROM discovery_engine_run_items WHERE outcome = 'reverted'")}
    assert details == {"The asset no longer matches the rule (revert on un-match)"}


@pytest.mark.asyncio
async def test_children_are_organised_never_polled_or_licensed(pg_db):
    fgt = add_device(pg_db, "fgt-01", "10.2.0.1")
    for i in range(3):
        add_device(pg_db, f"FAP-{i}", f"10.2.1.{i + 1}", poll_mode="via_controller", managed_by_device_id=fgt,
                   managed_source="fortiap")
    group = add_group(pg_db, "Access points")
    from app.services.license_capacity import count_nodes
    async with session(pg_db) as s:
        nodes_before = await count_nodes(s)
    with manager(pg_db) as c:
        r = c.post(BASE, json=rule("FortiAPs", [{"field": "controller", "operator": "equals", "value": "fgt-01"}],
                                   [{"key": "ping_interval", "value": 60}], kinds=["child"]))
        assert r.status_code == 422 and "does not apply to child" in r.json()["detail"]
        c.post(BASE, json=rule("FortiAPs", [{"field": "controller", "operator": "equals", "value": "fgt-01"}],
                               [{"key": "group", "value": group}, {"key": "tags", "value": ["fortiap"]}],
                               kinds=["child"])).raise_for_status()
    enable_engine(pg_db)
    out = await rpass(pg_db)
    assert out["applied"] == 6
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE group_id = $1 AND tags @> '[\"fortiap\"]'"
                        " AND poll_mode = 'via_controller'", group) == 3
    assert pg_db.scalar("SELECT tags FROM devices WHERE id = $1", fgt) == []
    async with session(pg_db) as s:
        assert await count_nodes(s) == nodes_before


@pytest.mark.asyncio
async def test_more_changes_than_the_cap_wait_for_approval(pg_db):
    group = add_group(pg_db, "G")
    for i in range(6):
        add_device(pg_db, f"fw{i}", f"10.3.0.{i + 1}")
    with manager(pg_db) as c:
        rid = c.post(BASE, json=rule("All Fortinet", [{"field": "vendor", "operator": "equals",
                                                       "value": "Fortinet"}],
                                     [{"key": "group", "value": group}, {"key": "owner", "value": "NOC"}])).json()["id"]
    enable_engine(pg_db, max_changes_per_run=5)
    out = await rpass(pg_db)
    assert out["changes"] == 12 and "needs_approval" in out
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE group_id IS NOT NULL") == 0
    assert (await rpass(pg_db))["skipped"] == "waiting_for_approval"
    with manager(pg_db) as c:
        pending = c.get(f"{BASE}/history/{out['needs_approval']}/pending").json()
        assert pending["status"] == "needs_approval" and len(pending["changes"]) == 12
        res = c.post(f"{BASE}/history/{out['needs_approval']}/approve").json()
        assert res["applied"] == 12 and res["status"] == "completed"
        assert c.post(f"{BASE}/history/{out['needs_approval']}/approve").status_code == 409
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE group_id = $1 AND owner = 'NOC'", group) == 6

    # A rule edit that would change everything again: rejected, then not proposed again.
    with manager(pg_db) as c:
        body = rule("All Fortinet", [{"field": "vendor", "operator": "equals", "value": "Fortinet"}],
                    [{"key": "group", "value": group}, {"key": "owner", "value": "Field team"}],
                    enforcement="enforce")
        c.put(f"{BASE}/{rid}", json={**body, "expected_revision": 1}).raise_for_status()
    held = await rpass(pg_db)             # the edit changes the owner on all 6: above the cap again
    assert "needs_approval" in held and held["changes"] == 6
    with manager(pg_db) as c:
        assert c.post(f"{BASE}/history/{held['needs_approval']}/reject").json()["status"] == "rejected"
    assert await rpass(pg_db) == {"assets": 0}
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE owner = 'NOC'") == 6
    actions = {a for (a,) in pg_db.query("SELECT action FROM audit_logs WHERE action LIKE 'discovery.rules.%'")}
    assert {"discovery.rules.approve", "discovery.rules.reject"} <= actions


@pytest.mark.asyncio
async def test_two_workers_never_apply_a_pass_twice(pg_db):
    for i in range(20):
        add_device(pg_db, f"fw{i}", f"10.4.0.{i + 1}")
    with manager(pg_db) as c:
        c.post(BASE, json=rule("Tag", [{"field": "vendor", "operator": "equals", "value": "Fortinet"}],
                               [{"key": "tags", "value": ["x"]}])).raise_for_status()
    enable_engine(pg_db)
    a, b = await asyncio.gather(rpass(pg_db), rpass(pg_db))
    assert a.get("applied", 0) + b.get("applied", 0) == 20
    assert pg_db.scalar("SELECT count(*) FROM discovery_engine_runs WHERE trigger = 'changed'"
                        " AND mode = 'apply'") == 1
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE tags = '[\"x\"]'::jsonb") == 20


def test_maps_and_dependencies_and_their_undo(pg_db):
    core = add_device(pg_db, "core-sw", "10.5.0.1", vendor="Cisco")
    fw = add_device(pg_db, "fw1", "10.5.0.2")
    m = str(pg_db.scalar("INSERT INTO manual_maps (name) VALUES ('Riyadh') RETURNING id"))
    with manager(pg_db) as c:
        c.post(BASE, json=rule("Riyadh firewalls", [{"field": "vendor", "operator": "equals", "value": "Fortinet"}],
                               [{"key": "map", "value": m}, {"key": "dependency_parent", "value": core}],
                               triggers=["discovery"])).raise_for_status()
        pre = c.post(f"{BASE}/inventory/preview", json={"targets": {"kinds": ["device"]}}).json()
        assert {(x["action_key"], x["asset_id"]) for x in pre["changes"]} == {("map", fw), ("dependency_parent", fw)}
        run_id = c.post(f"{BASE}/inventory/apply", json={"targets": {"kinds": ["device"]},
                                                         "digest": pre["digest"]}).json()["engine_run_id"]
        assert pg_db.scalar("SELECT count(*) FROM manual_map_nodes WHERE map_id = $1 AND device_id = $2", m, fw) == 1
        assert pg_db.query("SELECT parent_device_id::text, dependency_type FROM topology_dependencies"
                           " WHERE child_device_id = $1", fw) == [(core, "uplink")]
        again = c.post(f"{BASE}/inventory/preview", json={"targets": {"kinds": ["device"]}}).json()
        assert again["changes"] == []
        assert c.post(f"{BASE}/history/{run_id}/undo").json()["reverted"] == 2
    assert pg_db.scalar("SELECT count(*) FROM manual_map_nodes") == 0
    assert pg_db.scalar("SELECT count(*) FROM topology_dependencies") == 0


def test_export_import_and_starter_rules(pg_db):
    group = add_group(pg_db, "Firewalls")
    with manager(pg_db) as c:
        c.post(BASE, json=rule("FW", [{"field": "group_id", "operator": "is", "value": group}],
                               [{"key": "group", "value": group}, {"key": "tags", "value": ["fw"]}],
                               triggers=["discovery", "changed"], on_unmatch="revert")).raise_for_status()
        doc = c.get(f"{BASE}/export").json()
        assert doc["format"] == "zenplus-discovery-rules"
        exported = doc["rules"][0]
        assert exported["actions"][0]["value"] == {"$ref": "device_groups", "name": "Firewalls"}
        assert group not in str(doc)                                    # no ids travel
        out = c.post(f"{BASE}/import", json={"document": doc}).json()
        assert [x["name"] for x in out["created"]] == ["FW (imported)"] and out["skipped"] == []
        imported = c.get(f"{BASE}/{out['created'][0]['id']}").json()
        assert imported["mode"] == "draft" and imported["actions"][0]["value"] == group
        assert imported["on_unmatch"] == "revert" and imported["triggers"] == ["discovery", "changed"]
        doc["rules"][0]["actions"][0]["value"]["name"] = "Nowhere"
        out = c.post(f"{BASE}/import", json={"document": doc}).json()
        assert out["created"] == [] and "no device group named 'Nowhere'" in out["skipped"][0]["reason"]

        starters = c.get(f"{BASE}/starter").json()
        assert {"fortigate", "cisco", "printers"} <= {s["key"] for s in starters}
        made = c.post(f"{BASE}/starter", json={"keys": ["fortigate", "printers"]}).json()["created"]
        assert [m["key"] for m in made] == ["fortigate", "printers"]
        assert c.post(f"{BASE}/starter", json={"keys": ["fortigate"]}).json()["created"] == []
        modes = {r["name"]: r["mode"] for r in c.get(BASE).json()}
        assert modes["Fortinet FortiGate firewalls"] == "draft" and modes["Ignore printers (example)"] == "draft"
    with api_client(pg_db, role="viewer-only", permissions=["discovery.view"]) as c:
        assert c.post(f"{BASE}/import", json={"document": doc}).status_code == 403
        assert c.post(f"{BASE}/starter", json={"keys": ["cisco"]}).status_code == 403


def test_device_type_actions_follow_the_catalogue(pg_db):
    with manager(pg_db) as c:
        r = c.post(BASE, json=rule("T", [], [{"key": "device_type", "value": "toaster"}], triggers=["discovery"]))
        assert r.status_code == 422 and "Unknown device type" in r.json()["detail"]
        assert c.post(BASE, json=rule("T", [], [{"key": "device_type", "value": "firewall"}],
                                      triggers=["discovery"])).status_code == 201


def test_migration_194_reapplies_over_existing_data(pg_db):
    dev = add_device(pg_db, "fw1", "10.6.0.1")
    pg_db.execute("INSERT INTO discovery_engine_assignments (asset_kind, asset_id, action_key, item_key, value)"
                  " VALUES ('device', $1, 'owner', '', '\"NOC\"'::jsonb)", dev)
    pg_db.execute("INSERT INTO discovery_engine_asset_state (asset_kind, asset_id, created_evaluated_at)"
                  " VALUES ('device', $1, now())", dev)
    pg_db.execute("INSERT INTO discovery_engine_runs (trigger, mode, status) VALUES ('manual', 'apply', 'completed')")
    pg_db.psql_file(MIGRATION)
    pg_db.psql_file(MIGRATION)                                          # half-applied / re-run safe
    assert pg_db.scalar("SELECT count(*) FROM discovery_engine_assignments") == 1
    pg_db.execute("INSERT INTO discovery_engine_asset_state (asset_kind, asset_id, facts_hash)"
                  " VALUES ('child', gen_random_uuid(), 'x')")
    pg_db.execute("INSERT INTO discovery_engine_runs (trigger, mode, status) VALUES ('changed', 'apply', 'rejected')")


@pytest.mark.asyncio
async def test_explain_a_child_and_its_map_placement(pg_db):
    fgt = add_device(pg_db, "fgt-01", "10.7.0.1")
    ap = add_device(pg_db, "FAP-1", "10.7.1.1", poll_mode="via_controller", managed_by_device_id=fgt,
                    managed_source="fortiap")
    m = str(pg_db.scalar("INSERT INTO manual_maps (name) VALUES ('APs') RETURNING id"))
    with manager(pg_db) as c:
        c.post(BASE, json=rule("APs", [{"field": "managed_source", "operator": "equals", "value": "fortiap"}],
                               [{"key": "map", "value": m}], kinds=["child"])).raise_for_status()
    enable_engine(pg_db)
    assert (await rpass(pg_db))["applied"] == 1
    with manager(pg_db) as c:
        out = c.get(f"{BASE}/assets/child/{ap}").json()
    assert [(x["action_key"], x["item_key"], x["state"]) for x in out["settings"]] == [("map", m, "rule")]
    assert out["pending"] == [] and out["matching_rules"][0]["rule_name"] == "APs"
