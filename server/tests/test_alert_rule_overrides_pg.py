"""Device Templates phase 3 (migrate-193) on a disposable PostgreSQL: recommended rules on
templates, per-asset overrides, effective thresholds, display thresholds and interface roles.

Covers the plan's acceptance list (docs/plans/device-templates-plan.md, phase 3):
assigning templates creates no rule and Accept creates one scoped to the template; a group
override and a device override above it, in the evaluator and on the device's effective
thresholds; expired overrides; a changed built-in recommendation never changes the accepted
rule (Keep / Apply); colours follow the rule; an uplink-role rule covers uplinks only and a
manual role never changes "monitored"; every evaluator loads overrides once per pass; and the
migration on production-shaped data (two built-in rows of one name, re-run).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from inventory_fleet import api_client
from pg_fixture import SCRIPTS_DIR, copy_migration_set, pg_cluster, pg_db  # noqa: F401

T0 = 1_800_000_000.0
CHANNEL = "00000000-0000-0000-0000-00000000c0de"
MANAGER = ["alerts.view", "alerts.manage", "devices.view", "devices.manage", "servers.view"]
M193 = "migrate-193-alert-rule-packs-overrides.sql"


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def builtin(db, key):
    return str(db.scalar("SELECT id FROM device_profiles WHERE builtin_key = $1", key))


def add_device(db, name, ip, *, profile=None, group=None):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, snmp_enabled, snmp_poll_interval, profile_id, group_id)"
        " VALUES ($1, $2::inet, 'up', TRUE, 60, $3, $4) RETURNING id", name, ip, profile, group))


def add_group(db, name):
    return str(db.scalar("INSERT INTO device_groups (name) VALUES ($1) RETURNING id", name))


def add_rule(db, name, metric, threshold, *, operator=">", **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" for i in range(len(cols)))
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration, recovery_alert,"
        f" notify_channels{names}) VALUES ($1, $2, $3, $4, 'critical', 0, true, '[]'::jsonb{vals}) RETURNING id",
        name, metric, operator, threshold, *cols.values()))


def add_override(db, rule, *, device=None, server=None, group=None, reason="test", position=100, **changes):
    cols = ", ".join(changes)
    vals = ", ".join(f"${i + 7}" for i in range(len(changes)))
    return str(db.scalar(
        "INSERT INTO alert_rule_overrides (rule_id, device_id, server_id, group_id, reason, position"
        + (f", {cols}" if cols else "") + ") VALUES ($1, $2, $3, $4, $5, $6"
        + (f", {vals}" if vals else "") + ") RETURNING id",
        rule, device, server, group, reason, position, *changes.values()))


def manager(db, **kw):
    kw.setdefault("permissions", MANAGER)
    return api_client(db, role=kw.pop("role", "rule-manager"), persist_user=True,
                      username=kw.pop("username", "noc-lead"), **kw)


@pytest.fixture
def world(pg_db):
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rule_overrides; DELETE FROM alert_rules; "
               "DELETE FROM device_template_values; DELETE FROM device_interfaces; DELETE FROM devices; "
               "DELETE FROM device_profiles WHERE NOT builtin")
    return NS(db=db, fgt=builtin(db, "fortinet_fortigate"))


async def _no_dispatch(*a, **k):
    return 0


# ── Migration ───────────────────────────────────────────────────────────────

def test_migration_seeds_builtin_roles_and_packs_and_guards_overrides(world):
    db = world.db
    roles = json.loads(db.scalar("SELECT interface_roles::text FROM device_profiles WHERE id = $1", world.fgt))
    assert {"role": "wan", "field": "if_name", "pattern": r"^wan\d*$"} in roles
    keys = [i["key"] for i in json.loads(db.scalar("SELECT rule_pack::text FROM device_profiles WHERE id = $1", world.fgt))]
    assert keys == ["uplink_down", "wan_down", "uplink_util"]
    # Server templates get no network roles.
    assert db.scalar("SELECT count(*) FROM device_profiles WHERE builtin_key = 'linux_server' AND interface_roles IS NOT NULL") == 0
    rule = add_rule(db, "CPU", "cpu", 90)
    from pg_fixture import PsqlError
    with pytest.raises(PsqlError):     # no target
        db.psql(f"INSERT INTO alert_rule_overrides (rule_id, reason, threshold) VALUES ('{rule}', 'x', 1)")
    d = add_device(db, "fw", "192.0.2.1")
    with pytest.raises(PsqlError):     # changes nothing
        db.psql(f"INSERT INTO alert_rule_overrides (rule_id, reason, device_id) VALUES ('{rule}', 'x', '{d}')")
    with pytest.raises(PsqlError):     # template with a rule cannot be deleted from under it
        tpl = db.scalar("INSERT INTO device_profiles (name, builtin) VALUES ('Mine', FALSE) RETURNING id")
        add_rule(db, "Scoped", "cpu", 90, profile_id=tpl)
        db.psql(f"DELETE FROM device_profiles WHERE id = '{tpl}'")


def test_migration_on_duplicate_builtins_and_rerun(pg_cluster, tmp_path_factory):
    """Production-shaped data: an old-version copy of a built-in (see migrate-189) and a
    customised template. Only the keyed built-in gets roles and a pack; replaying is a no-op."""
    scripts, lock = copy_migration_set(tmp_path_factory.mktemp("pre193"), exclude=[M193])
    db = pg_cluster.create_database(f"zp_m193_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=lock)
        aruba = db.scalar("SELECT id FROM device_profiles WHERE builtin_key = 'aruba_wlc'")
        old = db.scalar("""INSERT INTO device_profiles (name, vendor, version, builtin, match_rules, oid_groups, builtin_key)
            SELECT name, vendor, version - 1, TRUE, match_rules, oid_groups, 'aruba_wireless_controller_0000beef'
              FROM device_profiles WHERE id = $1 RETURNING id""", aruba)
        custom = db.scalar("""INSERT INTO device_profiles (name, builtin, parent_id, overlay)
            VALUES ('Aruba (customised)', FALSE, $1, '{}'::jsonb) RETURNING id""", aruba)
        out = pg_cluster.replay_migrations(db.name)
        assert f"apply   {M193}" in out
        got = {str(r[0]): (r[1], r[2]) for r in db.query(
            "SELECT id, interface_roles IS NOT NULL, rule_pack IS NOT NULL FROM device_profiles WHERE id = ANY($1::uuid[])",
            [aruba, old, custom])}
        assert got == {str(aruba): (True, True), str(old): (False, False), str(custom): (False, False)}
        before = db.query("SELECT id, interface_roles::text, rule_pack::text FROM device_profiles ORDER BY id")
        db.psql_file(SCRIPTS_DIR / M193)
        assert db.query("SELECT id, interface_roles::text, rule_pack::text FROM device_profiles ORDER BY id") == before
    finally:
        db.drop()


# ── Recommended rules ───────────────────────────────────────────────────────

def test_assigning_creates_no_rule_and_accept_creates_one_scoped_to_the_template(world):
    db = world.db
    d = add_device(db, "fw-01", "192.0.2.10")
    with manager(db, permissions=MANAGER + ["devices.manage_templates"]) as c:
        assert c.post(f"/api/v1/snmp/profiles/{world.fgt}/assign", json={"device_ids": [d]}).status_code == 200
        assert db.scalar("SELECT count(*) FROM alert_rules") == 0
        items = c.get(f"/api/v1/snmp/profiles/{world.fgt}/recommended").json()["items"]
        by = {i["key"]: i for i in items}
        assert by["fgt_cpu:crit"]["status"] == "available" and by["fgt_cpu:crit"]["threshold"] == 90
        assert by["fgt_cpu:warn"]["severity"] == "warning" and by["uplink_down"]["if_role"] == "uplink"
        r = c.post(f"/api/v1/snmp/profiles/{world.fgt}/recommended/fgt_cpu:crit/accept", json={})
        assert r.status_code == 201, r.text
        rule = r.json()
        assert (rule["metric"], rule["threshold"], rule["profile_id"], rule["pack_key"]) == \
            ("tpl_fgt_cpu", 90.0, world.fgt, "fgt_cpu:crit")
        assert c.post(f"/api/v1/snmp/profiles/{world.fgt}/recommended/fgt_cpu:crit/accept", json={}).status_code == 409
        listed = [x["id"] for x in c.get("/api/v1/alert-rules").json()["data"]]
        assert rule["id"] in listed
        # A customised copy sees the parent's accepted rule (it covers its devices).
        child = c.post(f"/api/v1/snmp/profiles/{world.fgt}/customize", json={"move_devices": False}).json()
        ci = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{child['id']}/recommended").json()["items"]}
        assert ci["fgt_cpu:crit"]["status"] == "accepted" and ci["fgt_cpu:crit"]["accepted"]["on_this_template"] is False
        assert ci["uplink_down"]["status"] == "available"        # inherited pack


def test_changed_recommendation_never_changes_the_rule_until_applied(world):
    db = world.db

    def bump(value):           # what a release migration does to a built-in
        groups = json.loads(db.scalar("SELECT oid_groups::text FROM device_profiles WHERE id = $1", world.fgt))
        for g in groups:
            for m in g.get("metrics") or []:
                if m["key"] == "fgt_cpu":
                    m["thresholds"]["crit"] = value
        db.execute("UPDATE device_profiles SET oid_groups = $2::jsonb WHERE id = $1", world.fgt, groups)

    with manager(db) as c:
        rid = c.post(f"/api/v1/snmp/profiles/{world.fgt}/recommended/fgt_cpu:crit/accept", json={}).json()["id"]
        bump(95)
        it = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{world.fgt}/recommended").json()["items"]}["fgt_cpu:crit"]
        assert it["status"] == "changed"
        assert it["changes"] == [{"field": "threshold", "from": 90.0, "to": 95.0}]
        assert db.scalar("SELECT threshold FROM alert_rules WHERE id = $1", rid) == 90
        assert c.post(f"/api/v1/snmp/profiles/{world.fgt}/recommended/fgt_cpu:crit/keep").status_code == 200
        it = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{world.fgt}/recommended").json()["items"]}["fgt_cpu:crit"]
        assert it["status"] == "accepted" and db.scalar("SELECT threshold FROM alert_rules WHERE id = $1", rid) == 90
        bump(97)
        r = c.post(f"/api/v1/snmp/profiles/{world.fgt}/recommended/fgt_cpu:crit/apply")
        assert r.status_code == 200, r.text
        assert db.scalar("SELECT threshold FROM alert_rules WHERE id = $1", rid) == 97
        it = {i["key"]: i for i in c.get(f"/api/v1/snmp/profiles/{world.fgt}/recommended").json()["items"]}["fgt_cpu:crit"]
        assert it["status"] == "accepted"


def test_template_with_rules_cannot_be_deleted(world):
    db = world.db
    with manager(db, permissions=MANAGER + ["devices.manage_templates"]) as c:
        tpl = c.post("/api/v1/snmp/profiles", json={"name": "Edge", "oid_groups": []}).json()["id"]
        add_rule(db, "Edge CPU", "cpu", 90, profile_id=tpl)
        r = c.delete(f"/api/v1/snmp/profiles/{tpl}")
        assert r.status_code == 409 and "Edge CPU" in r.json()["detail"]


# ── Overrides in the evaluator ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_group_override_device_override_above_it_and_expiry(world, monkeypatch):
    from app.services import network_alert_service as worker
    from app.services import network_history

    db = world.db
    branch = add_group(db, "Branch firewalls")
    a = add_device(db, "fw-a", "192.0.2.21", profile=world.fgt, group=branch)
    b = add_device(db, "fw-b", "192.0.2.22", profile=world.fgt, group=branch)
    c_ = add_device(db, "fw-c", "192.0.2.23", profile=world.fgt)
    other = add_device(db, "rtr-d", "192.0.2.24")                       # not on the template
    rule = add_rule(db, "Firewall CPU", "cpu", 90, profile_id=world.fgt)
    grp = add_override(db, rule, group=branch, threshold=95, min_duration=0, reason="Branch boxes run hot")
    dev = add_override(db, rule, device=b, threshold=80, reason="Small model")
    add_override(db, rule, device=c_, threshold=99, reason="Was noisy",
                 expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    values = {a: 93.0, b: 85.0, c_: 92.0, other: 99.0}

    def fetch_history(metrics, since, until):
        return {d: {"cpu": [(T0 + i * 60, v) for i in range(5)]} for d, v in values.items()}, {}

    monkeypatch.setattr(network_history, "fetch_history", fetch_history)
    monkeypatch.setattr(worker, "dispatch_to_channels", _no_dispatch)
    async with session(db) as s:
        await worker.evaluate_network_rules(s, now=T0 + 245)
    rows = {r[0]: json.loads(r[1]) for r in db.query(
        "SELECT device_id::text, metadata::text FROM alerts WHERE rule_id = $1", rule)}
    assert set(rows) == {b, c_}                     # a held at 95 by its group; other not on the template
    assert rows[b]["override"]["id"] == dev and rows[b]["threshold"] == 80
    assert "override" not in rows[c_] and rows[c_]["threshold"] == 90     # expired row ignored

    with manager(db) as c:
        eff = {i["id"]: i for i in c.get("/api/v1/alert-rules/effective",
                                         params={"asset_kind": "device", "asset_id": a}).json()["items"]}[rule]
        assert eff["source"] == "override" and eff["override"]["id"] == grp
        assert eff["effective"]["conditions"][0]["threshold"] == 95 and eff["rule"]["conditions"][0]["threshold"] == 90
        listed = c.get(f"/api/v1/alert-rules/{rule}/overrides").json()["items"]
        assert [o["target_kind"] for o in listed] == ["device", "device", "group"]
        assert [o["expired"] for o in listed if o["device_id"] == c_] == [True]


@pytest.mark.asyncio
async def test_open_incident_follows_the_new_threshold_with_the_usual_holds(world, monkeypatch):
    """A device's threshold raised above its reading does not resolve the open incident at
    once: the reading must stay on the healthy side for the clear hold, and missing samples
    never count as clear time."""
    from app.services import network_alert_service as worker
    from app.services import network_history

    db = world.db
    a = add_device(db, "fw-a", "192.0.2.31", profile=world.fgt)
    rule = add_rule(db, "Firewall CPU", "cpu", 90, profile_id=world.fgt, clear_duration=300)
    # 99 % until T0+240, then 93 %: above the rule (90), below the override (95).
    points = [(T0 + i * 60, 99.0 if i <= 4 else 93.0) for i in range(40)]
    upto = {"t": T0 + 10_000}
    monkeypatch.setattr(network_history, "fetch_history", lambda m, s, u: (
        {a: {"cpu": [(t, v) for t, v in points if s <= t <= min(u, upto["t"])]}}, {}))
    monkeypatch.setattr(worker, "dispatch_to_channels", _no_dispatch)

    async def run(now):
        async with session(db) as s:
            await worker.evaluate_network_rules(s, now=now)
        return db.scalar("SELECT status FROM alerts WHERE rule_id = $1", rule)

    assert await run(T0 + 245) == "active"
    add_override(db, rule, device=a, threshold=95, reason="Accepted for this box")
    assert await run(T0 + 365) == "active"          # below 95 for 65 s only
    upto["t"] = T0 + 360                            # then the samples stop
    assert await run(T0 + 1500) == "active"         # unknown: never a recovery
    upto["t"] = T0 + 10_000
    assert await run(T0 + 700) == "resolved"        # below 95 for 400 s of real samples


@pytest.mark.asyncio
async def test_server_override(world, monkeypatch):
    from app.services import host_alert_service as has

    db = world.db
    quiet = str(db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status) "
                          "VALUES ('db-01', 'db-01', 'agent', 'healthy') RETURNING id"))
    loud = str(db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status) "
                         "VALUES ('app-01', 'app-01', 'agent', 'healthy') RETURNING id"))
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration, recovery_alert,"
        " notify_channels) VALUES ('Memory', 'host_memory_pct', 'gt', 90, 'warning', 0, true, '[]'::jsonb)"
        " RETURNING id"))
    add_override(db, rule, server=quiet, threshold=98, priority="P4", reason="Database cache")
    series = [(T0 + 100 + i * 30, 97.0) for i in range(12)]
    monkeypatch.setattr(has, "fetch_host_history", lambda m, s, u: {quiet: {"": series}, loud: {"": series}})
    monkeypatch.setattr(has, "dispatch_to_channels", _no_dispatch)
    async with session(db) as s:
        await has.evaluate_host_rules(s, now=T0 + 400)
    assert db.query("SELECT server_id::text FROM alerts WHERE rule_id = $1 AND status = 'active'", rule) == [(loud,)]


# ── Display thresholds ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_colour_follows_the_rule_and_its_override(world):
    from app.services import effective_thresholds as eff

    db = world.db
    branch = add_group(db, "Branch")
    a = add_device(db, "fw-a", "192.0.2.41", profile=world.fgt, group=branch)
    b = add_device(db, "fw-b", "192.0.2.42", profile=world.fgt)
    rule = add_rule(db, "CPU crit", "tpl_fgt_cpu", 90, operator=">=", profile_id=world.fgt)
    add_override(db, rule, group=branch, threshold=95, reason="Branch")
    async with session(db) as s:
        la, lb = await eff.display_thresholds(s, a), await eff.display_thresholds(s, b)
    assert la == {"fgt_cpu": {"op": ">=", "crit": 95.0}} and lb == {"fgt_cpu": {"op": ">=", "crit": 90.0}}
    groups = [{"key": "system", "metrics": [{"key": "fgt_cpu", "thresholds": {"warn": 80, "crit": 90}}]}]
    shaped = eff.apply_display(groups, la)[0]["metrics"][0]
    assert shaped["thresholds"] == {"warn": 80, "crit": 95.0, "op": ">="} and shaped["thresholds_source"] == "alert_rule"
    from app.services.device_insights import metric_status
    assert metric_status(shaped, 93, "") == "warn"     # alarm at 95 for this device: not red at 93


# ── Interface roles ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uplink_rule_covers_uplinks_only_and_roles_never_change_monitored(world, monkeypatch):
    from app.services import network_alert_service as worker
    from app.services import network_history

    db = world.db
    d = add_device(db, "fw-01", "192.0.2.51", profile=world.fgt)
    ifaces = [(1, "wan1", "", 6), (2, "port1", "Uplink to core", 6), (3, "port2", "", 6), (4, "port3", "", 6),
              (5, "ssl.root", "", 131)]
    for idx, name, alias, typ in ifaces:
        db.execute("INSERT INTO device_interfaces (device_id, if_index, if_name, if_alias, if_type, admin_status,"
                   " oper_status, monitored, last_up_at) VALUES ($1, $2, $3, $4, $5, 'up', 'down', TRUE, now())",
                   d, idx, name, alias, typ)
    with manager(db) as c:
        r = c.put(f"/api/v1/devices/{d}/interfaces/4/role", json={"role": "uplink"})
        assert r.status_code == 200, r.text
        assert (r.json()["role"], r.json()["role_source"]) == ("uplink", "manual")
        listed = {i["if_index"]: (i["role"], i["role_source"]) for i in c.get(f"/api/v1/devices/{d}/interfaces").json()}
    assert listed == {1: ("wan", "template"), 2: ("uplink", "template"), 3: (None, None),
                      4: ("uplink", "manual"), 5: ("tunnel", "template")}
    assert db.scalar("SELECT bool_and(monitored) FROM device_interfaces WHERE device_id = $1", d)
    rule = add_rule(db, "Uplink down", "if_oper_status", 2, operator="==", if_role="uplink", profile_id=world.fgt)

    def fetch_history(metrics, since, until):
        series = [(T0 + i * 60, 2.0) for i in range(5)]
        return {}, {(d, idx): {"if_oper_status": series} for idx, *_ in ifaces}

    monkeypatch.setattr(network_history, "fetch_history", fetch_history)
    monkeypatch.setattr(worker, "dispatch_to_channels", _no_dispatch)
    async with session(db) as s:
        await worker.evaluate_network_rules(s, now=T0 + 245)
    hit = sorted(r[0] for r in db.query("SELECT metadata->>'if_index' FROM alerts WHERE rule_id = $1", rule))
    assert hit == ["2", "4"]


# ── Permissions and validation ──────────────────────────────────────────────

def test_override_permissions_and_validation(world):
    db = world.db
    branch = add_group(db, "Branch")
    d = add_device(db, "fw-a", "192.0.2.61", profile=world.fgt)
    tagged = add_device(db, "fw-t", "192.0.2.62")
    db.execute("UPDATE devices SET tags = '[\"team-a\"]'::jsonb WHERE id = $1", tagged)
    rule = add_rule(db, "CPU", "cpu", 90)
    status = add_rule(db, "Down", "ping_status", 0, operator="==")
    with api_client(db, role="viewer", permissions=["alerts.view", "devices.view"]) as c:
        assert c.post(f"/api/v1/alert-rules/{rule}/overrides",
                      json={"device_id": d, "threshold": 95, "reason": "x"}).status_code == 403
    with manager(db) as c:
        assert c.post(f"/api/v1/alert-rules/{rule}/overrides",
                      json={"device_id": d, "reason": "x"}).status_code == 422          # changes nothing
        assert c.post(f"/api/v1/alert-rules/{status}/overrides",
                      json={"device_id": d, "threshold": 1, "reason": "x"}).status_code == 422   # no threshold rule
        assert c.post(f"/api/v1/alert-rules/{rule}/overrides",
                      json={"device_id": d, "threshold": 95, "reset_threshold": 97, "reason": "x"}).status_code == 422
        ok = c.post(f"/api/v1/alert-rules/{rule}/overrides",
                    json={"group_id": branch, "threshold": 95, "min_duration": 600, "reason": "Branch"})
        assert ok.status_code == 201, ok.text
        dev = c.post(f"/api/v1/alert-rules/{rule}/overrides",
                     json={"device_id": d, "threshold": 85, "reason": "Small box", "position": 5}).json()
        r = c.post(f"/api/v1/alert-rules/{rule}/overrides/reorder", json={"ids": [ok.json()["id"], dev["id"]]})
        assert r.status_code == 200
        assert [o["target_kind"] for o in r.json()["items"]] == ["device", "group"]   # device rows first
    with manager(db, username="team-a-lead", scope_tags=["team-a"]) as c:
        assert c.post(f"/api/v1/alert-rules/{rule}/overrides",
                      json={"group_id": branch, "threshold": 99, "reason": "x"}).status_code == 403
    with manager(db, permissions=MANAGER + ["devices.manage_policies"]) as c:
        r = c.delete(f"/api/v1/device-groups/{branch}")
        assert r.status_code == 409 and "override" in json.dumps(r.json())


# ── Loading ─────────────────────────────────────────────────────────────────

def test_lookup_order_and_instance_patterns():
    from app.services import alert_rule_overrides as ovr
    mk = lambda **k: ovr.Override(**{"id": k.pop("id"), "rule_id": "r", "position": k.pop("position", 100),  # noqa: E731
                                     "device_id": None, "server_id": None, "group_id": None, "instance_pattern": None,
                                     "metric": None, "threshold": None, "reset_threshold": None, "min_duration": None,
                                     "clear_duration": None, "priority": None, "reason": "x", "expires_at": None, **k})
    rows = sorted([mk(id="g", group_id="G", threshold=95), mk(id="d", device_id="D", threshold=80, position=500),
                   mk(id="p", device_id="D", instance_pattern="port*", threshold=70)], key=ovr._sort_key)
    o = ovr.Overrides({"r": rows})
    assert o.find("r", device_id="D", groups={"G"}, instance="port7").id == "p"
    assert o.find("r", device_id="D", groups={"G"}, instance="wan1").id == "d"
    assert o.find("r", device_id="X", groups={"G"}, instance=None).id == "g"
    assert o.find("r", device_id="X", groups=set()) is None
    assert o.longest_holds("r") == (0, 0)


@pytest.mark.asyncio
async def test_load_tolerates_a_database_without_the_table(world):
    from app.services import alert_rule_overrides as ovr
    from app.services.device_templates import family as tfamily
    db = world.db
    db.execute("ALTER TABLE alert_rule_overrides RENAME TO alert_rule_overrides_hidden")
    try:
        async with session(db) as s:
            assert not await ovr.load(s)
            assert (await tfamily.load(s)).ancestors(world.fgt) == [world.fgt]
    finally:
        db.execute("ALTER TABLE alert_rule_overrides_hidden RENAME TO alert_rule_overrides")
