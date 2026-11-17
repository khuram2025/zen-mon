"""Discovery rules phase 4 on a disposable PostgreSQL: the acceptance.

* interface rules monitor or ignore interfaces by name, alias, type or speed;
  the first matching pattern decides; a hand edit is kept; undo restores; a
  new interface is picked up by the "changed" pass;
* topology facts: LLDP/CDP neighbours count only with current evidence (seen
  within 24 h, both ports up), as network maps define it;
* sensor and site facts, and "monitor from sensor" adds a monitoring location
  only for an online, authorised sensor;
* servers: agent policy action (servers without an agent are skipped), site
  and software baseline facts;
* "service check from template" creates the check once; undo leaves it;
* a per-rule change cap and the match-growth stop hold an automatic run for
  approval;
* scheduled runs start at the next slot, then run once per slot with the
  same limits;
* the Unmatched report lists assets no active rule matches;
* export / import carries sensors, agent policies and templates by name.
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
           "servers.view", "servers.manage", "maps.view", "maps.manage", "service_checks.view",
           "service_checks.manage"]


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
    pg_db.execute("TRUNCATE devices, servers, sensors, sites, agent_policies, service_check_templates,"
                  " software_baselines CASCADE")


def enable_engine(db, **kw):
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'discovery_rules'",
               {**runner.DEFAULT_SETTINGS, "enabled": True, **kw})


def add_device(db, name, ip, *, vendor="Cisco", tags=(), **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" for i in range(len(cols)))
    return str(db.scalar(
        f"INSERT INTO devices (hostname, ip_address, vendor, tags{names})"
        f" VALUES ($1, $2::inet, $3, $4::jsonb{vals}) RETURNING id",
        name, ip, vendor, list(tags), *cols.values()))


def add_iface(db, dev, idx, name, *, alias=None, if_type=6, speed=1_000_000_000, monitored=True, up=True):
    db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_descr, if_alias, if_type, if_speed,"
               " monitored, admin_status, oper_status, last_up_at)"
               " VALUES ($1, $2, $3, $3, $4, $5, $6, $7, 'up', $8, CASE WHEN $9 THEN now() END)",
               dev, idx, name, alias, if_type, speed, monitored, "up" if up else "down", up)


def monitored(db, dev):
    return dict(db.query("SELECT if_name, monitored FROM device_interfaces WHERE device_id = $1", dev))


def manager(db, **kw):
    return api_client(db, role=kw.pop("role", "rules-manager"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, **kw)


def rule(name, condition, actions, **kw):
    return {"name": name, "mode": kw.pop("mode", "active"), "asset_kinds": kw.pop("kinds", ["device"]),
            "triggers": kw.pop("triggers", ["changed"]), "condition": {"op": "all", "conditions": condition},
            "actions": actions, **kw}


def create(c, body):
    r = c.post(BASE, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run_manual(c, **targets):
    body = {"targets": {"kinds": ["device"], **targets}}
    pre = c.post(f"{BASE}/inventory/preview", json=body)
    assert pre.status_code == 200, pre.text
    r = c.post(f"{BASE}/inventory/apply", json={**body, "digest": pre.json()["digest"]})
    assert r.status_code == 200, r.text
    return pre.json(), r.json()


async def rpass(db):
    async with session(db) as s:
        return await reconcile.reconcile_changed(s)


ANY_CISCO = [{"field": "vendor", "operator": "equals", "value": "Cisco"}]


# ── interfaces ──────────────────────────────────────────────────────────────

def test_interface_rules_first_pattern_wins_hand_edits_kept_undo_restores(pg_db):
    dev = add_device(pg_db, "sw1", "10.1.0.1")
    add_iface(pg_db, dev, 1, "Gi1/0/1", alias="uplink to core")
    add_iface(pg_db, dev, 2, "Gi1/0/2", alias="printer")
    add_iface(pg_db, dev, 3, "Vlan10", if_type=53)
    add_iface(pg_db, dev, 4, "Lo0", if_type=24, monitored=False)
    add_iface(pg_db, dev, 5, "Te1/1/1", speed=10_000_000_000, monitored=False)
    with manager(pg_db) as c:
        create(c, rule("Ports", ANY_CISCO, [{"key": "interfaces", "value": [
            {"set": "monitor", "field": "if_alias", "pattern": "uplink"},
            {"set": "ignore", "field": "if_type", "pattern": "53, 24"},
            {"set": "monitor", "field": "speed", "pattern": "10000-"},
            {"set": "ignore", "field": "if_name", "pattern": "^gi"},     # case-insensitive
        ]}], triggers=["discovery"]))
        pre, out = run_manual(c)
        assert out["applied"] == 3            # Gi1/0/2 off, Vlan10 off, Te1/1/1 on (Gi1/0/1 and Lo0 as wanted)
        assert monitored(pg_db, dev) == {"Gi1/0/1": True, "Gi1/0/2": False, "Vlan10": False, "Lo0": False,
                                         "Te1/1/1": True}
        assert {(ch["item_key"], ch["after"]) for ch in pre["changes"]} == {
            ("gi1/0/2", False), ("vlan10", False), ("te1/1/1", True)}
        # a hand edit after the rule set it is kept by a set-once rule
        pg_db.execute("UPDATE device_interfaces SET monitored = true WHERE if_name = 'Vlan10'")
        pre2 = c.post(f"{BASE}/inventory/preview", json={"targets": {"kinds": ["device"]}}).json()
        assert [(ch["item_key"], ch["outcome"]) for ch in pre2["changes"]] == [("vlan10", "kept_manual_edit")]
        undo = c.post(f"{BASE}/history/{out['engine_run_id']}/undo").json()
        assert undo["reverted"] == 2 and undo["skipped_changed"] == 1   # Vlan10 changed since
        assert monitored(pg_db, dev) == {"Gi1/0/1": True, "Gi1/0/2": True, "Vlan10": True, "Lo0": False,
                                         "Te1/1/1": False}


def test_interface_patterns_are_validated(pg_db):
    with manager(pg_db) as c:
        for bad, msg in (({"set": "drop", "field": "if_name", "pattern": "x"}, "monitor or ignore"),
                         ({"set": "ignore", "field": "if_type", "pattern": "gig"}, "IF-MIB type"),
                         ({"set": "ignore", "field": "speed", "pattern": "fast"}, "Mbit/s"),
                         ({"set": "ignore", "field": "if_name", "pattern": "(a+)+"}, "Nested quantifiers")):
            r = c.post(BASE, json=rule("Bad", ANY_CISCO, [{"key": "interfaces", "value": [bad]}]))
            assert r.status_code == 422 and msg in r.text, r.text
        r = c.post(BASE, json=rule("Kids", ANY_CISCO, [{"key": "interfaces", "value": [
            {"set": "ignore", "field": "if_name", "pattern": "x"}]}], kinds=["child"]))
        assert r.status_code == 422 and "does not apply to child" in r.text


@pytest.mark.asyncio
async def test_a_new_interface_is_picked_up_by_the_changed_pass(pg_db):
    dev = add_device(pg_db, "sw1", "10.1.0.1")
    add_iface(pg_db, dev, 1, "Gi1/0/1")
    with manager(pg_db) as c:
        create(c, rule("No loopbacks", ANY_CISCO, [{"key": "interfaces", "value": [
            {"set": "ignore", "field": "if_name", "pattern": "^lo"}]}]))
    enable_engine(pg_db)
    assert (await rpass(pg_db))["assets"] == 1
    assert await rpass(pg_db) == {"assets": 0}
    add_iface(pg_db, dev, 9, "Loopback0", if_type=24)
    out = await rpass(pg_db)
    assert out["assets"] == 1 and out["applied"] == 1
    assert monitored(pg_db, dev)["Loopback0"] is False


# ── topology ────────────────────────────────────────────────────────────────

def link(db, a, a_idx, a_name, b, b_name, *, age_h=1):
    db.execute("INSERT INTO topology_links (local_device_id, local_if_index, local_if_name, remote_device_id,"
               " remote_port_id, remote_if_name, protocol, last_seen_at)"
               " VALUES ($1, $2, $3, $4, $5, $5, 'lldp', now() - make_interval(hours => $6))",
               a, a_idx, a_name, b, b_name, age_h)


def test_neighbour_facts_use_current_evidence_only(pg_db):
    core = add_device(pg_db, "core-sw", "10.1.0.1")
    acc1 = add_device(pg_db, "acc-1", "10.1.0.2")
    acc2 = add_device(pg_db, "acc-2", "10.1.0.3")
    acc3 = add_device(pg_db, "acc-3", "10.1.0.4")
    for d in (core, acc1, acc2, acc3):
        add_iface(pg_db, d, 1, "Gi1/0/1")
        add_iface(pg_db, d, 2, "Gi1/0/2")
        add_iface(pg_db, d, 3, "Gi1/0/3", up=False)
    link(pg_db, acc1, 1, "Gi1/0/1", core, "Gi1/0/1")                 # fresh
    link(pg_db, acc2, 1, "Gi1/0/1", core, "Gi1/0/2", age_h=48)       # old evidence
    link(pg_db, acc3, 1, "Gi1/0/1", core, "Gi1/0/3")                 # core's port never up
    with manager(pg_db) as c:
        create(c, rule("Behind core", [{"field": "neighbors", "operator": "has_any", "value": ["core-sw"]}],
                       [{"key": "tags", "value": ["behind-core"]}], triggers=["discovery"]))
        _, out = run_manual(c)
        assert out["applied"] == 1
        tagged = {h for (h,) in pg_db.query("SELECT hostname FROM devices WHERE tags ? 'behind-core'")}
        assert tagged == {"acc-1"}
        ex = c.get(f"{BASE}/assets/device/{core}").json()
        assert ex["matching_rules"] == []
    from app.services.discovery_rules.facts import facts_from_device
    assert facts_from_device({"hostname": "x"})["neighbors"] is None     # not loaded: missing, not empty


# ── sensors and sites ───────────────────────────────────────────────────────

def add_sensor(db, name, *, site=None, status="online", enrolled=True):
    site_id = db.scalar("INSERT INTO sites (name) VALUES ($1) RETURNING id", site) if site else None
    return str(db.scalar(
        "INSERT INTO sensors (name, site_id, status, api_key_hash, last_heartbeat_at, bootstrap_config)"
        " VALUES ($1, $2, $3, $4, now(), '{}'::jsonb) RETURNING id",
        name, site_id, status, "h" if enrolled else None))


def test_monitor_from_sensor_needs_an_online_sensor_and_drives_site_facts(pg_db):
    jed = add_sensor(pg_db, "jeddah-1", site="Jeddah")
    off = add_sensor(pg_db, "dammam-1", site="Dammam", status="offline")
    d1 = add_device(pg_db, "r1", "10.20.0.1")
    d2 = add_device(pg_db, "r2", "10.30.0.1")
    with manager(pg_db) as c:
        create(c, rule("Jeddah", [{"field": "ip_address", "operator": "in_subnet", "value": "10.20.0.0/16"}],
                       [{"key": "sensor", "value": jed}], triggers=["discovery"]))
        create(c, rule("Dammam", [{"field": "ip_address", "operator": "in_subnet", "value": "10.30.0.0/16"}],
                       [{"key": "sensor", "value": off}], triggers=["discovery"]))
        _, out = run_manual(c)
        assert out["applied"] == 1 and out["not_ready"] == 1
        assert pg_db.query("SELECT target_id::text FROM sensor_assignments") == [(d1,)]
        # the facts the sensors give
        create(c, rule("Jeddah site", [{"field": "sites", "operator": "has_any", "value": ["Jeddah"]},
                                       {"field": "controller_polling", "operator": "is_false"}],
                       [{"key": "tags", "value": ["site-jeddah"]}], triggers=["discovery"]))
        run_manual(c)
        assert [h for (h,) in pg_db.query("SELECT hostname FROM devices WHERE tags ? 'site-jeddah'")] == ["r1"]
        ex = c.get(f"{BASE}/assets/device/{d2}").json()
        assert [p["outcome"] for p in ex["pending"]] == ["apply"]    # still wanted once the sensor is up
    pg_db.execute("UPDATE sensors SET status = 'online' WHERE id = $1", off)
    with manager(pg_db) as c:
        _, out = run_manual(c)
        assert out["applied"] == 1
        last = c.get(f"{BASE}/history").json()
        run_id = (last["runs"] if isinstance(last, dict) else last)[0]["id"]
        c.post(f"{BASE}/history/{run_id}/undo").raise_for_status()
    assert pg_db.query("SELECT count(*) FROM sensor_assignments WHERE target_id = $1", d2) == [(0,)]


# ── servers ─────────────────────────────────────────────────────────────────

def add_server(db, name, *, os_type="linux", tags=(), site=None):
    site_id = db.scalar("INSERT INTO sites (name) VALUES ($1) RETURNING id", site) if site else None
    return str(db.scalar("INSERT INTO servers (display_name, hostname, os_type, tags, site_id)"
                         " VALUES ($1, $1, $2, $3::jsonb, $4) RETURNING id", name, os_type, list(tags), site_id))


def test_agent_policy_and_server_facts(pg_db):
    default = str(pg_db.scalar("INSERT INTO agent_policies (name, platform) VALUES ('Default', 'any') RETURNING id"))
    db_pol = str(pg_db.scalar("INSERT INTO agent_policies (name, platform) VALUES ('Databases', 'linux')"
                              " RETURNING id"))
    s1 = add_server(pg_db, "db-1", tags=["postgres"], site="Riyadh")
    add_server(pg_db, "db-2", tags=["postgres"])          # no agent: skipped
    pg_db.execute("INSERT INTO agents (server_id, agent_uid, platform, policy_id) VALUES ($1, 'u1', 'linux', $2)",
                  s1, default)
    b = pg_db.scalar("INSERT INTO software_baselines (name, os_type, match_tags) VALUES"
                     " ('Postgres hosts', 'linux', '[\"postgres\"]'::jsonb) RETURNING id")
    rid = pg_db.scalar("INSERT INTO software_baseline_rules (baseline_id, package_match) VALUES ($1, 'postgresql')"
                       " RETURNING id", b)
    pg_db.execute("INSERT INTO server_baseline_results (server_id, rule_id, baseline_id, status)"
                  " VALUES ($1, $2, $3, 'missing')", s1, rid, b)
    with manager(pg_db) as c:
        create(c, rule("DB policy", [{"field": "tags", "operator": "has_any", "value": ["postgres"]}],
                       [{"key": "agent_policy", "value": db_pol}], kinds=["server"], triggers=["discovery"]))
        create(c, rule("Non compliant in Riyadh", [
            {"field": "baselines", "operator": "has_any", "value": ["Postgres hosts"]},
            {"field": "baseline_status", "operator": "is", "value": "non_compliant"},
            {"field": "sites", "operator": "has_any", "value": ["Riyadh"]}],
            [{"key": "tags", "value": ["fix-baseline"]}], kinds=["server"], triggers=["discovery"]))
        body = {"targets": {"kinds": ["server"]}}
        pre = c.post(f"{BASE}/inventory/preview", json=body).json()
        assert sorted(ch["action_key"] for ch in pre["changes"]) == ["agent_policy", "tags"]
        out = c.post(f"{BASE}/inventory/apply", json={**body, "digest": pre["digest"]}).json()
        assert out["applied"] == 2
    assert pg_db.query("SELECT policy_id::text FROM agents") == [(db_pol,)]
    assert pg_db.query("SELECT display_name FROM servers WHERE tags ? 'fix-baseline'") == [("db-1",)]
    with manager(pg_db) as c:
        c.post(f"{BASE}/history/{out['engine_run_id']}/undo").raise_for_status()
    assert pg_db.query("SELECT policy_id::text FROM agents") == [(default,)]


def test_agent_policy_needs_servers_manage(pg_db):
    pol = str(pg_db.scalar("INSERT INTO agent_policies (name) VALUES ('P') RETURNING id"))
    perms = [p for p in MANAGER if p != "servers.manage"]
    with manager(pg_db, role="no-servers", permissions=perms) as c:
        r = c.post(BASE, json=rule("P", [], [{"key": "agent_policy", "value": pol}], kinds=["server"]))
        assert r.status_code == 403 and "servers.manage" in r.text


# ── service checks from templates ───────────────────────────────────────────

def test_service_check_from_template_created_once_and_kept_on_undo(pg_db):
    tpl = str(pg_db.scalar("INSERT INTO service_check_templates (name, check_type, target_port_default)"
                           " VALUES ('SSH', 'tcp', 22) RETURNING id"))
    add_device(pg_db, "r1", "10.1.0.1")
    with manager(pg_db) as c:
        create(c, rule("SSH check", ANY_CISCO, [{"key": "service_template", "value": tpl}], triggers=["discovery"]))
        _, out = run_manual(c)
        assert out["applied"] == 1
        assert pg_db.query("SELECT name, target_port FROM service_checks") == [("SSH · r1", 22)]
        pre = c.post(f"{BASE}/inventory/preview", json={"targets": {"kinds": ["device"]}}).json()
        assert pre["changes"] == []                       # exists: not created twice
        undo = c.post(f"{BASE}/history/{out['engine_run_id']}/undo").json()
        assert undo["not_reversible"] == 1
    assert pg_db.scalar("SELECT count(*) FROM service_checks") == 1
    # a check of the generated name that is not this device's: reported, nothing recorded as applied
    add_device(pg_db, "r2", "10.1.0.2")
    pg_db.execute("INSERT INTO service_checks (name, check_type, target_host) VALUES ('SSH · r2', 'tcp', 'x')")
    with manager(pg_db) as c:
        _, out = run_manual(c)
        assert out["applied"] == 0 and out["error"] == 1
        assert "duplicate name" in pg_db.scalar(
            "SELECT detail FROM discovery_engine_run_items WHERE outcome = 'error'")
    with manager(pg_db, role="no-checks", permissions=[p for p in MANAGER if p != "service_checks.manage"]) as c:
        r = c.post(BASE, json=rule("SSH 2", ANY_CISCO, [{"key": "service_template", "value": tpl}]))
        assert r.status_code == 403 and "service_checks.manage" in r.text


# ── per-rule cap and growth stop ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_rule_cap_holds_the_run(pg_db):
    for i in range(5):
        add_device(pg_db, f"r{i}", f"10.1.0.{i + 1}")
    with manager(pg_db) as c:
        create(c, rule("Owner", ANY_CISCO, [{"key": "owner", "value": "NOC"}], max_changes_per_run=3))
    enable_engine(pg_db)
    out = await rpass(pg_db)
    assert out["changes"] == 5 and "its own limit is 3" in out["reasons"][0]
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE owner = 'NOC'") == 0
    with manager(pg_db) as c:
        pend = c.get(f"{BASE}/history/{out['needs_approval']}/pending").json()
        assert pend["reasons"] and len(pend["changes"]) == 5
        c.post(f"{BASE}/history/{out['needs_approval']}/approve").raise_for_status()
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE owner = 'NOC'") == 5
    assert pg_db.scalar("SELECT last_match_count FROM discovery_engine_rules") == 5


@pytest.mark.asyncio
async def test_growth_stop_after_a_rule_suddenly_matches_many_more(pg_db):
    for i in range(30):
        add_device(pg_db, f"r{i}", f"10.1.{i // 10}.{i % 10 + 1}")
    with manager(pg_db) as c:
        rid = create(c, rule("Lab", [{"field": "ip_address", "operator": "in_subnet", "value": "10.1.0.0/24"}],
                             [{"key": "location", "value": "Lab"}]))
    enable_engine(pg_db, growth_breaker_pct=50, growth_breaker_min=5)
    first = await rpass(pg_db)
    assert first["applied"] == 10                # never evaluated before: only the caps apply
    await rpass(pg_db)
    assert pg_db.scalar("SELECT last_match_count FROM discovery_engine_rules") == 10
    with manager(pg_db) as c:
        cur = c.get(f"{BASE}/{rid}").json()
        cur["condition"] = {"op": "all", "conditions": [
            {"field": "ip_address", "operator": "in_subnet", "value": "10.1.0.0/16"}]}
        c.put(f"{BASE}/{rid}", json=cur).raise_for_status()
    out = await rpass(pg_db)
    assert "needs_approval" in out and "growth stop" in out["reasons"][0]
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE location = 'Lab'") == 10
    enable_engine(pg_db, growth_breaker_pct=0)  # off: the rule edit supersedes nothing, the run still waits
    with manager(pg_db) as c:
        c.post(f"{BASE}/history/{out['needs_approval']}/approve").raise_for_status()
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE location = 'Lab'") == 30


# ── scheduled runs ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduled_runs_start_at_the_next_slot_and_run_once_per_slot(pg_db):
    for i in range(3):
        add_device(pg_db, f"r{i}", f"10.1.0.{i + 1}")
    with manager(pg_db) as c:
        create(c, rule("Nightly owner", ANY_CISCO, [{"key": "owner", "value": "NOC"}], triggers=["scheduled"]))
    enable_engine(pg_db, schedule_enabled=True, schedule_every_hours=24, schedule_time="02:00",
                  schedule_timezone="Asia/Riyadh")
    t0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)          # 15:00 in Riyadh
    async with session(pg_db) as s:
        assert (await reconcile.scheduled_pass(s, now=t0))["skipped"] == "schedule_started"
        assert (await reconcile.scheduled_pass(s, now=t0 + timedelta(hours=5)))["skipped"] == "not_due"
        out = await reconcile.scheduled_pass(s, now=t0 + timedelta(hours=11, minutes=5))   # 02:05 Riyadh
        assert out["applied"] == 3
        assert (await reconcile.scheduled_pass(s, now=t0 + timedelta(hours=12)))["skipped"] == "not_due"
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE owner = 'NOC'") == 3
    assert pg_db.query("SELECT trigger, status FROM discovery_engine_runs") == [("scheduled", "completed")]
    # the "changed" pass ignores scheduled-only rules
    assert (await rpass(pg_db))["skipped"] == "no_rules"
    # turned off and on again: starts at the next slot, never catches up a missed one
    enable_engine(pg_db, schedule_enabled=False)
    async with session(pg_db) as s:
        assert (await reconcile.scheduled_pass(s, now=t0 + timedelta(days=3)))["skipped"] == "schedule_off"
    enable_engine(pg_db, schedule_enabled=True, schedule_timezone="Asia/Riyadh")
    async with session(pg_db) as s:
        assert (await reconcile.scheduled_pass(s, now=t0 + timedelta(days=3)))["skipped"] == "schedule_started"


@pytest.mark.asyncio
async def test_scheduled_run_over_the_cap_waits_and_run_now(pg_db):
    for i in range(4):
        add_device(pg_db, f"r{i}", f"10.1.0.{i + 1}")
    with manager(pg_db) as c:
        create(c, rule("Owner", ANY_CISCO, [{"key": "owner", "value": "NOC"}], triggers=["scheduled"]))
        enable_engine(pg_db, max_changes_per_run=2)
        out = c.post(f"{BASE}/schedule/run-now").json()
        assert out["needs_approval"] and out["changes"] == 4
        st = c.get(f"{BASE}/schedule").json()
        assert st["rules"] == 1 and st["last_run"]["status"] == "needs_approval"
        assert c.post(f"{BASE}/schedule/run-now").json()["skipped"] == "waiting_for_approval"
        c.post(f"{BASE}/history/{out['needs_approval']}/approve").raise_for_status()
    assert pg_db.scalar("SELECT count(*) FROM devices WHERE owner = 'NOC'") == 4
    assert pg_db.scalar("SELECT last_match_count FROM discovery_engine_rules") == 4


def test_schedule_settings_are_validated(pg_db):
    with manager(pg_db) as c:
        for bad in ({"schedule_every_hours": 5}, {"schedule_time": "25:00"}, {"schedule_timezone": "Mars/Base"}):
            r = c.put(f"{BASE}/settings", json={**runner.DEFAULT_SETTINGS, **bad})
            assert r.status_code == 422, bad


# ── unmatched report, export / import ───────────────────────────────────────

def test_unmatched_report(pg_db):
    add_device(pg_db, "cisco-1", "10.1.0.1")
    add_device(pg_db, "hp-1", "10.1.0.2", vendor="HP", tags=["team-a"])
    add_server(pg_db, "web-1")
    with manager(pg_db) as c:
        create(c, rule("Cisco", ANY_CISCO, [{"key": "owner", "value": "NOC"}]))
        create(c, rule("Draft", [], [{"key": "owner", "value": "x"}], mode="draft", kinds=["server"]))
        out = c.get(f"{BASE}/unmatched", params={"kinds": "device,server"}).json()
    assert out["unmatched"] == 2 and {r["name"] for r in out["rows"]} == {"hp-1", "web-1"}
    assert out["kinds"]["device"] == {"assets": 2, "unmatched": 1}
    with api_client(pg_db, role="team-a", permissions=["discovery.view"], scope_tags=["team-a"],
                    persist_user=True) as c:
        scoped = c.get(f"{BASE}/unmatched").json()
    assert [r["name"] for r in scoped["rows"]] == ["hp-1"]


def test_export_import_resolves_phase4_references_by_name(pg_db):
    sensor = add_sensor(pg_db, "jeddah-1")
    tpl = str(pg_db.scalar("INSERT INTO service_check_templates (name, check_type) VALUES ('Ping', 'icmp')"
                           " RETURNING id"))
    with manager(pg_db) as c:
        create(c, rule("Jeddah", ANY_CISCO, [{"key": "sensor", "value": sensor},
                                             {"key": "service_template", "value": tpl}], max_changes_per_run=7))
        doc = c.get(f"{BASE}/export").json()
        acts = doc["rules"][0]["actions"]
        assert {a["value"]["name"] for a in acts} == {"jeddah-1", "Ping"} and doc["rules"][0]["max_changes_per_run"] == 7
        out = c.post(f"{BASE}/import", json={"document": doc}).json()
        assert len(out["created"]) == 1, out
        pg_db.execute("DELETE FROM sensors")
        out = c.post(f"{BASE}/import", json={"document": doc}).json()
        assert out["created"] == [] and "no sensor named 'jeddah-1'" in out["skipped"][0]["reason"]
