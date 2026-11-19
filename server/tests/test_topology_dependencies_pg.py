"""Parent/child dependencies (alerting plan G-13, migrate-203; docs/dependencies.md).

Pure parts (the suggestion planner, the gateway reader, the server
availability states) run without a database; the API, ownership, cycles,
scope, suggestions, the migration backfill and the server availability
signals run on a disposable PostgreSQL (pg_fixture).
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import server_availability as sa
from app.services import topology_dependencies as deps

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

REPO = Path(__file__).resolve().parents[2]
MANAGER = ["devices.view", "devices.manage", "servers.view"]


# ── Planner ─────────────────────────────────────────────────────────────────

def fs(*pairs):
    return [frozenset(p) for p in pairs]


NODES = {"gw": {"ip": "192.0.2.1", "last_rtt_ms": 2.0}, "core": {"ip": "192.0.2.2", "last_rtt_ms": 0.4},
         "a1": {"ip": "192.0.2.3", "last_rtt_ms": 1.0}, "a2": {"ip": "192.0.2.4", "last_rtt_ms": 1.0},
         "lone": {"ip": "192.0.2.9", "last_rtt_ms": None}}


def test_plan_from_the_gateway():
    p = deps.plan(NODES, fs(("gw", "core"), ("core", "a1"), ("core", "a2")), gateways=["192.0.2.1"])
    assert p["roots"] == [("gw", "gateway")]
    assert p["parents"] == {"core": ("gw", 1), "a1": ("core", 2), "a2": ("core", 2)}
    assert p["redundant"] == {} and p["unrooted"] == []


def test_plan_falls_back_to_lowest_rtt_and_honours_a_chosen_root():
    pairs = fs(("gw", "core"), ("core", "a1"))
    assert deps.plan(NODES, pairs)["roots"] == [("core", "lowest_rtt")]
    assert deps.plan(NODES, pairs)["parents"] == {"gw": ("core", 1), "a1": ("core", 1)}
    chosen = deps.plan(NODES, pairs, root="a1")
    assert chosen["roots"] == [("a1", "chosen")] and chosen["parents"]["gw"] == ("core", 2)


def test_plan_skips_redundant_uplinks_and_unrooted_parts():
    nodes = {**NODES, "x": {"ip": "192.0.2.10", "last_rtt_ms": None}, "y": {"ip": "192.0.2.11", "last_rtt_ms": None}}
    p = deps.plan(nodes, fs(("gw", "a1"), ("gw", "a2"), ("a1", "core"), ("a2", "core"), ("x", "y")),
                  gateways=["192.0.2.1"])
    # core hangs off both a1 and a2: either can carry it, so no parent is suggested.
    assert p["redundant"] == {"core": ["a1", "a2"]}
    assert "core" not in p["parents"]
    assert p["unrooted"] == [["x", "y"]]          # no gateway, no round-trip time


def test_default_gateways(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t0102000A\t0003\t0\t0\t100\t00000000\n"
        "eth0\t0002000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")
    assert deps.default_gateways(str(route)) == ["10.0.2.1"]
    assert deps.default_gateways(str(tmp_path / "missing")) == []


# ── Server availability ─────────────────────────────────────────────────────

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def sig(**kw):
    base = dict(server_id="s1", hostname="web-01", heartbeat_at=NOW - timedelta(minutes=10))
    base.update(kw)
    return sa.ServerSignals(**base)


def test_server_behind_a_down_parent_is_not_paged():
    s = sig(upstream="down", upstream_name="access-1")
    state = sa.judge(s, NOW.timestamp(), 180)
    assert state == "upstream_down"
    down_rule = {"metric": "ping_status", "trigger_on": "down"}
    assert sa.breach_for(down_rule, state) is None            # neither opens nor clears
    assert sa.covered([down_rule], s, state) is True          # no built-in agent_offline either
    assert sa.describe(down_rule, state, s, 180) == "agent silent for over 3 min; upstream access-1 is down"
    # Ping answering beats the dependency; a fresh heartbeat is up whatever the parent.
    assert sa.judge(sig(upstream="down", ping="up"), NOW.timestamp(), 180) == "agent_offline"
    assert sa.judge(sig(upstream="down", heartbeat_at=NOW), NOW.timestamp(), 180) == "up"
    assert sa.judge(sig(), NOW.timestamp(), 180) == "down"


# ── PostgreSQL ──────────────────────────────────────────────────────────────

def add_device(db, name, ip, *, status="up", tags=None, rtt=None):
    return str(db.scalar("INSERT INTO devices (hostname, ip_address, status, device_type, tags, last_rtt_ms) "
                         "VALUES ($1, $2, $3, 'switch', $4::jsonb, $5) RETURNING id",
                         name, ip, status, tags or "[]", rtt))


def add_server(db, name, ip, *, tags=None):
    return str(db.scalar("INSERT INTO servers (display_name, hostname, primary_ip, collection_mode, tags) "
                         "VALUES ($1, $1, $2, 'agent', $3::jsonb) RETURNING id", name, ip, tags or "[]"))


def link(db, a, a_name, b, b_name):
    db.execute("INSERT INTO topology_links (local_device_id, local_if_index, local_if_name, remote_device_id,"
               " remote_port_id, remote_if_name, protocol, last_seen_at)"
               " VALUES ($1, 1, $2, $3, $4, $4, 'lldp', now() - interval '1 hour')", a, a_name, b, b_name)


def client(db, **kw):
    return api_client(db, role=kw.pop("role", "dep-admin"), permissions=kw.pop("permissions", MANAGER),
                      persist_user=True, username=kw.pop("username", "dep-admin"), **kw)


def test_create_list_delete_and_audit(pg_db):
    db = pg_db
    core = add_device(db, "core-1", "198.51.100.1")
    acc = add_device(db, "access-1", "198.51.100.2")
    web = add_server(db, "web-01", "198.51.100.20")
    with client(db) as c:
        r = c.post("/api/v1/topology-dependencies", json={"parent_device_id": core, "child_device_id": acc})
        assert r.status_code == 201, r.text
        dep = r.json()
        assert dep["source"] == "manual" and dep["removable"] is True and dep["created_by"] == "dep-admin"
        assert dep["parent"]["name"] == "core-1" and dep["child"] == {
            "kind": "device", "id": acc, "name": "access-1", "ip": "198.51.100.2", "status": "up", "type": "switch"}
        s = c.post("/api/v1/topology-dependencies", json={"parent_device_id": acc, "child_server_id": web,
                                                          "dependency_type": "uplink", "notes": "rack 4"})
        assert s.status_code == 201, s.text
        assert s.json()["child"]["kind"] == "server" and s.json()["notes"] == "rack 4"

        # Duplicates, loops, self-dependencies and bad bodies are refused.
        assert c.post("/api/v1/topology-dependencies",
                      json={"parent_device_id": core, "child_device_id": acc}).status_code == 409
        loop = c.post("/api/v1/topology-dependencies", json={"parent_device_id": acc, "child_device_id": core})
        assert loop.status_code == 409 and "loop" in loop.json()["detail"]
        assert c.post("/api/v1/topology-dependencies",
                      json={"parent_device_id": core, "child_device_id": core}).status_code == 409
        assert c.post("/api/v1/topology-dependencies", json={"parent_device_id": core}).status_code == 422
        assert c.post("/api/v1/topology-dependencies", json={"parent_device_id": core, "child_device_id": acc,
                                                             "source": "controller"}).status_code == 422

        listed = c.get("/api/v1/topology-dependencies").json()["data"]
        assert {(d["parent"]["name"], d["child"]["name"]) for d in listed} == {("core-1", "access-1"),
                                                                               ("access-1", "web-01")}
        assert len(c.get(f"/api/v1/topology-dependencies?server_id={web}").json()["data"]) == 1
        assert len(c.get(f"/api/v1/topology-dependencies?device_id={acc}").json()["data"]) == 2

        assert c.delete(f"/api/v1/topology-dependencies/{dep['id']}").status_code == 200
        assert c.delete(f"/api/v1/topology-dependencies/{dep['id']}").status_code == 404
    actions = [r[0] for r in db.query("SELECT action FROM audit_logs WHERE resource_type = 'topology_dependency' "
                                      "ORDER BY created_at")]
    assert actions == ["topology_dependency.create", "topology_dependency.create", "topology_dependency.delete"]


def test_engine_rows_are_listed_but_not_deleted(pg_db):
    db = pg_db
    fw = add_device(db, "fw-1", "198.51.100.31")
    ap = add_device(db, "ap-1", "198.51.100.32")
    sw = add_device(db, "sw-9", "198.51.100.33")
    ctl = db.scalar("INSERT INTO topology_dependencies (parent_device_id, child_device_id, dependency_type, notes, "
                    "source) VALUES ($1, $2, 'controller', 'auto: managed-device sync', 'controller') RETURNING id",
                    fw, ap)
    rule = db.scalar("INSERT INTO topology_dependencies (parent_device_id, child_device_id, notes, source) "
                     "VALUES ($1, $2, 'Discovery rule: uplinks', 'discovery_rule') RETURNING id", fw, sw)
    with client(db) as c:
        rows = {d["id"]: d for d in c.get("/api/v1/topology-dependencies").json()["data"]}
        assert rows[str(ctl)]["removable"] is False and rows[str(ctl)]["source_label"] == "Controller sync"
        for dep in (ctl, rule):
            r = c.delete(f"/api/v1/topology-dependencies/{dep}")
            assert r.status_code == 409 and "change it there" in r.json()["detail"]
    assert db.scalar("SELECT count(*) FROM topology_dependencies") == 2


def test_writes_need_manage_and_scope(pg_db):
    db = pg_db
    mine = add_device(db, "a-1", "198.51.100.41", tags='["team-a"]')
    mine2 = add_device(db, "a-2", "198.51.100.42", tags='["team-a"]')
    theirs = add_device(db, "b-1", "198.51.100.43", tags='["team-b"]')
    with client(db, role="dep-viewer", permissions=["devices.view"], username="viewer") as c:
        assert c.post("/api/v1/topology-dependencies",
                      json={"parent_device_id": mine, "child_device_id": mine2}).status_code == 403
    with client(db, role="dep-scoped", username="scoped", scope_tags=("team-a",)) as c:
        assert c.post("/api/v1/topology-dependencies",
                      json={"parent_device_id": theirs, "child_device_id": mine}).status_code == 403
        assert c.post("/api/v1/topology-dependencies",
                      json={"parent_device_id": mine, "child_device_id": mine2}).status_code == 201
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id) VALUES ($1, $2)", theirs, mine)
    with client(db, role="dep-scoped", username="scoped", scope_tags=("team-a",)) as c:
        listed = c.get("/api/v1/topology-dependencies").json()["data"]
        assert [(d["parent"]["name"], d["child"]["name"]) for d in listed] == [("a-1", "a-2")]


def test_suggestions_follow_the_current_topology(pg_db, monkeypatch):
    db = pg_db
    gw = add_device(db, "gw-1", "198.51.100.1")
    core = add_device(db, "core-1", "198.51.100.2")
    acc = add_device(db, "access-1", "198.51.100.3")
    old = add_device(db, "old-1", "198.51.100.4")
    link(db, gw, "ge-0/0/1", core, "Te1/1/1")
    link(db, core, "Gi1/0/48", acc, "Gi0/1")
    # Evidence older than 24 h is not a neighbour.
    db.execute("INSERT INTO topology_links (local_device_id, local_if_index, local_if_name, remote_device_id,"
               " remote_port_id, protocol, last_seen_at) VALUES ($1, 2, 'Gi1/0/47', $2, 'Gi0/1', 'lldp', "
               "now() - interval '3 days')", core, old)
    monkeypatch.setattr(deps, "default_gateways", lambda path="/proc/net/route": ["198.51.100.1"])
    with client(db) as c:
        out = c.get("/api/v1/topology-dependencies/suggestions").json()
        assert [r["name"] for r in out["roots"]] == ["gw-1"] and out["roots"][0]["reason"] == "gateway"
        pairs = [(s["parent"]["name"], s["child"]["name"], s["hops"]) for s in out["suggestions"]]
        assert pairs == [("gw-1", "core-1", 1), ("core-1", "access-1", 2)]
        first = out["suggestions"][1]
        assert first["parent_interface"] == "Gi1/0/48" and first["protocol"] == "lldp"
        assert db.scalar("SELECT count(*) FROM topology_dependencies") == 0     # never created by itself

        # Accepting one is a topology-owned POST; it then drops out of the suggestions.
        r = c.post("/api/v1/topology-dependencies", json={"parent_device_id": core, "child_device_id": acc,
                                                          "source": "topology"})
        assert r.status_code == 201 and r.json()["source"] == "topology" and r.json()["removable"] is True
        again = c.get("/api/v1/topology-dependencies/suggestions").json()
        assert [(s["parent"]["name"], s["child"]["name"]) for s in again["suggestions"]] == [("gw-1", "core-1")]

        chosen = c.get(f"/api/v1/topology-dependencies/suggestions?root_device_id={acc}").json()
        assert chosen["roots"][0]["reason"] == "chosen"
        assert [(s["parent"]["name"], s["child"]["name"]) for s in chosen["suggestions"]] == [("core-1", "gw-1")]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def test_server_signals_read_the_parent(pg_db):
    db = pg_db
    sw = add_device(db, "access-1", "198.51.100.51", status="down")
    web = add_server(db, "web-01", "198.51.100.52")
    other = add_server(db, "web-02", "198.51.100.53")
    db.execute("INSERT INTO agents (agent_uid, server_id, status, last_heartbeat_at) "
               "VALUES ('u1', $1, 'online', now() - interval '10 minutes')", web)
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_server_id) VALUES ($1, $2)", sw, web)

    async def load():
        async with session(db) as s:
            return await sa.load_signals(s)

    signals = asyncio.run(load())
    assert (signals[web].upstream, signals[web].upstream_name) == ("down", "access-1")
    assert signals[other].upstream is None
    now = datetime.now(timezone.utc).timestamp()
    assert sa.judge(signals[web], now, 180) == "upstream_down"
    # A disabled or non-suppressing dependency does not count.
    db.execute("UPDATE topology_dependencies SET suppress_alerts = false")
    assert asyncio.run(load())[web].upstream is None


def test_discovery_rule_writes_and_undoes_only_its_own_rows(pg_db):
    from app.services.discovery_rules import inventory
    db = pg_db
    core = add_device(db, "core-1", "198.51.100.71")
    acc = add_device(db, "access-1", "198.51.100.72")
    acc2 = add_device(db, "access-2", "198.51.100.73")
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id) VALUES ($1, $2)", core, acc)

    async def write(child, value):
        async with session(db) as s:
            await inventory._write(s, {"asset_kind": "device", "action_key": "dependency_parent", "asset_id": child,
                                       "item_key": core, "rule_name": "uplinks"}, value)
            await s.commit()

    asyncio.run(write(acc2, core))
    assert db.scalar("SELECT source FROM topology_dependencies WHERE child_device_id = $1", acc2) == "discovery_rule"
    asyncio.run(write(acc2, None))
    asyncio.run(write(acc, None))          # the operator's row for core-1 -> access-1 is not the rule's
    assert [r[0] for r in db.query("SELECT d.hostname FROM topology_dependencies td "
                                   "JOIN devices d ON d.id = td.child_device_id")] == ["access-1"]


def test_migration_backfills_owners_and_is_idempotent(pg_db):
    db = pg_db
    fw = add_device(db, "fw-1", "198.51.100.61")
    ap = add_device(db, "ap-1", "198.51.100.62")
    sw = add_device(db, "sw-1", "198.51.100.63")
    sw2 = add_device(db, "sw-2", "198.51.100.64")
    db.execute("ALTER TABLE topology_dependencies ALTER COLUMN source DROP NOT NULL")
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id, dependency_type, notes, source) "
               "VALUES ($1, $2, 'controller', 'auto: managed-device sync', NULL)", fw, ap)
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id, notes, source) "
               "VALUES ($1, $2, 'Discovery rule: core uplinks', NULL)", fw, sw)
    db.execute("INSERT INTO topology_dependencies (parent_device_id, child_device_id, source) "
               "VALUES ($1, $2, NULL)", sw, sw2)
    db.psql_file(REPO / "scripts/migrate-203-topology-dependency-management.sql")
    db.psql_file(REPO / "scripts/migrate-203-topology-dependency-management.sql")
    got = dict(db.query("SELECT d.hostname, td.source FROM topology_dependencies td "
                        "JOIN devices d ON d.id = td.child_device_id"))
    assert got == {"ap-1": "controller", "sw-1": "discovery_rule", "sw-2": "manual"}
    with pytest.raises(Exception):
        db.execute("INSERT INTO topology_dependencies (parent_device_id) VALUES ($1)", fw)
