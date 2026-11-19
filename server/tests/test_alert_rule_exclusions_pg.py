"""Disabling a shared alert rule for one device or server (migrate-187).

The field report (dev appliance, Alert Rules): a rule that applies to every
server ("Server restart") or device could be switched off from a server's
page, but the switch flipped ``alert_rules.enabled`` and so silenced the rule
for everyone.

Now a shared rule is disabled for one asset with an exclusion row:

* the rule stays enabled and keeps evaluating every other device/server;
* the rule's open incidents on the excluded asset are resolved (no recovery
  notice) and nothing new is raised there;
* the rule's exclusions are listed with the asset, who and when, and
  re-enabling deletes the row so evaluation resumes;
* only ``alerts.manage`` may change exclusions, only for assets the caller can
  see, never for a rule pinned to the asset or one that does not apply to it.

Routes run through the real app on a disposable PostgreSQL; the evaluators
(SNMP metric rules, device status transitions, server availability and host
metrics) run against the same database with history and dispatch faked.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_rule_exclusions as rx
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0
CHANNEL = "00000000-0000-0000-0000-00000000c0de"
MANAGER = ["alerts.view", "alerts.manage", "devices.view", "servers.view"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


# ── Fixtures ────────────────────────────────────────────────────────────────

def add_device(db, name, ip, *, tags=(), group=None, status="up", snmp=False):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, tags, group_id, snmp_enabled, snmp_poll_interval)"
        " VALUES ($1, $2::inet, $3, $4::jsonb, $5, $6, 60) RETURNING id",
        name, ip, status, list(tags), group, snmp))


def add_server(db, name, *, tags=(), heartbeat=T0):
    sid = str(db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, tags)"
        " VALUES ($1, $1, 'agent', 'healthy', $2::jsonb) RETURNING id", name, list(tags)))
    db.execute("INSERT INTO agents (server_id, agent_uid, hostname, status, last_heartbeat_at)"
               " VALUES ($1, $2, $4, 'online', to_timestamp($3))", sid, f"uid-{name}", heartbeat, name)
    return sid


def add_rule(db, name, metric, *, operator="gt", threshold=0, trigger="any", **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 7}" for i in range(len(cols)))
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, trigger_on"
        f"{names}) VALUES ($1, $2, $3, $4, 'critical', $5::jsonb, $6{vals}) RETURNING id",
        name, metric, operator, threshold, [CHANNEL], trigger, *cols.values()))


def open_incident(db, rule, *, device=None, server=None, status="active"):
    return str(db.scalar(
        "INSERT INTO alerts (device_id, server_id, rule_id, severity, message, status, metadata)"
        " VALUES ($1, $2, $3, 'critical', 'synthetic', $4, '{}'::jsonb) RETURNING id",
        device, server, rule, status))


@pytest.fixture
def world(pg_db):
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    return NS(db=db)


def manager(db, **kw):
    kw.setdefault("permissions", MANAGER)
    return api_client(db, role=kw.pop("role", "rule-manager"), persist_user=True,
                      username=kw.pop("username", "noc-lead"), **kw)


# ── Routes ──────────────────────────────────────────────────────────────────

def test_migration_creates_the_table_with_cascades(world):
    db = world.db
    rule = add_rule(db, "Server restart", "host_restart")
    srv = add_server(db, "app-01")
    dev = add_device(db, "sw-01", "192.0.2.1")
    down = add_rule(db, "Device Down", "ping_status", operator="eq", trigger="down")
    db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, server_id) VALUES ($1, 'server', $2)", rule, srv)
    db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id) VALUES ($1, 'device', $2)", down, dev)
    with pytest.raises(Exception):      # one row per rule and asset
        db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, server_id) VALUES ($1, 'server', $2)", rule, srv)
    with pytest.raises(Exception):      # kind and asset column agree
        db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id) VALUES ($1, 'server', $2)", rule, dev)
    db.execute("DELETE FROM servers WHERE id = $1", srv)
    db.execute("DELETE FROM alert_rules WHERE id = $1", down)
    assert db.scalar("SELECT count(*) FROM alert_rule_exclusions") == 0


def test_disabling_a_global_server_rule_for_one_server_keeps_it_on_for_the_rest(world):
    db = world.db
    rule = add_rule(db, "Server restart", "host_restart")
    a, b = add_server(db, "app-01"), add_server(db, "app-02")
    inc_a = open_incident(db, rule, server=a)
    inc_b = open_incident(db, rule, server=b)

    with manager(db) as c:
        r = c.post(f"/api/v1/alert-rules/{rule}/exclusions",
                   json={"asset_kind": "server", "asset_id": a, "reason": "lab box, restarts nightly"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True and body["resolved_alerts"] == 1
        ex = body["exclusion"]
        assert (ex["asset_kind"], ex["asset_id"], ex["asset_name"]) == ("server", a, "app-01")
        assert ex["asset_path"] == f"/servers/{a}"
        assert ex["created_by_name"] == "noc-lead" and ex["created_at"]
        assert ex["reason"] == "lab box, restarts nightly"

        # Idempotent.
        again = c.post(f"/api/v1/alert-rules/{rule}/exclusions", json={"asset_kind": "server", "asset_id": a}).json()
        assert again["created"] is False and again["exclusion"]["id"] == ex["id"]

        # The rule itself is untouched.
        assert db.scalar("SELECT enabled FROM alert_rules WHERE id = $1", rule) is True

        # The rule's detail lists the excluded server.
        listed = c.get(f"/api/v1/alert-rules/{rule}/exclusions").json()
        assert [i["asset_id"] for i in listed["items"]] == [a] and listed["hidden"] == 0
        assert listed["kinds"] == ["server"]

        # The server page sees the rule as off here, on elsewhere.
        on_a = {i["id"]: i for i in c.get("/api/v1/host-alert-rules", params={"server_id": a}).json()["items"]}
        on_b = {i["id"]: i for i in c.get("/api/v1/host-alert-rules", params={"server_id": b}).json()["items"]}
        assert on_a[rule]["enabled"] is True and on_a[rule]["active"] is False
        assert on_a[rule]["excluded"]["created_by_name"] == "noc-lead"
        assert on_b[rule]["active"] is True and on_b[rule]["excluded"] is None

        # The rule list counts it.
        rules = {x["id"]: x for x in c.get("/api/v1/alert-rules").json()["data"]}
        assert rules[rule]["exclusion_count"] == 1

        # Impact says so too.
        assert c.get(f"/api/v1/alert-rules/{rule}/impact").json()["excluded_assets"] == 1

    # Open incident on the excluded server: resolved, recorded, not a recovery.
    status, meta = db.query("SELECT status, metadata FROM alerts WHERE id = $1", inc_a)[0]
    assert status == "resolved" and meta["resolved_by"] == "rule_excluded"
    assert "disabled for this server" in meta["resolution_note"]
    assert db.scalar("SELECT status FROM alerts WHERE id = $1", inc_b) == "active"
    [(kind, src)] = db.query("SELECT kind, source FROM alert_events WHERE alert_id = $1", inc_a)
    assert (kind, src) == ("resolved", "rule_exclusion")
    actions = [r[0] for r in db.query("SELECT action FROM audit_logs WHERE resource_id = $1", rule)]
    assert "alert_rule.exclude" in actions

    # Re-enable from the server page (or the rule's detail): the row goes.
    with manager(db) as c:
        r = c.delete(f"/api/v1/alert-rules/{rule}/exclusions/server/{a}")
        assert r.status_code == 200 and r.json() == {"removed": True}
        assert c.delete(f"/api/v1/alert-rules/{rule}/exclusions/server/{a}").json() == {"removed": False}
        assert c.get(f"/api/v1/alert-rules/{rule}/exclusions").json()["items"] == []
    assert "alert_rule.include" in [r[0] for r in db.query(
        "SELECT action FROM audit_logs WHERE resource_id = $1", rule)]


def test_rules_for_a_device_show_shared_and_own_rules_with_their_exclusion(world):
    db = world.db
    dev = add_device(db, "fw-01", "192.0.2.10", tags=("edge",))
    other = add_device(db, "sw-02", "192.0.2.11")
    down = add_rule(db, "Device Down", "ping_status", operator="eq", trigger="down")
    tagged = add_rule(db, "Edge CPU", "cpu", threshold=90, scope_tag="edge")
    own = add_rule(db, "fw-01 memory", "memory", threshold=95, device_id=dev)
    add_rule(db, "Other device only", "cpu", threshold=50, device_id=other)
    add_rule(db, "All servers restart", "host_restart")
    with manager(db) as c:
        assert c.post(f"/api/v1/alert-rules/{tagged}/exclusions",
                      json={"asset_kind": "device", "asset_id": dev}).status_code == 200
        out = c.get("/api/v1/alert-rules/for-asset", params={"asset_kind": "device", "asset_id": dev}).json()
    items = {i["id"]: i for i in out["items"]}
    assert set(items) == {down, tagged, own}
    assert items[down]["shared"] and items[down]["active"] and items[down]["excluded"] is None
    assert items[down]["scope"]["label"] == "All devices and servers"
    assert items[tagged]["active"] is False and items[tagged]["excluded"]["asset_id"] == dev
    assert items[own]["shared"] is False and items[own]["active"] is True
    assert [i["id"] for i in out["items"]][-1] == own          # shared rules first
    assert out["asset"] == {"kind": "device", "id": dev, "name": "fw-01"}


def test_pinned_and_non_applicable_rules_cannot_be_excluded(world):
    db = world.db
    dev = add_device(db, "fw-01", "192.0.2.10")
    srv = add_server(db, "app-01")
    own = add_rule(db, "fw-01 memory", "memory", threshold=95, device_id=dev)
    group = str(db.scalar("INSERT INTO device_groups (name) VALUES ('Core') RETURNING id"))
    core = add_rule(db, "Core CPU", "cpu", threshold=90, group_id=group)
    restart = add_rule(db, "Server restart", "host_restart")
    with manager(db) as c:
        r = c.post(f"/api/v1/alert-rules/{own}/exclusions", json={"asset_kind": "device", "asset_id": dev})
        assert r.status_code == 422 and "disable or delete the rule" in r.json()["detail"]
        r = c.post(f"/api/v1/alert-rules/{core}/exclusions", json={"asset_kind": "device", "asset_id": dev})
        assert r.status_code == 422 and "does not apply" in r.json()["detail"]
        # A server rule is not a device rule and the reverse.
        assert c.post(f"/api/v1/alert-rules/{restart}/exclusions",
                      json={"asset_kind": "device", "asset_id": dev}).status_code == 422
        assert c.post(f"/api/v1/alert-rules/{core}/exclusions",
                      json={"asset_kind": "server", "asset_id": srv}).status_code == 422
        r = c.post(f"/api/v1/alert-rules/{restart}/exclusions",
                   json={"asset_kind": "server", "asset_id": "00000000-0000-0000-0000-000000000001"})
        assert r.status_code == 404
        assert c.post(f"/api/v1/alert-rules/{restart}/exclusions",
                      json={"asset_kind": "switch", "asset_id": srv}).status_code == 422
    assert db.scalar("SELECT count(*) FROM alert_rule_exclusions") == 0


def test_device_down_can_be_excluded_for_a_server_it_covers(world):
    db = world.db
    srv = add_server(db, "db-01")
    down = add_rule(db, "Device Down", "ping_status", operator="eq", trigger="down")
    with manager(db) as c:
        assert c.post(f"/api/v1/alert-rules/{down}/exclusions",
                      json={"asset_kind": "server", "asset_id": srv}).status_code == 200
        assert c.get(f"/api/v1/alert-rules/{down}/exclusions").json()["kinds"] == ["device", "server"]


def test_only_rule_managers_change_exclusions_and_only_for_assets_they_see(world):
    db = world.db
    mine = add_device(db, "fw-team-a", "192.0.2.20", tags=("team-a",))
    theirs = add_device(db, "fw-team-b", "192.0.2.21", tags=("team-b",))
    down = add_rule(db, "Device Down", "ping_status", operator="eq", trigger="down")

    # Read-only: may see, may not change.
    with api_client(db, role="viewer-x", permissions=["alerts.view", "devices.view"]) as c:
        assert c.post(f"/api/v1/alert-rules/{down}/exclusions",
                      json={"asset_kind": "device", "asset_id": mine}).status_code == 403
        assert c.get(f"/api/v1/alert-rules/{down}/exclusions").status_code == 200

    # A team-scoped manager: their own device yes, the other team's no
    # (and it does not learn it exists).
    with manager(db, role="team-a-manager", role_scope_tags=("team-a",), username="lead-a") as c:
        assert c.post(f"/api/v1/alert-rules/{down}/exclusions",
                      json={"asset_kind": "device", "asset_id": mine}).status_code == 200
        assert c.post(f"/api/v1/alert-rules/{down}/exclusions",
                      json={"asset_kind": "device", "asset_id": theirs}).status_code == 404
        assert c.get("/api/v1/alert-rules/for-asset",
                     params={"asset_kind": "device", "asset_id": theirs}).status_code == 404
        # But changing the fleet rule itself still needs fleet scope.
        assert c.post(f"/api/v1/alert-rules/{down}/toggle").status_code == 403

    with manager(db) as c:
        assert c.post(f"/api/v1/alert-rules/{down}/exclusions",
                      json={"asset_kind": "device", "asset_id": theirs}).status_code == 200
    with manager(db, role="team-a-manager", role_scope_tags=("team-a",), username="lead-a") as c:
        listed = c.get(f"/api/v1/alert-rules/{down}/exclusions").json()
        assert [i["asset_id"] for i in listed["items"]] == [mine] and listed["hidden"] == 1
        assert c.delete(f"/api/v1/alert-rules/{down}/exclusions/device/{theirs}").status_code == 404
    assert db.scalar("SELECT count(*) FROM alert_rule_exclusions") == 2


# ── Evaluators ──────────────────────────────────────────────────────────────

def exclude(db, rule, *, device=None, server=None):
    db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id, server_id)"
               " VALUES ($1, $2, $3, $4)", rule, "device" if device else "server", device, server)


@pytest.mark.asyncio
async def test_network_rule_skips_only_the_excluded_device(world, monkeypatch):
    from app.services import network_alert_service as worker
    from app.services import network_history

    db = world.db
    a = add_device(db, "fw-a", "192.0.2.31", snmp=True)
    b = add_device(db, "fw-b", "192.0.2.32", snmp=True)
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " recovery_alert, notify_channels) VALUES ('Device CPU', 'cpu', '>', 90, 'warning', 0,"
        " true, $1::jsonb) RETURNING id", [CHANNEL]))
    exclude(db, rule, device=a)
    points = [(T0 + i * 60, 99.0) for i in range(5)]

    def fetch_history(metrics, since, until):
        series = [(t, v) for t, v in points if since <= t <= until]
        return {a: {"cpu": series}, b: {"cpu": series}}, {}

    sent = []

    async def dispatch(db_, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(network_history, "fetch_history", fetch_history)
    monkeypatch.setattr(worker, "dispatch_to_channels", dispatch)
    async with session(db) as s:
        await worker.evaluate_network_rules(s, now=T0 + 245)
    rows = db.query("SELECT device_id::text FROM alerts WHERE rule_id = $1", rule)
    assert rows == [(b,)]
    assert all(ctx["rule_id"] == rule for ctx in sent) and len(sent) == 1


@pytest.mark.asyncio
async def test_device_status_rule_skips_the_excluded_device(world, monkeypatch):
    from app.api.v1 import alert_engine
    from app.api.v1.alert_engine import StatusChangeEvent, evaluate_status_change
    from app.services import alert_funnel

    alert_funnel.reset_for_tests()
    monkeypatch.setattr(alert_engine, "_NOTIFY_WHILE_FLAPPING_SUPPORTED", False)
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", None)
    monkeypatch.setattr(alert_engine, "_dashboard_url", AsyncMock(return_value="https://zp.example/alerts"))
    monkeypatch.setattr(alert_engine, "_dispatch_planned", AsyncMock(return_value=1))
    db = world.db
    a = add_device(db, "edge-a", "10.88.0.1")
    b = add_device(db, "edge-b", "10.88.0.2")
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on, recovery_alert,"
        " cooldown, min_duration, severity, notify_channels)"
        " VALUES ('Device Down', 'ping_status', '==', 0, 'down', true, 0, 0, 'warning', '[]'::jsonb)"
        " RETURNING id"))
    exclude(db, rule, device=a)
    for did, ip in ((a, "10.88.0.1"), (b, "10.88.0.2")):
        async with session(db) as s:
            await evaluate_status_change(StatusChangeEvent(
                device_id=did, hostname="edge", ip_address=ip, old_status="up", new_status="down"), db=s)
    assert db.query("SELECT device_id::text FROM alerts WHERE rule_id = $1", rule) == [(b,)]


@pytest.mark.asyncio
async def test_server_rules_skip_the_excluded_server(world, monkeypatch):
    from app.services import host_alert_service as has
    from app.services import server_availability as sa

    db = world.db
    db.execute("INSERT INTO system_settings (key, value) VALUES ($1, to_jsonb($2::float8))"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", sa.SINCE_KEY, T0 - 86_400)
    sent = []

    async def dispatch(db_, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    quiet, loud = add_server(db, "lab-01"), add_server(db, "prod-01")
    down = add_rule(db, "Device Down", "ping_status", operator="eq", trigger="down")
    memory = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " recovery_alert, notify_channels) VALUES ('Memory', 'host_memory_pct', 'gt', 90, 'warning', 0,"
        " true, $1::jsonb) RETURNING id", [CHANNEL]))
    exclude(db, down, server=quiet)
    exclude(db, memory, server=quiet)

    def history(metric, since, until):
        series = [(T0 + 100 + i * 30, 97.0) for i in range(12)]
        return {quiet: {"": series}, loud: {"": series}}

    monkeypatch.setattr(has, "fetch_host_history", history)
    async with session(db) as s:          # both agents silent for 400 s, memory at 97 %
        await has.evaluate_host_rules(s, now=T0 + 400)
    rows = sorted(db.query("SELECT rule_id::text, server_id::text FROM alerts WHERE status = 'active'"))
    assert rows == sorted([(down, loud), (memory, loud)])
    assert {ctx["rule_id"] for ctx in sent} == {down, memory}


@pytest.mark.asyncio
async def test_load_tolerates_a_database_without_the_table(world):
    world.db.execute("DROP TABLE alert_rule_exclusions")
    async with session(world.db) as s:
        assert await rx.load(s) is rx.EMPTY
        assert (await s.execute(__import__("sqlalchemy").text("SELECT 1"))).scalar() == 1   # transaction intact
        assert await rx.counts(s) == {}


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_exclusions_lookup_and_drop():
    ex = rx.Exclusions(frozenset({("device", "r1", "d1"), ("server", "r2", "s1")}))
    assert ex.device("r1", "d1") and not ex.device("r1", "d2") and not ex.server("r1", "d1")
    assert ex.server("r2", "s1")
    rules = [{"id": "r1"}, {"id": "r2"}]
    assert rx.drop_device(rules, ex, "d1") == [{"id": "r2"}]
    assert rx.drop_device(rules, ex, "d9") == rules
    assert rx.drop_device(rules, rx.EMPTY, "d1") == rules
    assert not rx.EMPTY


def test_udt_and_syslog_scope_respect_exclusions():
    from app.services import udt_alert_service as udt

    ex = rx.Exclusions(frozenset({("device", "r1", "d1")}))
    rule = {"id": "r1", "device_id": None, "group_id": None, "device_type": None, "location": None}
    assert udt._rule_scope_ok(rule, {"id": "d2"}, ex)
    assert not udt._rule_scope_ok(rule, {"id": "d1"}, ex)
    assert udt._rule_scope_ok(rule, {"id": "d1"})          # no exclusions passed: unchanged


@pytest.mark.parametrize("rule, kinds", [
    ({"metric": "host_restart"}, {"server"}),
    ({"metric": "cpu"}, {"device"}),
    ({"metric": "trap"}, {"device"}),
    ({"metric": "syslog"}, {"device"}),
    ({"metric": "udt_new_endpoint"}, {"device"}),
    ({"metric": "ping_status", "trigger_on": "down"}, {"device", "server"}),
    ({"metric": "ping_status", "trigger_on": "down", "group_id": "g"}, {"device"}),
    ({"metric": "service_status"}, set()),
    ({"metric": "netflow_bandwidth_bps"}, set()),
    ({"metric": "apm_error_rate"}, set()),
])
def test_which_rules_can_be_excluded_for_what(rule, kinds):
    assert rx.kinds_for_rule(rule) == kinds
