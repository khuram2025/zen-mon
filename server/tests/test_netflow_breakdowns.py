"""NetFlow breakdowns (B2): talkers, conversations, services, applications,
interfaces, DSCP, TCP flags, network classes, countries, ASNs, next hops and
IP groups.

Three layers:

* unit + in-process HTTP over a fake ClickHouse (always run): service/kind
  helpers, legacy list shapes vs ``with_totals=1``, validation, half-open
  windows, dimensions a breakdown drops are not claimed as applied;
* scratch ClickHouse (``NETFLOW_TEST_CLICKHOUSE``, see netflow_scratch_ch.py):
  a fixture in a disposable database, read through the real router: one
  conversation per exchange (X-30), exporter-qualified interface pairs
  (X-29, X-19), bidirectional service inference (NF-20), sent/received
  ranking with deterministic ties and whole-scope denominators (NF-24/25),
  interfaces honouring every non-interface filter (NF-27), unknown TCP flags
  and next hops, AS/next-hop views (CR-20), IPv6 countries, IP groups,
  half-open windows and hybrid == raw totals;
* live API (``NETFLOW_TEST_API`` = a running API of this tree, e.g. :8112,
  with ``NETFLOW_TEST_TOKEN`` or admin/admin123): invariants on real traffic.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
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

from app.api.v1 import netflow  # noqa: E402
from app.api.v1.netflow import access, breakdowns as B, common as C, filters as F  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_classify, netflow_groups, netflow_rollup  # noqa: E402

URL = "/api/v1/netflow"
USER = SimpleNamespace(id=uuid4(), username="viewer", role="viewer", is_active=True, scope_tags=[])
UNRESTRICTED = F.UNRESTRICTED
SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                        permissions=frozenset({"netflow.view"}))


# ── Unit ──────────────────────────────────────────────────────────────────────

def test_service_info_distinguishes_service_icmp_and_portless():
    https = B._service_info(6, 443)
    assert https["service_port"] == 443 and https["service"] == "HTTPS" and https["port_applicable"]
    assert https["drill"] == {"protocol": 6, "service_port": 443}
    esp = B._service_info(50, 0)
    assert esp["service_port"] is None and esp["service"] is None and not esp["port_applicable"]
    assert B._service_label(esp) == "Not applicable" and esp["drill"] == {"protocol": 50}
    echo = B._service_info(1, 8 * 256)
    assert echo["service"] == "Echo request" and echo["drill"] == {"protocol": 1, "dst_port": 2048}
    unident = B._service_info(6, 0)
    assert unident["service"] is None and unident["port_applicable"] and "Unidentified" in B._service_label(unident)
    assert B._service_info(17, 443)["service"] == "QUIC"


def test_address_kinds_follow_the_network_classes():
    assert B._address_kind("10.1.2.3") == "private"
    assert B._address_kind("100.64.1.1") == "private"
    assert B._address_kind("fd00::1") == "private"
    assert B._address_kind("198.51.100.7") == "reserved"
    assert B._address_kind("2001:db8::1") == "reserved"
    assert B._address_kind("8.8.8.8") == "public"
    assert B._address_kind("2001:4860:4860::8888") == "public"


def test_paged_remainder_is_what_the_items_leave():
    items = [{"src_bytes": 60}, {"src_bytes": 30}]
    body = B._paged(items, total={"bytes": 100}, distinct=5, limit=2, offset=0, basis="sent_bytes",
                    additive=True, measure={"bytes": "src_bytes"})
    assert body["remainder"] == {"bytes": 10}
    # Non-additive rankings (endpoint involvement) have no remainder.
    assert B._paged(items, total={"bytes": 100}, distinct=5, limit=2, offset=0, basis="endpoint_bytes",
                    additive=False)["remainder"] is None
    # A later page has no meaningful remainder either.
    assert B._paged(items, total={"bytes": 100}, distinct=5, limit=2, offset=2, basis="sent_bytes",
                    additive=True, measure={"bytes": "src_bytes"})["remainder"] is None


def test_classification_explains_tunnel_limits():
    esp = B._classification("VPN / Tunneling", "high", "protocol", 50)
    assert "not visible" in esp["visibility_note"] and esp["ip_protocol"] == "ESP"
    web = B._classification("Web (HTTP/HTTPS)", "high", "src_rule", 6)
    assert web["visibility_note"] is None and "source service port" in web["basis_text"]


# ── HTTP over a fake ClickHouse ───────────────────────────────────────────────

TTL_ROWS = [
    ("flow_records", "MergeTree TTL toDateTime(timestamp) + toIntervalDay(30)"),
    ("flow_conversations_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_qos_1h", "AggregatingMergeTree TTL timestamp + toIntervalDay(90)"),
    ("flow_interface_5m", "AggregatingMergeTree TTL timestamp + toIntervalDay(400)"),
    ("flow_exporter_minutes", "AggregatingMergeTree TTL minute + toIntervalDay(400)"),
]


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}))
        if "system.tables" in sql:
            return FakeResult(TTL_ROWS)
        if "system.parts" in sql:
            return FakeResult([("flow_records", datetime(2026, 9, 1))])
        if "GROUP BY" in sql:
            return FakeResult([])
        return FakeResult([tuple([0] * 40)])

    def command(self, sql, parameters=None):
        return None

    def flow_calls(self):
        """Traffic queries: not system tables, and not the per-exporter notice
        lookups every flow answer makes (filters.warn_exporter_notes, scoped
        and cached; covered by test_netflow_collector_data_fixes)."""
        return [(s, p) for s, p in self.calls if "system." not in s and not _notice_lookup(s)]


def _notice_lookup(sql: str) -> bool:
    return "HAVING uniqExact(flow_direction) = 2" in sql or "sum(sampling_ambiguous_records)" in sql


class FakeDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


def _make_client(vis: FlowVisibility) -> TestClient:
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield FakeDB()

    async def user():
        return USER

    async def visibility():
        return vis

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


@pytest.fixture
def no_refresh(monkeypatch):
    async def noop(*a, **kw):
        return None

    async def repair():
        return {"reconciled_at": None, "verified_from": None, "pending_hours": {}, "rebuilt_hours": {},
                "unrecoverable_hours": {}, "rows_found_after_upgrade": 0}

    monkeypatch.setattr(netflow_classify, "load_rules", noop)
    monkeypatch.setattr(netflow_groups, "load_groups", noop)
    monkeypatch.setattr(netflow_rollup, "repair_status", repair)
    C._repair_cache.update(value=None, at=0.0)
    C._retention_cache.update(value=None, at=0.0)
    yield
    C._retention_cache.update(value=None, at=0.0)
    C._repair_cache.update(value=None, at=0.0)


@pytest.fixture
def fake(monkeypatch, no_refresh):
    ch = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)
    return ch, _make_client(UNRESTRICTED)


LIST_ENDPOINTS = ["/top-talkers", "/top-endpoints", "/top-conversations", "/protocols", "/ports",
                  "/applications", "/interfaces", "/dscp", "/network-classes", "/asns", "/next-hops"]


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
def test_legacy_list_shape_and_opt_in_totals(fake, path):
    _, api = fake
    legacy = api.get(URL + path, params={"hours": 1})
    assert legacy.status_code == 200, legacy.text
    assert isinstance(legacy.json(), list)
    body = api.get(URL + path, params={"hours": 1, "with_totals": 1, "envelope": 1}).json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert {"items", "total", "distinct", "limit", "offset", "basis", "additive", "remainder"} <= set(data)
    assert body["meta"]["window"]["source"] == "raw"


def test_tcp_flags_and_groups_keep_their_shapes(fake):
    _, api = fake
    flags = api.get(URL + "/tcp-flags", params={"hours": 1}).json()
    assert {"total_tcp", "syn_only", "ack_only", "rst", "fin", "psh", "urg", "no_flags"} <= set(flags)
    assert flags["support"] == "no_tcp" and flags["flag_coverage_pct"] is None
    # The one RST ratio (overview.rst_payload), not a second definition.
    assert flags["rst_ratio_pct"] is None and flags["rst_ratio"]["status"] == "no_tcp"
    # Independent of whatever groups the process (or the scratch database)
    # already holds: no groups configured -> the legacy empty list.
    saved = netflow_groups.groups_snapshot()
    netflow_groups.set_groups_for_tests([])
    try:
        assert api.get(URL + "/ip-groups", params={"hours": 1}).json() == []
    finally:
        netflow_groups.set_groups_for_tests(saved)


def test_every_breakdown_query_is_half_open_and_scoped(monkeypatch, no_refresh):
    ch = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: ch)
    api = _make_client(SCOPED)
    for path in LIST_ENDPOINTS + ["/tcp-flags", "/countries"]:
        ch.calls.clear()
        r = api.get(URL + path, params={"hours": 3, "envelope": 1})
        assert r.status_code == 200, (path, r.text)
        assert r.json()["meta"]["restricted"] is True
        for sql, params in ch.flow_calls():
            assert " BETWEEN %(start)s" not in sql and "BETWEEN %(nf_t" not in sql, path
            assert ">= %(nf_t0_from)s" in sql and "< %(nf_t0_to)s" in sql, (path, sql)
            assert any(isinstance(v, (list, tuple)) and list(v) == ["10.9.0.1"] for v in params.values()), path
            if "FROM zenplus.flow_records" in sql:
                assert "fw_event NOT IN (1, 3)" in sql, path
                # X-36: the corrupt-volume guard applies to every raw breakdown
                # (interfaces included), not only to the overview.
                assert "sampling_interval, 1) <= 1000000000000" in sql, path
        # Out-of-scope exporter: 404, never an empty page that confirms nothing.
        assert api.get(URL + path, params={"exporter": "10.9.0.2"}).status_code == 404, path


def test_breakdowns_do_not_claim_their_own_dimension(fake):
    _, api = fake
    cases = {
        "/applications": ({"app": "DNS", "protocol": "17"}, {"protocol": "17"}),
        "/dscp": ({"dscp": "46", "protocol": "17"}, {"protocol": "17"}),
        "/tcp-flags": ({"tcpflag": "rst", "exporter": "10.9.0.1"}, {"exporter": "10.9.0.1"}),
        "/network-classes": ({"netclass": "Public", "protocol": "6"}, {"protocol": "6"}),
        "/interfaces": ({"iface": "2@10.9.0.1", "iface_dir": "in", "protocol": "6"}, {"protocol": "6"}),
        "/asns": ({"asn": "15169", "protocol": "6"}, {"protocol": "6"}),
        "/top-talkers": ({"talker": "10.0.0.5", "protocol": "6"}, {"talker": "10.0.0.5", "protocol": "6"}),
    }
    for path, (params, applied) in cases.items():
        meta = api.get(URL + path, params={**params, "envelope": 1}).json()["meta"]
        assert meta["filters"] == applied, (path, meta["filters"])


def test_ranking_choices_are_validated(fake):
    _, api = fake
    for path, params in (("/top-talkers", {"by": "sideways"}), ("/top-talkers", {"sort": "loudness"}),
                         ("/top-conversations", {"sort": "x"}), ("/top-talkers", {"limit": 0}),
                         ("/top-talkers", {"offset": -1}), ("/ports", {"limit": 1000})):
        r = api.get(URL + path, params=params)
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), (path, params, r.text)


def test_interfaces_prefer_the_interface_rollup_for_long_windows(fake):
    ch, api = fake
    frm = (datetime.now(timezone.utc) - timedelta(days=10)).replace(minute=7).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) - timedelta(days=1)).replace(minute=7).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = api.get(URL + "/interfaces", params={"from": frm, "to": to, "envelope": 1}).json()["meta"]
    assert meta["window"]["source"] in ("hybrid", "interface_rollup")
    assert any("flow_interface_5m" in s for s, _ in ch.flow_calls())
    # Any non-interface filter the rollup cannot evaluate routes to raw records.
    ch.calls.clear()
    meta = api.get(URL + "/interfaces", params={"from": frm, "to": to, "protocol": 6, "envelope": 1}).json()["meta"]
    assert meta["window"]["source"] == "raw"
    assert all("flow_interface_5m" not in s for s, _ in ch.flow_calls())


# ── Scratch ClickHouse fixture ────────────────────────────────────────────────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

TRACKED = 1 << 31
BITS = {"flags": 1 << 0, "tos": 1 << 1, "ports": 1 << 2, "in": 1 << 3, "out": 1 << 4, "times": 1 << 5,
        "as": 1 << 6, "nh": 1 << 8}
FULL = TRACKED | sum(BITS.values())
NO_FLAGS = FULL & ~BITS["flags"]

COLUMNS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_addr6", "dst_addr6",
           "next_hop", "protocol", "src_port", "dst_port", "bytes", "packets", "tcp_flags", "tos",
           "input_snmp", "output_snmp", "input_if", "output_if", "fields_present", "src_as", "dst_as",
           "first_switched_ms", "last_switched_ms", "sampling_interval", "ip_version", "flow_version"]


def _flow(ts, src, dst, proto, sport, dport, nbytes, *, exporter="10.9.0.1", inp=3, out=4, flags=0x1B,
          fp=FULL, nh="0.0.0.0", sas=0, das=0, tos=0, packets=None):
    v6 = ":" in src
    ms = int(ts.timestamp() * 1000)
    return [ts, ts, exporter,
            "0.0.0.0" if v6 else src, "0.0.0.0" if v6 else dst,
            src if v6 else "::", dst if v6 else "::",
            nh, proto, sport, dport, nbytes, packets or max(1, nbytes // 1000), flags, tos,
            min(inp, 65535), min(out, 65535), inp, out, fp, sas, das, ms - 2000, ms, 1, 6 if v6 else 4, 9]


def fixture_rows(t0: datetime) -> list[list]:
    rows = []
    # HTTPS: 4 requests + 4 responses (+1 request also seen by a second
    # exporter on other interfaces), and a response to a PAT'd client port.
    for i in range(4):
        rows.append(_flow(t0 + timedelta(seconds=i), "10.0.0.5", "93.184.216.34", 6, 50001 + i, 443, 5000,
                          inp=3, out=4, nh="10.0.0.254", das=15133))
        rows.append(_flow(t0 + timedelta(seconds=10 + i), "93.184.216.34", "10.0.0.5", 6, 443, 50001 + i,
                          5_000_000, inp=4, out=3, sas=15133))
    rows.append(_flow(t0 + timedelta(seconds=20), "10.0.0.5", "93.184.216.34", 6, 50001, 443, 5000,
                      exporter="10.9.0.2", inp=7, out=8, fp=NO_FLAGS, flags=0))
    rows.append(_flow(t0 + timedelta(seconds=21), "93.184.216.34", "10.0.0.5", 6, 443, 3306, 10_000_000,
                      inp=4, out=3, sas=15133))
    # ESP both ways (portless), DSCP EF.
    rows.append(_flow(t0 + timedelta(seconds=30), "198.51.100.1", "203.0.113.10", 50, 0, 0, 1_000_000,
                      inp=9, out=10, flags=0, tos=46 << 2, sas=64500))
    rows.append(_flow(t0 + timedelta(seconds=31), "203.0.113.10", "198.51.100.1", 50, 0, 0, 1_000_000,
                      inp=10, out=9, flags=0, tos=46 << 2))
    # ICMP echo requests (type 8 in dst_port).
    for i in range(3):
        rows.append(_flow(t0 + timedelta(seconds=40 + i), "10.0.0.9", "10.0.0.1", 1, 0, 2048, 100, flags=0))
    # DNS request/response on ifIndex 2 of exporter 1, and UDP on ifIndex 2
    # of exporter 2 (same index, different interface).
    rows.append(_flow(t0 + timedelta(seconds=50), "10.0.0.5", "10.0.0.53", 17, 53000, 53, 200, inp=2, out=5, flags=0))
    rows.append(_flow(t0 + timedelta(seconds=51), "10.0.0.53", "10.0.0.5", 17, 53, 53000, 400, inp=5, out=2, flags=0))
    rows.append(_flow(t0 + timedelta(seconds=52), "10.0.0.7", "10.0.0.8", 17, 5004, 5004, 900, exporter="10.9.0.2",
                      inp=2, out=5, flags=0))
    # A receive-only host (unknown service: two ephemeral ports).
    rows.append(_flow(t0 + timedelta(seconds=60), "10.0.0.5", "10.0.0.99", 17, 40000, 41000, 300, flags=0))
    # Two senders tied on sent bytes (deterministic order: address).
    rows.append(_flow(t0 + timedelta(seconds=70), "10.0.0.21", "10.0.0.53", 17, 50000, 53, 777, flags=0))
    rows.append(_flow(t0 + timedelta(seconds=71), "10.0.0.20", "10.0.0.53", 17, 50000, 53, 777, flags=0))
    # IPv6 HTTPS response from a public address.
    rows.append(_flow(t0 + timedelta(seconds=80), "2001:4860:4860::8888", "fd00::5", 6, 443, 50010, 2_000_000,
                      sas=15169))
    # A legacy record (fields_present = 0): TCP with zero flags proves nothing.
    rows.append(_flow(t0 + timedelta(seconds=90), "10.0.0.30", "10.0.0.31", 6, 50100, 22, 1500, flags=0, fp=0))
    return rows


FIXTURE_TOTAL = (4 * 5000 + 4 * 5_000_000 + 5000 + 10_000_000 + 2_000_000 + 300 + 200 + 400 + 900 + 300
                 + 2 * 777 + 2_000_000 + 1500)


@pytest.fixture
def ch_api(monkeypatch, no_refresh):
    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    with flow_db(client) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        t0 = C.now_utc().replace(microsecond=0) - timedelta(minutes=20)
        client.insert(f"{db}.flow_records", fixture_rows(t0), column_names=COLUMNS)
        yield SimpleNamespace(client=client, db=db, api=_make_client(UNRESTRICTED), t0=t0)


def _get(env, path, **params):
    r = env.api.get(URL + path, params={"hours": 1, **params})
    assert r.status_code == 200, (path, r.text)
    return r.json()


def test_fixture_total_matches(ch_api):
    assert FIXTURE_TOTAL == sum(r[11] for r in fixture_rows(ch_api.t0))


def test_one_conversation_per_exchange_with_both_directions(ch_api):
    body = _get(ch_api, "/top-conversations", limit=50, with_totals=1)
    items = body["items"]
    https = [c for c in items if c["protocol"] == 6 and c["service_port"] == 443 and c["client"] == "10.0.0.5"]
    assert len(https) == 1, [(c["client"], c["server"], c["dst_port"]) for c in items]
    h = https[0]
    assert h["server"] == "93.184.216.34" and h["service"] == "HTTPS" and h["orientation"] == "client_server"
    assert h["application"] == "Web (HTTP/HTTPS)" and h["confidence"] in ("high", "medium")
    assert h["bytes_c2s"] == 5 * 5000 and h["bytes_s2c"] == 4 * 5_000_000 + 10_000_000
    assert h["src"] == h["client"] and h["dst"] == h["server"] and h["dst_port"] == 443
    assert {50001, 50002, 50003, 50004, 3306} <= set(h["client_ports"])
    # No response half keyed by an ephemeral client port (X-30).
    assert not [c for c in items if c["dst_port"] in (50001, 50002, 50003, 50004)]
    # Interfaces as observed per exporter, never a cross product (X-29).
    ins = {(x["exporter_ip"], x["ifindex"]) for x in h["input_interfaces"]}
    assert ins == {("10.9.0.1", 3), ("10.9.0.1", 4), ("10.9.0.2", 7)}
    assert {(x["exporter_ip"], x["ifindex"]) for x in h["client_side_interfaces"]} == {("10.9.0.1", 3), ("10.9.0.2", 7)}
    assert {(x["exporter_ip"], x["ifindex"]) for x in h["server_side_interfaces"]} == {("10.9.0.1", 4), ("10.9.0.2", 8)}
    assert all(x["iface"] == f"{x['ifindex']}@{x['exporter_ip']}" for x in h["input_interfaces"])
    assert h["avg_duration_ms"] == 2000.0
    # Flags come only from flagged records (the second exporter sent none).
    assert h["tcp_flags_known"] and h["tcp_flags"] == 0x1B and h["flagged_flows"] == 9
    # ESP: portless, oriented by address, tunnel limits stated.
    esp = next(c for c in items if c["protocol"] == 50)
    assert esp["service"] == "Not applicable" and esp["service_port"] is None and not esp["port_applicable"]
    assert esp["orientation"] == "address_order" and esp["bytes_c2s"] == esp["bytes_s2c"] == 1_000_000
    assert "not visible" in esp["visibility_note"] and esp["tcp_flags"] is None
    # Whole-scope denominator: the rows plus the remainder are the scope.
    assert body["total"]["bytes"] == FIXTURE_TOTAL and body["additive"]
    assert sum(c["bytes"] for c in items) + body["remainder"]["bytes"] == FIXTURE_TOTAL
    assert body["distinct"] == len(items)


def test_talkers_rank_by_side_with_stable_ties(ch_api):
    sent = _get(ch_api, "/top-talkers", by="src", limit=50, with_totals=1)
    ips = [t["ip"] for t in sent["items"]]
    assert ips[0] == "93.184.216.34"
    assert "10.0.0.99" not in ips                     # never sent anything
    assert all(t["src_flows"] > 0 and t["rank_value"] == t["src_bytes"] for t in sent["items"])
    values = [t["src_bytes"] for t in sent["items"]]
    assert values == sorted(values, reverse=True)
    assert ips.index("10.0.0.20") == ips.index("10.0.0.21") - 1     # tie broken by address
    assert sent["total"]["bytes"] == FIXTURE_TOTAL and sent["additive"] and sent["basis"] == "sent_bytes"
    assert sum(values) + sent["remainder"]["bytes"] == FIXTURE_TOTAL
    received = _get(ch_api, "/top-talkers", by="dst", limit=50)
    assert "10.0.0.99" in [t["ip"] for t in received]
    both = _get(ch_api, "/top-endpoints", limit=50, with_totals=1)
    assert both["additive"] is False and both["remainder"] is None
    # Endpoint involvement double counts by design: every flow has two ends.
    assert sum(t["bytes"] for t in both["items"]) == 2 * FIXTURE_TOTAL
    first = both["items"][0]
    assert first["share_pct"] == pytest.approx(100 * first["bytes"] / FIXTURE_TOTAL, abs=0.01)
    # Paging: the second page continues the ranking.
    page2 = _get(ch_api, "/top-talkers", by="src", limit=2, offset=2, with_totals=1)
    assert [t["ip"] for t in page2["items"]] == ips[2:4] and page2["remainder"] is None
    # Services of a host are classified both ways (the server's responses are HTTPS).
    server = next(t for t in sent["items"] if t["ip"] == "93.184.216.34")
    assert server["services"][0]["service"] == "HTTPS" and server["ports"][0]["port"] == 443


def test_services_applications_and_drilldown_parity(ch_api):
    apps = _get(ch_api, "/applications", with_totals=1)
    web = next(a for a in apps["items"] if a["name"] == "Web (HTTP/HTTPS)")
    https_bytes = 5 * 5000 + 4 * 5_000_000 + 10_000_000 + 2_000_000
    assert web["bytes"] == https_bytes
    assert web["request_bytes"] == 5 * 5000 and web["response_bytes"] == https_bytes - 5 * 5000
    assert sum(a["bytes"] for a in apps["items"]) == apps["total"]["bytes"] == FIXTURE_TOTAL
    vpn = next(a for a in apps["items"] if a["name"] == "VPN / Tunneling")
    assert vpn["visibility_note"] and vpn["services"][0]["service_note"] == "Not applicable"
    ports = _get(ch_api, "/ports", limit=50, with_totals=1)
    https = next(p for p in ports["items"] if p["protocol"] == 6 and p["port"] == 443)
    assert https["service"] == "HTTPS" and https["bytes"] == https_bytes
    icmp = next(p for p in ports["items"] if p["protocol"] == 1)
    assert icmp["service"] == "Echo request" and icmp["drill"] == {"protocol": 1, "dst_port": 2048}
    assert sum(p["bytes"] for p in ports["items"]) == FIXTURE_TOTAL
    # Drill-downs return exactly the flows behind the item (NF-08/NF-20).
    for drill_total in (
        _get(ch_api, "/protocols", service_port=443, protocol=6, with_totals=1)["total"]["bytes"],
        _get(ch_api, "/protocols", app="Web (HTTP/HTTPS)", with_totals=1)["total"]["bytes"],
    ):
        assert drill_total == https_bytes
    protos = _get(ch_api, "/protocols")
    tcp = next(p for p in protos if p["protocol"] == 6)
    assert tcp["services"][0]["service"] == "HTTPS" and tcp["ports"][0]["port"] == 443
    # A talker filter keeps destination-only occurrences.
    conv = _get(ch_api, "/top-conversations", talker="10.0.0.99")
    assert [(c["client"], c["server"]) for c in conv] == [("10.0.0.5", "10.0.0.99")]


def test_interfaces_are_exporter_qualified_and_follow_filters(ch_api):
    rows = _get(ch_api, "/interfaces", limit=50, with_totals=1)
    keys = {r["iface"] for r in rows["items"]}
    assert {"2@10.9.0.1", "2@10.9.0.2"} <= keys          # same ifIndex, two interfaces
    assert rows["additive"] is False and rows["total"] is None
    udp = _get(ch_api, "/interfaces", limit=50, protocol=17)
    udp_in = sum(r["in_bytes"] for r in udp)
    assert udp_in == 200 + 400 + 900 + 300 + 2 * 777     # only UDP traffic, every UDP record entered once
    assert {r["iface"] for r in udp} < keys
    picked = _get(ch_api, "/interfaces", limit=50, iface="2@10.9.0.1", with_totals=1, envelope=1)
    data, meta = picked["data"], picked["meta"]
    assert {"2@10.9.0.1", "2@10.9.0.2"} <= {r["iface"] for r in data["items"]}   # others kept for comparison
    assert [r["iface"] for r in data["items"] if r["selected"]] == ["2@10.9.0.1"]
    assert data["scope"]["interface_excluded"] and "iface" not in meta["filters"]


def test_interfaces_state_no_utilization_of_their_own(ch_api):
    """One utilization definition (wave C): this ranking counts records the
    page's way (count=all adds an exporter's ingress and egress copies of the
    same packets) and averages over the whole window (exporter gaps read as
    idle), so it states no utilization percentage -- Capacity does, and the
    body says so."""
    body = _get(ch_api, "/interfaces", limit=50, with_totals=1)
    assert body["items"]
    for row in body["items"]:
        assert "in_util_avg_pct" not in row and "out_util_avg_pct" not in row
        assert "in_bps_avg" in row and "speed_in_bps" in row          # rates and speeds remain
    assert body["utilization"]["source"] == "capacity" and body["utilization"]["path"] == "/netflow/capacity"
    assert "counted once" in body["utilization"]["note"]


def test_selected_interface_stays_visible_outside_the_top(ch_api):
    """The interface the page is filtered to is pinned after the ranked rows
    when it is not among them, without changing the ranked page or counts."""
    top = _get(ch_api, "/interfaces", limit=1, with_totals=1)
    assert len(top["items"]) == 1 and top["items"][0]["iface"] != "5@10.9.0.2"
    picked = _get(ch_api, "/interfaces", limit=1, iface="5@10.9.0.2", with_totals=1, envelope=1)
    data, meta = picked["data"], picked["meta"]
    assert [r["iface"] for r in data["items"]] == [top["items"][0]["iface"], "5@10.9.0.2"]
    assert [r["pinned"] for r in data["items"]] == [False, True]
    assert data["items"][1]["selected"] is True and data["items"][1]["out_bytes"] == 900
    assert data["distinct"] == top["distinct"]
    assert meta["truncated"]["returned"] == 1
    # Already on the page: nothing is appended.
    first = top["items"][0]["iface"]
    same = _get(ch_api, "/interfaces", limit=1, iface=first, with_totals=1)
    assert [r["iface"] for r in same["items"]] == [first] and not same["items"][0]["pinned"]


def test_tcp_flags_count_only_observed_flags(ch_api):
    flags = _get(ch_api, "/tcp-flags")
    # 12 TCP records: 10 exported flags 0x1B (FIN+SYN+PSH+ACK); the second
    # exporter's template has no flags field and one record is legacy (its
    # zero proves nothing) -- 2 unknown, never counted as "no flags".
    assert flags["total_tcp"] == 12 and flags["legacy_tcp"] == 1
    assert flags["flagged_tcp"] == 10 and flags["unknown_tcp"] == 2
    assert flags["fin"] == flags["psh"] == 10 and flags["syn_only"] == flags["ack_only"] == flags["rst"] == 0
    assert flags["no_flags"] == 0 and flags["support"] == "partial"
    assert _get(ch_api, "/tcp-flags", protocol=17)["support"] == "not_applicable"


def test_network_classes_partition_each_side(ch_api):
    body = _get(ch_api, "/network-classes", with_totals=1)
    assert sum(c["src_bytes"] for c in body["items"]) == FIXTURE_TOTAL
    assert sum(c["dst_bytes"] for c in body["items"]) == FIXTURE_TOTAL
    names = {c["name"] for c in body["items"]}
    assert {"Private 10/8", "Public", "Unique local (IPv6)"} <= names


def test_countries_include_ipv6_and_disclose_the_rest(ch_api):
    body = _get(ch_api, "/countries", with_totals=1)
    if not body.get("available"):
        pytest.skip("no GeoIP country database on this host")
    by_kind = {}
    for c in body["items"]:
        by_kind.setdefault(c["kind"], 0)
        by_kind[c["kind"]] += c["src_bytes"]
    assert sum(by_kind.values()) + body["remainder"]["src_bytes"] == FIXTURE_TOTAL
    assert by_kind.get("reserved", 0) >= 1_000_000              # ESP documentation addresses
    assert any(c["kind"] == "country" and c["src_bytes"] >= 2_000_000 for c in body["items"])   # IPv6 source


def test_asns_prefer_the_exporter_as_numbers(ch_api):
    body = _get(ch_api, "/asns", with_totals=1)
    by_asn = {a["asn"]: a for a in body["items"]}
    assert by_asn[15133]["source"] in ("flow", "flow+geoip")
    assert by_asn[15133]["src_bytes"] == 4 * 5_000_000 + 10_000_000
    assert by_asn[15169]["src_bytes"] == 2_000_000
    assert body["basis_bytes"]["flow"] > 0
    only = _get(ch_api, "/asns", asn=15133, envelope=1)
    assert "asn" not in only["meta"]["filters"]


def test_next_hops_separate_unknown_from_connected(ch_api):
    body = _get(ch_api, "/next-hops", with_totals=1)
    assert [h["next_hop"] for h in body["items"]] == ["10.0.0.254"]
    hop = body["items"][0]
    assert hop["bytes"] == 4 * 5000 and {(x["exporter_ip"], x["ifindex"]) for x in hop["out_interfaces"]} == {("10.9.0.1", 4)}
    unknown = body["without_next_hop"]
    assert unknown["legacy"]["bytes"] == 1500
    assert unknown["connected"]["bytes"] == FIXTURE_TOTAL - 4 * 5000 - 1500
    assert body["remainder"] == {"bytes": 0}


def test_ip_groups_split_sent_received_internal(ch_api):
    netflow_groups.set_groups_for_tests([
        netflow_groups.Group(id="g1", name="Clients", cidrs=(netflow_groups.parse_network("10.0.0.0/28"),)),
        netflow_groups.Group(id="g2", name="Tunnel peers", cidrs=(netflow_groups.parse_network("198.51.100.0/24"),)),
    ])
    try:
        body = _get(ch_api, "/ip-groups", with_totals=1)
    finally:
        netflow_groups.set_groups_for_tests([])
    g = {x["name"]: x for x in body["items"]}
    assert g["Tunnel peers"]["src_bytes"] == g["Tunnel peers"]["dst_bytes"] == 1_000_000
    assert g["Tunnel peers"]["bytes"] == 2_000_000 and g["Tunnel peers"]["internal_bytes"] == 0
    # 10.0.0.0/28: .5, .7, .8, .9, .1 (not .20/.21/.30/.31/.53/.99)
    clients = g["Clients"]
    assert clients["internal_bytes"] == 300 + 900
    assert body["ungrouped"]["bytes"] == FIXTURE_TOTAL - clients["bytes"] - 2_000_000
    assert body["additive"] is False


def test_windows_are_half_open(ch_api):
    t = ch_api.t0 + timedelta(minutes=5)
    ch_api.client.insert(f"{ch_api.db}.flow_records", [_flow(t, "10.1.1.1", "10.1.1.2", 17, 60000, 161, 12345)],
                         column_names=COLUMNS)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # noqa: E731
    before = _get(ch_api, "/top-talkers", **{"from": iso(t - timedelta(minutes=1)), "to": iso(t)}, by="src")
    after = _get(ch_api, "/top-talkers", **{"from": iso(t), "to": iso(t + timedelta(minutes=1))}, by="src")
    assert "10.1.1.1" not in [x["ip"] for x in before]
    assert "10.1.1.1" in [x["ip"] for x in after]


def test_long_windows_read_the_rollup_with_the_same_totals(ch_api):
    # Old traffic that only the hybrid plan's rollup interior covers.
    old = (C.now_utc() - timedelta(days=3)).replace(minute=0, second=0, microsecond=0) + timedelta(minutes=12)
    ch_api.client.insert(f"{ch_api.db}.flow_records", fixture_rows(old), column_names=COLUMNS)
    frm = (C.now_utc() - timedelta(days=4)).replace(minute=30, second=0, microsecond=0)
    params = {"from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": C.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")}
    r = ch_api.api.get(URL + "/top-conversations", params={**params, "limit": 50, "with_totals": 1, "envelope": 1})
    body = r.json()
    assert body["meta"]["window"]["source"] == "hybrid"
    assert body["data"]["total"]["bytes"] == 2 * FIXTURE_TOTAL
    https = [c for c in body["data"]["items"] if c["service_port"] == 443 and c["client"] == "10.0.0.5"]
    assert len(https) == 1 and https[0]["bytes_s2c"] == 2 * (4 * 5_000_000 + 10_000_000)
    talkers = ch_api.api.get(URL + "/top-talkers", params={**params, "by": "src", "with_totals": 1}).json()
    assert talkers["total"]["bytes"] == 2 * FIXTURE_TOTAL


# ── Live API (a running API of this tree) ─────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
live = pytest.mark.skipif(not LIVE, reason="set NETFLOW_TEST_API to a running API of this tree")


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
    r = requests.get(f"{LIVE}/api/v1/netflow{path}", params=params, headers=auth, timeout=120)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json()


def _fixed_window() -> dict:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return {"from": (end - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "to": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


@live
def test_live_conversations_are_unique_and_add_up(live_auth):
    w = _fixed_window()
    body = _live("/top-conversations", live_auth, limit=100, with_totals=1, **w)
    keys = [(c["client"], c["server"], c["protocol"], c["service_port"] or c.get("icmp_type_code")) for c in body["items"]]
    assert len(keys) == len(set(keys))
    for c in body["items"]:
        assert c["bytes_c2s"] + c["bytes_s2c"] == c["bytes"]
    assert sum(c["bytes"] for c in body["items"]) + body["remainder"]["bytes"] == body["total"]["bytes"]
    sent = _live("/top-talkers", live_auth, by="src", limit=20, with_totals=1, **w)
    assert sent["total"]["bytes"] == body["total"]["bytes"]
    assert all(t["src_flows"] > 0 for t in sent["items"])


@live
def test_live_legacy_consumers_still_get_lists(live_auth):
    for path, params in (("/top-talkers", {"minutes": 60, "limit": 6, "by": "total"}), ("/protocols", {"minutes": 60}),
                         ("/interfaces", {"minutes": 60, "limit": 6}), ("/applications", {"hours": 1}),
                         ("/top-conversations", {"hours": 1, "limit": 5})):
        rows = _live(path, live_auth, **params)
        assert isinstance(rows, list), path
    conv = _live("/top-conversations", live_auth, hours=1, limit=5)
    for row in conv:
        assert row["src"] and row["dst"] and "application" in row and "service" in row


@live
def test_live_interfaces_honour_protocol(live_auth):
    w = _fixed_window()
    everything = _live("/interfaces", live_auth, limit=50, **w)
    udp = _live("/interfaces", live_auth, limit=50, protocol=17, **w)
    assert sum(r["bytes"] for r in udp) < sum(r["bytes"] for r in everything)


def test_reverse_dns_follows_the_operator_switch(monkeypatch):
    """B7 dependency: resolve=1 honours NetFlow settings → Enrichment (on/off
    and deadline) instead of always resolving with a fixed 2 s deadline."""
    from app.api.v1.netflow import settings as S

    calls: list[tuple[list, float]] = []

    def fake_rdns(ips, deadline=2.0):
        calls.append((list(ips), deadline))
        return {ip: {"hostname": f"h-{ip}", "pending": False, "resolved_at": None} for ip in ips}

    monkeypatch.setattr(B, "_rdns", fake_rdns)
    cfg = {"enabled": True, "deadline_s": 0.5}

    async def rdns_config():
        return dict(cfg)

    monkeypatch.setattr(S, "rdns_config", rdns_config)
    rows = [{"ip": "10.0.0.1"}]
    asyncio.run(B._enrich(rows, "ip", True))
    assert calls == [(["10.0.0.1"], 0.5)] and rows[0]["hostname"] == "h-10.0.0.1"
    cfg["enabled"] = False
    rows = [{"ip": "10.0.0.2"}]
    asyncio.run(B._enrich(rows, "ip", True))
    assert len(calls) == 1 and rows[0]["hostname"] is None and rows[0]["hostname_pending"] is False
    rows = [{"ip": "10.0.0.3"}]
    asyncio.run(B._enrich(rows, "ip", False))
    assert len(calls) == 1 and "hostname_pending" not in rows[0]
