"""Service, software and watched-process conditions (discovery rules and
dynamic groups) and tag / group scope for server alert rules, on a disposable
PostgreSQL.

The owner's scenario end to end: "all the servers running SQL" are found by a
condition, classified by a dynamic group or a rule's tag, and a server alert
rule scoped to that group or tag covers exactly them.
"""
from __future__ import annotations

import contextlib
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import server_inventory_facts as sif
from app.services.discovery_rules import reconcile, runner
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0
CHANNEL = "00000000-0000-0000-0000-00000000c0de"
RULES = "/api/v1/discovery-v2/rules"
MANAGER = ["discovery.view", "discovery.run", "discovery.manage_rules", "devices.view", "devices.manage",
           "servers.view", "servers.manage", "alerts.view", "alerts.manage", "devices.manage_policies"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def clean(pg_db):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; TRUNCATE devices, servers CASCADE;"
                  " DELETE FROM device_groups")


def add_server(db, name, *, tags=(), os_type="windows"):
    sid = str(db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, tags, os_type)"
        " VALUES ($1, $1, 'agent', 'healthy', $2::jsonb, $3) RETURNING id", name, list(tags), os_type))
    db.execute("INSERT INTO agents (server_id, agent_uid, hostname, status, last_heartbeat_at, platform)"
               " VALUES ($1, $2, $4, 'online', to_timestamp($3), $5)", sid, f"uid-{name}", T0, name,
               "windows" if os_type == "windows" else "linux")
    return sid


def service(db, sid, name, display, state="running"):
    db.execute("INSERT INTO server_service_inventory (server_id, service_name, display_name, state, start_mode)"
               " VALUES ($1, $2, $3, $4, 'auto')", sid, name, display, state)


def software(db, sid, name, vendor=None, version="1.0"):
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor)"
               " VALUES ($1, $2, $3, $4)", sid, name, version, vendor)


def process(db, sid, pid, name, *, watched):
    db.execute("INSERT INTO server_process_inventory (server_id, pid, name, running, watchlisted)"
               " VALUES ($1, $2, $3, true, $4)", sid, pid, name, watched)


@pytest.fixture
def fleet(pg_db):
    """sql-01: SQL Server running; sql-02: installed, service stopped; web-01:
    IIS only; lnx-01: Linux, software but no services reported; new-01: no
    inventory at all."""
    db = pg_db
    f = {n: add_server(db, n, os_type="linux" if n == "lnx-01" else "windows")
         for n in ("sql-01", "sql-02", "web-01", "lnx-01", "new-01")}
    for n, state in (("sql-01", "running"), ("sql-02", "stopped")):
        service(db, f[n], "MSSQLSERVER", "SQL Server (MSSQLSERVER)", state)
        service(db, f[n], "W32Time", "Windows Time")
        software(db, f[n], "Microsoft SQL Server 2019 (64-bit)", "Microsoft Corporation", "15.0.2000.5")
    service(db, f["web-01"], "W3SVC", "World Wide Web Publishing Service")
    software(db, f["web-01"], "IIS 10.0 Express", "Microsoft Corporation")
    software(db, f["lnx-01"], "postgresql-15", "PostgreSQL Global Development Group")
    process(db, f["sql-01"], 4242, "sqlservr.exe", watched=True)
    process(db, f["web-01"], 77, "sqlservr.exe", watched=False)     # a busy process, not watched
    return f


def leaf(field, op, value=None):
    return {"op": "all", "conditions": [{"field": field, "operator": op, "value": value}]}


async def facts(db, ids, condition):
    async with session(db) as s:
        return await sif.load(s, ids, sif.needles([condition]))


# ── the facts ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_facts_are_narrow_and_unknown_is_not_absent(pg_db, fleet):
    f = fleet
    got = await facts(pg_db, list(f.values()), leaf("services", "has", ["mssqlserver"]))
    assert got[f["sql-01"]]["services"] == ["MSSQLSERVER", "SQL Server (MSSQLSERVER)"]
    assert got[f["web-01"]]["services"] == []                 # reported, no match (W3SVC not loaded)
    assert got[f["lnx-01"]]["services"] is None               # Linux: services not reported
    assert got[f["new-01"]]["services"] is None
    from app.services.discovery_rules.conditions import evaluate
    cond = {"op": "all", "conditions": [{"field": "services", "operator": "has_none", "value": ["MSSQLSERVER"]}]}
    assert evaluate(cond, {"services": got[f["web-01"]]["services"]}) is True
    assert evaluate(cond, {"services": None}) is False         # unknown is not "does not have"
    nr = {"op": "all", "conditions": [{"field": "services", "operator": "not_reported"}]}
    assert evaluate(nr, {"services": None}) is True
    running = await facts(pg_db, list(f.values()), leaf("running_services", "has_containing", "sql server"))
    assert running[f["sql-01"]]["running_services"] and running[f["sql-02"]]["running_services"] == []
    soft = await facts(pg_db, list(f.values()), leaf("software", "has_containing", "SQL Server"))
    assert soft[f["sql-02"]]["software"] == ["Microsoft SQL Server 2019 (64-bit)"]
    assert soft[f["lnx-01"]]["software"] == [] and soft[f["new-01"]]["software"] is None
    pub = await facts(pg_db, list(f.values()), leaf("software_publishers", "has", ["oracle"]))
    assert pub[f["sql-01"]]["software_publishers"] == []
    proc = await facts(pg_db, list(f.values()), leaf("watched_processes", "has", ["sqlservr.exe"]))
    assert proc[f["sql-01"]]["watched_processes"] == ["sqlservr.exe"]
    assert proc[f["web-01"]]["watched_processes"] == []       # running but not watched: not this fact
    like = await facts(pg_db, list(f.values()), leaf("software", "has_containing", "100%_x"))
    assert all(v["software"] in ([], None) for v in like.values())   # LIKE wildcards are literal


# ── dynamic groups + scoped alert rules: the owner's scenario ───────────────

@pytest.mark.asyncio
async def test_sql_servers_group_and_a_group_scoped_alert_rule(pg_db, fleet, monkeypatch):
    f = fleet
    with api_client(pg_db, role="ops", permissions=MANAGER, persist_user=True) as c:
        g = c.post("/api/v1/device-groups", json={
            "name": "SQL servers", "kind": "dynamic", "asset_kinds": ["server"],
            "condition": leaf("running_services", "has", ["MSSQLSERVER"])})
        assert g.status_code == 201, g.text
        gid = g.json()["id"]
        pv = c.post("/api/v1/device-groups/preview", json={
            "asset_kinds": ["server"], "condition": leaf("software", "has_containing", "sql server")}).json()
        assert pv["count"] == 2                                     # installed on sql-01 and sql-02
        cat = c.get("/api/v1/device-groups/catalog").json()
        fields = {x["key"]: x for x in (cat.get("fields") if isinstance(cat, dict) else cat)}
        assert [o["key"] for o in fields["services"]["operators"]] == [
            "has", "has_containing", "has_none", "has_none_containing", "reported", "not_reported"]
    from app.services.device_groups import membership
    async with session(pg_db) as s:
        await membership.reconcile(s, force=True)
        await s.commit()
    members = {r[0] for r in pg_db.query(
        "SELECT asset_id::text FROM device_group_members WHERE group_id = $1", gid)}
    assert members == {f["sql-01"]}                                # running, not merely installed

    with api_client(pg_db, role="ops", permissions=MANAGER, persist_user=True) as c:
        r = c.post("/api/v1/host-alert-rules", json={
            "name": "SQL memory", "metric": "host_memory_pct", "operator": "gt", "threshold": 90,
            "notify_channels": [CHANNEL], "group_id": gid})
        assert r.status_code == 200, r.text
        rule = r.json()
        assert rule["group_id"] == gid and rule["scope_tag"] is None
        bad = c.post("/api/v1/host-alert-rules", json={
            "name": "x", "metric": "host_memory_pct", "server_id": f["sql-01"], "scope_tag": "sql"})
        assert bad.status_code == 422
        listed = c.get(f"/api/v1/host-alert-rules?server_id={f['web-01']}").json()["items"]
        assert rule["id"] not in [x["id"] for x in listed]          # web-01 is outside the group
        listed = c.get(f"/api/v1/host-alert-rules?server_id={f['sql-01']}").json()["items"]
        assert rule["id"] in [x["id"] for x in listed]
        # the server's alert-rule card (rules_for_asset -> covers) agrees with the evaluator
        card = c.get(f"/api/v1/alert-rules/for-asset?asset_kind=server&asset_id={f['web-01']}").json()["items"]
        assert rule["id"] not in [x["id"] for x in card]
        card = c.get(f"/api/v1/alert-rules/for-asset?asset_kind=server&asset_id={f['sql-01']}").json()["items"]
        assert rule["id"] in [x["id"] for x in card]

    from app.services import host_alert_service as has
    sent = []

    async def dispatch(db_, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)

    def history(metric, since, until):
        series = [(T0 + 100 + i * 30, 97.0) for i in range(12)]
        return {sid: {"": series} for sid in f.values()}

    monkeypatch.setattr(has, "fetch_host_history", history)
    async with session(pg_db) as s:
        await has.evaluate_host_rules(s, now=T0 + 400)
    rows = pg_db.query("SELECT server_id::text, status FROM alerts WHERE rule_id = $1", rule["id"])
    assert rows == [(f["sql-01"], "active")]                        # only the group's member

    # SQL Server stops: the server leaves the group, and the incident is
    # resolved without a recovery notice (the rule no longer watches it).
    pg_db.execute("UPDATE server_service_inventory SET state = 'stopped' WHERE server_id = $1"
                  " AND service_name = 'MSSQLSERVER'", f["sql-01"])
    async with session(pg_db) as s:
        await membership.reconcile(s, force=True)
        await s.commit()
    sent.clear()
    async with session(pg_db) as s:
        await has.evaluate_host_rules(s, now=T0 + 430)
    status, meta = pg_db.query("SELECT status, metadata FROM alerts WHERE rule_id = $1", rule["id"])[0]
    meta = meta if isinstance(meta, dict) else json.loads(meta)
    assert status == "resolved" and meta["resolved_by"] == "out_of_scope"
    assert not [ctx for ctx in sent if ctx.get("rule_id") == rule["id"]]


# ── discovery rules: classify by tag, alert on the tag ──────────────────────

@pytest.mark.asyncio
async def test_rule_tags_sql_servers_and_a_tag_scoped_rule_covers_them(pg_db, fleet, monkeypatch):
    f = fleet
    with api_client(pg_db, role="ops", permissions=MANAGER, persist_user=True) as c:
        r = c.post(RULES, json={"name": "SQL servers", "mode": "active", "asset_kinds": ["server"],
                                "triggers": ["changed"],
                                "condition": leaf("software", "has_containing", "Microsoft SQL Server"),
                                "actions": [{"key": "tags", "value": ["sql"]}]})
        assert r.status_code == 201, r.text
        unknown = c.post(RULES, json={"name": "No IIS", "mode": "draft", "asset_kinds": ["server"],
                                      "condition": leaf("services", "has_none", ["W3SVC"]),
                                      "actions": [{"key": "tags", "value": ["no-iis"]}]})
        assert unknown.status_code == 201
        c.post(f"{RULES}/{unknown.json()['id']}/mode", json={"mode": "active"}).raise_for_status()
        pre = c.post(f"{RULES}/inventory/preview", json={"rule_ids": [unknown.json()["id"]],
                                                          "targets": {"kinds": ["server"]}}).json()
        # sql-01 and sql-02 report services without W3SVC; lnx-01 and new-01 report none: unknown
        assert sorted(ch["asset_name"] for ch in pre["changes"]) == ["sql-01", "sql-02"]
    pg_db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'discovery_rules'",
                  {**runner.DEFAULT_SETTINGS, "enabled": True})
    async with session(pg_db) as s:
        out = await reconcile.reconcile_changed(s)
    assert out["applied"] == 2
    tagged = sorted(n for (n,) in pg_db.query("SELECT display_name FROM servers WHERE tags ? 'sql'"))
    assert tagged == ["sql-01", "sql-02"]
    async with session(pg_db) as s:                                  # settle (tags changed the fingerprint)
        await reconcile.reconcile_changed(s)
    async with session(pg_db) as s:
        assert await reconcile.reconcile_changed(s) == {"assets": 0}
    # a new inventory snapshot on web-01 with SQL Server: picked up by the next pass
    software(pg_db, f["web-01"], "Microsoft SQL Server 2022 Express", "Microsoft Corporation")
    pg_db.execute("UPDATE servers SET inventory_updated_at = now() WHERE id = $1", f["web-01"])
    async with session(pg_db) as s:
        out = await reconcile.reconcile_changed(s)
    assert out["assets"] == 1 and out["applied"] == 1

    with api_client(pg_db, role="ops", permissions=MANAGER, persist_user=True) as c:
        rule = c.post("/api/v1/host-alert-rules", json={
            "name": "SQL CPU", "metric": "host_cpu_pct", "operator": "gt", "threshold": 80,
            "notify_channels": [CHANNEL], "scope_tag": "SQL"}).json()
    from app.services import host_alert_service as has
    monkeypatch.setattr(has, "dispatch_to_channels", lambda *a, **k: _zero())

    def history(metric, since, until):
        series = [(T0 + 100 + i * 30, 95.0) for i in range(12)]
        return {sid: {"": series} for sid in f.values()}

    monkeypatch.setattr(has, "fetch_host_history", history)
    async with session(pg_db) as s:
        await has.evaluate_host_rules(s, now=T0 + 400)
    hit = sorted(pg_db.query("SELECT server_id::text FROM alerts WHERE rule_id = $1", rule["id"]))
    assert hit == sorted([(f["sql-01"],), (f["sql-02"],), (f["web-01"],)])
    from app.services import alert_rule_scope as rs
    async with session(pg_db) as s:
        row = await rs.rule_row(s, rule["id"])
        assert (await rs.affected(s, row))["count"] == 3
        assert rs.describe_scope(row)["label"] == "Servers with tag SQL"


async def _zero():
    return 0


def test_tag_scoped_users_may_manage_a_server_rule_on_their_tag(pg_db):
    add_server(pg_db, "sql-01", tags=["team-db"])
    with api_client(pg_db, role="db-team", permissions=["alerts.view", "alerts.manage", "servers.view"],
                    scope_tags=["team-db"], persist_user=True) as c:
        ok = c.post("/api/v1/host-alert-rules", json={"name": "DB CPU", "metric": "host_cpu_pct",
                                                      "threshold": 90, "scope_tag": "team-db"})
        assert ok.status_code == 200, ok.text
        fleet_wide = c.post("/api/v1/host-alert-rules", json={"name": "All CPU", "metric": "host_cpu_pct",
                                                              "threshold": 90})
        assert fleet_wide.status_code == 403
