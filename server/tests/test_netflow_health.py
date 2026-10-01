"""NetFlow collector & exporter health (WP B6): services/netflow_health.py and
api/v1/netflow/health.py.

Findings and features covered: NF-01 / X-04 / X-24 (exporter receipt state on
the appliance clock, independent of page filters and windows, with a learned
or configured expected interval), FE-01 / CR-05 (collector process and
per-exporter decode, loss, clock, field and template health), FE-12 / CR-16
(exporter settings and the duplicate-observation estimate), CR-06 (onboarding
live check and setup information).

Layers:
  * pure rules (thresholds, states, learned cadence, loss, clock, field
    capabilities, status rows, state transitions, onboarding checks) — always;
  * in-process HTTP over fakes (visibility filtering, 404 for out-of-scope
    exporters, parameter validation) — always;
  * scratch ClickHouse (NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py):
    receipts, collector statistics, templates and raw records in a disposable
    database, read by the real queries;
  * live API with its database (NETFLOW_B6_API, e.g. http://127.0.0.1:8116,
    and NETFLOW_B6_PG, e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/zenplus):
    status shape and filter independence, collector health, the onboarding
    check, settings CRUD with permissions and audit, scoped visibility.
    Test users, devices and settings are removed afterwards.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.api.v1.netflow as netflow  # noqa: E402
from app.api.v1.netflow import access, common as C, filters as F, health as H  # noqa: E402
from app.api.v1.netflow.access import ExporterOwner, FlowVisibility  # noqa: E402
from app.api.v1.netflow.sql import ip_param  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.security import get_current_user  # noqa: E402
from app.services import netflow_health as nh  # noqa: E402

UTC = timezone.utc
URL = "/api/v1/netflow"
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


# ── Thresholds and states ─────────────────────────────────────────────────────

def test_thresholds_follow_configured_then_learned_then_default():
    d = nh.thresholds_for(None, None)
    assert (d.expected_s, d.basis, d.late_after_s, d.silent_after_s) == (60, "default", 180, 900)
    learned = nh.thresholds_for(None, 300.0)
    assert (learned.expected_s, learned.basis, learned.late_after_s, learned.silent_after_s) == (300, "learned", 660, 3000)
    # A configured interval wins over the learned cadence.
    conf = nh.thresholds_for(30, 300.0)
    assert (conf.expected_s, conf.basis, conf.late_after_s, conf.silent_after_s) == (30, "configured", 120, 900)
    # Learned cadences below a minute are treated as a minute (receipt minutes are whole minutes).
    assert nh.thresholds_for(None, 12.0).expected_s == 60
    # The silent threshold always leaves room after the late one.
    big = nh.thresholds_for(3600, None)
    assert big.late_after_s == 7260 and big.silent_after_s == 36000 and big.silent_after_s > big.late_after_s


@pytest.mark.parametrize("age, state", [(None, "never"), (0, "receiving"), (180, "receiving"), (180.5, "late"),
                                        (900, "late"), (901, "silent"), (86400 * 30, "silent")])
def test_exporter_state_boundaries(age, state):
    assert nh.exporter_state(age, nh.DEFAULT_THRESHOLDS) == state


def test_learned_interval_is_the_p95_gap_and_needs_enough_samples():
    assert nh.learned_interval([60] * 9) is None, "fewer than 10 gaps: not learned"
    assert nh.learned_interval([60] * 19 + [600]) == 60, "one long gap in 20 is outside the p95"
    assert nh.learned_interval([300] * 30) == 300
    assert nh.learned_interval([0, -5, None] + [120] * 10) == 120, "zero and negative gaps are ignored"
    gaps = [60] * 90 + [120] * 10
    assert nh.learned_interval(gaps) == 120


def test_loss_counts_missing_over_expected_and_unknown_is_none():
    assert nh.loss_pct(0, 0) is None, "nothing with a sequence number received: unknown, not 0 %"
    assert nh.loss_pct(None, None) is None
    assert nh.loss_pct(0, 100) == 0.0
    assert nh.loss_pct(5, 95) == 5.0
    assert nh.loss_pct(1, 2) == 33.33


def test_clock_assessment_sign_convention():
    assert nh.clock_assessment(None) == (None, None)
    assert nh.clock_assessment(59_999) == (False, None)
    assert nh.clock_assessment(-180_000) == (True, "ahead"), "received - header < 0: the exporter clock is ahead"
    assert nh.clock_assessment(120_000) == (True, "behind")


def test_collector_health_url_is_configurable(monkeypatch):
    monkeypatch.setenv(nh.COLLECTOR_HEALTH_URL_ENV, "http://127.0.0.1:18091/health")
    assert nh.collector_health_url() == "http://127.0.0.1:18091/health"
    monkeypatch.setenv(nh.COLLECTOR_HEALTH_URL_ENV, "  ")
    assert nh.collector_health_url().endswith("/health")


def test_parse_ts_accepts_go_nanoseconds_and_rejects_zero_time():
    assert nh._parse_ts("2026-10-01T11:59:58.123456789Z") == datetime(2026, 10, 1, 11, 59, 58, 123456, tzinfo=UTC)
    assert nh._parse_ts("2026-10-01T14:59:58+03:00") == datetime(2026, 10, 1, 11, 59, 58, tzinfo=UTC)
    assert nh._parse_ts(datetime(2026, 10, 1, 12, 0)) == datetime(2026, 10, 1, 12, 0, tzinfo=UTC), "naive = UTC"
    assert nh._parse_ts("0001-01-01T00:00:00Z") is None, "Go zero time means never"
    assert nh._parse_ts("") is None and nh._parse_ts(None) is None and nh._parse_ts("garbage") is None


def test_live_exporters_merge_streams_on_the_canonical_address():
    live = nh.CollectorLive(True, {"status": "ok", "exporters": [
        {"exporter": "::ffff:10.0.0.1", "export_protocol": "netflow9", "last_datagram_at": "2026-10-01T11:59:00Z",
         "clock_offset_ms": 10},
        {"exporter": "10.0.0.1", "export_protocol": "ipfix", "last_datagram_at": "2026-10-01T11:59:30Z",
         "clock_offset_ms": -90000},
        {"exporter": "not-an-ip"},
    ]}, None, NOW)
    out = nh.live_exporters(live)
    assert set(out) == {"10.0.0.1"}
    assert out["10.0.0.1"]["last_datagram_at"] == datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)
    assert out["10.0.0.1"]["clock_offset_ms"] == -90000, "offset of the most recent stream"
    assert len(out["10.0.0.1"]["streams"]) == 2
    assert nh.live_exporters(nh.CollectorLive(False, None, "timeout", NOW)) == {}


# ── Field capabilities and names ──────────────────────────────────────────────

def test_capability_status_distinguishes_all_some_none_na_unknown():
    counts = {"tcp_flags": (10, 10), "l4_ports": (10, 4), "interfaces": (10, 0), "direction": (0, 0)}
    caps = {c["key"]: c for c in nh.capability_status(counts, tracked=10)}
    assert caps["tcp_flags"]["status"] == "all" and caps["tcp_flags"]["pct"] == 100.0
    assert caps["l4_ports"]["status"] == "some" and caps["l4_ports"]["pct"] == 40.0
    assert caps["interfaces"]["status"] == "none"
    assert caps["direction"]["status"] == "n/a" and caps["direction"]["pct"] is None
    assert caps["tcp_flags"]["recommended"] and not caps["direction"]["recommended"]
    legacy = nh.capability_status({"tcp_flags": (10, 0)}, tracked=0)
    assert {c["status"] for c in legacy} == {"unknown"}, "legacy rows say nothing about presence"
    sql = nh.capability_sql()
    assert "bitTest(fp, 31)" in sql and "tcp_flags_n" in sql and "vlan_y" in sql


def test_field_names_and_bits():
    assert nh.field_name(6) == "tcpControlBits"
    assert nh.field_name(61) == "flowDirection"
    assert nh.field_name(40005) == "NF_F_FW_EXT_EVENT (NSEL)"
    assert nh.field_name(12345) == "field 12345"
    assert nh.field_name(56701, 9) == "enterprise 9 field 56701"
    assert nh.decode_bits((1 << 31) | 0b1001) == ["tcp_flags", "input_if", "tracked"]
    assert {1, 2, 6, 7, 8, 10, 11, 12, 14, 61, 95}.issubset(nh.DECODED_IES)


# ── Status rows ───────────────────────────────────────────────────────────────

def _rc(**kw):
    base = {"last_rx": NOW - timedelta(seconds=20), "first_rx": NOW - timedelta(days=3), "rec15": 900, "rec60": 3600,
            "tcp60": 1000, "flagged60": 1000, "legacy60": 0, "sampled60": 0, "maxsamp60": 1, "lagmin15": 1000,
            "domains": [0], "v660": 0}
    return base | kw


def _st(**kw):
    base = {"protocols": ["netflow9"], "domains": [0], "last_dgram": NOW - timedelta(seconds=10), "dg15": 100,
            "dec15": 900, "wr15": 900, "wait15": 0, "unk15": 0, "perr60": 0, "unsup60": 0, "impl60": 0, "q60": 0,
            "gaps60": 0, "units60": 400, "clamped60": 0, "offset15": 120, "samp60": 1, "dg60": 400, "dec60": 3600,
            "wr60": 3600}
    return base | kw


OWNER = ExporterOwner(exporter="10.0.0.1", device_id=str(uuid.uuid4()), hostname="edge-1", source="mapped", tags=frozenset({"a"}))


def _row(ip="10.0.0.1", **kw):
    args = dict(now=NOW, rc=_rc(), st=_st(), live=None, learned=None, tpl=None, setting=None, owner=OWNER,
                device={"status": "up"}, since=None)
    args.update(kw)
    return nh.build_row(ip, **args)


def test_healthy_row_has_the_contract_fields():
    r = _row()
    for key in ("exporter", "display_name", "device_id", "device_hostname", "device_status", "last_received_at",
                "receipt_age_s", "state", "records_15m", "expected_interval_s", "clock_offset_ms", "clock_suspect",
                "export_protocols", "loss_pct_1h", "flags_exported"):
        assert key in r, key
    assert r["state"] == "receiving" and r["receipt_age_s"] == 10.0, "the newest of record and datagram receipt"
    assert r["device_status"] == "up" and r["mapping_source"] == "mapped"
    assert r["loss_pct_1h"] == 0.0 and r["flags_exported"] is True and r["flag_coverage_pct"] == 100.0
    assert r["issues"] == [] and r["clock_suspect"] is False
    assert r["last_received_at"].endswith("Z")


def test_datagrams_waiting_for_a_template_still_prove_receipt():
    r = _row(rc=None, st=_st(dec15=0, wr15=0, wait15=40, last_dgram=NOW - timedelta(seconds=30)))
    assert r["state"] == "receiving" and r["records_15m"] == 0, "records unknown->0 only because receipt is proven"
    assert [i["code"] for i in r["issues"]] == ["waiting_template"]


def test_live_last_datagram_wins_over_the_minute_statistics():
    live = {"last_datagram_at": NOW - timedelta(seconds=2), "clock_offset_ms": -200_000, "streams": [{"export_protocol": "ipfix"}]}
    r = _row(rc=_rc(last_rx=NOW - timedelta(minutes=10)), st=_st(last_dgram=NOW - timedelta(minutes=9)), live=live)
    assert r["receipt_age_s"] == 2.0 and r["state"] == "receiving"
    assert r["clock_offset_ms"] == -200_000 and r["clock_suspect"] is True and r["clock_direction"] == "ahead"
    assert r["export_protocols"] == ["ipfix", "netflow9"]
    assert "clock_skew" in {i["code"] for i in r["issues"]}


def test_future_flow_stamps_reveal_a_clock_ahead_without_statistics():
    r = _row(st=None, rc=_rc(lagmin15=-300_000))
    assert r["clock_offset_ms"] == -300_000 and r["clock_suspect"] is True
    r2 = _row(st=None, rc=_rc(lagmin15=-5_000))
    assert r2["clock_offset_ms"] is None and r2["clock_suspect"] is None, "small negative lag is flow duration, not skew"


def test_late_silent_and_never_with_state_since():
    late = _row(rc=_rc(last_rx=NOW - timedelta(minutes=10)), st=None)
    assert late["state"] == "late"
    assert late["state_since"] == C._to_iso_utc(NOW - timedelta(minutes=10) + timedelta(seconds=180))
    silent = _row(rc=_rc(last_rx=NOW - timedelta(hours=2)), st=None)
    assert silent["state"] == "silent"
    assert silent["state_since"] == C._to_iso_utc(NOW - timedelta(hours=2) + timedelta(seconds=900))
    never = _row(rc=None, st=None, setting={"display_name": "Branch", "expected_interval_s": 300, "role": "core",
                                            "count_direction": "both"}, owner=None)
    assert never["state"] == "never" and never["receipt_age_s"] is None and never["records_15m"] is None
    assert never["display_name"] == "Branch" and never["expected_interval_s"] == 300 and never["interval_basis"] == "configured"
    assert never["role"] == "core" and never["count_direction"] == "both" and never["has_settings"] is True
    assert "unmapped" in {i["code"] for i in never["issues"]}


def test_learned_cadence_keeps_a_five_minute_exporter_receiving():
    rc = _rc(last_rx=NOW - timedelta(minutes=7))
    assert _row(rc=rc, st=None)["state"] == "late", "default 60 s cadence: 7 minutes is late"
    learned = _row(rc=rc, st=None, learned=300.0)
    assert learned["state"] == "receiving" and learned["interval_basis"] == "learned" and learned["late_after_s"] == 660
    configured = _row(rc=rc, st=None, learned=300.0, setting={"expected_interval_s": 60})
    assert configured["state"] == "late", "a configured interval overrides the learned one"


def test_receiving_since_comes_from_the_loop_transition():
    since = {"10.0.0.1": {"state": "receiving", "since": "2026-10-01T09:00:00.000Z"}}
    assert _row(since=since)["state_since"] == "2026-10-01T09:00:00.000Z"
    assert _row(since={"10.0.0.1": {"state": "late", "since": "x"}})["state_since"] is None
    assert _row()["state_since"] is None, "receiving at first sight: when it started is unknown"


def test_issue_detection():
    r = _row(st=_st(unk15=5, perr60=3, clamped60=7, gaps60=10, units60=90, wait15=2))
    codes = {i["code"]: i["severity"] for i in r["issues"]}
    assert codes == {"rejected_allowlist": "danger", "template_gaps": "info", "parse_errors": "warning",
                     "clock_clamped": "warning", "sequence_loss": "warning"}
    assert r["loss_pct_1h"] == 10.0
    no_flags = _row(rc=_rc(tcp60=100, flagged60=0))
    assert no_flags["flags_exported"] is False and "no_tcp_flags" in {i["code"] for i in no_flags["issues"]}
    tpl_only = _row(rc=_rc(tcp60=0, flagged60=0), tpl={"templates": 2, "with_flags": 0, "last_template": NOW})
    assert tpl_only["flags_exported"] is False and tpl_only["flag_coverage_pct"] is None
    v5 = _row(rc=_rc(tcp60=0, flagged60=0), st=_st(protocols=["netflow5"]))
    assert v5["flags_exported"] is True
    unknown = _row(rc=_rc(tcp60=0, flagged60=0), st=_st(protocols=["ipfix"]))
    assert unknown["flags_exported"] is None, "no TCP seen and no template: unknown, not false"


def test_sampling_takes_the_largest_known_rate():
    assert _row(rc=_rc(maxsamp60=100), st=_st(samp60=512))["sampling_interval"] == 512
    assert _row(rc=_rc(maxsamp60=1000), st=_st(samp60=None))["sampling_interval"] == 1000
    assert _row(rc=_rc(maxsamp60=None), st=None)["sampling_interval"] is None


def test_filter_status_hides_out_of_scope_exporters():
    doc = {"server_time": "x", "exporters": [{"exporter": "10.0.0.1"}, {"exporter": "10.0.0.2"}]}
    scoped = FlowVisibility(restricted=True, exporters=frozenset({"10.0.0.2"}), user_id="u", permissions=frozenset())
    assert [r["exporter"] for r in nh.filter_status(doc, scoped)["exporters"]] == ["10.0.0.2"]
    assert nh.filter_status(doc, F.UNRESTRICTED) is doc
    nobody = FlowVisibility(restricted=True, exporters=frozenset(), user_id="u", permissions=frozenset())
    assert nh.filter_status(doc, nobody)["exporters"] == []


def test_state_transitions():
    prev = {"10.0.0.1": {"state": "receiving", "since": "t0"}, "10.0.0.2": {"state": "receiving", "since": None},
            "10.0.0.9": {"state": "silent", "since": "t0"}}
    rows = [{"exporter": "10.0.0.1", "state": "receiving"}, {"exporter": "10.0.0.2", "state": "late"},
            {"exporter": "10.0.0.3", "state": "receiving"}]
    cur, transitions = nh.next_states(prev, rows, "t1")
    assert cur["10.0.0.1"] == {"state": "receiving", "since": "t0"}, "unchanged keeps its start"
    assert cur["10.0.0.2"] == {"state": "late", "since": "t1"}
    assert cur["10.0.0.3"] == {"state": "receiving", "since": None}, "first sight: start unknown"
    assert "10.0.0.9" not in cur, "exporters no longer listed are dropped"
    assert sorted(transitions) == [("10.0.0.2", "receiving", "late"), ("10.0.0.3", None, "receiving")]


def test_series_granularity():
    assert nh.series_granularity(timedelta(hours=1)) == 60
    assert nh.series_granularity(timedelta(hours=24)) == 300
    assert nh.series_granularity(timedelta(days=7)) == 3600
    assert nh.series_granularity(timedelta(days=30)) == 4 * 3600


def test_appliance_addresses_prefer_physical_and_skip_loopback(monkeypatch):
    doc = [
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}, {"local": "::1"}]},
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]},
        {"ifname": "ens18", "addr_info": [{"local": "192.168.8.221"}, {"local": "fe80::1"}, {"local": "2001:db8::5"}]},
    ]
    monkeypatch.setattr(nh.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(doc)))
    out = nh.appliance_addresses()
    assert [a["address"] for a in out] == ["192.168.8.221", "2001:db8::5", "172.17.0.1"]
    assert out[-1]["virtual"] is True and out[0]["interface"] == "ens18"

    def boom(*a, **k):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(nh.subprocess, "run", boom)
    assert nh.appliance_addresses() == []


# ── Onboarding checks (CR-06) ─────────────────────────────────────────────────

LIVE_OK = {"reachable": True, "status": "ok", "listening": True, "listen": ":2055", "reasons": []}


def _fields(**caps):
    keys = [c[0] for c in nh.CAPABILITIES]
    counts = {k: caps.get(k, (100, 100)) for k in keys}
    return {"capabilities": nh.capability_status(counts, tracked=100), "max_sampling": 1}


def test_check_waiting_for_the_first_datagram():
    row = _row(rc=None, st=None, owner=None)
    checks = H.build_checks(row, {"capabilities": nh.capability_status({}, 0), "max_sampling": None}, [], LIVE_OK, None)
    by = {c["id"]: c for c in checks}
    assert by["collector"]["status"] == "pass" and "UDP 2055" in by["collector"]["detail"]
    assert by["datagrams"]["status"] == "pending" and "10.0.0.1" in by["datagrams"]["hint"]
    assert "sFlow: 6343" in by["datagrams"]["hint"]
    assert by["templates"]["status"] == "pending" and by["records"]["status"] == "pending"
    assert by["fields"]["status"] == "pending", "field, sampling and clock checks wait for records as one line"
    assert not any(k.startswith("field_") for k in by) and "clock" not in by
    assert by["mapping"]["status"] == "warn"
    assert H.overall_check_status(checks) == "waiting"
    custom = H.build_checks(row, None, [], {**LIVE_OK, "listen": "[::]:12055", "sflow_listen": ":16343"}, None)
    hint = {c["id"]: c for c in custom}["datagrams"]["hint"]
    assert "UDP 12055 (sFlow: 16343)" in hint, "the collector's own ports, not the defaults"


def test_check_ready_exporter():
    row = _row()
    tpl = [{"is_options": False}, {"is_options": True}]
    checks = H.build_checks(row, _fields(), tpl, LIVE_OK, {"interfaces": 4, "with_speed": 2})
    by = {c["id"]: c for c in checks}
    assert by["templates"]["status"] == "pass" and "1 data template(s) and 1 options" in by["templates"]["detail"]
    assert by["records"]["status"] == "pass"
    assert all(by[f"field_{k}"]["status"] == "pass" for k in ("tcp_flags", "l4_ports", "interfaces", "flow_times", "direction"))
    assert by["clock"]["status"] == "pass" and by["loss"]["status"] == "pass" and by["mapping"]["status"] == "pass"
    assert by["sampling"]["status"] == "pass"
    assert H.overall_check_status(checks) == "ready"


def test_check_failures_and_gaps():
    row = _row(st=_st(dec15=0, wait15=10, unk15=3), rc=None, live={"last_datagram_at": NOW, "clock_offset_ms": 90_000,
                                                               "streams": []})
    checks = H.build_checks(row, _fields(tcp_flags=(100, 0), direction=(100, 0), l4_ports=(100, 50)), [],
                            {"reachable": False}, {"interfaces": 0, "with_speed": 0})
    by = {c["id"]: c for c in checks}
    assert by["collector"]["status"] == "warn"
    assert by["allowlist"]["status"] == "fail"
    assert by["templates"]["status"] == "fail" and by["records"]["status"] == "fail"
    assert by["field_tcp_flags"]["status"] == "warn" and "IE 6" in by["field_tcp_flags"]["hint"]
    assert by["field_direction"]["status"] == "info", "direction is optional"
    assert by["field_l4_ports"]["status"] == "warn" and "50%" in by["field_l4_ports"]["detail"]
    assert by["clock"]["status"] == "fail" and "behind" in by["clock"]["detail"]
    assert by["mapping"]["status"] == "warn" and "no SNMP interface inventory" in by["mapping"]["detail"]
    assert H.overall_check_status(checks) == "failing"


def test_check_v5_needs_no_templates_and_reports_sampling():
    row = _row(st=_st(protocols=["netflow5"]))
    checks = H.build_checks(row, {**_fields(), "max_sampling": 100}, [], LIVE_OK, {"interfaces": 1, "with_speed": 1})
    by = {c["id"]: c for c in checks}
    assert by["templates"]["status"] == "na"
    assert by["sampling"]["status"] == "info" and "1-in-100" in by["sampling"]["detail"]
    not_listening = H.build_checks(row, _fields(), [], {**LIVE_OK, "listening": False}, None)
    assert {c["id"]: c for c in not_listening}["collector"]["status"] == "fail"


# ── In-process HTTP over fakes ────────────────────────────────────────────────

USER = SimpleNamespace(id=uuid.uuid4(), username="viewer", role="viewer", is_active=True, scope_tags=[])
SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1"}), user_id="u1",
                        permissions=frozenset({"netflow.view"}))


class FakeDB:
    async def execute(self, statement, params=None):
        return SimpleNamespace(all=lambda: [], first=lambda: None, scalar=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []),
                               mappings=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))

    def begin_nested(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    """Stands in for ClickHouse in the in-process tests: never reaches a server."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}))
        if "system.tables" in sql:
            return FakeResult([("flow_records", "MergeTree TTL toDateTime(timestamp) + toIntervalDay(30)")])
        return FakeResult([])

    def command(self, sql, parameters=None):
        return None


@pytest.fixture(autouse=True)
def fake_ch(monkeypatch):
    """No test in this module talks to a real ClickHouse unless the scratch
    fixture (``ch``) replaces this client with its disposable database."""
    fake = FakeCH()
    monkeypatch.setattr(C, "get_ch_client", lambda: fake)
    C._retention_cache.update(value=None, at=0.0)
    yield fake
    C._retention_cache.update(value=None, at=0.0)


def make_client(vis: FlowVisibility, *, manage: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(netflow.router, prefix="/api/v1")

    async def fake_db():
        yield FakeDB()

    async def user():
        return USER

    async def visibility():
        return vis

    async def manager():
        if not manage:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return USER

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[access.require_view] = user
    app.dependency_overrides[access.require_manage] = manager
    app.dependency_overrides[access.flow_visibility] = visibility
    return TestClient(app)


STATUS_DOC = {
    "server_time": "2026-10-01T12:00:00.000Z",
    "thresholds": {"stale_s": 180, "silent_s": 900, "default_expected_s": 60},
    "collector": {"reachable": True, "status": "ok", "error": None},
    "exporters": [{"exporter": "10.9.0.1", "state": "receiving"}, {"exporter": "10.9.0.2", "state": "silent"}],
    "warnings": [{"code": "collector_unreachable", "message": "The collector did not answer"}],
}


@pytest.fixture
def fake_status(monkeypatch):
    calls = []

    async def cached(db, *, refresh=False):
        calls.append(refresh)
        return STATUS_DOC

    monkeypatch.setattr(nh, "cached_status", cached)
    return calls


def test_status_is_filtered_by_visibility_and_discloses_warnings(fake_status):
    r = make_client(SCOPED).get(URL + "/exporters/status", params={"envelope": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["exporter"] for e in body["data"]["exporters"]] == ["10.9.0.1"]
    assert body["meta"]["restricted"] is True
    assert "collector_unreachable" in {w["code"] for w in body["meta"]["warnings"]}
    legacy = make_client(F.UNRESTRICTED).get(URL + "/exporters/status").json()
    assert set(legacy) >= {"server_time", "thresholds", "exporters"} and len(legacy["exporters"]) == 2


def test_status_ignores_page_filters_and_windows(fake_status):
    """X-04: drill-down filters and windows never change exporter health."""
    c = make_client(F.UNRESTRICTED)
    plain = c.get(URL + "/exporters/status").json()
    filtered = c.get(URL + "/exporters/status", params={"protocol": 6, "talker": "10.1.1.1", "hours": 1,
                                                         "tcpflag": "rst", "hour": 3}).json()
    assert plain == filtered


@pytest.mark.parametrize("path", ["/exporters/10.9.0.2/health", "/exporters/10.9.0.2/check"])
def test_out_of_scope_exporter_details_are_404(path):
    r = make_client(SCOPED).get(URL + path, params={"envelope": 1})
    assert r.status_code == 404, r.text


def test_malformed_exporter_is_400():
    assert make_client(F.UNRESTRICTED).get(URL + "/exporters/not-an-ip/check").status_code == 400


@pytest.mark.parametrize("path, params", [("/collector/health", {"hours": 0}), ("/collector/health", {"hours": 721}),
                                          ("/exporters/10.9.0.1/health", {"hours": 9999}),
                                          ("/exporters/counting", {"minutes": 4}),
                                          ("/exporters/counting", {"minutes": 61})])
def test_window_parameters_are_validated(path, params):
    r = make_client(F.UNRESTRICTED).get(URL + path, params={"envelope": 1, **params})
    assert r.status_code == 400, (path, r.text)
    assert isinstance(r.json()["detail"], str)


def test_settings_writes_need_manage_and_a_visible_exporter():
    body = {"role": "core", "count_direction": "auto"}
    assert make_client(F.UNRESTRICTED).put(URL + "/exporters/10.9.0.1/settings", json=body).status_code == 403
    assert make_client(F.UNRESTRICTED).delete(URL + "/exporters/10.9.0.1/settings").status_code == 403
    scoped_mgr = make_client(SCOPED, manage=True)
    assert scoped_mgr.put(URL + "/exporters/10.9.0.2/settings", json=body).status_code == 404
    assert scoped_mgr.put(URL + "/exporters/0.0.0.0/settings", json=body).status_code == 400
    assert scoped_mgr.put(URL + "/exporters/224.0.0.1/settings", json=body).status_code == 400


@pytest.mark.parametrize("body", [{"role": "transit"}, {"count_direction": "sideways"}, {"expected_interval_s": 0},
                                  {"expected_interval_s": 86401}, {"display_name": "x" * 121}])
def test_settings_payload_is_validated(body):
    r = make_client(F.UNRESTRICTED, manage=True).put(URL + "/exporters/10.9.0.1/settings", json=body)
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str) and r.json()["fields"], "validation is a 400 naming the field (§9.2)"


def test_settings_blank_strings_become_null():
    s = H.ExporterSettingsIn(display_name="   ", note=" keep ")
    assert s.display_name is None and s.note == "keep" and s.role == "edge" and s.count_direction == "auto"


def test_settings_listing_hides_out_of_scope(monkeypatch):
    async def settings(db):
        return {"10.9.0.1": {"ip": "10.9.0.1", "role": "edge", "count_direction": "auto", "display_name": "a",
                             "expected_interval_s": None, "note": None, "updated_at": NOW, "updated_by": None},
                "10.9.0.2": {"ip": "10.9.0.2", "role": "core", "count_direction": "auto", "display_name": "b",
                             "expected_interval_s": 300, "note": None, "updated_at": NOW, "updated_by": None}}

    monkeypatch.setattr(nh, "load_exporter_settings", settings)
    rows = make_client(SCOPED).get(URL + "/exporters/settings").json()
    assert [r["exporter"] for r in rows] == ["10.9.0.1"]
    assert make_client(F.UNRESTRICTED).get(URL + "/exporters/settings").json()[1]["expected_interval_s"] == 300


def test_setup_info_reports_collector_ports(monkeypatch):
    async def live(**kw):
        return nh.CollectorLive(True, {"status": "ok", "listen": ":12055", "sflow_listen": "[::]:16343"}, None, NOW)

    monkeypatch.setattr(nh, "fetch_collector_live", live)
    monkeypatch.setattr(nh, "appliance_addresses", lambda: [{"address": "192.0.2.5", "interface": "eth0",
                                                              "family": 4, "virtual": False}])
    d = make_client(F.UNRESTRICTED).get(URL + "/setup/info", params={"envelope": 1}).json()["data"]
    assert d["collector"]["netflow_port"] == 12055 and d["collector"]["sflow_port"] == 16343
    assert d["collector"]["port_source"] == "collector" and d["collector"]["sflow_listening"] is True
    assert d["addresses"][0]["address"] == "192.0.2.5"
    assert d["recommended"]["active_timeout_s"] == 60 and d["recommended"]["template_refresh_s"] == 60

    async def down(**kw):
        return nh.CollectorLive(False, None, "unreachable", NOW)

    monkeypatch.setattr(nh, "fetch_collector_live", down)
    d = make_client(F.UNRESTRICTED).get(URL + "/setup/info").json()
    assert d["collector"]["netflow_port"] == 2055 and d["collector"]["sflow_port"] == 6343
    assert d["collector"]["port_source"] == "default" and d["collector"]["sflow_listening"] is None


def test_collector_live_is_scoped(monkeypatch):
    live = nh.CollectorLive(True, {"status": "ok", "counters": {"datagrams": 5}, "exporters": [
        {"exporter": "10.9.0.1", "last_datagram_at": "2026-10-01T11:59:00Z"},
        {"exporter": "10.9.0.2", "last_datagram_at": "2026-10-01T11:59:00Z"}],
        "started_at": "2026-10-01T11:00:00Z", "server_time": "2026-10-01T12:00:00Z"}, None, NOW)
    out = H._live_summary(live, SCOPED)
    assert [e["exporter"] for e in out["exporters"]] == ["10.9.0.1"]
    assert out["counters"] is None, "appliance-wide counters are not shown to scoped users"
    assert out["uptime_s"] == 3600
    assert H._live_summary(live, F.UNRESTRICTED)["counters"] == {"datagrams": 5}
    down = H._live_summary(nh.CollectorLive(False, None, "timeout", NOW), F.UNRESTRICTED)
    assert down["status"] == "unknown" and down["error"] == "timeout" and down["exporters"] == []


# ── Scratch ClickHouse ────────────────────────────────────────────────────────

EXTRA_TABLES = ("flow_collector_stats", "flow_templates")
EM_COLS = ["minute", "exporter_ip", "exporter_ip6", "observation_domain", "records", "bytes", "tcp_records",
           "tcp_flagged_records", "v6_records", "sampled_records", "legacy_records", "max_sampling",
           "first_received_at", "last_received_at", "min_flow_ts", "max_flow_ts", "lag_ms_min", "lag_ms_max"]
STATS_COLS = ["ts", "collector_id", "exporter", "observation_domain", "export_protocol", "datagrams",
              "records_decoded", "records_written", "data_sets_waiting_template", "sequence_gaps", "clock_offset_ms",
              "sampling_interval", "templates_active", "insert_failures", "queue_len", "queue_cap",
              "kernel_rcvbuf_errors", "records_dropped_queue"]
RAW_COLS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_port", "dst_port", "protocol",
            "tcp_flags", "bytes", "packets", "sampling_interval", "first_switched_ms", "last_switched_ms", "input_if",
            "output_if", "fields_present", "ip_version", "export_protocol", "flow_version", "flow_direction"]
TRACKED_ALL = (1 << 31) | 0x23F     # flags, tos, ports, input/output if, flow times, direction
TRACKED_NO_FLAGS = TRACKED_ALL & ~1


@pytest.fixture
def ch(monkeypatch):
    from netflow_scratch_ch import flow_db, scratch_client

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    with flow_db(client) as db:
        for name in EXTRA_TABLES:
            try:
                ddl = client.query(f"SHOW CREATE TABLE zenplus.{name}").result_rows[0][0]
            except Exception:
                pytest.skip(f"scratch zenplus database lacks {name}")
            client.command(ddl.replace("zenplus.", f"{db}."))
        monkeypatch.setattr(C, "get_ch_client", thread_client)
        monkeypatch.setattr(C, "CH_DB", db)
        C._retention_cache.update(value=None, at=0.0)
        yield SimpleNamespace(client=client, db=db)
    C._retention_cache.update(value=None, at=0.0)


def _minutes(ip, start, end, step_s, *, now, domain=0, records=60, lag_ms=1500):
    rows = []
    t = start
    while t < end:
        m = t.replace(second=0, microsecond=0)
        rx = min(t + timedelta(seconds=5), now - timedelta(seconds=1))
        rows.append([m, ipaddress.IPv4Address(ip), ipaddress.IPv6Address("::"), domain, records, records * 800,
                     records // 2, records // 2, 0, 0, 0, 1, rx, rx, rx - timedelta(seconds=30), rx, lag_ms, lag_ms])
        t += timedelta(seconds=step_s)
    return rows


def _patch_pg(monkeypatch, *, settings=None, owners=None):
    async def load_settings(db):
        return settings or {}

    async def mapped(db):
        return set()

    async def resolve(db, ips=None):
        return {ip: o for ip, o in (owners or {}).items() if ips is None or ip in ips}

    async def devices(db, ids):
        return {i: {"status": "up"} for i in ids}

    async def since(db, *, refresh=False):
        return {}

    async def live(**kw):
        return nh.CollectorLive(False, None, "unreachable", nh.now_utc())

    monkeypatch.setattr(nh, "load_exporter_settings", load_settings)
    monkeypatch.setattr(nh, "_mapped_exporters", mapped)
    monkeypatch.setattr(nh, "resolve_exporter_owners", resolve)
    monkeypatch.setattr(nh, "_device_status", devices)
    monkeypatch.setattr(nh, "load_state_since", since)
    monkeypatch.setattr(nh, "fetch_collector_live", live)


def test_receipt_states_from_the_flow_database(ch, monkeypatch):
    now = nh.now_utc().replace(microsecond=0)
    rows = []
    rows += _minutes("10.77.0.1", now - timedelta(hours=3), now, 60, now=now)                      # every minute
    rows += _minutes("10.77.0.2", now - timedelta(hours=20), now - timedelta(minutes=7), 300, now=now)  # every 5 min
    rows += _minutes("10.77.0.7", now - timedelta(hours=20), now - timedelta(minutes=7), 300, now=now)
    rows += _minutes("10.77.0.3", now - timedelta(hours=3), now - timedelta(minutes=10), 60, now=now)  # stopped 10 min ago
    rows += _minutes("10.77.0.4", now - timedelta(hours=5), now - timedelta(hours=2), 60, now=now)    # stopped 2 h ago
    rows += _minutes("10.77.0.8", now - timedelta(days=40), now - timedelta(days=39), 60, now=now)   # beyond the recent window
    ch.client.insert(f"{ch.db}.flow_exporter_minutes", rows, column_names=EM_COLS)
    stats = []
    for i in range(1, 60):           # 59 intervals, all inside the last hour
        ts = (now - timedelta(minutes=i)).replace(second=0)
        stats.append([ts, "test", "10.77.0.1", 0, "netflow9", 19, 60, 60, 0, 1 if i <= 5 else 0, -120_000, 100, 2,
                      0, 0, 0, 0, 0])
    # Data waiting for a template: datagrams but no decoded records, nothing in flow_exporter_minutes.
    stats.append([(now - timedelta(seconds=30)).replace(microsecond=0), "test", "10.77.0.6", 0, "ipfix", 12, 0, 0, 9, 0,
                  50, 0, 0, 0, 0, 0, 0, 0])
    stats.append([(now - timedelta(minutes=1)).replace(second=0), "test", "", 0, "", 0, 0, 0, 0, 0, 0, 0, 0, 2, 7, 1000,
                  3, 4])
    ch.client.insert(f"{ch.db}.flow_collector_stats", stats, column_names=STATS_COLS)
    owner = ExporterOwner(exporter="10.77.0.7", device_id=str(uuid.uuid4()), hostname="core-7", source="ip-match", tags=frozenset())
    _patch_pg(monkeypatch, settings={
        "10.77.0.5": {"ip": "10.77.0.5", "display_name": "Configured, silent", "role": "edge", "count_direction": "auto",
                      "expected_interval_s": None},
        "10.77.0.7": {"ip": "10.77.0.7", "display_name": None, "role": "core", "count_direction": "auto",
                      "expected_interval_s": 60},
        "10.77.0.8": {"ip": "10.77.0.8", "display_name": None, "role": "edge", "count_direction": "auto",
                      "expected_interval_s": None},
    }, owners={"10.77.0.7": owner})

    doc = asyncio.run(nh.compute_status(None, now=now))
    by = {r["exporter"]: r for r in doc["exporters"]}
    assert by["10.77.0.1"]["state"] == "receiving" and by["10.77.0.1"]["interval_basis"] == "learned"
    assert by["10.77.0.1"]["expected_interval_s"] == 60
    assert by["10.77.0.1"]["loss_pct_1h"] == round(5 / (5 + 19 * 59) * 100, 2)
    assert by["10.77.0.1"]["clock_offset_ms"] == -120_000 and by["10.77.0.1"]["clock_suspect"] is True
    assert by["10.77.0.1"]["export_protocols"] == ["netflow9"] and by["10.77.0.1"]["sampling_interval"] == 100
    # A 5-minute exporter is on time at 7 minutes once its cadence is learned ...
    assert by["10.77.0.2"]["state"] == "receiving" and by["10.77.0.2"]["expected_interval_s"] == 300
    # ... unless its configured interval says otherwise.
    assert by["10.77.0.7"]["state"] == "late" and by["10.77.0.7"]["interval_basis"] == "configured"
    assert by["10.77.0.7"]["device_hostname"] == "core-7" and by["10.77.0.7"]["device_status"] == "up"
    assert by["10.77.0.3"]["state"] == "late"
    assert by["10.77.0.4"]["state"] == "silent" and by["10.77.0.4"]["records_15m"] == 0
    assert by["10.77.0.5"]["state"] == "never" and by["10.77.0.5"]["display_name"] == "Configured, silent"
    # An exporter with only old receipts is silent with its real last receipt, not 'never'.
    assert by["10.77.0.8"]["state"] == "silent" and by["10.77.0.8"]["receipt_age_s"] > 39 * 86400
    six = by["10.77.0.6"]
    assert six["state"] == "receiving" and six["records_15m"] == 0 and six["datagrams_15m"] == 12
    assert "waiting_template" in {i["code"] for i in six["issues"]}
    assert {w["code"] for w in doc["warnings"]} == {"collector_unreachable"}
    assert doc["server_time"] == C._to_iso_utc(now)

    one = asyncio.run(nh.exporter_row(None, "10.77.0.1", now=now))
    assert {k: one[k] for k in ("state", "loss_pct_1h", "clock_offset_ms")} == \
        {k: by["10.77.0.1"][k] for k in ("state", "loss_pct_1h", "clock_offset_ms")}

    totals = asyncio.run(nh.collector_totals(now - timedelta(hours=1), now + timedelta(seconds=1), None))
    assert totals["totals"]["datagrams"] == 19 * 59 + 12 and totals["totals"]["exporters"] == 2
    assert totals["process"]["insert_failures"] == 2 and totals["process"]["kernel_rcvbuf_errors"] == 3
    assert totals["process"]["records_dropped_queue"] == 4 and totals["process"]["queue_cap"] == 1000
    scoped = asyncio.run(nh.collector_totals(now - timedelta(hours=1), now + timedelta(seconds=1), ["10.77.0.6"]))
    assert scoped["totals"]["datagrams"] == 12 and scoped["totals"]["data_sets_waiting_template"] == 9
    nobody = asyncio.run(nh.collector_totals(now - timedelta(hours=1), now + timedelta(seconds=1), []))
    assert nobody["totals"]["datagrams"] == 0, "an empty visible set reads nothing"
    series = asyncio.run(nh.stats_series(now - timedelta(hours=1), now + timedelta(seconds=1), 60, ["10.77.0.1"],
                                         process=False))
    assert sum(p["datagrams"] for p in series) == 19 * 59 and sum(p["sequence_gaps"] for p in series) == 5
    streams = asyncio.run(nh.exporter_streams("10.77.0.1", now - timedelta(hours=1), now + timedelta(seconds=1)))
    assert len(streams) == 1 and streams[0]["export_protocol"] == "netflow9" and streams[0]["templates_active"] == 2
    assert streams[0]["loss_pct"] == by["10.77.0.1"]["loss_pct_1h"]


def test_fields_templates_and_interfaces_from_the_flow_database(ch):
    now = nh.now_utc().replace(microsecond=0)
    recs = []
    for i in range(40):
        t = now - timedelta(minutes=2, seconds=i)
        ms = int(t.timestamp() * 1000)
        proto = 6 if i < 30 else 17
        fp = TRACKED_ALL if i < 20 else TRACKED_NO_FLAGS
        recs.append([t, t + timedelta(seconds=1), ipaddress.IPv4Address("10.77.1.1"),
                     ipaddress.IPv4Address("10.0.0.1"), ipaddress.IPv4Address("192.0.2.1"), 40000, 443, proto,
                     0x18, 1000, 2, 1, ms - 500, ms, 3, 4, fp, 4, "ipfix", 10, 0])
    ch.client.insert(f"{ch.db}.flow_records", recs, column_names=RAW_COLS)
    ch.client.insert(f"{ch.db}.flow_templates", [
        ["10.77.1.1", 0, "ipfix", 256, [8, 12, 7, 11, 4, 6, 1, 2, 10, 14, 152, 153, 61, 999], [4] * 14, [0] * 14,
         0, 1, 0, 1, 1, 0, 0, 0, 0, now - timedelta(hours=1), now, "test"],
        ["10.77.1.1", 0, "ipfix", 257, [34, 35, 56701], [4, 1, 2], [0, 0, 9], 1, 0, 0, 0, 0, 0, 1, 0, 0,
         now - timedelta(hours=1), now, "test"],
    ], column_names=["exporter", "observation_domain", "export_protocol", "template_id", "field_ids", "field_lengths",
                     "enterprise_ids", "is_options", "has_tcp_flags", "has_ipv6", "has_ifindex32", "has_direction",
                     "has_app_id", "has_sampling", "has_nat", "has_fw_event", "first_seen", "last_seen", "collector_id"])

    fields = asyncio.run(nh.exporter_fields("10.77.1.1", now))
    caps = {c["key"]: c for c in fields["capabilities"]}
    assert fields["records"] == 40 and fields["tracked"] == 40 and fields["protocols"] == ["ipfix"]
    assert caps["tcp_flags"]["status"] == "some" and caps["tcp_flags"]["pct"] == pytest.approx(20 / 30 * 100, abs=0.1)
    assert caps["l4_ports"]["status"] == "all" and caps["direction"]["status"] == "all"
    assert caps["app_id"]["status"] == "none" and caps["nat"]["status"] == "none"
    assert fields["direction"] == {"ingress": 40, "egress": 0}

    tpls = asyncio.run(nh.exporter_templates("10.77.1.1"))
    assert [t["template_id"] for t in tpls] == [256, 257]
    data = tpls[0]
    names = {f["id"]: (f["name"], f["decoded"]) for f in data["fields"]}
    assert names[6] == ("tcpControlBits", True) and names[61] == ("flowDirection", True)
    assert names[999] == ("field 999", False), "unknown IEs are listed as not used"
    assert data["flags"]["tcp_flags"] is True and data["flags"]["direction"] is True and data["is_options"] is False
    opts = tpls[1]
    assert opts["is_options"] is True and opts["fields"][2]["name"] == "enterprise 9 field 56701"
    assert asyncio.run(nh._template_flags(exporters=["10.77.1.1"], budget=10))["10.77.1.1"]["templates"] == 1

    ifs = asyncio.run(nh.exporter_ifindexes("10.77.1.1", now - timedelta(hours=1), now + timedelta(minutes=5)))
    assert {i["ifindex"] for i in ifs} == {3, 4}
    assert next(i for i in ifs if i["ifindex"] == 3)["rx_bytes"] == pytest.approx(40 * 1000)


def test_duplicate_observation_estimate(ch, monkeypatch):
    """FE-12: the same flows reported by two exporters are counted twice in
    'all' mode; edge mode keeps only the edge exporter."""
    now = nh.now_utc().replace(microsecond=0)
    t0 = (now - timedelta(minutes=3)).replace(second=10)
    recs = []
    for exp, n in (("10.77.2.1", 20), ("10.77.2.2", 20), ("10.77.2.3", 10)):
        for i in range(n):
            ms = int(t0.timestamp() * 1000)
            src = f"10.1.0.{i + 1}" if exp != "10.77.2.3" else f"10.2.0.{i + 1}"
            recs.append([t0, t0 + timedelta(seconds=2), ipaddress.IPv4Address(exp), ipaddress.IPv4Address(src),
                         ipaddress.IPv4Address("192.0.2.9"), 40000 + i, 443, 6, 0x18, 1000, 2, 1, ms - 100, ms, 1, 2,
                         TRACKED_ALL, 4, "netflow9", 9, 0])
    ch.client.insert(f"{ch.db}.flow_records", recs, column_names=RAW_COLS)

    async def policy(db):
        return F.CountingPolicy(excluded=(ip_param("10.77.2.2"),))

    async def settings(db):
        return {"10.77.2.2": {"ip": "10.77.2.2", "role": "core", "count_direction": "auto", "display_name": "core"}}

    monkeypatch.setattr(H, "load_counting_policy", policy)
    monkeypatch.setattr(nh, "load_exporter_settings", settings)
    r = make_client(F.UNRESTRICTED).get(URL + "/exporters/counting", params={"envelope": 1, "minutes": 15})
    assert r.status_code == 200, r.text
    d, meta = r.json()["data"], r.json()["meta"]
    assert d["records"] == 50 and d["flow_keys"] == 30
    assert d["duplicate_observation_pct"] == 40.0 and d["multi_point_keys"] == 20
    assert d["multi_point_key_pct"] == pytest.approx(66.7, abs=0.1)
    assert d["bytes"] == 50_000 and d["bytes_unique_estimate"] == 30_000 and d["byte_inflation_pct"] == pytest.approx(66.7, abs=0.1)
    assert d["modes"]["edge"]["records"] == 30 and d["modes"]["edge"]["records_pct"] == 60.0
    by = {e["exporter"]: e for e in d["exporters"]}
    assert by["10.77.2.2"]["role"] == "core" and by["10.77.2.2"]["counted_in_edge_mode"] is False
    assert by["10.77.2.1"]["shared_pct"] == 100.0 and by["10.77.2.3"]["shared_pct"] == 0.0
    assert d["pairs"] == [{"a": "10.77.2.1", "b": "10.77.2.2", "flow_keys": 20}]
    assert meta["window"]["source"] == "raw"
    # A scoped user's estimate only reads visible exporters.
    only = FlowVisibility(restricted=True, exporters=frozenset({"10.77.2.1"}), user_id="u", permissions=frozenset())
    d2 = make_client(only).get(URL + "/exporters/counting", params={"envelope": 1}).json()["data"]
    assert d2["records"] == 20 and d2["duplicate_observation_pct"] == 0.0 and [e["exporter"] for e in d2["exporters"]] == ["10.77.2.1"]


# ── Live API ──────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B6_API")
PG = os.environ.get("NETFLOW_B6_PG")
ADMIN = (os.environ.get("NETFLOW_B6_USER", "admin"), os.environ.get("NETFLOW_B6_PASS", "admin123"))
PASS = "B6-Health-Passw0rd!"
TAG_A, TAG_B = "b6t-health-a", "b6t-health-b"
USERS = {"b6t_h_viewer": ("viewer", []), "b6t_h_mgr": ("operator", []), "b6t_h_scoped": ("viewer", [TAG_A])}
DEV_A = "b6e00000-0000-4000-8000-0000000000a1"
DEV_B = "b6e00000-0000-4000-8000-0000000000b1"
EXP_A, EXP_B, EXP_SETTINGS = "127.0.0.74", "127.0.0.75", "127.0.0.78"


def _api_up() -> bool:
    if not (API and PG):
        return False
    import requests

    try:
        requests.get(f"{API}/openapi.json", timeout=5)
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="set NETFLOW_B6_API and NETFLOW_B6_PG to a running API of this tree")


def _run_pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(PG)
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


def _login(user, password=PASS):
    import requests

    r = requests.post(f"{API}/api/v1/auth/login", json={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _req(method, headers, path, **kw):
    import requests

    params = kw.pop("params", {})
    if method == "GET":
        params = {"envelope": 1, **params}
    return requests.request(method, f"{API}/api/v1/netflow{path}", headers=headers, timeout=120, params=params, **kw)


@pytest.fixture(scope="module")
def tokens():
    from app.core.security import hash_password

    names = list(USERS)
    ips = [EXP_A, EXP_B, EXP_SETTINGS]
    cleanup = [
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = ANY(:ips)", {"ips": ips}),
        ("DELETE FROM netflow_exporter_settings WHERE host(exporter_ip) = ANY(:ips)", {"ips": ips}),
        ("DELETE FROM audit_logs WHERE resource_type IN ('netflow_exporter_settings', 'netflow_exporter_device') "
         "AND resource_id = ANY(:ips)", {"ips": ips}),
        ("DELETE FROM devices WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))", {"a": DEV_A, "b": DEV_B}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
    ]
    _run_pg(cleanup)
    _run_pg([("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
              "(:u, :m, :h, :r, CAST(:s AS jsonb))",
              {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": role, "s": json.dumps(scope)})
             for u, (role, scope) in USERS.items()]
            + [("INSERT INTO devices (id, hostname, ip_address, device_type, tags, status) VALUES "
                "(CAST(:id AS uuid), :h, CAST(:ip AS inet), 'router', CAST(:t AS jsonb), 'up')",
                {"id": d, "h": h, "ip": ip, "t": json.dumps([t])})
               for d, h, ip, t in ((DEV_A, "b6t-health-a", EXP_A, TAG_A), (DEV_B, "b6t-health-b", EXP_B, TAG_B))])
    out = {u: _login(u) for u in USERS}
    out["admin"] = _login(*ADMIN)
    yield out
    _run_pg(cleanup)


@live
def test_live_status_shape_and_filter_independence(tokens):
    r = _req("GET", tokens["admin"], "/exporters/status")
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert set(d["thresholds"]) >= {"stale_s", "silent_s"} and d["server_time"].endswith("Z")
    for e in d["exporters"]:
        assert e["state"] in nh.STATES
        assert (e["receipt_age_s"] is None) == (e["state"] == "never")
        assert isinstance(e["export_protocols"], list)
        assert e["flags_exported"] in (True, False, None)
    filtered = _req("GET", tokens["admin"], "/exporters/status",
                    params={"protocol": 17, "talker": "203.0.113.9", "hours": 1, "from": "2020-01-01T00:00:00Z"}).json()["data"]
    assert {e["exporter"]: e["state"] for e in filtered["exporters"]} == {e["exporter"]: e["state"] for e in d["exporters"]}


@live
def test_live_collector_health_and_exporter_detail(tokens):
    c = _req("GET", tokens["admin"], "/collector/health", params={"hours": 1})
    assert c.status_code == 200, c.text
    d = c.json()["data"]
    assert d["window"]["granularity_s"] == 60 and "live" in d and "stats" in d and "series" in d
    assert d["live"]["status"] in ("ok", "degraded", "down", "unknown")
    status = _req("GET", tokens["admin"], "/exporters/status").json()["data"]
    receiving = [e for e in status["exporters"] if e["state"] == "receiving"]
    if not receiving:
        pytest.skip("no exporter is sending to the scratch collector")
    ip = receiving[0]["exporter"]
    h = _req("GET", tokens["admin"], f"/exporters/{ip}/health", params={"hours": 1})
    assert h.status_code == 200, h.text
    hd = h.json()["data"]
    assert hd["status"]["exporter"] == ip and hd["status"]["state"] == "receiving"
    assert {c["key"] for c in hd["fields"]["capabilities"]} >= {"tcp_flags", "l4_ports", "interfaces"}
    chk = _req("GET", tokens["admin"], f"/exporters/{ip}/check")
    assert chk.status_code == 200, chk.text
    cd = chk.json()["data"]
    assert cd["overall"] in ("ready", "partial", "failing")
    assert {c["id"]: c["status"] for c in cd["checks"]}["datagrams"] == "pass"
    unknown = _req("GET", tokens["admin"], "/exporters/198.51.100.77/check").json()["data"]
    assert unknown["overall"] == "waiting"
    info = _req("GET", tokens["admin"], "/setup/info").json()["data"]
    assert info["collector"]["netflow_port"] > 0 and info["collector"]["sflow_port"] > 0


@live
def test_live_settings_crud_permissions_and_audit(tokens):
    body = {"display_name": "B6 test core", "role": "core", "count_direction": "ingress", "expected_interval_s": 300,
            "note": "pytest"}
    assert _req("PUT", tokens["b6t_h_viewer"], f"/exporters/{EXP_SETTINGS}/settings", json=body).status_code == 403
    r = _req("PUT", tokens["b6t_h_mgr"], f"/exporters/{EXP_SETTINGS}/settings", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["exporter"] == EXP_SETTINGS and saved["role"] == "core" and saved["expected_interval_s"] == 300
    listed = {s["exporter"]: s for s in _req("GET", tokens["b6t_h_viewer"], "/exporters/settings").json()["data"]}
    assert listed[EXP_SETTINGS]["updated_by_name"] == "b6t_h_mgr"
    row = {e["exporter"]: e for e in _req("GET", tokens["admin"], "/exporters/status").json()["data"]["exporters"]}[EXP_SETTINGS]
    assert row["state"] == "never" and row["display_name"] == "B6 test core" and row["expected_interval_s"] == 300
    assert row["role"] == "core" and row["interval_basis"] == "configured"
    # The scoped user cannot see (or change) an exporter nobody owns.
    assert EXP_SETTINGS not in {s["exporter"] for s in _req("GET", tokens["b6t_h_scoped"], "/exporters/settings").json()["data"]}
    assert _req("DELETE", tokens["b6t_h_viewer"], f"/exporters/{EXP_SETTINGS}/settings").status_code == 403
    assert _req("DELETE", tokens["b6t_h_mgr"], f"/exporters/{EXP_SETTINGS}/settings").status_code == 204
    assert _req("DELETE", tokens["b6t_h_mgr"], f"/exporters/{EXP_SETTINGS}/settings").status_code == 404
    audit = _run_pg([("SELECT action, actor_username, metadata FROM audit_logs WHERE resource_type = "
                      "'netflow_exporter_settings' AND resource_id = :ip ORDER BY created_at", {"ip": EXP_SETTINGS})])[0]
    assert [(a[0], a[1]) for a in audit] == [("update", "b6t_h_mgr"), ("delete", "b6t_h_mgr")]
    md = audit[0][2] if isinstance(audit[0][2], dict) else json.loads(audit[0][2])
    assert md["before"] is None and md["after"]["role"] == "core"


@live
def test_live_scoped_user_sees_only_owned_exporters(tokens):
    scoped = {e["exporter"] for e in _req("GET", tokens["b6t_h_scoped"], "/exporters/status").json()["data"]["exporters"]}
    admin = {e["exporter"] for e in _req("GET", tokens["admin"], "/exporters/status").json()["data"]["exporters"]}
    assert scoped <= admin
    assert EXP_B not in scoped
    assert _req("GET", tokens["b6t_h_scoped"], f"/exporters/{EXP_B}/health").status_code == 404
    assert _req("GET", tokens["b6t_h_scoped"], f"/exporters/{EXP_B}/check").status_code == 404
    meta = _req("GET", tokens["b6t_h_scoped"], "/collector/health").json()["meta"]
    assert meta["restricted"] is True
