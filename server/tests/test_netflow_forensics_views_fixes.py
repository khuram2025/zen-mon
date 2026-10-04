"""Forensics / saved-views fixes from the NetFlow acceptance pass (wave C).

Each test pins one reproduced defect:

  * crafted page cursor: the duplicate counter can no longer raise the page
    query's LIMIT (it is bounded by the offset, and duplicates are skipped in
    ClickHouse with OFFSET over the equal-key rows only);
  * DSCP of records whose ToS was not exported is unknown, never DSCP 0: the
    ``dscp`` filter needs a known ToS (raw ``fields_present`` bit 1 / legacy,
    rollup ``tos_known``) and ``/dscp`` reports a separate ``not_exported``
    bucket with a warning;
  * "All N matching rows" exports are pinned to the search's received-at
    snapshot (``snapshot``);
  * a reopened finding keeps first_seen and the evidence window over every
    counted occurrence (also in test_netflow_detection's lifecycle test);
  * the legacy ``GET /forensics?format=csv`` needs ``netflow.export`` and is
    audited (``mode: legacy``);
  * NSEL connection-created / denied events are counted and disclosed in
    Forensics, and ``fw_event=flows`` excludes them (rollups, built through
    the volume guard, accept it as already true);
  * ICMP records carry ``icmp_type``/``icmp_code``; CSV exports leave the
    encoded "ports" blank and add type/code columns;
  * section saved views keep their page settings (``query.params``: by,
    side, resolve, col, dir).

Layers: unit (always), scratch ClickHouse (``NETFLOW_TEST_CLICKHOUSE``, see
netflow_scratch_ch.py) and the live API (``NETFLOW_TEST_API`` +
``NETFLOW_TEST_TOKEN``, optionally ``NETFLOW_TEST_TOKEN_VIEWER``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
import time
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
from app.api.v1.netflow import access, common as C, filters as F, forensics as FX, saved_views as SV  # noqa: E402
from app.api.v1.netflow import sql as S  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_classify, netflow_groups, netflow_rollup  # noqa: E402
from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

URL = "/api/v1/netflow"
USER = SimpleNamespace(id=uuid4(), username="fvfix", role="viewer", is_active=True, scope_tags=[])
VIEW_ONLY = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"netflow.view"}))
CAN_EXPORT = FlowVisibility(restricted=False, exporters=None, user_id=None,
                            permissions=frozenset({"netflow.view", "netflow.export"}))
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
TRACKED = 1 << 31
TOS_BIT = 1 << 1


def _spec(**kw) -> FX.SearchSpec:
    base = dict(start=T0 - timedelta(hours=1), end=T0, snapshot_ms=int(T0.timestamp() * 1000),
                filter=F.FlowFilter(), vis=VIEW_ONLY)
    base.update(kw)
    return FX.SearchSpec(**base)


def _cursor(**kw) -> FX.Cursor:
    base = dict(sort="timestamp", order="desc", start=T0 - timedelta(hours=1), end=T0, snapshot_ms=123,
                sort_value=None, ts_ms=1_790_000_000_123, row_key=77, dup=1, offset=1,
                fingerprint=FX.filter_fingerprint(F.FlowFilter()), mode="relative")
    base.update(kw)
    return FX.Cursor(**base)


def _body(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def _enc(body: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")


# ── Crafted cursor: the duplicate counter never sets the fetch size ─────────

def test_cursor_duplicate_counter_cannot_exceed_the_rows_already_emitted():
    good = _body(FX.encode_cursor(_cursor(dup=3, offset=500)))
    assert FX.decode_cursor(_enc(good)).dup == 3
    for d, x in ((600_000, 1), (501, 500), (0, 500), (-1, 500), (2 ** 60, 2 ** 60)):
        with pytest.raises(NetflowError) as exc:
            FX.decode_cursor(_enc(dict(good, d=d, x=x)))
        assert exc.value.status_code == 400, (d, x)
    # d == x is a legal (if crafted) cursor: it still costs one page.
    assert FX.decode_cursor(_enc(dict(good, d=600_000, x=600_000))).dup == 600_000


@pytest.mark.parametrize("sort", ["timestamp", "bytes"])
def test_page_query_limit_is_the_page_size_whatever_the_duplicate_counter(sort):
    c = _cursor(sort=sort, sort_value=None if sort == "timestamp" else 100, dup=600_000, offset=600_000)
    sql, params = FX.build_page_sql(_spec(sort=sort, after=c), 501)
    assert params["fx_limit"] == 501 and params["fx_dup"] == 600_000
    assert "UNION ALL" in sql and sql.endswith("LIMIT %(fx_limit)s")
    # The counter only skips rows of the equal-key part, inside ClickHouse.
    assert sql.count("OFFSET %(fx_dup)s") == 1
    assert "LIMIT %(fx_limit)s OFFSET %(fx_dup)s" in sql
    assert f"{FX.ROW_KEY} = %(fx_kr)s" in sql
    # No duplicate counter: the plain keyset query, no union.
    plain, p2 = FX.build_page_sql(_spec(sort=sort, after=_cursor(sort=sort, sort_value=None if sort == "timestamp" else 1,
                                                                 dup=0, offset=0)), 11)
    assert "UNION ALL" not in plain and p2["fx_limit"] == 11


def test_fetch_page_asks_for_one_page_plus_one(monkeypatch):
    seen = {}

    async def fake_run(sql, params, **kw):
        seen["limit"] = params["fx_limit"]
        return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(FX, "run_query", fake_run)
    rows, nxt = asyncio.run(FX.fetch_page(_spec(after=_cursor(dup=2_000_000, offset=2_000_000)), 1,
                                          budget_s=5, label="t"))
    assert seen["limit"] == 2 and rows == [] and nxt is None


@pytest.fixture
def real_ch(monkeypatch):
    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    yield client
    C._retention_cache.update(value=None, at=0.0)


RAW_COLS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_addr6", "dst_addr6",
            "ip_version", "src_port", "dst_port", "protocol", "bytes", "packets", "first_switched_ms",
            "last_switched_ms", "fields_present", "fw_event", "tcp_flags", "tos", "flow_sequence"]


def _raw(t, src, dst, *, exporter="10.9.0.1", sport=1000, dport=80, proto=6, nbytes=100, packets=1,
         fp=TRACKED | 0b111111, fw=0, flags=0x12, tos=0, seq=0):
    return [t, t, exporter, src, dst, "::", "::", 4, sport, dport, proto, nbytes, packets, 1_000, 2_000,
            fp, fw, flags, tos, seq]


def test_crafted_cursor_on_real_duplicates_loads_at_most_one_page(real_ch, monkeypatch):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc()
        t = now - timedelta(minutes=5)
        rows = [_raw(t, "10.2.0.1", "10.2.0.2", seq=1) for _ in range(2500)]           # one key, 2500 copies
        rows += [_raw(now - timedelta(minutes=6 + i), "10.3.0.1", "10.3.0.2", sport=2000 + i, seq=10 + i)
                 for i in range(5)]
        real_ch.insert(f"{db}.flow_records", rows, column_names=RAW_COLS)
        spec = FX.SearchSpec(start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
                             snapshot_ms=int(now.timestamp() * 1000) + 60_000, filter=F.FlowFilter(), vis=VIEW_ONLY)
        fetched: list[int] = []
        real_run = FX.run_query

        async def counting_run(sql, params, **kw):
            res = await real_run(sql, params, **kw)
            fetched.append(len(res.result_rows))
            return res

        monkeypatch.setattr(FX, "run_query", counting_run)
        # Paging through the duplicates with small pages returns every copy once.
        out, current, pages = [], spec, 0
        while True:
            got, nxt = asyncio.run(FX.fetch_page(current, 400, budget_s=30, label="t"))
            out.extend(got)
            pages += 1
            if nxt is None:
                break
            current = FX.replace(current, after=FX.decode_cursor(FX.encode_cursor(nxt)))
        assert len(out) == 2505 and pages == 7 and max(fetched) <= 401
        # A crafted cursor (dup = offset = 2,000,000) on the duplicated key:
        # one page at most comes back from ClickHouse, nothing is skipped in Python.
        first, nxt = asyncio.run(FX.fetch_page(spec, 1, budget_s=30, label="t"))
        body = _body(FX.encode_cursor(nxt))
        crafted = FX.decode_cursor(_enc(dict(body, d=2_000_000, x=2_000_000)))
        fetched.clear()
        got, _ = asyncio.run(FX.fetch_page(FX.replace(spec, after=crafted), 1, budget_s=30, label="t"))
        assert fetched == [2] and len(got) == 1
        assert got[0][FX._IDX["src"]] == "10.3.0.1"     # the duplicates were skipped, the rest follows


# ── DSCP: not exported is not Best Effort ─────────────────────────────────────

def test_dscp_filter_requires_a_known_tos_on_raw_and_rollup():
    f = F.parse_flow_filter({"dscp": "0"})
    raw_sql, params = F.compile_filter(f, S.RAW, VIEW_ONLY)
    assert "bitShiftRight(tos, 2) = %(nf_dscp)s" in raw_sql and S.TOS_KNOWN_RAW in raw_sql and params["nf_dscp"] == 0
    qos_sql, _ = F.compile_filter(f, S.QOS_1H, VIEW_ONLY)
    assert "tos_known != 0" in qos_sql
    assert S.table_caps(S.QOS_1H).can_serve(f)


def test_qos_rollup_keeps_tos_known_in_its_key():
    assert ("tos_known", "tos_known") in netflow_rollup._QOS_OUT
    assert "tos_known" in netflow_rollup._QOS_BODY.split("GROUP BY")[1]
    assert netflow_rollup.QOS.probe_column == "tos_known"
    mig = (Path(__file__).resolve().parents[2] / "scripts" / "migrate-137-netflow-schema-v2-clickhouse.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS tos_known UInt8" in mig and "MODIFY COLUMN tos_known UInt8 DEFAULT 2" in mig
    assert "AS tos_known" in mig.split("CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_qos_1h_mv")[1]


class FakeDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


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


def _client(vis: FlowVisibility) -> TestClient:
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


def _dscp_rows(t: datetime) -> list[list]:
    no_tos = TRACKED | 0b111101                       # ports, times, interfaces, flags -- no ToS
    with_tos = TRACKED | 0b111111
    return [
        _raw(t, "10.0.0.1", "10.0.0.2", exporter="10.9.0.5", nbytes=1000, fp=no_tos, tos=0, seq=1),   # unknown
        _raw(t, "10.0.0.1", "10.0.0.3", exporter="10.9.0.5", nbytes=500, fp=with_tos, tos=0, seq=2),  # BE
        _raw(t, "10.0.0.1", "10.0.0.4", exporter="10.9.0.5", nbytes=200, fp=with_tos, tos=46 << 2, seq=3),
        _raw(t, "10.0.0.1", "10.0.0.5", exporter="10.9.0.5", nbytes=300, fp=0, tos=0, seq=4),         # legacy
    ]


def test_dscp_breakdown_and_filter_never_count_missing_tos_as_best_effort(real_ch, monkeypatch, no_refresh):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc().replace(microsecond=0)
        hour = now.replace(minute=0, second=0) - timedelta(hours=3)
        recent = now - timedelta(minutes=20)
        real_ch.insert(f"{db}.flow_records", _dscp_rows(recent) + _dscp_rows(hour + timedelta(minutes=10)),
                       column_names=RAW_COLS)
        api = _client(VIEW_ONLY)
        # Short window: raw records.
        body = api.get(URL + "/dscp", params={"hours": 1, "with_totals": 1, "envelope": 1}).json()
        data, meta = body["data"], body["meta"]
        assert meta["window"]["source"] == "raw"
        assert {i["dscp"]: i["bytes"] for i in data["items"]} == {0: 800, 46: 200}
        assert data["not_exported"]["bytes"] == 1000 and data["not_exported"]["flows"] == 1
        assert data["total"]["bytes"] == 2000 and data["tos_coverage_pct"] == 50.0
        assert "dscp_not_exported" in [w["code"] for w in meta["warnings"]]
        # The filter matches the same set: forensics and a breakdown with dscp=0.
        spec = FX.SearchSpec(start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
                             snapshot_ms=int(now.timestamp() * 1000) + 60_000,
                             filter=F.parse_flow_filter({"dscp": "0"}), vis=VIEW_ONLY)
        assert asyncio.run(FX.count_matches(spec)).bytes == 800
        proto = api.get(URL + "/protocols", params={"hours": 1, "dscp": 0, "with_totals": 1}).json()
        assert proto["total"]["bytes"] == 800
        # Long window: the QoS rollup (tos_known in its key) agrees for the complete hours.
        frm = (hour - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%SZ")      # over ROLLUP_MIN_SECONDS
        to = (hour + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rolled = api.get(URL + "/dscp", params={"from": frm, "to": to, "with_totals": 1, "envelope": 1}).json()
        assert rolled["meta"]["window"]["source"] in ("rollup", "hybrid")
        assert {i["dscp"]: i["bytes"] for i in rolled["data"]["items"]} == {0: 800, 46: 200}
        assert rolled["data"]["not_exported"]["bytes"] == 1000
        known = real_ch.query(f"SELECT tos_known, sum(bytes) FROM {db}.flow_qos_1h GROUP BY tos_known ORDER BY tos_known"
                              ).result_rows
        assert [(int(k), int(b)) for k, b in known] == [(0, 2000), (1, 1400), (2, 600)]


# ── Exports pinned to the search's snapshot ───────────────────────────────────

def test_parse_snapshot_accepts_the_search_value_and_clamps_the_future():
    now = datetime(2026, 10, 1, 9, 47, 27, tzinfo=timezone.utc)
    ms = int(now.timestamp() * 1000)
    assert FX.parse_snapshot(None, now) == ms and FX.parse_snapshot("", now) == ms
    assert FX.parse_snapshot(str(ms - 70_000), now) == ms - 70_000
    assert FX.parse_snapshot("2026-10-01T09:46:17.250Z", now) == int(datetime(2026, 10, 1, 9, 46, 17, 250000,
                                                                               tzinfo=timezone.utc).timestamp() * 1000)
    assert FX.parse_snapshot(str(ms + 3_600_000), now) == ms           # a snapshot only narrows
    for bad in ("yesterday", "2026-10-01T09:46:17", "1" * 17, "12"):
        with pytest.raises(NetflowError) as exc:
            FX.parse_snapshot(bad, now)
        assert exc.value.status_code == 400


def test_search_returns_the_snapshot_that_exports_take(monkeypatch, no_refresh):
    ch_calls = []

    class CH:
        def query(self, sql, parameters=None, settings=None):
            ch_calls.append((sql, parameters or {}))
            if "system.tables" in sql or "system.parts" in sql:
                return SimpleNamespace(result_rows=[])
            return SimpleNamespace(result_rows=[], summary={})

    monkeypatch.setattr(C, "get_ch_client", lambda: CH())
    api = _client(CAN_EXPORT)
    body = api.get(URL + "/forensics/search", params={"hours": 1, "page_size": 5}).json()
    assert isinstance(body["snapshot_ms"], int)
    assert body["snapshot_at"] == FX._iso_ms(body["snapshot_ms"])
    # The export with that snapshot binds the same received_at bound.
    ch_calls.clear()
    api.get(URL + "/forensics/export", params={"from": body["window"]["from"], "to": body["window"]["to"],
                                               "snapshot": body["snapshot_ms"]})
    snaps = {p.get("fx_snap") for _, p in ch_calls if "fx_snap" in p}
    assert snaps == {body["snapshot_ms"]}


def test_new_search_pins_the_newest_stored_receipt_not_now(monkeypatch):
    # Records still in the collector's insert queue carry receipt times after
    # the newest stored one; a "now" snapshot let them join the pinned export
    # (live test: export 96,041 rows for a search that counted 95,601).
    now_ms = FX._snapshot_ms()
    seen = []

    async def stored(newest):
        async def fake_run_query(sql, params=None, **kw):
            seen.append((sql, dict(params or {})))
            if isinstance(newest, Exception):
                raise newest
            return SimpleNamespace(result_rows=[(newest,)])
        return fake_run_query

    scoped = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                            permissions=frozenset({"netflow.view"}))
    for newest, vis, want in (
        (now_ms - 4_000, VIEW_ONLY, now_ms - 4_000),                 # stored receipts lag the queue
        (now_ms - 4_000, scoped, now_ms - 4_000),
        (0, VIEW_ONLY, None),                                        # nothing stored yet: now
        (now_ms + 60_000, VIEW_ONLY, None),                          # never later than now
        (NetflowError(503, "down", code="unavailable"), VIEW_ONLY, None),
    ):
        monkeypatch.setattr(FX, "run_query", asyncio.run(stored(newest)))
        got = asyncio.run(FX._visible_snapshot_ms(vis))
        if want is None:
            assert now_ms <= got <= FX._snapshot_ms()
        else:
            assert got == want
    scoped_sql, scoped_params = seen[1]
    assert "flow_exporter_minutes" in scoped_sql and "WHERE" in scoped_sql
    assert any(list(v) == ["10.9.0.1"] for v in scoped_params.values() if isinstance(v, (list, tuple)))
    assert "WHERE" not in seen[0][0]


# ── Legacy CSV is an export ───────────────────────────────────────────────────

def test_legacy_csv_needs_export_permission_and_is_audited(monkeypatch, no_refresh):
    class CH:
        def query(self, sql, parameters=None, settings=None):
            if "system.tables" in sql or "system.parts" in sql:
                return SimpleNamespace(result_rows=[])
            return SimpleNamespace(result_rows=[], summary={})

    monkeypatch.setattr(C, "get_ch_client", lambda: CH())
    audits = []

    async def audit(db, **kw):
        audits.append(kw)

    monkeypatch.setattr(FX, "write_audit_log", audit)
    q = {"hours": 1, "talker": "10.77.1.1", "format": "csv", "limit": 2000}
    viewer = _client(VIEW_ONLY)
    r = viewer.get(URL + "/forensics", params=q)
    assert r.status_code == 403 and not audits
    assert viewer.get(URL + "/forensics", params={"hours": 1, "talker": "10.77.1.1"}).status_code == 200
    exporter = _client(CAN_EXPORT)
    r = exporter.get(URL + "/forensics", params=q)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert len(audits) == 1 and audits[0]["action"] == "netflow.export"
    assert audits[0]["metadata"]["mode"] == "legacy" and audits[0]["metadata"]["rows"] == 0


# ── NSEL create/deny events ───────────────────────────────────────────────────

def test_fw_event_flows_filter_on_raw_and_rollups():
    f = F.parse_flow_filter({"fw_event": "flows"})
    assert f.fw_event == "flows"
    raw_sql, _ = F.compile_filter(f, S.RAW, VIEW_ONLY, volume_guard=False)
    assert "fw_event NOT IN (1, 3)" in raw_sql
    for table in (S.CONVERSATIONS_1H, S.QOS_1H, S.INTERFACE_5M):
        caps = S.table_caps(table)
        assert caps.can_serve(f), table
        sql, _ = F.compile_filter(f, table, VIEW_ONLY)
        assert "fw_event" not in sql, table
    # Single events still need raw records.
    assert not S.table_caps(S.CONVERSATIONS_1H).can_serve(F.parse_flow_filter({"fw_event": "denied"}))
    with pytest.raises(NetflowError):
        F.parse_flow_filter({"fw_event": "everything"})


def test_firewall_events_warning_counts_and_names_the_exclusion():
    counted = FX.CountResult(total=18456, exact=True, bytes=1, packets=1, sampled=0, max_sampling=1,
                             fw_created=4514, fw_denied=4088)
    note = FX.firewall_events_warning(counted, F.FlowFilter())
    assert "4,514 connection-created and 4,088 denied" in note and "fw_event=flows" in note
    assert FX.firewall_events_warning(counted, F.parse_flow_filter({"fw_event": "flows"})) is None
    none = FX.CountResult(total=5, exact=True, bytes=1, packets=1, sampled=0, max_sampling=1)
    assert FX.firewall_events_warning(none, F.FlowFilter()) is None
    partial = FX.CountResult(total=5, exact=False, bytes=1, packets=1, sampled=0, max_sampling=1, fw_denied=3)
    assert FX.firewall_events_warning(partial, F.FlowFilter()).startswith("At least 3 denied")


def test_forensics_counts_events_and_flows_matches_the_cards(real_ch, monkeypatch, no_refresh):
    with flow_db(real_ch) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        now = C.now_utc().replace(microsecond=0)
        t = now - timedelta(minutes=10)
        nsel = TRACKED | 0b111101 | (1 << 13)
        rows = [_raw(t, "10.6.0.1", "10.6.0.2", exporter="10.9.0.6", fp=nsel, fw=2, nbytes=900, seq=i)
                for i in range(4)]                                                         # teardown
        rows += [_raw(t, "10.6.0.1", "10.6.0.3", exporter="10.9.0.6", fp=nsel, fw=1, nbytes=0, packets=0,
                      seq=10 + i) for i in range(3)]                                       # created
        rows += [_raw(t, "10.6.0.9", "10.6.0.3", exporter="10.9.0.6", fp=nsel, fw=3, nbytes=0, packets=0,
                      seq=20 + i) for i in range(2)]                                       # denied
        real_ch.insert(f"{db}.flow_records", rows, column_names=RAW_COLS)
        api = _client(VIEW_ONLY)
        body = api.get(URL + "/forensics/search", params={"hours": 1, "page_size": 50, "envelope": 1}).json()
        data = body["data"]
        assert data["total"] == 9 and data["fw_events"] == {"created": 3, "denied": 2, "exact": True}
        assert "firewall_events_included" in [w["code"] for w in body["meta"]["warnings"]]
        flows_only = api.get(URL + "/forensics/search", params={"hours": 1, "page_size": 50, "fw_event": "flows",
                                                               "envelope": 1}).json()
        card = api.get(URL + "/protocols", params={"hours": 1, "with_totals": 1}).json()
        assert flows_only["data"]["total"] == card["total"]["flows"] == 4
        assert "firewall_events_included" not in [w["code"] for w in flows_only["meta"]["warnings"]]


# ── ICMP type/code are not ports ──────────────────────────────────────────────

def _fx_row(**values) -> tuple:
    d = {
        "ts_ms": int(T0.timestamp() * 1000) - 1000, "rx_ms": int(T0.timestamp() * 1000) - 900, "rk": 1, "sv": 0,
        "exporter": "192.0.2.1", "observation_domain": 0, "export_protocol": "netflow9", "flow_version": 9,
        "collector_id": "c1", "src": "10.20.4.77", "dst": "10.30.9.200", "src_port": 0, "dst_port": 2048,
        "protocol": 1, "ip_version": 4, "tcp_flags": 0, "tos": 0, "input_if": 3, "output_if": 4,
        "packets": 10, "bytes": 1500, "sampling_interval": 1, "duration_ms": 250, "src_as": 0, "dst_as": 0,
        "src_mask": 24, "dst_mask": 24, "next_hop": "0.0.0.0", "flow_direction": 255, "app_id": 0, "fw_event": 0,
        "xlate_src": "::", "xlate_dst": "::", "xlate_src_port": 0, "xlate_dst_port": 0, "flow_end_reason": 0,
        "fields_present": TRACKED | 0b111111,
    }
    d.update(values)
    return tuple(d[name] for name in FX._COLS)


def test_icmp_rows_carry_type_and_code_and_csv_leaves_ports_blank():
    echo = FX.format_row(_fx_row())
    assert echo["icmp_type"] == 8 and echo["icmp_code"] == 0 and echo["service"] == "Echo request"
    unreach = FX.format_row(_fx_row(protocol=58, ip_version=6, src="2001:db8::1", dst="2001:db8::2",
                                    dst_port=(1 << 8) | 4))
    assert unreach["icmp_type"] == 1 and unreach["icmp_code"] == 4
    tcp = FX.format_row(_fx_row(protocol=6, src_port=51000, dst_port=443))
    assert tcp["icmp_type"] is None and tcp["icmp_code"] is None
    cols = ("src", "src_port", "dst", "dst_port", "protocol", "icmp_type", "icmp_code")
    assert set(cols) <= set(FX.EXPORT_COLUMN_NAMES)
    lines = FX.csv_lines([echo, tcp], cols, {}).splitlines()
    assert lines[0] == "10.20.4.77,,10.30.9.200,,1,8,0"
    assert lines[1] == "10.20.4.77,51000,10.30.9.200,443,6,,"


# ── Section saved views keep their page settings ──────────────────────────────

def test_saved_view_params_are_kept_and_validated():
    raw = {"v": 2, "kind": "section", "filter": {"exporter": "198.18.211.1"},
           "window": {"mode": "fixed", "from": "2026-10-01T02:00:00Z", "to": "2026-10-01T03:00:00Z"},
           "sort": "packets", "path": "/netflow/top-talkers", "params": {"by": "dst", "dir": "asc", "col": "packets"}}
    out, issues, _, _ = SV.normalize_query(raw, kind="section", strict=True)
    assert out["params"] == {"by": "dst", "col": "packets", "dir": "asc"} and out["sort"] == "packets"
    assert not issues
    side, _, _, _ = SV.normalize_query({**raw, "path": "/netflow/countries", "params": {"side": "src"}},
                                       kind="section", strict=True)
    assert side["params"] == {"side": "src"}
    for bad in ({"by": "sideways"}, {"evil": "1"}, {"col": "Bytes; DROP"}, ["by", "dst"]):
        with pytest.raises(NetflowError):
            SV.normalize_query({**raw, "params": bad}, kind="section", strict=True)
    # Reads keep what is valid and list the rest.
    lenient, issues, _, _ = SV.normalize_query({**raw, "params": {"by": "dst", "evil": "1"}}, kind="section",
                                               strict=False)
    assert lenient["params"] == {"by": "dst"} and issues


# ── Live API (the integrated stack) ───────────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
TOKEN = os.environ.get("NETFLOW_TEST_TOKEN")
live = pytest.mark.skipif(not (LIVE and TOKEN), reason="set NETFLOW_TEST_API and NETFLOW_TEST_TOKEN")


def _get(path, token=None, **params):
    return requests.get(f"{LIVE}/api/v1/netflow{path}", params=params,
                        headers={"Authorization": f"Bearer {token or TOKEN}"}, timeout=120)


@live
def test_live_crafted_cursor_is_refused_or_costs_one_page():
    first = _get("/forensics/search", hours=24, page_size=1)
    assert first.status_code == 200
    token = first.json().get("next_cursor")
    if not token:
        pytest.skip("not enough records for a second page")
    body = _body(token)
    r = _get("/forensics/search", hours=24, page_size=1, cursor=_enc(dict(body, d=600_000)))
    assert r.status_code == 400 and r.json()["fields"] == {"cursor": "Invalid cursor"}
    started = time.time()
    r = _get("/forensics/search", hours=24, page_size=1, cursor=_enc(dict(body, d=600_000, x=600_000)))
    assert r.status_code == 200 and len(r.json()["items"]) <= 1 and time.time() - started < 30


@live
def test_live_legacy_csv_refused_without_export_permission():
    viewer = os.environ.get("NETFLOW_TEST_TOKEN_VIEWER")
    if not viewer:
        pytest.skip("set NETFLOW_TEST_TOKEN_VIEWER (a role without netflow.export)")
    r = _get("/forensics", token=viewer, hours=1, format="csv", limit=10)
    assert r.status_code == 403
    assert _get("/forensics", token=viewer, hours=1, limit=1).status_code == 200


@live
def test_live_export_matches_the_search_snapshot():
    # The busiest exporter small enough for a streamed export.
    status = _get("/exporters", hours=1).json()
    rows = status if isinstance(status, list) else status.get("items", [])
    candidates = [x.get("exporter_ip") or x.get("ip") for x in rows if (x.get("flows") or 0) < FX.EXPORT_STREAM_MAX_ROWS]
    s = None
    for ip in [c for c in candidates if c][:5] or [None]:
        q = {"hours": 1, "page_size": 1, **({"exporter": ip} if ip else {})}
        s = _get("/forensics/search", **q).json()
        if s["total"] and s["total"] <= FX.EXPORT_STREAM_MAX_ROWS and s["total_exact"]:
            break
        s = None
    if s is None:
        pytest.skip("needs a search small enough for a streamed export")
    time.sleep(2)                                    # records keep arriving after the snapshot
    r = _get("/forensics/export", **{"from": s["window"]["from"], "to": s["window"]["to"], "tz": "UTC",
                                     "snapshot": s["snapshot_ms"],
                                     **({"exporter": q["exporter"]} if "exporter" in q else {})})
    assert r.status_code == 200 and int(r.headers["X-Total-Count"]) == s["total"]
    assert r.text.count("\n") - 1 == s["total"]


@live
def test_live_dscp_reports_records_without_tos_apart():
    body = _get("/dscp", hours=24, with_totals=1, envelope=1).json()
    data = body["data"]
    ne = data.get("not_exported")
    total = data["total"]["bytes"] if data["total"] else 0
    assert sum(i["bytes"] for i in data["items"]) + (ne["bytes"] if ne else 0) == total
    if ne:
        assert "dscp_not_exported" in [w["code"] for w in body["meta"]["warnings"]]
