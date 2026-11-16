"""Discovery rules on a disposable PostgreSQL (migrate-190): the phase 1 acceptance.

* a FortiGate rule (sysObjectID subtree AND subnet) with template, group, two
  tags and auto-import gives a scan on an ``auto_match`` profile devices with
  exactly those settings and no manual step;
* the same scan on a ``review`` profile imports nothing, and the results say
  which rule matched and what it would have done;
* the preview of an unsaved rule equals what the saved rule then applies;
* with 3 license nodes free and 10 results marked for auto-import, 3 are
  imported, 7 stay in review with the capacity reason, the run is partial;
* an ignore rule puts the address on the Ignored list with the rule as reason;
* without ``discovery.manage_rules`` every write is refused; a tag-scoped user
  cannot change rules; activation needs the permissions the actions use;
* with the engine off nothing is evaluated (the default after the upgrade).

Routes run through the real app; the engine runs against the same database.
"""
from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import license_capacity as lc
from app.services.discovery_rules import runner
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

FORTI_OID = ".1.3.6.1.4.1.12356.101.1.1000"
MANAGER = ["discovery.view", "discovery.run", "discovery.manage_rules", "devices.view", "devices.manage"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def empty_inventory(pg_db):
    """The template database carries seeded demo devices; start from none."""
    pg_db.execute("TRUNCATE devices, servers CASCADE")


def add_profile(db, name="Riyadh scan", mode="auto_match", tags=()):
    return str(db.scalar(
        "INSERT INTO discovery_profiles (name, import_mode, default_tags, enable_monitoring)"
        " VALUES ($1, $2, $3::jsonb, true) RETURNING id", name, mode, list(tags)))


def add_run(db, profile):
    return str(db.scalar(
        "INSERT INTO discovery_runs (profile_id, trigger_type, status, phase)"
        " VALUES ($1, 'scheduled', 'completed', 'done') RETURNING id", profile))


def add_result(db, run, profile, ip, *, oid=None, vendor=None, status="new", ready=True, cred="valid",
               hostname=None, device_type=None):
    return db.scalar(
        "INSERT INTO discovery_results_v2 (run_id, profile_id, ip_address, hostname, sys_object_id, vendor,"
        " device_type, status, import_ready, credential_status, raw_data)"
        " VALUES ($1, $2, $3::inet, $4, $5, $6, $7, $8, $9, $10, '{}'::jsonb) RETURNING id",
        run, profile, ip, hostname or f"host-{ip}", oid, vendor, device_type, status, ready, cred)


def add_group(db, name):
    return str(db.scalar("INSERT INTO device_groups (name) VALUES ($1) RETURNING id", name))


def add_template(db, name):
    return str(db.scalar("INSERT INTO device_profiles (name, vendor) VALUES ($1, 'Fortinet') RETURNING id", name))


def enable_engine(db, **kw):
    value = {**runner.DEFAULT_SETTINGS, "enabled": True, **kw}
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'discovery_rules'", value)


def forti_rule(group, template, *, decision="auto_import", name="FortiGate Riyadh", mode="active"):
    return {
        "name": name, "mode": mode, "asset_kinds": ["device"],
        "condition": {"op": "all", "conditions": [
            {"field": "sys_object_id", "operator": "starts_with", "value": "1.3.6.1.4.1.12356"},
            {"field": "ip_address", "operator": "in_subnet", "value": ["10.51.0.0/16"]}]},
        "actions": [{"key": "decision", "value": decision}, {"key": "template", "value": template},
                    {"key": "group", "value": group}, {"key": "tags", "value": ["firewall", "riyadh"]}],
    }


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "rules-manager"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, **kw)


async def evaluate(db, run, **kw):
    async with session(db) as s:
        return await runner.evaluate_discovery_run(s, run, trigger="discovery", **kw)


# ── acceptance ──────────────────────────────────────────────────────────────

def test_engine_is_off_after_the_upgrade(pg_db):
    with api_client(pg_db, role="admin", persist_user=True) as c:
        assert c.get("/api/v1/discovery-v2/rules/settings").json()["enabled"] is False
        run = add_run(pg_db, add_profile(pg_db))
        r = c.post(f"/api/v1/discovery-v2/rules/evaluate-scan/{run}", json={"apply": True})
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_scheduled_scan_on_auto_match_profile_imports_with_rule_settings(pg_db):
    group, template = add_group(pg_db, "Riyadh firewalls"), add_template(pg_db, "FortiGate template")
    with manager(pg_db) as c:
        assert c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).status_code == 201
    enable_engine(pg_db)
    profile = add_profile(pg_db, tags=["discovered"])
    run = add_run(pg_db, profile)
    fw = add_result(pg_db, run, profile, "10.51.2.12", oid=FORTI_OID, vendor="Fortinet", device_type="firewall")
    elsewhere = add_result(pg_db, run, profile, "10.60.0.1", oid=FORTI_OID, vendor="Fortinet")
    cisco = add_result(pg_db, run, profile, "10.51.3.3", oid=".1.3.6.1.4.1.9.1.1208", vendor="Cisco")

    summary = await evaluate(pg_db, run)
    assert summary["imported"] == 1 and summary["status"] == "completed"

    dev = pg_db.query("SELECT group_id::text, profile_id::text, tags, device_type, host(ip_address)"
                      " FROM devices WHERE ip_address = '10.51.2.12'")
    assert dev == [(group, template, ["discovered", "firewall", "riyadh"], "firewall", "10.51.2.12")]
    assert pg_db.scalar("SELECT count(*) FROM devices") == 1
    rows = dict(pg_db.query("SELECT id, disposition FROM discovery_results_v2 WHERE run_id = $1", run))
    assert rows == {fw: "imported", elsewhere: "no_match", cisco: "no_match"}
    items = pg_db.query("SELECT action_key, outcome FROM discovery_engine_run_items ORDER BY id")
    assert ("decision", "applied") in items and ("group", "applied") in items and ("tags", "applied") in items
    assert pg_db.scalar("SELECT last_match_count FROM discovery_engine_rules") == 1


@pytest.mark.asyncio
async def test_review_profile_imports_nothing_and_shows_the_decision(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).raise_for_status()
    enable_engine(pg_db)
    profile = add_profile(pg_db, mode="review")
    run = add_run(pg_db, profile)
    add_result(pg_db, run, profile, "10.51.2.12", oid=FORTI_OID)

    await evaluate(pg_db, run)
    assert pg_db.scalar("SELECT count(*) FROM devices") == 0
    with manager(pg_db) as c:
        res = c.get(f"/api/v1/discovery-v2/runs/{run}/results").json()
    assert res[0]["disposition"] == "would_import"
    assert res[0]["rule_plan"]["reason"] == "profile_review"
    assert res[0]["rule_plan"]["matched"][0]["rule_name"] == "FortiGate Riyadh"
    assert res[0]["suggested_group_id"] == group and res[0]["matched_template_id"] == template
    assert res[0]["suggested_tags"] == ["firewall", "riyadh"]
    # A manual import later uses the rule's settings.
    with manager(pg_db) as c:
        out = c.post(f"/api/v1/discovery-v2/runs/{run}/import", json={"result_ids": [res[0]["id"]]}).json()
    assert out["devices_created"] == 1
    assert pg_db.query("SELECT group_id::text, tags FROM devices") == [(group, ["firewall", "riyadh"])]


def test_preview_of_an_unsaved_rule_equals_what_the_saved_rule_applies(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    profile = add_profile(pg_db)
    run = add_run(pg_db, profile)
    for i in range(1, 5):
        add_result(pg_db, run, profile, f"10.51.0.{i}", oid=FORTI_OID)
    add_result(pg_db, run, profile, "10.51.0.9", oid=FORTI_OID, status="unknown", ready=False, cred="invalid")
    add_result(pg_db, run, profile, "10.99.0.1", oid=FORTI_OID)
    add_result(pg_db, run, profile, "10.51.0.20", oid=".1.3.6.1.4.1.9.1")
    draft = forti_rule(group, template, mode="draft")
    enable_engine(pg_db, max_auto_import_per_run=3)
    with manager(pg_db) as c:
        pre = c.post("/api/v1/discovery-v2/rules/preview",
                     json={"rule": draft, "scope": "only", "discovery_run_id": run}).json()
        expected = {row["result_id"]: (row["disposition"], row["reason"],
                                       {k: v["value"] for k, v in row["plan"]["settings"].items()})
                    for row in pre["rows"]}
        assert pre["summary"]["auto_import"] == 3          # 4 match, the per-scan limit is 3
        c.post("/api/v1/discovery-v2/rules", json={**draft, "mode": "active"}).raise_for_status()
        summary = c.post(f"/api/v1/discovery-v2/rules/evaluate-scan/{run}", json={"apply": True}).json()
        assert summary["imported"] == 3 and summary["blocked_cap"] == 1
    applied = {}
    for rid, disp, plan in pg_db.query("SELECT id, disposition, rule_plan FROM discovery_results_v2"
                                       " WHERE run_id = $1", run):
        applied[rid] = ({"imported": "auto_import", "ignored": "ignore"}.get(disp, disp), plan["reason"],
                        {k: v["value"] for k, v in plan["settings"].items()})
    assert applied == expected


@pytest.mark.asyncio
async def test_license_capacity_keeps_the_rest_in_review(pg_db, monkeypatch):
    for i in range(2):
        pg_db.execute("INSERT INTO devices (hostname, ip_address) VALUES ($1, $2::inet)", f"old{i}", f"192.0.2.{i + 1}")

    async def five_nodes(db, *, snapshot=None):
        return lc.compute_capacity({"name": "Pro", "plan": "pro", "is_active": True, "is_expired": False,
                                    "license": {"total_node_cap": 5}}, await lc.count_usage(db))
    monkeypatch.setattr(lc, "get_capacity", five_nodes)
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).raise_for_status()
    enable_engine(pg_db)
    profile = add_profile(pg_db)
    run = add_run(pg_db, profile)
    for i in range(10):
        add_result(pg_db, run, profile, f"10.51.1.{i + 1}", oid=FORTI_OID)
    before = pg_db.query("SELECT id, hostname, tags FROM devices ORDER BY hostname")

    summary = await evaluate(pg_db, run)
    assert summary["imported"] == 3 and summary["blocked_license"] == 7 and summary["status"] == "partial"
    assert pg_db.scalar("SELECT count(*) FROM devices") == 5
    reasons = pg_db.query("SELECT disposition, rule_plan->>'reason', count(*) FROM discovery_results_v2"
                          " WHERE run_id = $1 GROUP BY 1, 2 ORDER BY 1", run)
    assert reasons == [("imported", None, 3), ("review", "license_capacity_reached", 7)]
    assert pg_db.scalar("SELECT status FROM discovery_engine_runs") == "partial"
    assert pg_db.query("SELECT id, hostname, tags FROM devices WHERE hostname LIKE 'old%' ORDER BY hostname") == before


@pytest.mark.asyncio
async def test_license_reserve_is_left_free(pg_db, monkeypatch):
    async def five_nodes(db, *, snapshot=None):
        return lc.compute_capacity({"name": "Pro", "plan": "pro", "is_active": True, "is_expired": False,
                                    "license": {"total_node_cap": 5}}, await lc.count_usage(db))
    monkeypatch.setattr(lc, "get_capacity", five_nodes)
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json={
            "name": "all", "mode": "active", "condition": {"op": "all", "conditions": []},
            "actions": [{"key": "decision", "value": "auto_import"}]}).raise_for_status()
    enable_engine(pg_db, license_reserve_nodes=2)
    profile = add_profile(pg_db)
    run = add_run(pg_db, profile)
    for i in range(6):
        add_result(pg_db, run, profile, f"10.1.1.{i + 1}")
    summary = await evaluate(pg_db, run)
    assert summary["imported"] == 3 and summary["blocked_license"] == 3


@pytest.mark.asyncio
async def test_ignore_rule_adds_to_the_ignored_list_with_the_rule_as_reason(pg_db):
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json={
            "name": "Printers", "mode": "active",
            "condition": {"op": "all", "conditions": [{"field": "open_ports", "operator": "has_any",
                                                        "value": ["9100"]}]},
            "actions": [{"key": "decision", "value": "ignore"}]}).raise_for_status()
    enable_engine(pg_db)
    profile = add_profile(pg_db, mode="ignore_match")
    run = add_run(pg_db, profile)
    printer = add_result(pg_db, run, profile, "10.5.5.5")
    pg_db.execute("UPDATE discovery_results_v2 SET open_ports = '[80, 9100]'::jsonb WHERE id = $1", printer)
    await evaluate(pg_db, run)
    assert pg_db.query("SELECT host(ip_address), reason FROM discovery_ignored_devices") == [
        ("10.5.5.5", "Discovery rule: Printers")]
    assert pg_db.query("SELECT status, ignored, disposition FROM discovery_results_v2 WHERE id = $1", printer) == [
        ("ignored", True, "ignored")]
    # The executor skips addresses on the Ignored list (it loads them before matching).
    assert pg_db.scalar("SELECT count(*) FROM discovery_ignored_devices WHERE ip_address = '10.5.5.5'") == 1


@pytest.mark.asyncio
async def test_existing_devices_are_annotated_never_changed(pg_db):
    pg_db.execute("INSERT INTO devices (hostname, ip_address, tags) VALUES ('fw', '10.51.2.12', '[\"mine\"]')")
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).raise_for_status()
    enable_engine(pg_db)
    profile = add_profile(pg_db)
    run = add_run(pg_db, profile)
    add_result(pg_db, run, profile, "10.51.2.12", oid=FORTI_OID, status="existing", ready=False)
    await evaluate(pg_db, run)
    assert pg_db.query("SELECT group_id, tags FROM devices") == [(None, ["mine"])]
    assert pg_db.scalar("SELECT disposition FROM discovery_results_v2") == "existing"


# ── API: CRUD, order, versions, permissions ─────────────────────────────────

def test_crud_order_versions_and_audit(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    base = "/api/v1/discovery-v2/rules"
    with manager(pg_db) as c:
        a = c.post(base, json=forti_rule(group, template, mode="draft")).json()
        b = c.post(base, json={**forti_rule(group, template, mode="draft"), "name": "Second"}).json()
        first = c.post(base, json={**forti_rule(group, template, mode="draft"), "name": "Top",
                                   "position": "first"}).json()
        assert [r["name"] for r in c.get(base).json()] == ["Top", "FortiGate Riyadh", "Second"]
        assert c.post(base, json={**forti_rule(group, template), "name": "fortigate riyadh"}).status_code == 409

        reordered = c.post(f"{base}/reorder", json={"ids": [b["id"], a["id"], first["id"]]}).json()
        assert [r["name"] for r in reordered] == ["Second", "FortiGate Riyadh", "Top"]
        assert [r["priority"] for r in reordered] == [10, 20, 30]
        assert c.post(f"{base}/reorder", json={"ids": [a["id"]]}).status_code == 422

        upd = {**forti_rule(group, template, mode="draft"), "description": "edited", "expected_revision": 1}
        assert c.put(f"{base}/{a['id']}", json=upd).json()["revision"] == 2
        assert c.put(f"{base}/{a['id']}", json=upd).status_code == 409          # stale revision
        assert c.post(f"{base}/{a['id']}/mode", json={"mode": "active"}).json()["mode"] == "active"
        versions = c.get(f"{base}/{a['id']}/versions").json()
        assert [v["revision"] for v in versions] == [3, 2, 1]
        assert versions[0]["reason"] == "mode draft -> active"
        clone = c.post(f"{base}/{a['id']}/clone").json()
        assert clone["mode"] == "draft" and clone["name"] == "FortiGate Riyadh (copy)"
        assert c.delete(f"{base}/{clone['id']}").status_code == 204
        assert c.get(f"{base}/{clone['id']}").status_code == 404

        bad = {**forti_rule(group, template), "name": "bad"}
        bad["condition"]["conditions"][0]["operator"] = "matches"
        bad["condition"]["conditions"][0]["value"] = "(a+)+"
        r = c.post(base, json=bad)
        assert r.status_code == 422 and "Nested quantifiers" in r.json()["detail"]
        r = c.post(base, json={**forti_rule("00000000-0000-0000-0000-000000000009", template), "name": "x"})
        assert r.status_code == 422 and "Unknown device group" in r.json()["detail"]
    actions = {a for (a,) in pg_db.query("SELECT action FROM audit_logs WHERE action LIKE 'discovery.rules.%'")}
    assert {"discovery.rules.create", "discovery.rules.reorder", "discovery.rules.update",
            "discovery.rules.mode", "discovery.rules.clone", "discovery.rules.delete"} <= actions


def test_tags_are_canonicalised_to_the_registry_spelling(pg_db):
    pg_db.execute("INSERT INTO tags (name, color) VALUES ('Riyadh', '#123456')")
    with manager(pg_db) as c:
        r = c.post("/api/v1/discovery-v2/rules", json={
            "name": "t", "condition": {"op": "all", "conditions": []},
            "actions": [{"key": "tags", "value": ["riyadh", "new-tag"]}]}).json()
    assert r["actions"][0]["value"] == ["Riyadh", "new-tag"]
    assert pg_db.scalar("SELECT count(*) FROM tags WHERE name = 'new-tag'") == 1


def test_writes_need_manage_rules_and_viewers_can_read(pg_db):
    base = "/api/v1/discovery-v2/rules"
    rule = {"name": "r", "condition": {"op": "all", "conditions": []},
            "actions": [{"key": "decision", "value": "review"}]}
    with manager(pg_db) as c:
        rid = c.post(base, json=rule).json()["id"]
    with api_client(pg_db, role="disc-viewer", permissions=["discovery.view", "discovery.run"]) as c:
        assert c.get(base).status_code == 200
        assert c.get(f"{base}/catalog").status_code == 200
        for method, path, body in (
                ("post", base, rule), ("put", f"{base}/{rid}", rule), ("delete", f"{base}/{rid}", None),
                ("post", f"{base}/{rid}/mode", {"mode": "active"}), ("post", f"{base}/{rid}/clone", None),
                ("post", f"{base}/reorder", {"ids": [rid]}), ("put", f"{base}/settings", {"enabled": True})):
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, path)
    with api_client(pg_db, role="no-discovery", permissions=["devices.view"]) as c:
        assert c.get(base).status_code == 403


def test_tag_scoped_users_cannot_change_fleet_wide_rules(pg_db):
    with manager(pg_db, role="scoped-mgr", scope_tags=("team-a",)) as c:
        r = c.post("/api/v1/discovery-v2/rules", json={
            "name": "r", "condition": {"op": "all", "conditions": []},
            "actions": [{"key": "decision", "value": "review"}]})
        assert r.status_code == 403 and "whole network" in r.json()["detail"]


def test_activation_needs_the_permissions_the_actions_use(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    perms = ["discovery.view", "discovery.run", "discovery.manage_rules"]
    with api_client(pg_db, role="rules-only", permissions=perms, persist_user=True) as c:
        r = c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template))
        assert r.status_code == 403 and "devices.manage" in r.json()["detail"]
        draft = c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template, mode="draft"))
        assert draft.status_code == 201
        assert c.post(f"/api/v1/discovery-v2/rules/{draft.json()['id']}/mode",
                      json={"mode": "active"}).status_code == 403
        server_rule = {"name": "srv", "mode": "active", "condition": {"op": "all", "conditions": []},
                       "actions": [{"key": "decision", "value": "auto_import", "import_as": "auto"}]}
        assert c.post("/api/v1/discovery-v2/rules", json=server_rule).status_code == 403


def test_preview_with_sample_facts(pg_db):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with api_client(pg_db, role="disc-viewer", permissions=["discovery.view"]) as c:
        out = c.post("/api/v1/discovery-v2/rules/preview", json={
            "rule": forti_rule(group, template, mode="draft"), "scope": "only",
            "facts": {"ip_address": "10.51.9.9", "sys_object_id": "1.3.6.1.4.1.12356.1"}}).json()
    row = out["rows"][0]
    assert row["disposition"] == "auto_import" and row["plan"]["settings"]["group"]["value"] == group


def test_settings_round_trip_and_history(pg_db):
    with api_client(pg_db, role="admin", persist_user=True) as c:
        s = c.put("/api/v1/discovery-v2/rules/settings", json={
            "enabled": True, "max_auto_import_per_run": 25, "license_reserve_nodes": 5, "history_days": 90}).json()
        assert s == {"enabled": True, "max_auto_import_per_run": 25, "license_reserve_nodes": 5,
                     "history_days": 90}
        profile = add_profile(pg_db, mode="review")
        run = add_run(pg_db, profile)
        add_result(pg_db, run, profile, "10.0.0.1")
        out = c.post(f"/api/v1/discovery-v2/rules/evaluate-scan/{run}", json={"apply": False}).json()
        assert out["evaluated"] == 1 and out["no_match"] == 1
        hist = c.get("/api/v1/discovery-v2/rules/history").json()
        assert hist[0]["mode"] == "annotate" and hist[0]["profile_name"] == "Riyadh scan"
        assert c.get(f"/api/v1/discovery-v2/rules/history/{hist[0]['id']}/items").json() == []


# ── the real scan executor (probes faked) ───────────────────────────────────

def _identity(ip):
    return {"_responsive": True, "_probes": {}, "hostname": f"fw-{ip}", "sys_object_id": FORTI_OID,
            "vendor": "Fortinet", "device_type": "firewall", "credential_status": "valid",
            "open_ports": [443], "protocols_detected": ["icmp", "snmp"], "raw_data": {"sys_descr": "FortiGate"}}


async def _scan(pg_db, monkeypatch, profile, *, fail_engine=False):
    from app.services import discovery_executor as ex

    engine = create_async_engine(pg_db.url, poolclass=NullPool)
    monkeypatch.setattr(ex, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))

    async def fake_probe(ip, *a, **k):
        return _identity(ip)
    monkeypatch.setattr(ex, "_probe_ip", fake_probe)
    if fail_engine:
        async def boom(*a, **k):
            raise RuntimeError("rules exploded")
        monkeypatch.setattr(runner, "evaluate_discovery_run", boom)
    run = str(pg_db.scalar("INSERT INTO discovery_runs (profile_id, trigger_type, status)"
                           " VALUES ($1, 'scheduled', 'queued') RETURNING id", profile))
    import uuid as _uuid
    try:
        await ex.execute_run(_uuid.UUID(run))
    finally:
        await engine.dispose()
    return run


@pytest.mark.asyncio
async def test_a_scheduled_scan_auto_imports_through_the_executor(pg_db, monkeypatch):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).raise_for_status()
    enable_engine(pg_db)
    profile = add_profile(pg_db)
    pg_db.execute("UPDATE discovery_profiles SET targets = '[\"10.51.2.12\", \"10.60.0.1\"]'::jsonb WHERE id = $1",
                  profile)
    run = await _scan(pg_db, monkeypatch, profile)
    assert pg_db.query("SELECT host(ip_address), group_id::text, profile_id::text, tags FROM devices") == [
        ("10.51.2.12", group, template, ["firewall", "riyadh"])]
    status, log = pg_db.query("SELECT status, activity_log FROM discovery_runs WHERE id = $1", run)[0]
    assert status == "completed"
    assert any("Discovery rules: 1 matched, 1 auto-imported" in e["msg"] for e in log)


@pytest.mark.asyncio
async def test_engine_off_or_failing_never_changes_the_scan(pg_db, monkeypatch):
    group, template = add_group(pg_db, "G"), add_template(pg_db, "T")
    with manager(pg_db) as c:
        c.post("/api/v1/discovery-v2/rules", json=forti_rule(group, template)).raise_for_status()
    profile = add_profile(pg_db)
    pg_db.execute("UPDATE discovery_profiles SET targets = '[\"10.51.2.12\"]'::jsonb WHERE id = $1", profile)
    run = await _scan(pg_db, monkeypatch, profile)                     # engine off (default)
    assert pg_db.scalar("SELECT count(*) FROM devices") == 0
    assert pg_db.query("SELECT rule_plan, disposition, suggested_group_id FROM discovery_results_v2"
                       " WHERE run_id = $1", run) == [(None, None, None)]
    enable_engine(pg_db)
    run = await _scan(pg_db, monkeypatch, profile, fail_engine=True)
    status, log = pg_db.query("SELECT status, activity_log FROM discovery_runs WHERE id = $1", run)[0]
    assert status == "completed" and pg_db.scalar("SELECT count(*) FROM devices") == 0
    assert any("Discovery rules failed: rules exploded" in e["msg"] for e in log)
