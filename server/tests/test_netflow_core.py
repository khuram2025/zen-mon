"""NetFlow API core (contract §9, NF-31, NF-04): safe errors, query budgets,
the NetflowRoute error/envelope handling, router-level permission, visibility
on the endpoints that used to bypass the shared scope, /coverage, /retention and
the background task registry.

Layers:
  * unit and in-process HTTP (TestClient over the real /netflow router with a
    fake ClickHouse client and dependency overrides) — always run;
  * scratch ClickHouse (NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py):
    real timeout and error mapping, /coverage over real tables;
  * live API (NETFLOW_TEST_API=http://127.0.0.1:8101, and NETFLOW_TEST_TOKEN or
    NETFLOW_TEST_USER / NETFLOW_TEST_PASS): a running uvicorn of this tree. With
    NETFLOW_TEST_API_CH_BROKEN=1 the target's ClickHouse account is expected to
    fail every flow query, and only the failure test runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError  # noqa: E402

import app.api.v1.netflow as netflow  # noqa: E402
from app.api.v1.netflow import access, common as C, filters as F, meta as M  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError, NetflowRoute, map_query_error  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_classify, netflow_groups, netflow_tasks  # noqa: E402

LEAKS = ("HTTPDriver", "Code:", "http://", "DB::Exception", "version", "SELECT", "zenplus.", "127.0.0.1")


def _clean(body: dict) -> None:
    text = str(body)
    for needle in LEAKS:
        assert needle not in text, f"{needle!r} leaked in {text}"


# ── Errors ────────────────────────────────────────────────────────────────────

def test_error_body_shape():
    err = NetflowError(500, "The flow query failed", code="query_failed", error_id="nf-1a2b3c4d")
    assert err.body() == {"detail": "The flow query failed (ref nf-1a2b3c4d)", "error_id": "nf-1a2b3c4d",
                          "code": "query_failed"}
    bad = NetflowError(400, "port must be between 0 and 65535", code="invalid_filter", fields={"port": "x"})
    assert bad.body()["error_id"] is None and bad.body()["fields"] == {"port": "x"}
    with pytest.raises(ValueError):
        NetflowError(500, "x", code="oops")
    assert C.new_error_id().startswith("nf-") and len(C.new_error_id()) == 11


@pytest.mark.parametrize("exc,status,code", [
    (DatabaseError("HTTPDriver for http://10.0.0.5:8123 received ClickHouse error code 159\n Code: 159. "
                   "DB::Exception: Timeout exceeded: elapsed 20.1 seconds (TIMEOUT_EXCEEDED) (version 24.10.2.80)"),
     504, "query_timeout"),
    (DatabaseError("HTTPDriver for http://10.0.0.5:8123 received ClickHouse error code 241\n Code: 241. "
                   "DB::Exception: Memory limit (MEMORY_LIMIT_EXCEEDED)"), 503, "unavailable"),
    (OperationalError("Error HTTPConnectionPool(host='10.0.0.5', port=8123): Max retries exceeded"), 503, "unavailable"),
    (DatabaseError("HTTPDriver for http://10.0.0.5:8123 received ClickHouse error code 47\n Code: 47. "
                   "DB::Exception: Unknown expression identifier `nope` in scope SELECT nope FROM zenplus.flow_records"),
     500, "query_failed"),
    (RuntimeError("anything else"), 500, "query_failed"),
])
def test_clickhouse_errors_map_to_safe_responses(exc, status, code, caplog):
    with caplog.at_level(logging.ERROR, logger=C.logger.name):
        err = map_query_error(exc, budget_s=20.0, label="unit", sql="SELECT nope FROM zenplus.flow_records")
    assert (err.status_code, err.code) == (status, code)
    _clean(err.body())
    assert err.error_id in err.detail
    # The operator's log carries the id next to the real exception.
    record = next(r for r in caplog.records if err.error_id in r.getMessage())
    assert record.exc_info is not None


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    """Stands in for the thread-local ClickHouse client."""

    def __init__(self, responder=None):
        self.calls: list[tuple[str, dict, dict]] = []
        self.commands: list[str] = []
        self.responder = responder or default_responder

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}, settings or {}))
        return FakeResult(self.responder(sql, parameters or {}))

    def command(self, sql, parameters=None):
        self.commands.append(sql)

    def flow_sql(self) -> list[str]:
        return [c[0] for c in self.calls if "system." not in c[0]]


TTL_ROWS = [
    ("flow_records", "MergeTree PARTITION BY toYYYYMMDD(timestamp) TTL toDateTime(timestamp) + toIntervalDay(30)"),
    ("flow_conversations_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_qos_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_interface_5m", "AggregatingMergeTree TTL timestamp + toIntervalDay(400)"),
    ("flow_exporter_minutes", "AggregatingMergeTree TTL minute + toIntervalDay(400)"),
]


def default_responder(sql, params):
    if "system.tables" in sql:
        return TTL_ROWS
    if "system.parts" in sql:
        return [("flow_records", datetime(2026, 9, 1)), ("flow_exporter_minutes", datetime(2026, 9, 29, 8, 0))]
    if "GROUP BY" in sql or "ORDER BY" in sql:
        return []
    return [(0, 0, 0, 0, 0, 0, None, 0, 0, 0, 0, 0)]


NO_REPAIR = {"reconciled_at": "2026-09-30T08:00:00Z", "verified_from": "2026-09-01T06:00:00Z",
             "pending_hours": {}, "rebuilt_hours": {}, "unrecoverable_hours": {}, "rows_found_after_upgrade": 0}


@pytest.fixture
def repair(monkeypatch):
    """The X-02 repair state netflow_rollup.repair_status() reports (PostgreSQL)."""
    from app.services import netflow_rollup

    state = {"value": dict(NO_REPAIR)}

    async def status():
        if isinstance(state["value"], Exception):
            raise state["value"]
        return dict(state["value"])

    monkeypatch.setattr(netflow_rollup, "repair_status", status)
    C._repair_cache.update(value=None, at=0.0)
    yield state
    C._repair_cache.update(value=None, at=0.0)


@pytest.fixture
def fake_ch(monkeypatch, repair):
    ch = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)
    C._retention_cache.update(value=None, at=0.0)
    yield ch
    C._retention_cache.update(value=None, at=0.0)


# ── run_query ─────────────────────────────────────────────────────────────────

def test_run_query_sets_budget_and_query_id(fake_ch):
    res = asyncio.run(C.run_query("SELECT 1", {"a": 1}, budget_s=7.5, label="unit test"))
    assert res.result_rows
    sql, params, settings = fake_ch.calls[0]
    assert (sql, params) == ("SELECT 1", {"a": 1})
    assert settings["max_execution_time"] == 8 and settings["query_id"].startswith("nf-unit-test-")


def test_run_query_raises_safe_error(monkeypatch, caplog):
    def boom():
        raise OperationalError("Error HTTPConnectionPool(host='127.0.0.1', port=18123): refused")

    monkeypatch.setattr(C, "get_ch_client", boom)
    with pytest.raises(NetflowError) as exc:
        asyncio.run(C.run_query("SELECT 1"))
    assert exc.value.status_code == 503 and exc.value.code == "unavailable"
    _clean(exc.value.body())


def test_run_query_budget_backstop_kills_a_stuck_query(monkeypatch):
    ch = FakeCH()

    def slow(sql, parameters=None, settings=None):
        time.sleep(0.5)
        return FakeResult([])

    ch.query = slow
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)
    monkeypatch.setattr(C, "_BUDGET_GRACE_S", 0.05)

    async def call():
        started = time.monotonic()
        with pytest.raises(NetflowError) as exc:
            await C.run_query("SELECT slow", budget_s=0.05, label="stuck")
        return time.monotonic() - started, exc.value

    elapsed, err = asyncio.run(call())
    # The API answers at budget + grace although the query thread is still busy.
    assert elapsed < 0.45
    assert err.status_code == 504 and err.code == "query_timeout"
    deadline = time.monotonic() + 2
    while not ch.commands and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ch.commands and ch.commands[0].startswith("KILL QUERY")


# ── Retention ─────────────────────────────────────────────────────────────────

def test_retention_reads_ttls_and_degrades(fake_ch, monkeypatch):
    r = asyncio.run(C.get_retention())
    assert (r.raw_days, r.rollup_days, r.qos_days, r.interface_days, r.exporter_minutes_days) == (30, 90, 90, 400, 400)
    assert r.known and r.earliest["flow_records"] == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert asyncio.run(C.get_retention()) is r              # cached
    monkeypatch.setattr(C, "get_ch_client", lambda: (_ for _ in ()).throw(OperationalError("down")))
    down = asyncio.run(C.get_retention(refresh=True))
    assert not down.known and down.raw_days is None and down.horizon(C.RAW, C.now_utc()) is None


# ── HTTP: the real router over a fake ClickHouse ──────────────────────────────

USER = SimpleNamespace(id=uuid4(), username="viewer", role="viewer", is_active=True, scope_tags=[])


class FakeDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


def make_client(vis: FlowVisibility):
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield FakeDB()

    async def user():
        return USER

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user          # endpoints that still take `user`
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.flow_visibility] = visibility
    def _guard(*perms):
        async def dependency():
            if not any(vis.can(p) for p in perms):
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            return USER
        return dependency

    app.dependency_overrides[access.require_manage] = _guard(access.MANAGE_PERMISSION)
    app.dependency_overrides[access.require_export] = _guard(access.EXPORT_PERMISSION)
    app.dependency_overrides[access.require_triage] = _guard(access.MANAGE_PERMISSION, "alerts.acknowledge")
    return TestClient(app)


@pytest.fixture
def api(fake_ch, monkeypatch):
    async def no_refresh(*a, **kw):
        return None

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    return make_client(F.UNRESTRICTED)


URL = "/api/v1/netflow"


def test_every_route_requires_netflow_view_and_uses_the_netflow_route_class():
    assert netflow.router.routes
    for route in netflow.router.routes:
        assert isinstance(route, NetflowRoute), route.path
        assert any(d.dependency is access.require_view for d in route.dependencies), route.path


def test_role_without_netflow_view_gets_403(monkeypatch):
    from app.core import security

    async def no_perms(db, role):
        return set()

    monkeypatch.setattr(security, "get_role_permissions", no_perms)
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield FakeDB()

    async def user():
        return USER

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[security.get_current_user] = user
    client = TestClient(app)
    # Every route of the module, whatever it is (the permission is router-level).
    checked = 0
    for route in netflow.router.routes:
        path = route.path.replace("{", "").replace("}", "")      # any value for path parameters
        for method in route.methods:
            assert client.request(method, "/api/v1" + path).status_code == 403, (method, route.path)
            checked += 1
    assert checked >= 25


def test_invalid_values_are_400_with_string_detail(api):
    r = api.get(URL + "/forensics", params={"dst_port": "70000"})
    assert r.status_code == 400
    body = r.json()
    assert isinstance(body["detail"], str) and body["code"] == "invalid_filter" and "dst_port" in body["fields"]
    for params in ({"dst_port": "abc"}, {"dscp_eq": "200"}, {"proto": "999"}, {"app": "NoSuchApp"},
                   {"tcpflag": "bogus"}, {"netclass": "Bogus"}, {"group": "NoSuchGroup"}, {"hour": "99"}):
        r = api.get(URL + "/overview", params=params)
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), params


def test_fastapi_validation_errors_become_400_strings(api):
    r = api.get(URL + "/overview", params={"hours": "abc"})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_window" and "hours" in r.json()["detail"]
    r = api.get(URL + "/top-talkers", params={"limit": "500"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_filter" and isinstance(r.json()["detail"], str)


def test_window_contract_over_http(api):
    frm = (datetime.now(timezone.utc) - timedelta(days=46)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A 45-day custom range with the UI's rounded hours works on a rollup view...
    assert api.get(URL + "/overview", params={"hours": 1080, "from": frm, "to": to}).status_code == 200
    # ...and is refused, naming the limit, on a raw-only one.
    r = api.get(URL + "/forensics", params={"from": frm, "to": to})
    assert r.status_code == 400 and "31 days" in r.json()["detail"]
    r = api.get(URL + "/overview", params={"from": "0001-01-01T00:00:00Z", "to": "0002-01-01T00:00:00Z"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_window"
    r = api.get(URL + "/overview", params={"from": frm})
    assert r.status_code == 400 and "both from and to" in r.json()["detail"]


def test_clickhouse_failure_is_a_safe_500(api, fake_ch, caplog):
    def fail(sql, params):
        if "system." in sql:
            return default_responder(sql, params)
        raise DatabaseError("HTTPDriver for http://127.0.0.1:18123 received ClickHouse error code 47\n Code: 47. "
                            "DB::Exception: Unknown identifier in SELECT ... FROM zenplus.flow_records (version 24.10)")

    fake_ch.responder = fail
    with caplog.at_level(logging.ERROR):
        r = api.get(URL + "/overview")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "query_failed" and body["error_id"].startswith("nf-") and body["error_id"] in body["detail"]
    _clean(body)
    assert any(body["error_id"] in rec.getMessage() for rec in caplog.records)


def test_unexpected_exception_is_a_safe_500(api, monkeypatch):
    def broken(*a, **kw):
        raise KeyError("internal detail zenplus.flow_records")

    monkeypatch.setattr(netflow.meta, "_retention_payload", broken)
    r = api.get(URL + "/retention")
    assert r.status_code == 500 and r.json()["code"] == "query_failed"
    _clean(r.json())


def test_legacy_shape_without_envelope_and_meta_with_it(api):
    legacy = api.get(URL + "/overview", params={"hours": 1})
    assert legacy.status_code == 200
    assert set(legacy.json()) >= {"bytes", "packets", "flows", "exporters", "src_hosts", "dst_hosts", "last_seen",
                                  "current_bps", "top_protocol"}
    wrapped = api.get(URL + "/overview", params={"hours": 1, "envelope": 1, "protocol": "0", "tz": "Europe/Berlin"})
    body = wrapped.json()
    assert set(body) == {"data", "meta"} and body["data"] == legacy.json()
    meta = body["meta"]
    assert set(meta) == {"window", "server_time", "query_ms", "restricted", "filters", "retention", "truncated",
                         "sampling", "warnings"}
    assert set(meta["window"]) == {"from", "to", "mode", "tz", "effective_from", "effective_to", "source",
                                   "granularity_s"}
    assert meta["window"]["mode"] == "relative" and meta["window"]["source"] == "raw"
    assert meta["server_time"].endswith("Z") and meta["window"]["from"].endswith("Z")
    assert meta["filters"] == {"protocol": "0", "tz": "Europe/Berlin"}
    assert meta["retention"] == {"raw_days": 30, "rollup_days": 90, "interface_days": 400, "limited": False}
    assert meta["restricted"] is False and isinstance(meta["query_ms"], int)
    # A dimension the endpoint ignores on purpose is not claimed as applied.
    dscp = api.get(URL + "/dscp", params={"envelope": 1, "dscp": "46", "protocol": "17"}).json()["meta"]
    assert dscp["filters"] == {"protocol": "17"}
    cap = api.get(URL + "/capacity", params={"envelope": 1, "exporter": "10.9.0.1"}).json()["meta"]
    assert cap["filters"] == {"exporter": "10.9.0.1"}
    # Lists are wrapped the same way; errors are never wrapped.
    assert set(api.get(URL + "/top-talkers", params={"envelope": 1}).json()) == {"data", "meta"}
    assert "data" not in api.get(URL + "/overview", params={"envelope": 1, "port": "x"}).json()


def test_long_window_is_hybrid_and_never_widened(api):
    """NF-17: a long window not aligned to the hour is served hybrid -- whole
    interior hours from the rollup, the partial edges from raw -- so the
    effective bounds are the requested half-open window (the legacy rollup
    path widened both edges to whole hours and said so as rollup_edges)."""
    frm = (datetime.now(timezone.utc) - timedelta(days=5)).replace(minute=30, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) - timedelta(days=1)).replace(minute=30, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = api.get(URL + "/overview", params={"from": frm, "to": to, "envelope": 1}).json()["meta"]
    assert meta["window"]["source"] == "hybrid"
    assert meta["window"]["effective_from"] == meta["window"]["from"]
    assert meta["window"]["effective_to"] == meta["window"]["to"]
    assert "rollup_edges" not in {w["code"] for w in meta["warnings"]}


def test_legacy_raw_window_beyond_retention_is_disclosed(api):
    now = datetime.now(timezone.utc)
    frm, to = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # A half-hour zone: its local hours do not line up with the hourly rollup, so
    # the heatmap reads raw records (whole-hour zones read the rollup).
    meta = api.get(URL + "/heatmap", params={"from": frm, "to": to, "envelope": 1, "tz": "Asia/Kolkata"}).json()["meta"]
    assert meta["window"]["source"] == "raw" and meta["retention"]["limited"] is True
    effective = datetime.fromisoformat(meta["window"]["effective_from"].replace("Z", "+00:00"))
    assert abs((effective - (now - timedelta(days=30))).total_seconds()) < 120
    assert [w["code"] for w in meta["warnings"]] == ["raw_retention"]
    # The coverage endpoint measures its own bounds: no inferred cut.
    cov = api.get(URL + "/coverage", params={"from": frm, "to": to, "envelope": 1}).json()["meta"]
    assert "raw_retention" not in {w["code"] for w in cov["warnings"]}


def test_legacy_endpoints_take_tz_from_the_request(api):
    meta = api.get(URL + "/capacity", params={"envelope": 1, "tz": "Asia/Tokyo"}).json()["meta"]
    assert meta["window"]["tz"] == "Asia/Tokyo"
    r = api.get(URL + "/capacity", params={"tz": "Mars/Olympus"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_window"


# ── NetFlow v5 sampling repair disclosure (X-02) ──────────────────────────────

AFFECTED = {**NO_REPAIR, "pending_hours": {"flow_conversations_1h": 3},
            "rebuilt_hours": {"flow_qos_1h": 12}, "unrecoverable_hours": {"flow_conversations_1h": 2}}


def test_sampling_repair_warnings():
    w = lambda start: C.Window(start=start, end=datetime(2026, 9, 30, tzinfo=timezone.utc), mode="fixed")  # noqa: E731
    before, after = datetime(2026, 8, 20, tzinfo=timezone.utc), datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    # An appliance that never had sampled v5 traffic discloses nothing.
    assert C.sampling_repair_warnings({**NO_REPAIR, "affected": False}, w(before)) == []
    assert C.sampling_repair_warnings(None, w(before)) == []
    status = {**AFFECTED, "affected": C.sampling_repair_affected(AFFECTED)}
    assert status["affected"]
    codes = [x["code"] for x in C.sampling_repair_warnings(status, w(before))]
    assert codes == ["sampling_repair_pending", "sampling_unverified", "sampling_unrecoverable"]
    assert "2 rollup hours" in C.sampling_repair_warnings(status, w(before))[2]["message"]
    # A window after the reconciliation only sees the pending correction.
    assert [x["code"] for x in C.sampling_repair_warnings(status, w(after))] == ["sampling_repair_pending"]
    assert C.sampling_repair_affected({**NO_REPAIR, "rows_found_after_upgrade": 5})


def test_rollup_responses_disclose_the_sampling_repair(api, repair):
    repair["value"] = AFFECTED
    frm = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rollup = api.get(URL + "/overview", params={"from": frm, "to": to, "envelope": 1}).json()["meta"]
    assert "sampling_repair_pending" in {w["code"] for w in rollup["warnings"]}
    # Raw answers were corrected by the migration: nothing to disclose.
    raw = api.get(URL + "/overview", params={"hours": 1, "envelope": 1}).json()["meta"]
    assert not {w["code"] for w in raw["warnings"]} & {"sampling_repair_pending", "sampling_unverified"}
    # An unreadable repair state is unknown: no claim either way, never a failure.
    repair["value"] = RuntimeError("pg down")
    C._repair_cache.update(value=None, at=0.0)
    r = api.get(URL + "/overview", params={"from": frm, "to": to, "envelope": 1})
    assert r.status_code == 200
    assert api.get(URL + "/retention").json()["sampling_repair"] is None


SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                        permissions=frozenset({"netflow.view"}))


def _binds_scope(params: dict) -> bool:
    """The query bound SCOPED's visible exporter set (whatever the parameter name)."""
    return any(isinstance(v, (list, tuple)) and list(v) == ["10.9.0.1"] for v in params.values())


@pytest.mark.parametrize("path", [
    "/overview", "/timeseries", "/top-talkers", "/top-endpoints", "/top-conversations", "/protocols", "/ports",
    "/applications", "/device-status", "/heatmap", "/interfaces", "/dscp", "/tcp-flags", "/network-classes",
    "/forensics", "/capacity", "/anomalies", "/exporters", "/exporter-devices", "/coverage",
])
def test_every_flow_query_is_scoped_for_restricted_users(fake_ch, monkeypatch, path):
    async def no_refresh(*a, **kw):
        return None

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    client = make_client(SCOPED)
    r = client.get(URL + path, params={"envelope": 1})
    if r.status_code == 404:
        pytest.skip(f"{path} no longer exists")
    assert r.status_code == 200, (path, r.text)
    assert r.json()["meta"]["restricted"] is True
    flow_calls = [(sql, params) for sql, params, _ in fake_ch.calls if "system." not in sql]
    assert flow_calls, path
    for sql, params in flow_calls:
        assert _binds_scope(params), (path, sql)
        # Volume guard on raw aggregates (forensics lists every record, §3.3).
        if path != "/forensics" and "FROM zenplus.flow_records" in sql and "flow_conversations_1h" not in sql:
            assert "fw_event NOT IN (1, 3)" in sql, (path, sql)
    # Naming an exporter outside the scope is a 404, not an empty page.
    if path != "/exporter-devices":           # a listing without an exporter parameter
        assert client.get(URL + path, params={"exporter": "10.9.0.2"}).status_code == 404, path


def test_countries_and_ip_groups_are_scoped(fake_ch, monkeypatch):
    from app.services import geoip

    async def no_refresh(*a, **kw):
        return None

    monkeypatch.setattr(netflow_classify, "load_rules", no_refresh)
    monkeypatch.setattr(netflow_groups, "load_groups", no_refresh)
    monkeypatch.setattr(geoip, "available", lambda: True)
    netflow_groups.set_groups_for_tests([
        netflow_groups.Group(id="g1", name="DC A", cidrs=(netflow_groups.parse_network("10.20.0.0/16"),)),
    ])
    try:
        client = make_client(SCOPED)
        for path in ("/countries", "/ip-groups"):
            assert client.get(URL + path).status_code == 200, path
            sql, params, _ = fake_ch.calls[-1]
            assert _binds_scope(params) and "fw_event NOT IN (1, 3)" in sql, (path, sql)
    finally:
        netflow_groups.set_groups_for_tests([])


def test_device_status_continuity_query_is_compiled_for_raw(api, fake_ch):
    frm = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = api.get(URL + "/device-status", params={"from": frm, "to": to, "tcpflag": "no_flags"})
    if r.status_code == 404:
        pytest.skip("/device-status no longer exists")
    assert r.status_code == 200
    # Each query carries the flag predicate of its own table, never the other's.
    raw = [sql for sql in fake_ch.flow_sql() if "flow_records" in sql]
    assert raw
    for sql in fake_ch.flow_sql():
        if "flow_qos_1h" in sql:
            assert "flags_known = 1" in sql, sql
    for sql in raw:
        assert "flags_known" not in sql and "bitTest(fields_present, 0)" in sql, sql


def test_mapping_writes_hide_out_of_scope_exporters(fake_ch):
    """Contract §6: mapping writes need netflow.manage (403 without it, for any
    exporter, so nothing is revealed); a scoped manager gets 404 for an
    exporter outside their scope, exactly as if it did not exist."""
    manager = replace(SCOPED, permissions=frozenset({"netflow.view", "netflow.manage"}))
    client = make_client(manager)
    r = client.put(URL + "/exporter-devices/10.9.0.2", json={"device_id": str(uuid4())})
    assert r.status_code == 404
    assert client.delete(URL + "/exporter-devices/10.9.0.2").status_code == 404
    viewer = make_client(SCOPED)
    for ip in ("10.9.0.1", "10.9.0.2"):
        assert viewer.put(URL + f"/exporter-devices/{ip}", json={"device_id": str(uuid4())}).status_code == 403
        assert viewer.delete(URL + f"/exporter-devices/{ip}").status_code == 403


def test_interface_names_follow_the_exporter_owner(monkeypatch):
    """X-46: names come from the device the exporter resolver picks (an
    explicit mapping beats a management-IP match), keyed as the caller gave them."""
    from app.api.v1.netflow.access import ExporterOwner

    mapped = uuid4()
    asked: list = []

    async def owners(db, ips):
        asked.extend(ips)
        return {"10.9.0.1": ExporterOwner(exporter="10.9.0.1", device_id=str(mapped), hostname="edge-1",
                                          source="mapped")}

    class DB:
        async def execute(self, statement, params=None):
            rows = [SimpleNamespace(device_id=mapped, if_index=70000, if_name="Gi0/0/1", if_descr=None,
                                    if_alias="WAN", if_speed=1_000_000_000)]
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(C, "resolve_exporter_owners", owners)
    out = asyncio.run(C._resolve_interface_names([("::ffff:10.9.0.1", 70000), ("10.9.0.2", 1)], DB()))
    assert sorted(asked) == ["10.9.0.2", "::ffff:10.9.0.1"]
    assert out == {("::ffff:10.9.0.1", 70000): {"if_name": "Gi0/0/1", "if_descr": None, "if_alias": "WAN",
                                                "if_speed": 1_000_000_000, "device_hostname": "edge-1"}}


# ── /retention and /coverage ──────────────────────────────────────────────────

def test_retention_endpoint(api, repair):
    body = api.get(URL + "/retention").json()
    assert body["raw_days"] == 30 and body["rollup_days"] == 90 and body["interface_days"] == 400
    assert body["exporter_minutes_days"] == 400 and body["earliest_raw"] == "2026-09-01T00:00:00.000Z"
    assert body["known"] is True
    # Nothing configured in Settings -> Storage: every table keeps its install TTL.
    assert body["configured"] == {"raw_days": None, "rollup_days": None, "qos_days": None,
                                  "interface_days": None, "exporter_minutes_days": None}
    assert body["sampling_repair"] == {**NO_REPAIR, "affected": False}


def test_retention_reports_storage_settings(api, monkeypatch):
    from app.services import storage_service

    async def settings(db):
        return {"categories": {"flows": 14, "rollups_1h": 180, "flow_history": 365},
                "table_overrides": {"flow_qos_1h": 60}, "auto_purge": {}}

    monkeypatch.setattr(storage_service, "get_retention_settings", settings)
    assert api.get(URL + "/retention").json()["configured"] == {
        "raw_days": 14, "rollup_days": 180, "qos_days": 60, "interface_days": 365, "exporter_minutes_days": 365,
    }


def test_storage_retention_categories_for_flow_tables():
    from app.services.storage_service import CATEGORIES, classify_table

    # Template definitions and NBAR names are state: never TTL'd or purged.
    assert classify_table("flow_templates") is None and classify_table("flow_app_names") is None
    # The 90/400-day NetFlow tables are not raw metrics (30 days).
    for table in ("flow_interface_5m", "flow_exporter_minutes", "flow_if_counters"):
        assert classify_table(table) == "flow_history", table
    assert CATEGORIES["flow_history"]["default_days"] == 400
    assert classify_table("flow_records") == "flows"
    assert classify_table("flow_collector_stats") == "raw_metrics"          # 30-day per-minute deltas
    assert classify_table("flow_conversations_1h") == "rollups_1h"
    assert classify_table("flow_traffic_5m") == "rollups_5m"
    assert classify_table("flow_interface_5m_mv") is None


def test_coverage_endpoint_shape_and_sql(api, fake_ch):
    def rows(sql, params):
        if "system." in sql:
            return default_responder(sql, params)
        import ipaddress
        ip = ipaddress.IPv6Address("::ffff:10.9.0.1")
        if "has_data" in sql:   # covered minutes: (with data, with receipts, running minute with data)
            return [(ip, 42, 44, 0)] if "GROUP BY e" in sql else [(45, 47, 0)]
        agg = (100, 40, 30, 5, 10, 64, 2, datetime(2026, 9, 30, 9, 0), datetime(2026, 9, 30, 9, 59))
        if "GROUP BY e" in sql:
            return [(ip, *agg)]
        return [(None, *agg)]

    fake_ch.responder = rows
    body = api.get(URL + "/coverage", params={"hours": 1, "protocol": "6", "envelope": 1}).json()
    data, meta = body["data"], body["meta"]
    exp = data["exporters"][0]
    assert exp["exporter"] == "10.9.0.1" and exp["records"] == 100
    assert (exp["minutes_with_data"], exp["minutes_with_receipts"]) == (42, 44)
    assert exp["flag_coverage_pct"] == 75.0 and exp["sampled_share"] == 0.1 and exp["max_sampling"] == 64
    minutes = data["window"]["minutes_in_window"]
    assert minutes in (59, 60) and data["retention"]["raw_days"] == 30
    assert exp["minutes_in_window"] == minutes and exp["coverage_pct"] == round(42 / minutes * 100, 1)
    totals = data["totals"]
    assert (totals["minutes_with_data"], totals["minutes_with_receipts"], totals["minutes_in_window"]) == (45, 47, minutes)
    assert meta["sampling"] == {"sampled_share": 0.1, "max_interval": 64}
    # Coverage describes exporters: traffic dimensions are not applied (and not claimed).
    assert meta["filters"] == {}
    sqls = [q for q in fake_ch.flow_sql() if "flow_exporter_minutes" in q]
    assert sqls and not any("protocol" in q for q in sqls)
    assert any("uniqExactIf(minute, has_data), uniqExact(minute)" in q and "records > 0 AS has_data" in q
               for q in sqls)


def test_coverage_without_delivery_data_is_unknown_not_zero(api, fake_ch):
    fake_ch.responder = lambda sql, params: TTL_ROWS if "system.tables" in sql else []
    body = api.get(URL + "/coverage", params={"envelope": 1}).json()
    totals = body["data"]["totals"]
    assert body["data"]["exporters"] == [] and totals["records"] is None
    assert totals["minutes_with_data"] is None and totals["minutes_in_window"] is None
    assert body["meta"]["warnings"][0]["code"] == "coverage_unavailable"


# ── Reverse DNS (X-50) ────────────────────────────────────────────────────────

def test_slow_reverse_lookups_are_pending_not_cached_as_nameless(monkeypatch):
    def fake_lookup(ip):
        if ip.startswith("10.1."):
            time.sleep(0.3)                  # dead reverse zone
        return None if ip.endswith(".99") else f"host-{ip}"

    monkeypatch.setattr(C, "_rdns_one", fake_lookup)
    monkeypatch.setattr(C, "_rdns_cache", {})
    ips = [f"10.1.0.{i}" for i in range(4)] + [f"10.2.0.{i}" for i in range(20)] + ["10.2.0.99"]
    first = C._resolve_hostnames(ips, deadline=0.1)
    assert all(first[ip] is C.RDNS_PENDING for ip in ips[:4])
    assert first["10.2.0.5"] == "host-10.2.0.5" and first["10.2.0.99"] is None
    time.sleep(0.5)
    second = C._resolve_hostnames(ips, deadline=0.1)
    assert second["10.1.0.2"] == "host-10.1.0.2"            # finished in the background, cached
    ttl_name = C._rdns_cache["10.2.0.5"][0] - time.monotonic()
    ttl_none = C._rdns_cache["10.2.0.99"][0] - time.monotonic()
    assert ttl_none < ttl_name and ttl_none <= C._RDNS_NEGATIVE_TTL
    from app.api.v1.netflow import breakdowns as B

    names = B._rdns(["10.1.0.3", "10.3.0.1"], 0.1)
    assert names["10.1.0.3"]["hostname"] == "host-10.1.0.3" and names["10.1.0.3"]["pending"] is False


# ── Background tasks ──────────────────────────────────────────────────────────

def test_start_netflow_tasks_starts_available_loops_and_supervises(monkeypatch):
    import types

    runs = {"n": 0}

    async def flaky_loop():
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("first run fails")
        await asyncio.sleep(3600)

    mod = types.ModuleType("nf_fake_loops")
    mod.flaky_loop = flaky_loop
    mod.not_async = lambda: None
    monkeypatch.setitem(sys.modules, "nf_fake_loops", mod)
    monkeypatch.setattr(netflow_tasks, "RESTART_BACKOFF_S", (0.01,))
    app = FastAPI()

    async def scenario():
        tasks = netflow_tasks.start_netflow_tasks(app, (
            ("nf_fake_loops", "flaky_loop"), ("nf_fake_loops", "not_async"), ("nf_fake_loops", "missing"),
            ("app.services.netflow_does_not_exist", "loop"),
        ))
        assert [t.get_name() for t in tasks] == ["netflow:nf_fake_loops.flaky_loop"]
        await asyncio.sleep(0.1)
        assert runs["n"] == 2 and not tasks[0].done()     # crashed once, restarted
        await netflow_tasks.stop_netflow_tasks(app)
        assert tasks[0].cancelled() and app.state.netflow_tasks == []

    asyncio.run(scenario())
    # The shutdown hook is registered once however often the app starts.
    asyncio.run(_restart_twice(app))
    assert len(app.router.on_shutdown) == 1


async def _restart_twice(app):
    netflow_tasks.start_netflow_tasks(app, ())
    netflow_tasks.start_netflow_tasks(app, ())


def test_registry_lists_the_contract_loops():
    assert ("app.services.netflow_rollup", "netflow_rollup_loop") in netflow_tasks.LOOPS
    assert len(netflow_tasks.LOOPS) == 6


# ── Scratch ClickHouse: real budget and error mapping ─────────────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402


@pytest.fixture
def real_ch(monkeypatch):
    import threading

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        # Like app.core.database.get_ch_client: one session per thread.
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    yield client
    C._retention_cache.update(value=None, at=0.0)


def test_clickhouse_enforces_the_budget(real_ch):
    started = time.monotonic()
    with pytest.raises(NetflowError) as exc:
        asyncio.run(C.run_query("SELECT sleep(3)", budget_s=1, label="budget"))
    assert time.monotonic() - started < 3
    assert exc.value.status_code == 504 and exc.value.code == "query_timeout"
    _clean(exc.value.body())


def test_real_clickhouse_error_is_sanitised(real_ch):
    with pytest.raises(NetflowError) as exc:
        asyncio.run(C.run_query("SELECT no_such_column FROM system.one", label="bad"))
    assert exc.value.status_code == 500
    _clean(exc.value.body())


def test_coverage_over_real_tables(real_ch, monkeypatch):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        rows = []
        for m in range(30):
            t = now - timedelta(minutes=m * 2 + 1)
            rows.append([t, t, "10.9.0.1", 6, 0x1B if m % 2 else 0, (1 << 31) | 1 if m % 2 else 1 << 31,
                         100, 1 if m < 20 else 10, 0])
        # Two minutes that carry only NSEL create events: received, but no
        # countable records (the counters apply the volume guard).
        for m in (61, 63):
            t = now - timedelta(minutes=m)
            rows.append([t, t, "10.9.0.1", 6, 0, (1 << 31) | (1 << 13), 100, 1, 1])
        real_ch.insert(f"{db}.flow_records", rows, column_names=[
            "timestamp", "received_at", "exporter_ip", "protocol", "tcp_flags", "fields_present", "bytes",
            "sampling_interval", "fw_event"])

        async def call():
            vis = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset())
            win = C.resolve_window(2, max_span=C.MAX_SPAN_ROLLUP)
            return await M.netflow_coverage(win=win, f=F.FlowFilter(), vis=vis)

        body = asyncio.run(call())
    exp = body["exporters"][0]
    assert exp["exporter"] == "10.9.0.1" and exp["records"] == 30 and exp["tcp_records"] == 30
    assert exp["tcp_flagged_records"] == 15 and exp["flag_coverage_pct"] == 50.0
    assert exp["sampled_records"] == 10 and exp["max_sampling"] == 10
    assert 28 <= exp["minutes_with_data"] <= 30
    assert exp["minutes_with_receipts"] == exp["minutes_with_data"] + 2
    # Tracking starts at the table's first row (an upgrade), not the window start.
    assert body["totals"]["records"] == 30 and 63 <= body["totals"]["minutes_in_window"] <= 64


# ── Live API (own uvicorn) ────────────────────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
BROKEN = os.environ.get("NETFLOW_TEST_API_CH_BROKEN") == "1"
live = pytest.mark.skipif(not LIVE or BROKEN, reason="set NETFLOW_TEST_API to a running API of this tree")
live_broken = pytest.mark.skipif(not LIVE or not BROKEN, reason="needs NETFLOW_TEST_API with a failing ClickHouse")


@pytest.fixture(scope="module")
def live_auth():
    token = os.environ.get("NETFLOW_TEST_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{LIVE}/api/v1/auth/login", json={
        "username": os.environ.get("NETFLOW_TEST_USER", "admin"),
        "password": os.environ.get("NETFLOW_TEST_PASS", "admin123"),
    }, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _live(path, auth, **params):
    return requests.get(f"{LIVE}/api/v1/netflow{path}", params=params, headers=auth, timeout=120)


@live
def test_live_custom_45_day_window(live_auth):
    now = datetime.now(timezone.utc)
    frm, to = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for path in ("/overview", "/timeseries", "/top-talkers", "/protocols", "/device-status"):
        r = _live(path, live_auth, hours=1080, **{"from": frm, "to": to})
        assert r.status_code == 200, (path, r.text)
    meta = _live("/overview", live_auth, **{"from": frm, "to": to, "envelope": 1}).json()["meta"]
    # NF-17: whole hours from the rollup, the partial newest hour from raw.
    assert meta["window"]["mode"] == "fixed" and meta["window"]["source"] == "hybrid"
    # The rollups keep 90 days, but the partial OLDEST hour could only come
    # from raw (30 days): it is left out and disclosed, never widened.
    eff = datetime.fromisoformat(meta["window"]["effective_from"].replace("Z", "+00:00"))
    start = datetime.fromisoformat(meta["window"]["from"].replace("Z", "+00:00"))
    if start.minute or start.second:
        assert meta["retention"]["limited"] is True and "raw_retention" in {w["code"] for w in meta["warnings"]}
        assert eff == start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        assert meta["retention"]["limited"] is False and eff == start
    # The heatmap reads the hourly rollup in whole-hour zones (raw edges only);
    # a half-hour zone still needs raw records and says it cannot cover all of it.
    meta = _live("/heatmap", live_auth, **{"from": frm, "to": to, "envelope": 1}).json()["meta"]
    assert meta["window"]["source"] in ("rollup", "hybrid")
    meta = _live("/heatmap", live_auth, **{"from": frm, "to": to, "envelope": 1, "tz": "Asia/Kolkata"}).json()["meta"]
    assert meta["window"]["source"] == "raw" and meta["retention"]["limited"] is True


@live
def test_live_invalid_values_are_400_strings(live_auth):
    for path, params in (("/forensics", {"dst_port": "70000"}), ("/forensics", {"dst_port": "abc"}),
                         ("/overview", {"port": "70000"}), ("/top-talkers", {"port": "-1"}),
                         ("/overview", {"app": "NoSuchApp"}), ("/overview", {"dscp": "64"}),
                         ("/overview", {"from": "0001-01-01T00:00:00Z", "to": "0002-01-01T00:00:00Z"})):
        r = _live(path, live_auth, **params)
        assert r.status_code == 400, (path, params, r.text)
        assert isinstance(r.json()["detail"], str) and r.json()["code"] in ("invalid_filter", "invalid_window")


@live
def test_live_legacy_shapes_without_envelope(live_auth):
    overview = _live("/overview", live_auth, hours=1).json()
    assert {"bytes", "flows", "exporters", "current_bps", "top_protocol"} <= set(overview)
    assert isinstance(_live("/top-talkers", live_auth, hours=1).json(), list)
    assert isinstance(_live("/forensics", live_auth, hours=1, limit=5).json(), list)
    assert isinstance(_live("/exporters", live_auth, hours=1).json(), list)
    assert _live("/device-status", live_auth, hours=1).json()["flow_derived"] is True
    assert "cells" in _live("/heatmap", live_auth, hours=24).json()
    csv = _live("/forensics", live_auth, hours=1, limit=5, format="csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert set(_live("/overview", live_auth, hours=1, envelope=1).json()) == {"data", "meta"}
    assert "exporters" in _live("/coverage", live_auth, hours=1).json()
    assert "raw_days" in _live("/retention", live_auth).json()


@live_broken
def test_live_clickhouse_failure_is_a_safe_500(live_auth):
    r = _live("/overview", live_auth, hours=1)
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "query_failed" and body["error_id"] in body["detail"]
    _clean(body)
