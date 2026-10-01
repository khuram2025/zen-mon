"""NetFlow alert rules (CR-07, NF-23): definition, state machine, evaluators, API.

Unit tests run everywhere. Live tests are opt-in:

* ``NETFLOW_TEST_CLICKHOUSE`` -- scratch ClickHouse with schema v2; evaluators
  run against a disposable ``nftest_*`` copy of the flow tables and views.
* ``ZENPLUS_NETFLOW_TEST_PG`` -- a disposable PostgreSQL database with
  migrate-143 (and 138); the state machine raises/resolves real alert rows for
  a *disabled* rule (so no running evaluator touches it) and cleans up.
* ``ZENPLUS_NETFLOW_B8_URL`` + ``ZENPLUS_NETFLOW_B8_PG`` -- a running API on
  that database: rule CRUD, dry-run evaluation and scope governance with real
  scoped users (admin password from ``ZENPLUS_ADMIN_PASS``, default admin123).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.v1.netflow import common as C
from app.api.v1.netflow.access import FlowVisibility
from app.services import alert_rule_scope as rs
from app.services import netflow_alert_service as nfa

NOW = datetime(2026, 9, 30, 12, 0, 30, tzinfo=timezone.utc)


def _rule(metric=nfa.TRAFFIC, operator=">", threshold=1e6, scope=None, params=None, **extra):
    row = {"id": str(uuid.uuid4()), "name": "r", "metric": metric, "operator": operator,
           "threshold": threshold, "flow_scope": scope or {}, "flow_params": params or {}}
    row.update(extra)
    return nfa.rule_from_row(row)


# ── Definition ──────────────────────────────────────────────────────────────

def test_every_metric_has_defaults_that_validate():
    for metric, info in nfa.METRIC_INFO.items():
        op = info["operators"][0]
        thr = 1 if metric == nfa.FINDING else (120 if metric == nfa.SILENT else 50)
        p = nfa.parse_params(metric, op, thr, {})
        assert p.hold == info["defaults"]["hold"]
        stored = p.as_dict(metric)
        # The stored form round-trips.
        assert nfa.parse_params(metric, op, thr, stored) == p


@pytest.mark.parametrize("metric,op,thr,params,message", [
    (nfa.TRAFFIC, "==", 1, {}, "operators"),
    (nfa.ENDPOINT, "<", 1, {}, "operators"),
    (nfa.TRAFFIC, "!=", 100, {}, "band_high"),
    (nfa.TRAFFIC, "!=", 100, {"band_high": 50}, "above the threshold"),
    (nfa.TRAFFIC, ">", 100, {"band_high": 500}, "only applies"),
    (nfa.TRAFFIC, ">", -1, {}, "negative"),
    (nfa.TRAFFIC, ">", 1, {"window_min": 0}, "window_min"),
    (nfa.TRAFFIC, ">", 1, {"window_min": 61}, "window_min"),
    (nfa.TRAFFIC, ">", 1, {"hold": 0}, "hold"),
    (nfa.TRAFFIC, ">", 1, {"hold": 2.5}, "hold"),
    (nfa.TRAFFIC, ">", 1, {"flap_max": 1}, "flap_max"),
    (nfa.TRAFFIC, ">", 1, {"split": "device"}, "split"),
    (nfa.TRAFFIC, ">", 1, {"bogus": 1}, "Unknown NetFlow rule parameter"),
    (nfa.UTIL, ">", 80, {"window_min": 7}, "multiple of 5"),
    (nfa.UTIL, ">", 2000, {}, "between 0 and 1000"),
    (nfa.SILENT, ">", 30, {}, "at least 60"),
    (nfa.ENDPOINT, ">", 1, {"side": "both"}, "side"),
    (nfa.FINDING, "==", 1, {"min_severity": "urgent"}, "min_severity"),
    (nfa.FINDING, "==", 1, {"kinds": "x" * 60}, "kinds"),
])
def test_invalid_parameters_are_rejected_with_a_reason(metric, op, thr, params, message):
    with pytest.raises(nfa.RuleDefinitionError) as exc:
        nfa.parse_params(metric, op, thr, params)
    assert message in str(exc.value)


def test_zero_is_a_value_not_unset():
    p = nfa.parse_params(nfa.TRAFFIC, "<", 0, {"settle_s": 0})
    assert p.settle_s == 0
    rule = _rule(operator="<", threshold=0)
    assert nfa.breaches(rule, value=0.0) is False


@pytest.mark.parametrize("metric,scope,message", [
    (nfa.SILENT, {"app": "DNS"}, "cannot be scoped by: app"),
    (nfa.UTIL, {"talker": "10.0.0.1"}, "cannot be scoped by: talker"),
    (nfa.TRAFFIC, {"fw_event": "denied"}, "cannot be scoped by: fw_event"),
    (nfa.TRAFFIC, {"exporter": "not-an-ip"}, "Invalid flow scope"),
    (nfa.TRAFFIC, {"protocol": "300"}, "Invalid flow scope"),
])
def test_scope_keys_are_validated_never_ignored(metric, scope, message):
    with pytest.raises(nfa.RuleDefinitionError) as exc:
        nfa.parse_scope(metric, scope)
    assert message in str(exc.value)


def test_scope_is_stored_canonically():
    f = nfa.parse_scope(nfa.TRAFFIC, {"exporter": "::ffff:10.1.1.1", "dst_port": 0, "proto": None,
                                      "iface": "70000@10.1.1.1"})
    assert nfa.scope_canonical(f) == {"exporter": "10.1.1.1", "iface": "70000@10.1.1.1", "dst_port": "0"}


def test_util_interface_scope_needs_its_exporter():
    with pytest.raises(nfa.RuleDefinitionError):
        asyncio.run(nfa.validate_definition(nfa.UTIL, ">", 80, {"iface": "3"}, {}))
    scope, params = asyncio.run(nfa.validate_definition(nfa.UTIL, ">", 80, {"iface": "3@10.0.0.1"}, {}))
    assert scope == {"iface": "3@10.0.0.1"} and params["direction"] == "either"


# ── Conditions ──────────────────────────────────────────────────────────────

def test_breach_semantics():
    assert nfa.breaches(_rule(operator=">", threshold=10), value=10.0) is False
    assert nfa.breaches(_rule(operator=">=", threshold=10), value=10.0) is True
    assert nfa.breaches(_rule(operator="<", threshold=10), value=9.9) is True
    band = _rule(operator="!=", threshold=10, params={"band_high": 20})
    assert [nfa.breaches(band, value=v) for v in (9.9, 10, 15, 20, 20.1)] == [True, False, False, False, True]
    assert nfa.breaches(band, value=None) is None          # unknown is not a breach or a clear
    finding = _rule(metric=nfa.FINDING, operator="==", threshold=1)
    assert nfa.breaches(finding, value=1.0) is True and nfa.breaches(finding, value=0.0) is False


def _params(**kw):
    base = dict(hold=2, clear_hold=2, flap_window_min=60, flap_max=4)
    base.update(kw)
    return nfa.RuleParams(**base)


def _run(breaches, params, *, start=NOW, alert_open_after_raise=True):
    """Feed a breach sequence one evaluation per minute; returns actions."""
    s = nfa.SubjectState()
    open_ = False
    actions = []
    for i, b in enumerate(breaches):
        d = nfa.step(s, b, alert_open=open_, params=params, now=start + timedelta(minutes=i))
        s = d.state
        actions.append(d.action)
        if d.action == "raise" and alert_open_after_raise:
            open_ = True
        if d.action == "resolve":
            open_ = False
    return actions, s


def test_hold_for_n_evaluations_before_raising_and_clear_hold_before_resolving():
    actions, s = _run([True, True, True, False, True, False, False], _params(hold=2, clear_hold=2))
    assert actions == ["none", "raise", "none", "none", "none", "none", "resolve"]
    assert s.state == "ok" and s.clear_streak == 2


def test_unknown_readings_neither_raise_nor_resolve():
    actions, s = _run([True, None, True, False, None, None, False], _params(hold=2, clear_hold=2))
    assert actions == ["none", "none", "raise", "none", "none", "none", "resolve"]


def test_flapping_condition_is_held_open_then_resolves_when_calm():
    p = _params(hold=1, clear_hold=1, flap_max=4, flap_window_min=30)
    seq = [True, False, True, False, True, False, False]
    actions, s = _run(seq, p)
    # raise, resolve, raise, then the 4th state change is suppressed: held open.
    assert actions[:4] == ["raise", "resolve", "raise", "flap_hold"]
    assert s.flapping is True
    # Calm for longer than the flap window: the held alert resolves.
    d = nfa.step(s, False, alert_open=True, params=p, now=NOW + timedelta(minutes=45))
    assert d.action == "resolve" and d.state.flapping is False


def test_flap_suppression_off():
    actions, _ = _run([True, False] * 4, _params(hold=1, clear_hold=1, flap_max=0))
    assert actions == ["raise", "resolve"] * 4


def test_one_evaluation_per_slot_however_many_workers():
    traffic = _rule(params={"settle_s": 120})
    util = _rule(metric=nfa.UTIL, operator=">", threshold=80, params={"settle_s": 300})
    a, b = NOW, NOW + timedelta(seconds=20)
    assert nfa.eval_key(traffic, a) == nfa.eval_key(traffic, b)            # same closed minute
    assert nfa.eval_key(traffic, a) != nfa.eval_key(traffic, a + timedelta(minutes=1))
    assert nfa.eval_key(util, a) == nfa.eval_key(util, a + timedelta(minutes=3))
    assert not nfa._due(traffic, nfa.eval_key(traffic, a), b)


# ── Wording ─────────────────────────────────────────────────────────────────

def test_sentences_say_what_was_measured_and_never_claim_a_device_is_down():
    silent = _rule(metric=nfa.SILENT, threshold=600)
    r = nfa.Reading("exporter:10.0.0.1", "edge-1 (10.0.0.1)", 1800.0, exporter="10.0.0.1")
    text = nfa.event_sentence(silent, r)
    assert "no flow records for 30 minutes" in text and "does not mean the device is down" in text
    assert nfa.event_sentence(silent, r, recovered=True).endswith("delivering flow records again.")

    band = _rule(operator="!=", threshold=1e6, params={"band_high": 5e6, "window_min": 5})
    r = nfa.Reading("all", "exporter 10.0.0.1", 7.5e6)
    assert nfa.event_sentence(band, r) == ("Flow traffic for exporter 10.0.0.1 is 7.5 Mbps averaged over "
                                           "5 min, outside 1 Mbps – 5 Mbps.")
    assert "no longer outside" in nfa.event_sentence(band, r, recovered=True)
    ep = _rule(metric=nfa.ENDPOINT, threshold=1e9, params={"side": "src", "window_min": 15})
    assert nfa.event_sentence(ep, nfa.Reading("host:10.9.9.9", "10.9.9.9", 2.5e9)).startswith(
        "Host 10.9.9.9 sent 2.5 GB in 15 min, above 1 GB")


def test_scope_description_and_link_keep_the_filter_and_exact_window():
    assert nfa.describe_scope({}) == "all flow traffic"
    assert nfa.describe_scope({"exporter": "10.0.0.1", "app": "DNS", "ip_version": "6"}) == \
        "exporter 10.0.0.1 · application DNS · IPv6"
    link = nfa.netflow_link("/netflow", {"exporter": "10.0.0.1", "dst_port": "0"},
                            NOW - timedelta(minutes=5), NOW)
    assert link == ("/netflow?exporter=10.0.0.1&dst_port=0&from=2026-09-30T11%3A55%3A30Z"
                    "&to=2026-09-30T12%3A00%3A30Z")


def test_phrasing_registered_with_the_platform():
    from app.services import alert_phrasing as ap

    assert ap.metric_noun(nfa.TRAFFIC) == "Flow traffic"
    assert ap.format_value(nfa.TRAFFIC, 1.5e9) == "1.5 Gbps"


def test_preview_uses_the_evaluator_sentence():
    v = nfa.preview_phrasing({"metric": nfa.SILENT, "operator": ">", "threshold": 600, "flow_scope": {},
                              "flow_params": {}}, is_recovery=False)
    assert "flow-export silence" in v["event_sentence"]
    assert nfa.preview_phrasing({"metric": "cpu"}, is_recovery=False) is None
    # An invalid draft falls back to the generic preview instead of failing.
    assert nfa.preview_phrasing({"metric": nfa.TRAFFIC, "operator": "==", "threshold": 1},
                                is_recovery=False) is None


def test_finding_rule_matching():
    rule = _rule(metric=nfa.FINDING, operator="==", threshold=1,
                 params={"kinds": ["syn_scan", "icmp_flood"], "min_severity": "high", "min_confidence": "medium"})
    assert nfa.finding_matches(rule, "syn_scan", "critical", "high")
    assert not nfa.finding_matches(rule, "syn_scan", "warning", "high")     # below min severity
    assert not nfa.finding_matches(rule, "syn_scan", "high", "low")         # below min confidence
    assert not nfa.finding_matches(rule, "volume_spike", "critical", "high")  # other kind
    anykind = _rule(metric=nfa.FINDING, operator="==", threshold=1, params={"min_severity": None})
    assert nfa.finding_matches(anykind, None, None, None)


# ── Rule scope / governance ─────────────────────────────────────────────────

def test_family_and_scope_description():
    rule = {"metric": nfa.TRAFFIC, "flow_scope": {"exporter": "10.0.0.1", "group": "Servers"}}
    assert rs.family(rule) == "netflow"
    d = rs.describe_scope(rule)
    assert d["kind"] == "netflow" and d["label"] == "NetFlow — exporter 10.0.0.1 · IP group Servers"
    assert rs.describe_scope({"metric": nfa.SILENT, "flow_scope": None})["label"] == "NetFlow — every flow exporter"
    assert asyncio.run(rs.affected(None, rule)) == {"count": 1, "noun": "flow exporter", "shared": False}


def test_scoped_users_may_only_watch_exporters_they_can_see(monkeypatch):
    from app.api.v1.netflow import access

    async def owners(db, ips=None):
        out = {"10.0.0.1": access.ExporterOwner("10.0.0.1", "d1", "edge-1", "mapped", frozenset({"team-a"})),
               "10.0.0.2": access.ExporterOwner("10.0.0.2", "d2", "edge-2", "mapped", frozenset({"team-b"}))}
        return {ip: o for ip, o in out.items() if ips is None or ip in ips}

    monkeypatch.setattr(access, "resolve_exporter_owners", owners)
    scope = ["team-a"]
    ok = {"metric": nfa.TRAFFIC, "flow_scope": {"exporter": "10.0.0.1"}}
    other = {"metric": nfa.TRAFFIC, "flow_scope": {"exporter": "10.0.0.2"}}
    iface = {"metric": nfa.UTIL, "flow_scope": {"iface": "4@10.0.0.1"}}
    fleet = {"metric": nfa.TRAFFIC, "flow_scope": {"app": "DNS"}}
    unowned = {"metric": nfa.SILENT, "flow_scope": {"exporter": "10.0.0.9"}}
    assert asyncio.run(rs.rule_in_user_scope(None, ok, scope)) is True
    assert asyncio.run(rs.rule_in_user_scope(None, iface, scope)) is True
    assert asyncio.run(rs.rule_in_user_scope(None, other, scope)) is False
    assert asyncio.run(rs.rule_in_user_scope(None, fleet, scope)) is False
    assert asyncio.run(rs.rule_in_user_scope(None, unowned, scope)) is False
    assert asyncio.run(rs.rule_in_user_scope(None, fleet, None)) is True


# ── API (fake database) ─────────────────────────────────────────────────────

def test_api_rejects_invalid_netflow_rules(client, as_operator):
    h = {"Authorization": "Bearer test"}
    base = {"name": "Flow", "metric": nfa.TRAFFIC, "operator": ">", "threshold": 1e6, "severity": "warning"}
    r = client.post("/api/v1/alert-rules", headers=h, json={**base, "flow_scope": {"talker": "999.1.1.1"}})
    assert r.status_code == 422 and "Invalid flow scope" in r.json()["detail"]
    r = client.post("/api/v1/alert-rules", headers=h, json={**base, "flow_params": {"window_min": 999}})
    assert r.status_code == 422 and "window_min" in r.json()["detail"]
    r = client.post("/api/v1/alert-rules", headers=h, json={
        **base, "conditions": [{"metric": nfa.TRAFFIC, "operator": ">", "threshold": 1},
                               {"metric": "rtt", "operator": ">", "threshold": 1}]})
    assert r.status_code == 422 and "single condition" in r.json()["detail"]
    r = client.post("/api/v1/alert-rules", headers=h, json={
        "name": "cpu", "metric": "cpu", "operator": ">", "threshold": 90, "flow_scope": {"exporter": "10.0.0.1"}})
    assert r.status_code == 422 and "only apply to NetFlow" in r.json()["detail"]
    r = client.post("/api/v1/alert-rules", headers=h, json={**base, "metric": "netflow_bogus"})
    assert r.status_code == 422


def test_api_accepts_a_valid_netflow_rule(client, as_operator):
    r = client.post("/api/v1/alert-rules", headers={"Authorization": "Bearer test"}, json={
        "name": "Flow", "metric": nfa.TRAFFIC, "operator": ">=", "threshold": 1e6,
        "flow_scope": {"exporter": "10.0.0.1", "dst_port": 0}, "flow_params": {"window_min": 10, "hold": 3},
        "device_id": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["metric"] == nfa.TRAFFIC and body["operator"] == ">="
    # Device scope columns mean nothing for a flow rule and are cleared.
    assert body["device_id"] is None and body["family"] == "netflow"


def test_catalog_lists_every_metric_with_its_scope_keys(client, as_operator):
    r = client.get("/api/v1/alert-rules/netflow/catalog", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    metrics = {m["metric"]: m for m in r.json()["metrics"]}
    assert set(metrics) == set(nfa.METRICS)
    assert metrics[nfa.SILENT]["scope_keys"] == ["exporter"]
    assert "group" in metrics[nfa.TRAFFIC]["scope_keys"] and "fw_event" not in metrics[nfa.TRAFFIC]["scope_keys"]


def test_preview_of_a_netflow_draft(client, as_admin):
    r = client.post("/api/v1/alert-rules/preview", headers={"Authorization": "Bearer test"}, json={
        "name": "Exporter silent", "metric": nfa.SILENT, "operator": ">", "threshold": 600,
        "recovery_alert": True, "flow_scope": {"exporter": "10.0.0.1"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "flow-export silence" in body["alert"]["email_body"]
    assert "delivering flow records again" in body["recovery"]["email_body"]
    assert "Flow scope" in body["alert"]["email_html"]


# ── Live: ClickHouse evaluators ─────────────────────────────────────────────

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

UNRESTRICTED = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"system.admin"}))
EXP_A, EXP_B = "10.88.0.1", "10.88.0.2"


@pytest.fixture
def real_ch(monkeypatch):
    import threading

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    with flow_db(client) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        yield client, db
    C._retention_cache.update(value=None, at=0.0)


_COLS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_port", "dst_port", "protocol",
         "bytes", "packets", "input_if", "output_if", "input_snmp", "output_snmp", "fields_present",
         "first_switched_ms", "last_switched_ms"]


def _flow(t, exporter, src, dst, nbytes, *, dport=443, proto=6, iin=1, iout=2, received=None):
    ms = int(t.timestamp() * 1000)
    return [t, received or t, exporter, src, dst, 50000, dport, proto, nbytes, max(1, nbytes // 1000),
            iin, iout, iin, iout, (1 << 31) | 0x3F, ms, ms]


def _insert(client, db, rows):
    client.insert(f"{db}.flow_records", rows, column_names=_COLS)


def test_live_traffic_and_endpoint_readings(real_ch):
    client, db = real_ch
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = nfa.floor_to(now - timedelta(seconds=120), 60)
    rows = []
    for m in range(1, 6):            # 5 minutes inside the evaluated window
        t = end - timedelta(minutes=m) + timedelta(seconds=10)
        rows.append(_flow(t, EXP_A, "10.1.0.5", "8.8.8.8", 3_000_000, dport=53, proto=17, received=now))
        rows.append(_flow(t, EXP_B, "10.2.0.9", "1.1.1.1", 1_000_000, received=now))
    # Outside the window (after its end): never counted (half-open).
    rows.append(_flow(end + timedelta(seconds=5), EXP_A, "10.1.0.5", "8.8.8.8", 9_999_999, received=now))
    _insert(client, db, rows)

    traffic = _rule(threshold=1_000_000, params={"window_min": 5, "split": "exporter"})
    ev = asyncio.run(nfa.evaluate(traffic, vis=UNRESTRICTED, now=now))
    by = {r.subject: r for r in ev.readings}
    assert ev.status == "ok" and ev.window_to == end
    assert by[f"exporter:{EXP_A}"].value == pytest.approx(15_000_000 * 8 / 300)
    assert by[f"exporter:{EXP_B}"].value == pytest.approx(5_000_000 * 8 / 300)

    # A FlowFilter scope narrows (DNS = UDP/53 on EXP_A only).
    dns = _rule(threshold=1, scope={"dst_port": "53"}, params={"window_min": 5})
    ev = asyncio.run(nfa.evaluate(dns, vis=UNRESTRICTED, now=now))
    assert ev.readings[0].value == pytest.approx(15_000_000 * 8 / 300)

    # Visibility restricts before the scope: a viewer of EXP_B sees only EXP_B.
    only_b = FlowVisibility(restricted=True, exporters=frozenset({EXP_B}), user_id=None, permissions=frozenset())
    ev = asyncio.run(nfa.evaluate(traffic, vis=only_b, now=now))
    assert {r.subject for r in ev.readings} == {f"exporter:{EXP_B}"}

    # Endpoint volume: the DNS client moved 15 MB in the window.
    ep = _rule(metric=nfa.ENDPOINT, threshold=10_000_000, params={"window_min": 5, "side": "src"})
    ev = asyncio.run(nfa.evaluate(ep, vis=UNRESTRICTED, now=now, tracked={"host:10.9.9.9"}))
    by = {r.subject: r for r in ev.readings}
    assert by["host:10.1.0.5"].value == 15_000_000 and by["host:10.1.0.5"].exporter == EXP_A
    assert "host:10.2.0.9" not in by                              # below threshold, not tracked
    assert by["host:10.9.9.9"].value == 0.0                       # tracked, no traffic: genuinely zero

    # No exporter delivered anything: unknown, never zero.
    empty = _rule(threshold=1, scope={"exporter": "10.88.0.99"}, params={"window_min": 5})
    ev = asyncio.run(nfa.evaluate(empty, vis=UNRESTRICTED, now=now))
    assert ev.status == "no_data" and ev.readings[0].value is None and ev.readings[0].reason == "no_coverage"


def test_live_exporter_silence(real_ch):
    client, db = real_ch
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _insert(client, db, [
        _flow(now - timedelta(minutes=30), EXP_A, "10.1.0.5", "8.8.8.8", 1000, received=now - timedelta(minutes=30)),
        _flow(now - timedelta(minutes=1), EXP_B, "10.2.0.9", "1.1.1.1", 1000, received=now - timedelta(minutes=1)),
    ])
    rule = _rule(metric=nfa.SILENT, threshold=600)
    ev = asyncio.run(nfa.evaluate(rule, vis=UNRESTRICTED, now=now))
    by = {r.subject: r for r in ev.readings}
    assert nfa.breaches(rule, value=by[f"exporter:{EXP_A}"].value) is True
    assert nfa.breaches(rule, value=by[f"exporter:{EXP_B}"].value) is False
    assert 1790 <= by[f"exporter:{EXP_A}"].value <= 1900


def test_live_traffic_snapshot_shares_are_of_the_whole_scope(real_ch):
    client, db = real_ch
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=10)
    rows = [_flow(start + timedelta(minutes=1), EXP_A, f"10.1.0.{i}", "8.8.8.8", 1000 * (i + 1), received=now)
            for i in range(15)]
    _insert(client, db, rows)
    from app.api.v1.netflow.filters import FlowFilter

    snap = asyncio.run(nfa.traffic_snapshot(FlowFilter(), UNRESTRICTED, start, now, limit=10))
    total = sum(1000 * (i + 1) for i in range(15))
    assert snap["total_bytes"] == total and len(snap["talkers"]) == 10
    assert snap["talkers"][0]["share_pct"] == round(15000 / total * 100, 1)
    assert sum(t["bytes"] for t in snap["talkers"]) < total      # a top-N subtotal is not the total


# ── Live: PostgreSQL state machine and alert rows ──────────────────────────

PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (disposable DB with migrate-143)")


@needs_pg
def test_live_raise_resolve_cycle_with_owner_device_and_evidence(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.api.v1.netflow import access

    exporter = "10.253.143.1"

    async def snapshot(f, vis, start, end, limit=10):
        return {"total_bytes": 100, "talkers": [{"src": "10.0.0.1", "dst": "10.0.0.2", "dst_port": 443,
                                                  "bytes": 60, "share_pct": 60.0}], "applications": []}

    monkeypatch.setattr(nfa, "traffic_snapshot", snapshot)

    async def run():
        engine = create_async_engine(PG_DSN)
        rid = uuid.uuid4()
        did = uuid.uuid4()
        try:
            async with AsyncSession(engine) as db:
                await db.execute(text("DELETE FROM devices WHERE hostname = 'b8-test-edge'"))
                await db.execute(text(
                    "INSERT INTO devices (id, hostname, ip_address, tags) VALUES "
                    "(:id, 'b8-test-edge', CAST(:ip AS inet), '[\"b8-team\"]'::jsonb)"), {"id": did, "ip": exporter})
                await db.execute(text(
                    "INSERT INTO alert_rules (id, name, enabled, metric, operator, threshold, severity, "
                    "flow_scope, flow_params, recovery_alert, notify_channels) VALUES (:id, 'b8 test rule', FALSE, "
                    "'netflow_traffic_bps', '>', 1000, 'critical', CAST(:s AS jsonb), CAST(:p AS jsonb), TRUE, '[]')"),
                    {"id": rid, "s": '{"exporter": "%s"}' % exporter,
                     "p": '{"hold": 2, "clear_hold": 1, "window_min": 5, "flap_max": 0}'})
                await db.commit()
                access.invalidate_visibility_cache()
                row = (await db.execute(text("SELECT * FROM alert_rules WHERE id = :id"), {"id": rid})).mappings().first()
                rule = nfa.rule_from_row(dict(row))
                t0 = datetime.now(timezone.utc)

                def ev(value, minute):
                    e = nfa.Evaluation(str(rid), rule.metric, "ok", window_from=t0 - timedelta(minutes=5),
                                       window_to=t0 + timedelta(minutes=minute))
                    e.readings.append(nfa.Reading("all", f"exporter {exporter}", value, exporter=exporter))
                    return e

                r1 = await nfa.apply_evaluation(db, rule, ev(5000.0, 0), now=t0)
                assert r1.raised == 0                      # hold = 2: not yet
                r2 = await nfa.apply_evaluation(db, rule, ev(6000.0, 1), now=t0 + timedelta(minutes=1))
                assert r2.raised == 1
                alert = (await db.execute(text(
                    "SELECT device_id, status, severity, metadata FROM alerts WHERE rule_id = :r"),
                    {"r": rid})).first()
                assert str(alert.device_id) == str(did)     # scoped via the exporter's owner device
                assert alert.status == "active" and alert.severity == "critical"
                meta = alert.metadata
                assert meta["netflow_dedupe"] == "netflow_traffic_bps:all" and meta["dedupe"] == f"rule:{rid}"
                assert meta["snapshot"]["talkers"][0]["share_pct"] == 60.0
                assert meta["netflow_link"].startswith(f"/netflow?exporter={exporter}&from=")
                assert meta["policy_snapshot"]["name"] == "b8 test rule"   # platform trigger ran
                # Unknown does not resolve; a clear does (clear_hold = 1).
                await nfa.apply_evaluation(db, rule, ev(None, 2), now=t0 + timedelta(minutes=2))
                assert (await db.execute(text("SELECT status FROM alerts WHERE rule_id = :r"),
                                         {"r": rid})).scalar() == "active"
                r4 = await nfa.apply_evaluation(db, rule, ev(10.0, 3), now=t0 + timedelta(minutes=3))
                assert r4.resolved == 1
                assert (await db.execute(text("SELECT status FROM alerts WHERE rule_id = :r"),
                                         {"r": rid})).scalar() == "resolved"
                status = await nfa.rule_status(db, str(rid), UNRESTRICTED)
                assert status["subjects"][0]["state"] == "ok" and status["rule"]["context"]["status"] == "ok"
                # A scoped viewer who cannot see the exporter sees no subjects.
                hidden = FlowVisibility(restricted=True, exporters=frozenset(), user_id=None, permissions=frozenset())
                assert (await nfa.rule_status(db, str(rid), hidden))["subjects"] == []
                # Maintenance suppresses the raise (state kept, no alert row).
                await db.execute(text("UPDATE devices SET status = 'maintenance' WHERE id = :d"), {"d": did})
                await db.commit()
                for i, v in enumerate((5000.0, 5000.0)):
                    r = await nfa.apply_evaluation(db, rule, ev(v, 4 + i), now=t0 + timedelta(minutes=4 + i))
                assert r.suppressed == 1
                assert (await db.execute(text("SELECT count(*) FROM alerts WHERE rule_id = :r"),
                                         {"r": rid})).scalar() == 1
                # reset_rule closes open alerts administratively and forgets state.
                await db.execute(text("UPDATE devices SET status = 'up' WHERE id = :d"), {"d": did})
                await db.commit()
                await nfa.apply_evaluation(db, rule, ev(5000.0, 7), now=t0 + timedelta(minutes=7))
                closed = await nfa.reset_rule(db, str(rid))
                await db.commit()
                assert closed == 1
                assert (await db.execute(text("SELECT count(*) FROM netflow_alert_state WHERE rule_id = :r"),
                                         {"r": rid})).scalar() == 0
        finally:
            async with AsyncSession(engine) as db:
                await db.execute(text("DELETE FROM alerts WHERE rule_id = :r"), {"r": rid})
                await db.execute(text("DELETE FROM alert_rules WHERE id = :r"), {"r": rid})
                await db.execute(text("DELETE FROM devices WHERE id = :d"), {"d": did})
                await db.commit()
            access.invalidate_visibility_cache()
            await engine.dispose()

    asyncio.run(run())


# ── Live: API end to end ────────────────────────────────────────────────────

E2E_URL = os.environ.get("ZENPLUS_NETFLOW_B8_URL")
E2E_PG = os.environ.get("ZENPLUS_NETFLOW_B8_PG")
E2E_EXPORTER = os.environ.get("ZENPLUS_NETFLOW_B8_EXPORTER", "127.0.0.63")
E2E_OTHER = os.environ.get("ZENPLUS_NETFLOW_B8_OTHER", "127.0.0.62")
needs_e2e = pytest.mark.skipif(not (E2E_URL and E2E_PG), reason="set ZENPLUS_NETFLOW_B8_URL and ZENPLUS_NETFLOW_B8_PG")
E2E_TAG, E2E_PASS = "b8-e2e-team", "B8-Scoped-Passw0rd!"
E2E_VIEWER, E2E_OPERATOR = "b8_scoped_viewer", "b8_scoped_operator"


def _pg(statements):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(E2E_PG)
        try:
            async with engine.begin() as conn:
                out = []
                for sql, params in statements:
                    res = await conn.execute(text(sql), params)
                    out.append(res.fetchall() if res.returns_rows else None)
                return out
        finally:
            await engine.dispose()

    return asyncio.run(run())


_TOKENS: dict[str, str] = {}


def _login(user, password=E2E_PASS):
    """One login per user per test session (the login endpoint is rate limited);
    a 429 waits for Retry-After once."""
    import time

    import requests

    if user in _TOKENS:
        return _TOKENS[user]
    for attempt in range(2):
        r = requests.post(f"{E2E_URL.rstrip('/')}/api/v1/auth/login",
                          json={"username": user, "password": password}, timeout=10)
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(float(r.headers.get("Retry-After") or 30), 65))
            continue
        break
    r.raise_for_status()
    _TOKENS[user] = r.json()["access_token"]
    return _TOKENS[user]


def _call(token, method, path, **kw):
    import requests

    r = requests.request(method, f"{E2E_URL.rstrip('/')}/api/v1{path}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=60, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


@contextmanager
def seeded_e2e():
    """A device owning E2E_EXPORTER, tagged E2E_TAG, and a scoped viewer and
    operator carrying that tag. Removed afterwards."""
    from app.core.security import hash_password

    cleanup = [
        ("DELETE FROM alerts WHERE rule_id IN (SELECT id FROM alert_rules WHERE name LIKE 'b8-e2e %')", {}),
        ("DELETE FROM alert_rules WHERE name LIKE 'b8-e2e %'", {}),
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = :e", {"e": E2E_EXPORTER}),
        ("DELETE FROM devices WHERE hostname = 'b8-e2e-router'", {}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": [E2E_VIEWER, E2E_OPERATOR]}),
    ]
    _pg(cleanup)
    for u in (E2E_VIEWER, E2E_OPERATOR):      # recreated below: earlier tokens name deleted users
        _TOKENS.pop(u, None)
    did = uuid.uuid4()
    _pg([
        ("INSERT INTO devices (id, hostname, ip_address, tags) VALUES "
         "(:id, 'b8-e2e-router', CAST('10.254.63.1' AS inet), CAST(:t AS jsonb))",
         {"id": did, "t": f'["{E2E_TAG}"]'}),
        ("INSERT INTO netflow_exporter_devices (exporter_ip, device_id) VALUES (CAST(:e AS inet), :d)",
         {"e": E2E_EXPORTER, "d": did}),
        *[("INSERT INTO users (id, username, email, password_hash, role, is_active, scope_tags) VALUES "
           "(:id, :u, :m, :p, :r, TRUE, CAST(:s AS jsonb))",
           {"id": uuid.uuid4(), "u": u, "m": f"{u}@example.invalid", "p": hash_password(E2E_PASS), "r": role,
            "s": f'["{E2E_TAG}"]'}) for u, role in ((E2E_VIEWER, "viewer"), (E2E_OPERATOR, "operator"))],
    ])
    # These rows bypass the API, so the API process's ownership cache (15 s,
    # kept warm by its own background loops) only sees them once it expires.
    from app.api.v1.netflow.access import CACHE_TTL_S

    time.sleep(CACHE_TTL_S + 1)
    try:
        yield did
    finally:
        _pg(cleanup)


@pytest.fixture(scope="module")
def e2e_seed():
    with seeded_e2e() as did:
        yield did


@needs_e2e
def test_e2e_rule_crud_dry_run_and_scope_governance(e2e_seed):
    admin = _login(os.environ.get("ZENPLUS_ADMIN", "admin"), os.environ.get("ZENPLUS_ADMIN_PASS", "admin123"))
    op = _login(E2E_OPERATOR)
    viewer = _login(E2E_VIEWER)
    rule = {"name": "b8-e2e traffic", "metric": nfa.TRAFFIC, "operator": ">", "threshold": 1, "enabled": False,
            "flow_scope": {"exporter": E2E_EXPORTER}, "flow_params": {"window_min": 5}}
    status, body = _call(admin, "POST", "/alert-rules", json=rule)
    assert status == 201, body
    rid = body["id"]
    assert body["flow_scope"] == {"exporter": E2E_EXPORTER} and body["flow_params"]["window_min"] == 5
    status, impact = _call(admin, "GET", f"/alert-rules/{rid}/impact")
    assert impact["scope"]["kind"] == "netflow" and impact["affected"]["count"] == 1

    # Changing the definition is validated on the merged rule.
    status, body = _call(admin, "PUT", f"/alert-rules/{rid}", json={"operator": "!="})
    assert status == 422 and "band_high" in body["detail"]
    status, body = _call(admin, "PUT", f"/alert-rules/{rid}", json={"operator": "!=",
                                                                    "flow_params": {"band_high": 1e12}})
    assert status == 200 and body["flow_params"]["band_high"] == 1e12

    # Dry run: live readings for the owned exporter.
    draft = {"metric": nfa.TRAFFIC, "operator": ">", "threshold": 1, "flow_params": {"split": "exporter"}}
    status, ev = _call(viewer, "POST", "/alert-rules/netflow/evaluate", json=draft)
    assert status == 200, ev
    subjects = {r["subject"] for r in ev["readings"]}
    assert subjects <= {f"exporter:{E2E_EXPORTER}"}          # restricted to what the viewer sees
    status, ev_admin = _call(admin, "POST", "/alert-rules/netflow/evaluate", json=draft)
    assert f"exporter:{E2E_OTHER}" in {r["subject"] for r in ev_admin["readings"]}
    status, _ = _call(viewer, "POST", "/alert-rules/netflow/evaluate",
                      json={**draft, "flow_scope": {"exporter": E2E_OTHER}})
    assert status == 404                                      # out-of-scope exporter: not found

    # Governance: a scoped operator may watch its own exporter only.
    status, body = _call(op, "POST", "/alert-rules", json={**rule, "name": "b8-e2e fleet", "flow_scope": {}})
    assert status == 403
    status, body = _call(op, "POST", "/alert-rules",
                         json={**rule, "name": "b8-e2e other", "flow_scope": {"exporter": E2E_OTHER}})
    assert status == 403
    status, body = _call(op, "POST", "/alert-rules", json={**rule, "name": "b8-e2e own"})
    assert status == 201, body

    status, st = _call(admin, "GET", f"/alert-rules/{rid}/netflow-status")
    assert status == 200 and "subjects" in st
    status, _ = _call(admin, "DELETE", f"/alert-rules/{rid}")
    assert status == 204
