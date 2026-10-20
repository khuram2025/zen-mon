"""License node capacity: the decision, the node count and the refused creates.

A node is a device or a server; a server linked to a device counts once and
controller children never count. The pure decision is tested directly; the
count and the create routes run against the private PostgreSQL cluster from
``pg_fixture`` (never the appliance database) through a plain ``TestClient``.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import license_capacity as lc
from pg_fixture import PgDatabase, pg_cluster, pg_db  # noqa: F401

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
# conftest's autouse fixture swaps in an unmetered capacity for fake-DB tests;
# this module tests the real one.
REAL_GET_CAPACITY = lc.get_capacity
REPO = Path(__file__).resolve().parents[2]


def snap(cap=10, used=None, reported=None, **extra):
    s = {"name": "Pro", "plan": "professional", "is_active": True, "is_expired": False,
         "expires_at": None, "max_devices": 0,
         "license": {"total_node_cap": cap, "used_node_count": used if used is not None else 0,
                     "available_nodes": cap - (used or 0)}}
    if reported is not None:
        s["reported_node_count"] = reported
    s.update(extra)
    return s


def usage(devices=0, servers=0, linked=0, children=0):
    return lc.NodeUsage(devices=devices, servers=servers, linked_servers=linked,
                        controller_children=children)


# ── decision ─────────────────────────────────────────────────────────

def test_devices_and_servers_share_one_node_pool():
    cap = lc.compute_capacity(snap(10), usage(devices=4, servers=3, linked=2, children=30), now=NOW)
    assert (cap.state, cap.limit, cap.used, cap.available, cap.can_add) == ("ok", 10, 7, 3, True)
    assert cap.as_dict()["usage"] == {"devices": 4, "servers": 3, "linked_servers": 2,
                                      "controller_children": 30, "nodes": 7}


def test_reaching_the_limit_blocks_adding_with_the_capacity_message():
    cap = lc.compute_capacity(snap(10), usage(devices=6, servers=4), now=NOW)
    assert (cap.state, cap.available, cap.can_add) == ("full", 0, False)
    assert "You have reached your license capacity (10 of 10 nodes)" in cap.message
    assert "Extend your license" in cap.message
    err = lc.capacity_error(cap)
    assert err.status_code == 402
    assert err.detail["code"] == "license_capacity_reached"
    assert err.detail["message"] == cap.message


def test_over_the_limit_after_a_smaller_license_blocks_but_keeps_existing_nodes():
    cap = lc.compute_capacity(snap(5), usage(devices=8), now=NOW)
    assert (cap.state, cap.available, cap.can_add) == ("over_limit", 0, False)
    assert "8 of 5" in cap.message


def test_near_limit_warns_but_still_allows():
    cap = lc.compute_capacity(snap(10), usage(devices=9), now=NOW)
    assert (cap.state, cap.can_add, cap.available) == ("near_limit", True, 1)


def test_shared_pool_subtracts_the_other_appliances_usage():
    # Pool of 100; the last check-in reported 30 here and 70 in total, so the
    # other appliances use 40 and this one may use 60.
    cap = lc.compute_capacity(snap(100, used=70, reported=30), usage(devices=35), now=NOW)
    assert (cap.limit, cap.pool_total, cap.pool_other_usage, cap.available) == (60, 100, 40, 25)


def test_snapshot_without_a_reported_count_gives_this_appliance_the_whole_pool():
    cap = lc.compute_capacity(snap(100, used=70), usage(devices=35), now=NOW)
    assert (cap.limit, cap.pool_other_usage) == (100, 0)


def test_expired_and_inactive_licenses_block_adding():
    by_flag = lc.compute_capacity(snap(100, is_expired=True), usage(devices=1), now=NOW)
    by_date = lc.compute_capacity(snap(100, expires_at=(NOW - timedelta(days=1)).isoformat()),
                                  usage(devices=1), now=NOW)
    inactive = lc.compute_capacity(snap(100, is_active=False), usage(devices=1), now=NOW)
    assert (by_flag.state, by_flag.can_add) == ("expired", False)
    assert (by_date.state, by_date.can_add) == ("expired", False)
    assert (inactive.state, inactive.can_add) == ("inactive", False)
    assert "renewed" in by_flag.message
    future = lc.compute_capacity(snap(100, expires_at=(NOW + timedelta(days=3)).isoformat()),
                                 usage(devices=1), now=NOW)
    assert future.state == "ok"


def test_unregistered_appliance_gets_the_evaluation_allowance():
    cap = lc.compute_capacity(None, usage(devices=lc.EVALUATION_NODE_LIMIT), now=NOW)
    assert (cap.source, cap.state, cap.limit, cap.can_add) == ("evaluation", "full", 50, False)
    assert "Register this appliance" in cap.message


def test_no_node_cap_falls_back_to_max_devices_then_unmetered():
    fallback = lc.compute_capacity(snap(0, max_devices=20), usage(devices=5), now=NOW)
    assert (fallback.limit, fallback.state) == (20, "ok")
    unmetered = lc.compute_capacity(snap(0), usage(devices=5000), now=NOW)
    assert (unmetered.state, unmetered.enforced, unmetered.can_add, unmetered.limit) == (
        "unmetered", False, True, None)


def test_bulk_error_names_how_many_nodes_are_left():
    cap = lc.compute_capacity(snap(10), usage(devices=8), now=NOW)
    err = lc.capacity_error(cap, requested=5)
    assert "Adding 5 nodes would exceed your license capacity: only 2 of 10" in err.detail["message"]


def test_budget_counts_down_and_unlimited_never_runs_out():
    budget = lc.NodeBudget(lc.compute_capacity(snap(10), usage(devices=8), now=NOW))
    assert budget.take() and budget.take() and not budget.take()
    assert "reached your license capacity" in budget.exhausted_message
    free = lc.NodeBudget(lc.compute_capacity(snap(0), usage(devices=8), now=NOW))
    assert free.unlimited and all(free.take() for _ in range(100))


def test_annotate_records_the_reported_count_but_not_a_failed_zero():
    assert lc.annotate_snapshot({"plan": "p"}, 12) == {"plan": "p", "reported_node_count": 12}
    assert lc.annotate_snapshot({"plan": "p"}, 0) == {"plan": "p"}


def test_load_snapshot_tolerates_missing_and_corrupt_files(tmp_path):
    assert lc.load_snapshot(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert lc.load_snapshot(bad) is None
    good = tmp_path / "subscription.json"
    good.write_text(json.dumps(snap(10)))
    loaded = lc.load_snapshot(good)
    assert loaded["license"]["total_node_cap"] == 10 and loaded["_snapshot_at"]


def _updater_node_sql() -> str:
    tree = ast.parse((REPO / "updater" / "inventory.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "NODE_COUNT_SQL" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("updater/inventory.py has no NODE_COUNT_SQL")


def test_updater_reports_the_same_node_count_the_api_enforces():
    assert _updater_node_sql() == lc.NODE_COUNT_SQL


# ── count + routes on a disposable database ──────────────────────────

def add_user(db: PgDatabase) -> SimpleNamespace:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    user_id = db.scalar("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, 'x', 'admin') "
                        "RETURNING id", name, f"{name}@example.test")
    return SimpleNamespace(id=user_id, username=name, email=f"{name}@example.test", full_name=name,
                           role="admin", is_active=True, last_login=None, password_hash="", scope_tags=[])


def add_device(db: PgDatabase, n: int, *, poll_mode: str = "direct"):
    return db.scalar("INSERT INTO devices (hostname, ip_address, poll_mode) VALUES ($1, $2::inet, $3) RETURNING id",
                     f"dev-{n}", f"192.0.2.{n}", poll_mode)


def add_server(db: PgDatabase, name: str, device_id=None):
    return db.scalar("INSERT INTO servers (display_name, hostname, device_id) VALUES ($1, $1, $2) RETURNING id",
                     name, device_id)


@pytest.fixture
def api(pg_db, tmp_path, monkeypatch):
    admin = add_user(pg_db)
    path = tmp_path / "subscription.json"
    monkeypatch.setattr(lc, "SUBSCRIPTION_PATH", path)
    monkeypatch.setattr(lc, "get_capacity", REAL_GET_CAPACITY)
    # The migrations seed demo inventory; every limit is relative to it.
    base = pg_db.query(lc.NODE_COUNT_SQL)[0]
    base_nodes = base[0] + base[1]

    def license_of(extra):
        path.write_text(json.dumps(snap(base_nodes + extra)))

    async def session():
        engine = create_async_engine(pg_db.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                yield db
        finally:
            await engine.dispose()

    async def signed_in():
        return admin

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = signed_in
    try:
        yield SimpleNamespace(client=TestClient(app), db=pg_db, admin=admin, license_of=license_of,
                              base=base, base_nodes=base_nodes)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_node_count_counts_linked_pairs_once_and_skips_controller_children(api):
    d1 = add_device(api.db, 1)
    add_device(api.db, 2)
    child = add_device(api.db, 3, poll_mode="via_controller")
    add_server(api.db, "srv-linked", d1)          # same machine as dev-1
    add_server(api.db, "srv-alone")               # its own node
    add_server(api.db, "srv-on-child", child)     # child holds no node, so the server does
    api.license_of(10)

    body = api.client.get("/api/v1/subscription/capacity").json()
    b = api.base
    assert body["usage"] == {"devices": b[0] + 2, "servers": b[1] + 2, "linked_servers": b[2] + 1,
                             "controller_children": b[3] + 1, "nodes": api.base_nodes + 4}
    assert (body["available"], body["state"], body["can_add"]) == (6, "ok", True)


def test_add_server_is_refused_at_capacity_with_the_message(api):
    add_device(api.db, 1)
    add_server(api.db, "srv-1")
    api.license_of(2)
    servers = api.db.scalar("SELECT count(*) FROM servers")

    resp = api.client.post("/api/v1/servers", json={"display_name": "srv-new", "hostname": "srv-new"})
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    full = api.base_nodes + 2
    assert detail["code"] == "license_capacity_reached"
    assert f"You have reached your license capacity ({full} of {full} nodes)" in detail["message"]
    assert api.db.scalar("SELECT count(*) FROM servers") == servers


def test_a_server_linked_to_a_device_needs_no_extra_node(api):
    dev = add_device(api.db, 1)
    add_server(api.db, "srv-1")
    api.license_of(2)

    resp = api.client.post("/api/v1/servers", json={"display_name": "dev-1 host", "hostname": "dev-1",
                                                     "device_id": str(dev)})
    assert resp.status_code == 200, resp.text
    assert api.client.get("/api/v1/subscription/capacity").json()["used"] == api.base_nodes + 2


def test_add_device_is_refused_at_capacity_and_allowed_below_it(api):
    add_server(api.db, "srv-1")
    api.license_of(2)
    devices = api.db.scalar("SELECT count(*) FROM devices")

    ok = api.client.post("/api/v1/devices", json={"hostname": "edge-1", "ip_address": "198.51.100.1"})
    assert ok.status_code == 201, ok.text
    refused = api.client.post("/api/v1/devices", json={"hostname": "edge-2", "ip_address": "198.51.100.2"})
    assert refused.status_code == 402, refused.text
    assert refused.json()["detail"]["capacity"]["state"] == "full"
    assert api.db.scalar("SELECT count(*) FROM devices") == devices + 1


def test_bulk_import_creates_up_to_the_limit_and_reports_the_rest(api):
    api.license_of(2)
    rows = [{"hostname": f"bulk-{i}", "ip_address": f"198.51.100.{10 + i}"} for i in range(4)]
    body = api.client.post("/api/v1/devices/bulk-import", json={"devices": rows}).json()
    assert (body["created"], body["skipped"]) == (2, 2)
    assert all("license capacity" in e for e in body["errors"])
    refused = api.client.post("/api/v1/devices/bulk-import", json={"devices": rows[:1]})
    assert refused.status_code == 402


def test_expired_license_blocks_adding_but_deletes_still_work(api):
    dev = add_device(api.db, 1)
    lc.SUBSCRIPTION_PATH.write_text(json.dumps(snap(100, is_expired=True)))
    refused = api.client.post("/api/v1/servers", json={"display_name": "srv-new"})
    assert refused.status_code == 402
    assert refused.json()["detail"]["capacity"]["state"] == "expired"
    assert api.client.delete(f"/api/v1/devices/{dev}").status_code == 204
