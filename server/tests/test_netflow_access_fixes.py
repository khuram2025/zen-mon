"""Access-security fixes from the NetFlow acceptance pass (wave C): NF-04, NF-31,
X-01, X-15 and the new defects found next to them.

Layers:
  * unit: scoped detection status (coverage, notes, coverage_pct over the
    visible exporters only), the bounded NBAR-names refresh (NF-31), /coverage
    on unreadable retention, saved-view readability for host filters, the
    streamed body limit and per-owner quota, strict filter objects (explain
    body, report scope, schedule scope), the TI download destination checks
    (SSRF), NetFlow alert redaction, report-schedule scope withholding, the
    device and link-utilization scope guards;
  * HTTP end to end (gated on ZENPLUS_NETFLOW_E2E_URL + ZENPLUS_NETFLOW_E2E_PG,
    the API's own database, and NETFLOW_TEST_CLICKHOUSE for SNMP marker rows):
    scoped users (team A, team B, a scope matching no exporter), a scoped
    operator, a role without netflow.view and the administrator exercise every
    reproduced leak: detector coverage/notes, exception match counts, shared
    views with host filters, the device/exporter claim, out-of-scope interface
    speed writes, link utilization, NetFlow alert payloads, report schedules,
    unknown filter keys, TI feed URLs, finding assignment, chunked saved-view
    bodies, the empty-scope warning. Fixtures are prefixed ``acsfix`` and
    removed afterwards.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.api.v1.netflow as netflow
from app.api.v1.netflow import access, anomalies as AN, common as C, meta as M, saved_views as SV
from app.api.v1.netflow.access import FlowVisibility
from app.api.v1.netflow.anomalies import FindingAction
from app.api.v1.netflow.common import NetflowError
from app.api.v1.netflow.filters import parse_flow_filter_strict, reject_unknown_filter_keys
from app.core.database import get_db
from app.core.security import get_current_user
from app.services import netflow_alert_service as nfa
from app.services import netflow_classify as NC
from app.services import netflow_detection as det
from app.services import netflow_report as NR
from app.services import netflow_ti as ti


def _vis(exporters=None, perms=("netflow.view",), scope=None, user_id=None):
    return FlowVisibility(
        restricted=exporters is not None,
        exporters=None if exporters is None else frozenset(exporters),
        user_id=user_id, permissions=frozenset(perms),
        scope=None if scope is None else frozenset(scope),
    )


# ── NF-04: detection status narrowed to the visible exporters ────────────────

A, B, V6 = "10.0.0.1", "10.0.0.2", "2001:db8::9"


def _state():
    cov = det.Coverage(exporters={
        A: det.ExporterCoverage(A, records=100, tcp_records=80, tcp_flagged=80, icmp_records=5, icmp_typed=5,
                                duration_valid=100),
        B: det.ExporterCoverage(B, records=900, tcp_records=500, tcp_flagged=0, icmp_records=9, icmp_typed=0,
                                duration_valid=0, max_sampling=100),
        V6: det.ExporterCoverage(V6, records=50, tcp_records=10, tcp_flagged=10, max_sampling=256),
    })
    # What run_engine_once stores: notes generated over the whole fleet.
    detectors = {}
    for k in det.KINDS:
        out = det.Outcome(kind=k, status="ok")
        det.annotate_coverage(k, cov, out)
        detectors[k] = out.as_dict(datetime(2026, 10, 1, 11, 57, tzinfo=timezone.utc))
    detectors["volume_spike"]["truncated"] = True
    return {
        "last_run_at": "2026-10-01T11:57:00Z", "last_bucket_end": "2026-10-01T11:55:00Z",
        "exporters": sorted(cov.exporters), "coverage": cov.as_dict(),
        "coverage_exporters": cov.exporter_rows(), "detectors": detectors,
    }


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_fleet_state_names_every_exporter_so_the_scope_matters():
    state = _state()
    blob = json.dumps(det.scoped_detectors(state, det.default_settings(), None, NOW))
    assert B in blob and V6 in blob                      # unrestricted callers see the fleet's notes
    assert det.engine_status(state, NOW)["coverage"]["sampled_exporters"] == sorted([B, V6])


def test_scoped_detectors_and_engine_coverage_mention_only_visible_exporters():
    state = _state()
    rows = det.scoped_detectors(state, det.default_settings(), frozenset({A}), NOW)
    blob = json.dumps(rows)
    assert B not in blob and V6 not in blob
    by = {d["kind"]: d for d in rows}
    # A exports TCP flags: SYN checks run, at 100 % flag coverage of A's TCP (not the fleet's 15 %).
    assert by["syn_scan"]["status"] == "ok" and by["syn_scan"]["coverage_pct"] == 100.0
    assert by["syn_scan"]["exporters_unavailable"] == [] and by["syn_scan"]["reduced_sensitivity"] == []
    assert not any("Reduced sensitivity" in n for n in by["syn_scan"]["notes"])
    # A run-wide cap is disclosed generically, without the run's own wording.
    assert det._TRUNCATED_NOTE in by["volume_spike"]["notes"]
    eng = det.engine_status(state, NOW, frozenset({A}))
    assert eng["coverage"]["exporters"] == 1 and eng["coverage"]["records"] == 100
    assert eng["coverage"]["sampled_exporters"] == [] and eng["coverage"]["flag_coverage_pct"] == 100.0


def test_scoped_detectors_for_the_flagless_exporter_say_unavailable_with_its_own_address():
    by = {d["kind"]: d for d in det.scoped_detectors(_state(), det.default_settings(), frozenset({B}), NOW)}
    assert by["syn_scan"]["status"] == "unavailable" and B in by["syn_scan"]["reason"] and A not in by["syn_scan"]["reason"]
    assert by["syn_scan"]["reduced_sensitivity"] == [] or by["syn_scan"]["reduced_sensitivity"][0]["exporter"] == B
    assert by["icmp_sweep"]["status"] == "unavailable" and V6 not in json.dumps(by)


def test_empty_scope_gets_no_figures_and_no_addresses():
    state = _state()
    rows = det.scoped_detectors(state, det.default_settings(), frozenset(), NOW)
    blob = json.dumps(rows)
    assert "10.0.0." not in blob and "2001:db8" not in blob
    assert {d["status"] for d in rows} <= {"no_coverage", "disabled"}
    eng = det.engine_status(state, NOW, frozenset())
    assert eng["coverage"]["exporters"] == 0 and eng["coverage"]["records"] == 0
    assert eng["coverage"]["sampled_exporters"] == []


def test_state_without_per_exporter_coverage_withholds_rather_than_leaks():
    state = _state()
    state.pop("coverage_exporters")
    rows = det.scoped_detectors(state, det.default_settings(), frozenset({A}), NOW)
    assert B not in json.dumps(rows) and V6 not in json.dumps(rows)
    assert all(d["coverage_pct"] is None and d["notes"] == [] for d in rows)
    assert det.engine_status(state, NOW, frozenset({A}))["coverage"] is None


def test_exception_match_count_is_scoped():
    sql, params = AN._exception_select(_vis({A}))
    assert "nf.exporter_ip = ANY(CAST(:x_vis AS inet[]))" in sql and params == {"x_vis": [A]}
    sql, params = AN._exception_select(_vis(set()))
    assert "AND FALSE" in sql
    sql, params = AN._exception_select(_vis(None))
    assert "x_vis" not in sql and params == {}


def test_finding_action_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        FindingAction(action="assign", assignee=str(uuid.uuid4()))
    assert FindingAction(action="assign", user_id=uuid.uuid4()).action == "assign"


# ── NF-31: bounded NBAR-names refresh, /coverage on unreadable retention ─────

@pytest.fixture
def names_env(monkeypatch):
    """A fresh classifier cache with a controllable NBAR-names reader."""
    monkeypatch.setattr(NC, "_names", NC._NamesRefresh())
    monkeypatch.setattr(NC, "NAMES_WAIT_S", 0.3)
    monkeypatch.setattr(NC, "NAMES_RETRY_S", 30.0)
    monkeypatch.setattr(NC, "_rules", NC.build_rules())
    monkeypatch.setattr(NC, "_loaded_at", 0.0)

    async def no_db_rules(db):
        return [], NC.DEFAULT_EPHEMERAL_MIN

    monkeypatch.setattr(NC, "_read_db_rules", no_db_rules)

    class _Own:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    import app.core.database as database
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _Own())
    return monkeypatch


def test_nbar_refresh_never_holds_a_request_past_its_wait(names_env):
    release = threading.Event()
    calls = []

    def stalled():
        calls.append(time.monotonic())
        release.wait(5)          # a ClickHouse that accepts and never answers
        return {7: ("late-app", "Business")}

    names_env.setattr(NC, "_read_app_names", stalled)

    async def run():
        t0 = time.monotonic()
        # Concurrent requests share one read (single flight) and give up together.
        rules = await asyncio.gather(*(NC.load_rules(force=True) for _ in range(5)))
        took = time.monotonic() - t0
        assert took < 1.5, took
        assert all(r.app_names == {} for r in rules)       # previous (built-in) names kept
        assert len(calls) == 1
        # A request after the wait does not wait again on the same stalled read.
        t1 = time.monotonic()
        NC.invalidate_rules_cache()
        await NC.load_rules()
        assert time.monotonic() - t1 < 0.2 and len(calls) == 1
        # The late answer is applied by the next refresh, without another read.
        release.set()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if NC._names.pending_apply:
                break
        assert (await NC.load_rules()).app_names == {7: ("late-app", "Business")}
        assert len(calls) == 1

    asyncio.run(run())


def test_nbar_refresh_backs_off_after_a_failure(names_env):
    calls = []

    def broken():
        calls.append(1)
        raise ConnectionError("clickhouse down")

    names_env.setattr(NC, "_read_app_names", broken)
    names_env.setattr(NC, "CACHE_TTL_S", 0.0)          # every call is a refresh attempt

    async def run():
        await NC.load_rules(force=True)
        await asyncio.sleep(0.05)
        for _ in range(5):
            await NC.load_rules()
        return len(calls)

    assert asyncio.run(run()) == 1                        # retried no sooner than NAMES_RETRY_S


def test_names_client_has_short_timeouts(monkeypatch):
    seen = {}

    def fake_get_client(**kw):
        seen.update(kw)
        return SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[(5, "x", "y")]))

    import clickhouse_connect
    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    monkeypatch.setattr(NC._names_local, "client", None, raising=False)
    assert NC._read_app_names() == {5: ("x", "y")}
    assert seen["send_receive_timeout"] <= 10 and seen["connect_timeout"] <= 5


def _coverage_client(vis):
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    class _DB:
        async def execute(self, *a, **k):
            return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None)

    async def fake_db():
        yield _DB()

    async def user():
        return SimpleNamespace(id=uuid.uuid4(), username="u", role="viewer", is_active=True, scope_tags=[])

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


def test_coverage_is_an_error_when_clickhouse_cannot_be_read(monkeypatch):
    async def unknown(*a, **k):
        return C._UNKNOWN_RETENTION

    monkeypatch.setattr(M, "get_retention", unknown)
    r = _coverage_client(_vis(None)).get("/api/v1/netflow/coverage", params={"hours": 1, "envelope": 1})
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "unavailable" and body["error_id"].startswith("nf-")
    assert "No exporter delivery data" not in json.dumps(body)


def test_query_read_timeout_follows_the_budget():
    from urllib3 import Timeout

    t = C._bounded_timeout(Timeout(connect=10, read=300), 25)
    assert t.connect_timeout == 10 and t.read_timeout == 25
    assert C._bounded_timeout(Timeout(connect=3, read=15), 305).read_timeout == 305   # exports get their budget
    assert C._bounded_timeout(None, 25) is None                                       # test doubles untouched
    client = SimpleNamespace(timeout=Timeout(connect=10, read=300))
    with C._read_timeout(client, 7):
        assert client.timeout.read_timeout == 7
    assert client.timeout.read_timeout == 300


def _in_request(coro_fn):
    """Run ``coro_fn(meta)`` with a request's QueryMeta set (as NetflowRoute does)."""
    async def run():
        meta = C.QueryMeta(route="/test")
        token = C._META.set(meta)
        try:
            return await coro_fn(meta)
        finally:
            C._META.reset(token)
    return asyncio.run(run())


def test_a_request_fails_fast_after_clickhouse_gave_no_answer(monkeypatch):
    calls = []

    class Hung:
        timeout = None

        def query(self, sql, parameters=None, settings=None):
            calls.append(sql)
            time.sleep(0.5)

        def command(self, *a, **k):
            return None

    monkeypatch.setattr(C, "get_ch_client", lambda: Hung())
    monkeypatch.setattr(C, "_BUDGET_GRACE_S", 0.05)

    async def three_queries(meta):
        codes = []
        started = time.monotonic()
        for i in range(3):
            try:
                await C.run_query(f"SELECT {i}", budget_s=0.1, label="q")
            except NetflowError as exc:
                codes.append((exc.status_code, exc.code))
        return codes, time.monotonic() - started, meta.ch_unanswered

    codes, elapsed, flagged = _in_request(three_queries)
    assert codes == [(504, "query_timeout"), (503, "unavailable"), (503, "unavailable")]
    assert len(calls) == 1 and flagged and elapsed < 0.6           # one budget, not three


def test_a_refused_connection_also_fails_the_rest_of_the_request_fast(monkeypatch):
    from clickhouse_connect.driver.exceptions import OperationalError

    calls = []

    def refused():
        calls.append(1)
        raise OperationalError("Error HTTPConnectionPool(host='127.0.0.1', port=18123): Read timed out")

    monkeypatch.setattr(C, "get_ch_client", refused)

    async def two(meta):
        out = []
        for _ in range(2):
            with pytest.raises(NetflowError) as exc:
                await C.run_query("SELECT 1", label="q")
            out.append(exc.value.code)
        return out

    assert _in_request(two) == ["unavailable", "unavailable"] and len(calls) == 1


def test_requests_waiting_on_a_failed_retention_lookup_fail_fast(monkeypatch):
    release = threading.Event()

    def hung_retention():
        release.wait(2)
        raise TimeoutError("no answer")

    monkeypatch.setattr(C, "_retention_sync", hung_retention)
    C._retention_cache.update(value=None, at=0.0)

    async def run():
        async def one():
            meta = C.QueryMeta(route="/r")
            token = C._META.set(meta)
            try:
                r = await C.get_retention()
                return r.known, meta.ch_unanswered
            finally:
                C._META.reset(token)

        task1 = asyncio.ensure_future(one())
        await asyncio.sleep(0.05)
        task2 = asyncio.ensure_future(one())          # waits on the lookup task1 started
        await asyncio.sleep(0.05)
        release.set()
        return await task1, await task2

    try:
        (k1, f1), (k2, f2) = asyncio.run(run())
        assert (k1, f1) == (False, True) and (k2, f2) == (False, True)
        # A later, unrelated request is not flagged by a past failure.
        later = _in_request(lambda meta: _flag_after(meta))
        assert later is False
    finally:
        C._retention_cache.update(value=None, at=0.0)


async def _flag_after(meta):
    await C.get_retention()
    return meta.ch_unanswered


# ── NF-04 / new: saved views ─────────────────────────────────────────────────

OWNER_B, READER = uuid.uuid4(), uuid.uuid4()


def _view(flt, visibility="shared", owner=OWNER_B):
    return SimpleNamespace(id=uuid.uuid4(), name="v", description=None, kind="forensics", visibility=visibility,
                           pinned=False, owner_id=owner, updated_by=owner, query_version=2,
                           query={"v": 2, "kind": "forensics", "filter": flt, "window": {"mode": "relative", "range": "1h"}},
                           created_at=None, updated_at=None)


def test_shared_views_naming_hosts_follow_the_owner_scope():
    view_a = _vis({"127.0.0.91"}, scope={"team-a"}, user_id=str(READER))
    host_view = _view({"talker": "172.31.92.10"})
    # The owner (team B) sees more than the reader: hidden, whoever the owner is.
    assert not SV.can_read(host_view, view_a, owner_scope=frozenset({"team-b"}))
    assert not SV.can_read(host_view, view_a, owner_scope=None)           # unrestricted owner
    assert not SV.can_read(host_view, _vis(set(), scope={"nomatch"}), owner_scope=frozenset({"team-b"}))
    # A teammate's (owner scope within the reader's) host view is shared.
    assert SV.can_read(host_view, view_a, owner_scope=frozenset({"team-a"}))
    # Pinned to an exporter the reader sees: readable; to another one: not.
    assert SV.can_read(_view({"talker": "172.31.91.10", "exporter": "127.0.0.91"}), view_a)
    assert not SV.can_read(_view({"talker": "172.31.92.10", "exporter": "127.0.0.92"}), view_a,
                           owner_scope=frozenset({"team-a"}))
    # Views without hosts keep the exporter-only rule; unrestricted readers see everything shared.
    assert SV.can_read(_view({"app": "HTTPS"}), view_a)
    assert SV.can_read(host_view, _vis(None))
    assert SV.query_names_hosts({"filter": {"not_dst": "10.0.0.0/8"}})


def test_readable_sql_carries_the_same_rule():
    params = {"sv_admin_roles": ["admin"]}
    sql = SV._readable_sql(_vis({"127.0.0.91"}, scope={"team-a"}), params, alias="nsv")
    assert "nsv.visibility = 'shared'" in sql and "jsonb_each(" in sql and "ou.scope_tags" in sql
    assert params["sv_vis"] == ["127.0.0.91"] and params["sv_scope"] == ["team-a"]
    assert set(params["sv_hosts"]) == set(SV.HOST_KEYS)
    assert " v." not in sql                                               # alias honoured everywhere


def _bounded_client():
    router = APIRouter(route_class=SV.BoundedBodyRoute)
    parsed = []

    @router.post("/echo")
    async def echo(request: Request):
        body = await request.json()
        parsed.append(len(json.dumps(body)))
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), parsed


def test_saved_view_body_limit_holds_for_chunked_bodies():
    client, parsed = _bounded_client()
    big = json.dumps({"x": "a" * (SV.MAX_BODY_BYTES * 3)}).encode()

    def chunks():
        for i in range(0, len(big), 4096):
            yield big[i:i + 4096]

    r = client.post("/echo", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and parsed == []                          # never parsed
    r = client.post("/echo", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and parsed == []
    r = client.post("/echo", json={"x": "small"})
    assert r.status_code == 200 and parsed


def test_saved_view_owner_quota(monkeypatch):
    class _DB:
        def __init__(self, n):
            self.n = n

        async def execute(self, statement, params=None):
            return SimpleNamespace(scalar=lambda: self.n)

    user = SimpleNamespace(id=uuid.uuid4())
    asyncio.run(SV._check_owner_quota(_DB(SV.MAX_VIEWS_PER_OWNER - 1), user))
    with pytest.raises(Exception) as full:
        asyncio.run(SV._check_owner_quota(_DB(SV.MAX_VIEWS_PER_OWNER), user))
    assert getattr(full.value, "status_code", None) == 409


# ── Unknown filter keys (explain body, report scope, schedule scope) ─────────

def test_unknown_filter_keys_are_400_and_named():
    with pytest.raises(NetflowError) as e:
        reject_unknown_filter_keys(["talker", "talkr"])
    assert e.value.status_code == 400 and "talkr" in e.value.detail and e.value.fields == {"talkr": "Unknown filter key"}
    reject_unknown_filter_keys(["proto", "dscp_eq", "exporter"])            # read aliases are filter keys
    assert parse_flow_filter_strict({"dst_ports": [80, 443], "protocol": 6}).canonical() == {
        "protocol": "6", "dst_ports": "80,443"}
    with pytest.raises(NetflowError):
        parse_flow_filter_strict({"talker": True})


def test_report_scope_typo_is_rejected_not_widened():
    assert NR.scope_from_query("talker=172.31.91.10") == {"talker": "172.31.91.10"}
    with pytest.raises(NetflowError) as e:
        NR.scope_from_query("talkr=172.31.91.10")
    assert "talkr" in e.value.detail
    with pytest.raises(NetflowError) as e:
        NR.scope_from_mapping({"exporter": "not-an-ip'; drop", "evil": 1})
    assert e.value.fields == {"filters.netflow.evil": "Unknown filter key"}
    with pytest.raises(NetflowError):
        NR.scope_from_mapping({"exporter": "not-an-ip'; drop"})
    with pytest.raises(NetflowError):
        NR.scope_from_mapping("exporter=1.2.3.4")
    assert NR.scope_from_mapping({}) == {}


def test_report_schedule_scope_is_withheld_from_callers_who_cannot_see_it():
    from app.api.v1.report_schedules import _netflow_scope_visible as visible

    scope = {"exporter": "127.0.0.92", "talker": "172.31.92.10"}
    assert visible(scope, _vis(None))
    assert not visible(scope, _vis(None, perms=("alerts.view",)))          # no netflow.view
    assert not visible(scope, _vis({"127.0.0.91"}))                         # other team's exporter
    assert visible(scope, _vis({"127.0.0.92"}))
    assert not visible({"talker": "172.31.92.10"}, _vis({"127.0.0.92"}))    # unpinned host filter
    assert visible({"app": "HTTPS"}, _vis({"127.0.0.92"}))


# ── TI feed downloads: public destinations only (SSRF) ───────────────────────

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "240.0.0.1",
])
def test_internal_destinations_are_refused(addr, monkeypatch):
    monkeypatch.delenv(ti.FETCH_ALLOW_ENV, raising=False)
    with pytest.raises(ti.FetchRefused):
        ti.check_destination(addr)


def test_public_and_explicitly_allowed_destinations_pass(monkeypatch):
    monkeypatch.delenv(ti.FETCH_ALLOW_ENV, raising=False)
    ti.check_destination("8.8.8.8")
    ti.check_destination("2606:4700::1111")
    monkeypatch.setenv(ti.FETCH_ALLOW_ENV, "10.20.0.0/16, bogus")
    ti.check_destination("10.20.3.4")
    with pytest.raises(ti.FetchRefused):
        ti.check_destination("10.21.0.1")


def test_feed_url_literals_are_checked_at_create(monkeypatch):
    monkeypatch.delenv(ti.FETCH_ALLOW_ENV, raising=False)
    for url in ("http://127.0.0.1:18091/health", "http://[::1]:8120/", "http://169.254.169.254/latest/meta-data",
                "http://localhost:55441/"):
        with pytest.raises(ti.FetchRefused):
            ti.check_url_literal(url)
    ti.check_url_literal("https://feeds.example.org/list.txt")     # host names are checked at fetch


class _Redirect(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()

    def log_message(self, *a):
        pass


def _serve(handler, host):
    srv = HTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_fetch_refuses_internal_targets_and_redirects_into_them(monkeypatch):
    import ipaddress

    monkeypatch.delenv(ti.FETCH_ALLOW_ENV, raising=False)
    with pytest.raises(ti.FetchRefused):
        asyncio.run(ti.fetch_url("http://127.0.0.1:18091/health"))
    # A server the operator allowed (127.0.0.2) that redirects into loopback.
    monkeypatch.setattr(ti, "_allowed_networks", lambda: [ipaddress.ip_network("127.0.0.2/32")])
    _Redirect.target = "http://127.0.0.1:1/internal"
    srv = _serve(_Redirect, "127.0.0.2")
    try:
        with pytest.raises(ti.FetchRefused):
            asyncio.run(ti.fetch_url(f"http://127.0.0.2:{srv.server_address[1]}/feed"))
    finally:
        srv.shutdown()


def test_fetch_failures_do_not_tell_internal_services_apart(monkeypatch):
    import ipaddress

    monkeypatch.setattr(ti, "_allowed_networks", lambda: [ipaddress.ip_network("127.0.0.2/32")])
    s = socket.socket()
    s.bind(("127.0.0.2", 0))
    port = s.getsockname()[1]
    s.close()                                                         # closed port: connection refused
    with pytest.raises(ValueError) as refused:
        asyncio.run(ti.fetch_url(f"http://127.0.0.2:{port}/x"))
    assert str(refused.value) == ti.UNREACHABLE
    hung = socket.socket()
    hung.bind(("127.0.0.2", 0))
    hung.listen(1)                                                    # accepts, then hangs up

    def accept_and_close():
        conn, _ = hung.accept()
        conn.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    with pytest.raises(ValueError) as reset:
        asyncio.run(ti.fetch_url(f"http://127.0.0.2:{hung.getsockname()[1]}/x"))
    hung.close()
    assert str(reset.value) == ti.UNREACHABLE


# ── X-01: NetFlow alerts without netflow.view ────────────────────────────────

NF_META = {
    "source": "netflow", "rule_id": "r1", "metric": "netflow.traffic_bps", "threshold": 1e6, "operator": ">",
    "condition": "above 1 Mbps", "value": 40.4e6, "reading": "40.4 Mbps", "exporter": "127.0.0.92",
    "subject_label": "exporter 127.0.0.92", "netflow_subject": "127.0.0.92",
    "detail": {"bytes": 1, "flows": 2, "exporters_receiving": 16},
    "snapshot": {"talkers": [{"src": "172.31.92.10", "dst": "8.8.8.8", "bytes": 5, "dst_port": 443}]},
    "flow_scope": {"talker": "172.31.92.10"}, "flow_scope_label": "host 172.31.92.10",
    "netflow_link": "/netflow?talker=172.31.92.10", "window": {"from": "a", "to": "b"},
}


def test_alert_payload_redaction_keeps_triage_fields_only():
    payload = {"id": "x", "message": "Flow traffic for exporter 127.0.0.92 is 40.4 Mbps", "metadata": dict(NF_META)}
    out = nfa.redact_alert_payload(payload)
    blob = json.dumps(out)
    for secret in ("172.31.92.10", "127.0.0.92", "40.4", "talkers", "exporters_receiving"):
        assert secret not in blob, secret
    assert out["message"] == nfa.REDACTED_MESSAGE and out["metadata"]["netflow_redacted"] is True
    assert out["metadata"]["rule_id"] == "r1" and out["metadata"]["condition"] == "above 1 Mbps"
    other = {"id": "y", "message": "CPU high", "metadata": {"source": "snmp", "value": 99}}
    assert nfa.redact_alert_payload(other) is other


def test_alert_search_cannot_probe_withheld_netflow_messages():
    from app.services.alert_service import AlertFilters

    w, _p = AlertFilters(search="172.31.92.10", hide_netflow_text=True).where()
    assert any("a.metadata->>'source', '') <> 'netflow'" in c for c in w)
    w, _p = AlertFilters(search="172.31.92.10").where()
    assert not any("<> 'netflow'" in c for c in w)


# ── Devices, link utilization, flow-exporter counts ──────────────────────────

def test_every_device_id_route_is_scoped():
    from app.api.v1 import devices

    for route in devices.router.routes:
        if "{device_id}" not in route.path:
            continue
        inline = route.endpoint.__name__ in ("get_device", "get_device_status_explanation")
        deps = [d.dependency for d in route.dependencies]
        assert inline or devices._device_in_scope in deps, route.path


def test_flow_exporter_count_ignores_management_ips_that_never_exported():
    vis = _vis({"10.99.91.1", "127.0.0.91", "127.0.0.93"})
    assert access.flow_exporter_count(vis, frozenset({"127.0.0.91", "127.0.0.93", "127.0.0.62"})) == 2
    assert access.flow_exporter_count(vis, None) is None
    assert access.flow_exporter_count(_vis(None), frozenset({"127.0.0.91"})) is None
    assert access.flow_exporter_count(_vis(set()), frozenset({"127.0.0.91"})) == 0


# ── HTTP end to end ──────────────────────────────────────────────────────────

E2E_URL = os.environ.get("ZENPLUS_NETFLOW_E2E_URL")
E2E_PG = os.environ.get("ZENPLUS_NETFLOW_E2E_PG")
CH_URL = os.environ.get("NETFLOW_TEST_CLICKHOUSE")
needs_e2e = pytest.mark.skipif(not (E2E_URL and E2E_PG),
                               reason="set ZENPLUS_NETFLOW_E2E_URL and ZENPLUS_NETFLOW_E2E_PG")

PASS = "AcsFix-Pass-2026!"
TEAM_A, TEAM_B = "acsfix-team-a", "acsfix-team-b"
EXP_A, EXP_B, EXP_UNOWNED = "127.0.0.91", "127.0.0.92", "127.0.0.94"   # flows from the acceptance markers
DEV_A, DEV_B = uuid.UUID("acf10000-0000-4000-8000-0000000000a1"), uuid.UUID("acf10000-0000-4000-8000-0000000000b1")
IF_B = 182001
USERS = {
    "acsfix_view_a": ("viewer", [TEAM_A]), "acsfix_view_a2": ("viewer", [TEAM_A]),
    "acsfix_view_b": ("viewer", [TEAM_B]), "acsfix_view_b2": ("viewer", [TEAM_B]),
    "acsfix_view_none": ("viewer", ["acsfix-nomatch"]), "acsfix_op_a": ("operator", [TEAM_A]),
    "acsfix_nonf": ("acsfix_no_netflow", []),
}
ALERT_ID = uuid.UUID("acf10000-0000-4000-8000-00000000a1e7")
FIND_A, FIND_B = uuid.UUID("acf10000-0000-4000-8000-0000000f0a01"), uuid.UUID("acf10000-0000-4000-8000-0000000f0b01")
EXC_ID = uuid.UUID("acf10000-0000-4000-8000-0000000e0c01")


def _pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(E2E_PG)
        out = []
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    res = await conn.execute(text(sql), params)
                    out.append(res.fetchall() if res.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(run())


def _cleanup():
    names = list(USERS)
    return [
        ("DELETE FROM report_schedules WHERE name LIKE 'acsfix %'", {}),
        ("DELETE FROM netflow_saved_views WHERE name LIKE 'acsfix %'", {}),
        ("DELETE FROM netflow_ti_feeds WHERE name LIKE 'acsfix %'", {}),
        ("DELETE FROM alerts WHERE id = :a", {"a": ALERT_ID}),
        ("DELETE FROM netflow_findings WHERE id = ANY(:f)", {"f": [FIND_A, FIND_B]}),
        ("DELETE FROM netflow_detector_exceptions WHERE id = :x", {"x": EXC_ID}),
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = ANY(:e)", {"e": [EXP_A, EXP_B]}),
        ("DELETE FROM device_interfaces WHERE device_id = ANY(:d)", {"d": [DEV_A, DEV_B]}),
        ("DELETE FROM devices WHERE id = ANY(:d) OR hostname LIKE 'acsfix-%'", {"d": [DEV_A, DEV_B]}),
        ("DELETE FROM report_runs WHERE title LIKE 'acsfix %'", {}),
        ("DELETE FROM audit_logs WHERE actor_id IN (SELECT id FROM users WHERE username = ANY(:u))", {"u": names}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
        ("DELETE FROM roles WHERE name = 'acsfix_no_netflow'", {}),
    ]


def _ch(sql, data=None):
    import requests

    r = requests.post(CH_URL, params={"query": sql}, data=data, timeout=30)
    r.raise_for_status()
    return r.text


@pytest.fixture(scope="module")
def world():
    import requests

    from app.core.security import hash_password

    _pg(_cleanup())
    now = datetime.now(timezone.utc)
    _pg([
        ("INSERT INTO roles (name, display_name, permissions) VALUES ('acsfix_no_netflow', 'acsfix no NetFlow', "
         "'[\"dashboard.view\", \"devices.view\", \"alerts.view\", \"reports.view\"]'::jsonb)", {}),
        *[("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES (:u, :m, :h, :r, CAST(:s AS jsonb))",
           {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": r, "s": json.dumps(s)})
          for u, (r, s) in USERS.items()],
        ("INSERT INTO devices (id, hostname, ip_address, tags) VALUES (:a, 'acsfix-dev-a', CAST('10.254.91.1' AS inet), "
         "CAST(:ta AS jsonb)), (:b, 'acsfix-dev-b', CAST('10.254.92.1' AS inet), CAST(:tb AS jsonb))",
         {"a": DEV_A, "b": DEV_B, "ta": json.dumps([TEAM_A]), "tb": json.dumps([TEAM_B])}),
        ("INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:ea AS inet), :a), (CAST(:eb AS inet), :b)",
         {"ea": EXP_A, "eb": EXP_B, "a": DEV_A, "b": DEV_B}),
        ("INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed, monitored) VALUES "
         "(:a, 91001, 'acsfix-a-Gi91001', 1000000000, TRUE), (:b, :ib, 'acsfix-b-SECRET-Gi92001', 1000000000, TRUE)",
         {"a": DEV_A, "b": DEV_B, "ib": IF_B}),
        ("INSERT INTO alerts (id, device_id, status, severity, message, triggered_at, metadata) VALUES "
         "(:id, :d, 'active', 'warning', 'Flow traffic for exporter 127.0.0.92 is 40.4 Mbps averaged over 5 min, above 1 Mbps.', "
         "NOW(), CAST(:m AS jsonb))", {"id": ALERT_ID, "d": DEV_B, "m": json.dumps(NF_META)}),
        ("INSERT INTO netflow_detector_exceptions (id, kind, src_cidr, reason) VALUES (:x, 'port_scan', '172.31.0.0/16', 'acsfix')",
         {"x": EXC_ID}),
        *[("INSERT INTO netflow_findings (id, fingerprint, kind, severity, confidence, status, title, description, src_ip, "
           "exporter_ip, first_seen, last_seen, metrics) VALUES (:id, :fp, 'port_scan', 'warning', 'medium', 'open', "
           "'acsfix scan', 'acsfix', CAST(:src AS inet), CAST(:e AS inet), :t, :t, CAST(:m AS jsonb))",
           {"id": fid, "fp": f"acsfix-{fid.hex}", "src": src, "e": exp, "t": now - timedelta(minutes=10),
            "m": json.dumps({"exception_id": str(EXC_ID)})})
          for fid, src, exp in ((FIND_A, "172.31.91.10", EXP_A), (FIND_B, "172.31.92.10", EXP_B))],
    ])
    if CH_URL:
        rows = []
        for minutes in range(1, 30, 5):
            ts = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
            for dev, idx in ((DEV_A, 91001), (DEV_B, IF_B)):
                rows.append(json.dumps({"device_id": str(dev), "if_index": idx, "timestamp": ts, "in_octets": 1,
                                        "out_octets": 1, "oper_status": 1, "in_bps": 250e6, "out_bps": 1e6,
                                        "poller_id": "acsfix-marker"}))
        _ch("INSERT INTO zenplus.snmp_if_metrics FORMAT JSONEachRow", "\n".join(rows))
    time.sleep(access.CACHE_TTL_S + 1)            # the API's ownership cache sees the direct-PG seed
    tokens = {}
    for name in ["admin", *USERS]:
        pw = "admin123" if name == "admin" else PASS
        for _ in range(6):
            r = requests.post(f"{E2E_URL}/api/v1/auth/login", json={"username": name, "password": pw}, timeout=10)
            if r.status_code != 429:
                break
            time.sleep(10)
        r.raise_for_status()
        tokens[name] = r.json()["access_token"]
    yield tokens
    _pg(_cleanup())
    if CH_URL:
        _ch("ALTER TABLE zenplus.snmp_if_metrics DELETE WHERE poller_id = 'acsfix-marker'")


def _call(tokens, user, method, path, **kw):
    import requests

    r = requests.request(method, f"{E2E_URL}/api/v1{path}", headers={"Authorization": f"Bearer {tokens[user]}"},
                         timeout=60, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def _mentions(body, *needles) -> list[str]:
    blob = json.dumps(body)
    return [n for n in needles if n in blob]


@needs_e2e
def test_e2e_detection_status_is_scoped(world):
    for user in ("acsfix_view_none", "acsfix_view_a"):
        for path in ("/netflow/detectors", "/netflow/findings?hours=24"):
            status, body = _call(world, user, "GET", path)
            assert status == 200, (user, path, body)
            blob = json.dumps({"engine": body.get("engine"), "detectors": body.get("detectors")})
            others = [ip for ip in ("127.0.0.6", "127.0.0.7", "::1") if ip in blob]
            assert not others, (user, path, others)
        status, body = _call(world, user, "GET", "/netflow/detectors")
        cov = body["engine"]["coverage"]
        assert cov is None or (cov["exporters"] <= 1 and set(cov["sampled_exporters"]) <= {EXP_A})
    _, admin = _call(world, "admin", "GET", "/netflow/detectors")
    assert admin["engine"]["coverage"] is None or admin["engine"]["coverage"]["exporters"] >= 1


@needs_e2e
def test_e2e_exception_matches_count_visible_findings_only(world):
    def matches(user):
        _, body = _call(world, user, "GET", "/netflow/detector-exceptions")
        return next(i["matches"] for i in body["items"] if i["id"] == str(EXC_ID))

    assert matches("admin") == 2 and matches("acsfix_view_a") == 1 and matches("acsfix_view_none") == 0


@needs_e2e
def test_e2e_findings_assignment_respects_visibility(world):
    view_b = _pg([("SELECT id::text FROM users WHERE username = 'acsfix_view_b'", {})])[0][0][0]
    nonf = _pg([("SELECT id::text FROM users WHERE username = 'acsfix_nonf'", {})])[0][0][0]
    a2 = _pg([("SELECT id::text FROM users WHERE username = 'acsfix_view_a2'", {})])[0][0][0]
    for target in (view_b, nonf):
        status, body = _call(world, "acsfix_op_a", "POST", f"/netflow/findings/{FIND_A}/actions",
                             json={"action": "assign", "user_id": target})
        assert status == 400, body
    status, body = _call(world, "acsfix_op_a", "POST", f"/netflow/findings/{FIND_A}/actions",
                         json={"action": "assign", "assignee": a2})
    assert status == 400                                               # unknown key, not a silent un-assign
    status, body = _call(world, "acsfix_op_a", "POST", f"/netflow/findings/{FIND_A}/actions",
                         json={"action": "assign", "user_id": a2})
    assert status == 200 and body["assigned_to"]["id"] == a2
    status, people = _call(world, "acsfix_view_a", "GET", f"/netflow/findings-assignees?finding={FIND_A}")
    names = {p["username"] for p in people}
    assert {"acsfix_view_a", "acsfix_view_a2", "acsfix_op_a", "admin"} <= names
    assert not names & {"acsfix_view_b", "acsfix_view_b2", "acsfix_nonf", "acsfix_view_none"}
    status, people = _call(world, "acsfix_view_a", "GET", "/netflow/findings-assignees")
    assert not {p["username"] for p in people} & {"acsfix_view_b", "acsfix_nonf"}


@needs_e2e
def test_e2e_shared_views_with_host_filters_stay_in_their_scope(world):
    status, view = _call(world, "acsfix_view_b", "POST", "/netflow/saved-views", json={
        "name": "acsfix team-b host", "visibility": "shared",
        "query": {"v": 2, "kind": "forensics", "filter": {"talker": "172.31.92.10"},
                  "window": {"mode": "relative", "range": "1h"}}})
    assert status in (200, 201), view
    vid = view["id"]
    for user in ("acsfix_view_a", "acsfix_view_none"):
        _, listing = _call(world, user, "GET", "/netflow/saved-views?limit=200")
        assert vid not in {v["id"] for v in listing} and not _mentions(listing, "172.31.92.10"), user
        assert _call(world, user, "GET", f"/netflow/saved-views/{vid}")[0] == 404
    for user in ("acsfix_view_b2", "admin"):
        _, listing = _call(world, user, "GET", "/netflow/saved-views?limit=200")
        assert vid in {v["id"] for v in listing}, user
    # Pinned to the reader's exporter: shared across the team.
    status, pinned = _call(world, "acsfix_view_a", "POST", "/netflow/saved-views", json={
        "name": "acsfix team-a pinned", "visibility": "shared",
        "query": {"v": 2, "kind": "forensics", "filter": {"talker": "172.31.91.10", "exporter": EXP_A},
                  "window": {"mode": "relative", "range": "1h"}}})
    assert _call(world, "acsfix_view_a2", "GET", f"/netflow/saved-views/{pinned['id']}")[0] == 200
    assert _call(world, "acsfix_view_b", "GET", f"/netflow/saved-views/{pinned['id']}")[0] == 404


@needs_e2e
def test_e2e_saved_view_chunked_body_is_refused(world):
    import requests

    big = json.dumps({"name": "acsfix big", "query": {"v": 2, "x": "a" * 200_000}}).encode()

    def chunks():
        for i in range(0, len(big), 8192):
            yield big[i:i + 8192]

    r = requests.post(f"{E2E_URL}/api/v1/netflow/saved-views", data=chunks(), timeout=30,
                      headers={"Authorization": f"Bearer {world['acsfix_view_b']}", "Content-Type": "application/json"})
    assert r.status_code == 413


@needs_e2e
def test_e2e_scoped_operator_cannot_claim_exporters_or_push_devices_into_other_scopes(world):
    for body in (
        {"hostname": "acsfix-claim-94", "ip_address": EXP_UNOWNED, "tags": [TEAM_A]},     # unowned exporter
        {"hostname": "acsfix-claim-b", "ip_address": "10.254.199.7", "tags": [TEAM_B]},   # other team's tag
        {"hostname": "acsfix-hidden", "ip_address": "10.254.199.8", "tags": ["acsfix-misc"]},  # no own scope tag
    ):
        status, resp = _call(world, "acsfix_op_a", "POST", "/devices", json=body)
        assert status == 403, (body, resp)
    status, created = _call(world, "acsfix_op_a", "POST", "/devices",
                            json={"hostname": "acsfix-own", "ip_address": "10.254.199.9", "tags": [TEAM_A, "acsfix-site"]})
    assert status == 201, created
    # Re-addressing an own device onto the unowned exporter is the same claim.
    status, resp = _call(world, "acsfix_op_a", "PUT", f"/devices/{created['id']}", json={"ip_address": EXP_UNOWNED})
    assert status == 403, resp
    _, exporters = _call(world, "acsfix_view_a", "GET", "/netflow/exporters?hours=24")
    assert EXP_UNOWNED not in json.dumps(exporters)
    # Another team's device is not found, for reads and writes alike.
    for method, path, body in (
        ("PUT", f"/devices/{DEV_B}", {"tags": [TEAM_A]}),
        ("PATCH", f"/devices/{DEV_B}/interfaces/{IF_B}", {"configured_speed_bps": 5}),
        ("POST", f"/devices/{DEV_B}/interfaces/bulk-speed", {"if_indexes": [IF_B], "configured_speed_bps": 5}),
        ("GET", f"/devices/{DEV_B}/interfaces", None),
        ("DELETE", f"/devices/{DEV_B}", None),
    ):
        status, resp = _call(world, "acsfix_op_a", method, path, json=body)
        assert status == 404, (method, path, resp)
    assert _call(world, "acsfix_op_a", "POST", "/devices/bulk-tag",
                 json={"device_ids": [str(DEV_B)], "add": [TEAM_A]})[0] == 404
    speed = _pg([("SELECT configured_speed_bps FROM device_interfaces WHERE device_id = :d AND if_index = :i",
                  {"d": DEV_B, "i": IF_B})])[0][0][0]
    assert speed is None
    # Admin is not limited.
    status, resp = _call(world, "admin", "PATCH", f"/devices/{DEV_B}/interfaces/{IF_B}", json={"configured_speed_bps": None})
    assert status == 200


@needs_e2e
def test_e2e_link_utilization_is_scoped(world):
    status, body = _call(world, "acsfix_view_a", "GET", "/link-utilization?hours=2&search=acsfix")
    assert status == 200
    assert not _mentions(body, "acsfix-dev-b", "SECRET", str(DEV_B))
    if CH_URL:
        assert any(i["device_id"] == str(DEV_A) for i in body["items"])
    assert _call(world, "acsfix_view_a", "GET", f"/link-utilization/{DEV_B}/{IF_B}")[0] == 404
    assert _call(world, "acsfix_view_a", "PUT", f"/link-utilization/favorites/{DEV_B}/{IF_B}")[0] == 404
    status, detail = _call(world, "admin", "GET", f"/link-utilization/{DEV_B}/{IF_B}")
    assert status == 200 and detail["hostname"] == "acsfix-dev-b"
    status, own = _call(world, "acsfix_view_a", "GET", f"/link-utilization/{DEV_A}/91001")
    assert status == 200
    # Only exporters that sent flows are listed (the device's management IP never exported).
    assert own["netflow"]["exporters"] == [EXP_A]


@needs_e2e
def test_e2e_netflow_alerts_are_redacted_without_netflow_view(world):
    status, body = _call(world, "acsfix_nonf", "GET", f"/alerts/{ALERT_ID}")
    assert status == 200, body
    assert not _mentions(body, "172.31.92.10", "40.4", "talkers", "exporters_receiving", "127.0.0.92")
    assert body["message"] == nfa.REDACTED_MESSAGE
    _, listing = _call(world, "acsfix_nonf", "GET", "/alerts?view=all&search=172.31.92.10")
    assert str(ALERT_ID) not in {a["id"] for a in listing["data"]}
    _, listing = _call(world, "acsfix_nonf", "GET", f"/alerts?view=all&device_id={DEV_B}")
    mine = [a for a in listing["data"] if a["id"] == str(ALERT_ID)]
    assert mine and not _mentions(mine, "172.31.92.10", "40.4")
    status, full = _call(world, "acsfix_view_b", "GET", f"/alerts/{ALERT_ID}")
    assert status == 200 and "40.4" in full["message"] and full["metadata"]["snapshot"]["talkers"]


@needs_e2e
def test_e2e_report_schedules_validate_and_withhold_netflow_scopes(world):
    status, body = _call(world, "admin", "POST", "/report-schedules", json={
        "name": "acsfix bad", "report_type": "traffic", "format": "none",
        "filters": {"netflow": {"exporter": "not-an-ip'; drop", "evil": 1}}})
    assert status == 400 and "evil" in json.dumps(body)
    status, sched = _call(world, "admin", "POST", "/report-schedules", json={
        "name": "acsfix ok", "report_type": "traffic", "format": "none",
        "filters": {"netflow": {"exporter": EXP_B, "talker": "172.31.92.10"}}})
    assert status == 201, sched
    for user in ("acsfix_view_a", "acsfix_view_none", "acsfix_nonf"):
        _, listing = _call(world, user, "GET", "/report-schedules")
        mine = next(s for s in listing["data"] if s["id"] == sched["id"])
        assert mine["filters"].get("netflow_withheld") is True and "netflow" not in mine["filters"], user
        assert not _mentions(mine, "172.31.92.10", EXP_B)
    _, listing = _call(world, "acsfix_view_b", "GET", "/report-schedules")
    assert next(s for s in listing["data"] if s["id"] == sched["id"])["filters"]["netflow"]["talker"] == "172.31.92.10"
    # A legacy invalid scope already stored: run-now is not a clean success.
    _pg([("UPDATE report_schedules SET filters = CAST(:f AS jsonb) WHERE id = :id",
          {"f": json.dumps({"netflow": {"evil": 1}}), "id": uuid.UUID(sched["id"])})])
    status, run = _call(world, "admin", "POST", f"/report-schedules/{sched['id']}/run-now")
    assert status == 200 and run["status"] == "partial" and any("NetFlow scope" in e for e in run["errors"])


@needs_e2e
def test_e2e_unknown_filter_keys_are_refused(world):
    status, body = _call(world, "acsfix_view_a", "POST", "/netflow/explain", json={"hours": 1, "filter": {"evil": "1"}})
    assert status == 400 and "evil" in json.dumps(body)
    status, body = _call(world, "acsfix_view_a", "GET", "/reports/render/traffic?hours=1&netflow=talkr%3D172.31.91.10&format=json")
    assert status == 400 and "talkr" in json.dumps(body)


@needs_e2e
def test_e2e_ti_feed_urls_cannot_reach_internal_services(world):
    for url in ("http://127.0.0.1:18091/health", "http://127.0.0.1:55441/", "http://169.254.169.254/latest/"):
        status, body = _call(world, "acsfix_op_a", "POST", "/netflow/ti/feeds",
                             json={"name": f"acsfix {url[-12:]}", "source": "url", "url": url})
        assert status == 400, (url, body)


@needs_e2e
def test_e2e_empty_scope_is_reported_as_such(world):
    status, scope = _call(world, "acsfix_view_none", "GET", "/netflow/scope")
    assert status == 200 and scope["restricted"] and scope["scope_empty"] is True and scope["flow_exporters"] == 0
    _, overview = _call(world, "acsfix_view_none", "GET", "/netflow/overview?hours=1&envelope=1")
    assert "scope_empty" in {w["code"] for w in overview["meta"]["warnings"]}
    _, mine = _call(world, "acsfix_view_a", "GET", "/netflow/scope")
    assert mine["scope_empty"] is False and mine["flow_exporters"] == 1      # 10.254.91.1 never exported
    _, admin = _call(world, "admin", "GET", "/netflow/scope")
    assert admin == {**admin, "restricted": False, "scope_empty": False, "flow_exporters": None}
    assert _call(world, "acsfix_nonf", "GET", "/netflow/scope")[0] == 403
