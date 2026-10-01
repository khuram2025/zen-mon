"""Regression tests for the detection / health / UX acceptance fixes (wave C).

Covered:
  * CR-06 — the Setup live check counts datagrams as received only while the
    exporter is receiving (its expected interval): a late exporter is 'late',
    a silent one 'stopped' with "Not receiving … since", never "Working";
  * Setup check cost — the field scan is bounded to the exporter's own
    flow-time span (flow_exporter_minutes), costs no raw scan for an exporter
    that sent nothing, can sample the newest records, and is shared by the
    polls of the next 30 s (but an empty answer is not cached);
  * X-09 — the reduced-sensitivity note names the most heavily sampled
    exporters first and says "and N more"; reduced_sensitivity lists all;
  * FE-09 — exporter-to-device suggestions send no reverse-DNS query when
    reverse DNS is turned off, and say so (sources.reverse_dns_status);
  * CR-10 — a downloaded feed's content is never echoed in its parse stats
    (uploads keep their samples), on top of the public-destination guard.

Layers: pure rules and in-process fakes (always); scratch ClickHouse
(NETFLOW_TEST_CLICKHOUSE, see netflow_scratch_ch.py) for the bounded field
scan; live API (NETFLOW_TEST_API + NETFLOW_TEST_TOKEN) for the end-to-end
check of a silent exporter and the reverse-DNS switch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import sys
import threading
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.v1.netflow import common as C, exporters as E, health as H  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.services import netflow_detection as det  # noqa: E402
from app.services import netflow_health as nh  # noqa: E402
from app.services import netflow_ti as ti  # noqa: E402

from test_netflow_health import LIVE_OK, NOW, _fields, _rc, _row, _st  # noqa: E402

# ── CR-06: live check honours the exporter's receipt state ───────────────────


def _aged(age_s: float, **kw):
    """A status row whose newest receipt (record and datagram) is ``age_s`` old."""
    t = NOW - timedelta(seconds=age_s)
    return _row(rc=_rc(last_rx=t, rec15=kw.pop("rec15", 0), rec60=0), st=_st(last_dgram=t), **kw)


def test_silent_exporter_is_not_receiving_and_not_working():
    row = _aged(2.1 * 3600)
    assert row["state"] == "silent"
    checks = H.build_checks(row, _fields(), [{"is_options": False}], LIVE_OK, None)
    by = {c["id"]: c for c in checks}
    assert by["datagrams"]["status"] == "fail"
    assert by["datagrams"]["detail"].startswith("Not receiving: nothing from 10.0.0.1 since 2026-10-01 09:54:00 UTC (")
    assert "2.1 h ago" in by["datagrams"]["detail"] and "UTC" in by["datagrams"]["detail"]
    assert by["records"]["status"] == "fail" and "not sending" in by["records"]["detail"]
    assert "Datagrams arrive" not in by["records"]["detail"], "never claims datagrams arrive"
    # Field checks over the last 15 minutes would all be empty: one line instead.
    assert by["fields"]["status"] == "pending" and not any(k.startswith("field_") for k in by)
    assert H.overall_check_status(checks) == "stopped"


def test_late_exporter_warns_and_is_not_working():
    row = _aged(240)          # default thresholds: late after 180 s, silent after 900 s
    assert row["state"] == "late"
    checks = H.build_checks(row, _fields(), [{"is_options": False}], LIVE_OK, None)
    by = {c["id"]: c for c in checks}
    assert by["datagrams"]["status"] == "warn" and "late after 3 min" in by["datagrams"]["detail"]
    assert H.overall_check_status(checks) == "late"
    # Records within the last 15 minutes: the field checks still run on them.
    row2 = _aged(240, rec15=500)
    by2 = {c["id"]: c for c in H.build_checks(row2, _fields(), [{"is_options": False}], LIVE_OK, None)}
    assert by2["records"]["status"] == "pass" and "field_tcp_flags" in by2
    assert H.overall_check_status(list(by2.values())) == "late"


def test_receiving_exporter_still_passes():
    row = _aged(20, rec15=900)
    checks = H.build_checks(row, _fields(), [{"is_options": False}], LIVE_OK, {"interfaces": 2, "with_speed": 2})
    by = {c["id"]: c for c in checks}
    assert by["datagrams"]["status"] == "pass" and "20 s ago" in by["datagrams"]["detail"]
    assert H.overall_check_status(checks) == "ready"


def test_never_seen_exporter_is_still_waiting():
    row = _row(rc=None, st=None, owner=None)
    checks = H.build_checks(row, {"capabilities": nh.capability_status({}, 0), "max_sampling": None}, [], LIVE_OK, None)
    assert H.overall_check_status(checks) == "waiting"


# ── Setup check cost: bounded, sampled, cached field scan ────────────────────


def test_field_scan_skips_raw_records_when_the_exporter_sent_nothing(monkeypatch):
    calls = []

    async def fake_run(sql, params=None, *, budget_s=None, label=None):
        calls.append((label, sql, dict(params or {})))
        if label == "exporter-fields-span":
            return SimpleNamespace(result_rows=[(0, 0, None, None)])
        raise AssertionError("no raw scan for an exporter without records")

    monkeypatch.setattr(nh, "run_query", fake_run)
    out = asyncio.run(nh.exporter_fields("10.0.0.1", NOW, timedelta(minutes=15)))
    assert out["records"] == 0 and all(c["status"] == "unknown" for c in out["capabilities"])
    assert [c[0] for c in calls] == ["exporter-fields-span"]
    assert "flow_exporter_minutes" in calls[0][1]


def test_field_scan_is_bounded_to_the_exporters_flow_times(monkeypatch):
    calls = []
    lo, hi = NOW - timedelta(minutes=40, microseconds=250_000), NOW - timedelta(seconds=3, microseconds=500_000)

    async def fake_run(sql, params=None, *, budget_s=None, label=None):
        calls.append((label, sql, dict(params or {})))
        if label == "exporter-fields-span":
            return SimpleNamespace(result_rows=[(120, 15, lo, hi)])
        return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(nh, "run_query", fake_run)
    asyncio.run(nh.exporter_fields("10.0.0.1", NOW, timedelta(minutes=15), max_records=50_000))
    raw = calls[1]
    assert raw[0] == "exporter-fields"
    # Whole seconds around the exporter's own span, not three hours of every exporter.
    assert raw[2]["ts_since"] == lo.replace(microsecond=0)
    assert raw[2]["until"] == hi.replace(microsecond=0) + timedelta(seconds=1)
    assert raw[2]["since"] == NOW - timedelta(minutes=15)
    assert "ORDER BY timestamp DESC LIMIT 50000" in raw[1]


def test_field_scan_falls_back_to_the_wide_bound_without_the_rollup(monkeypatch):
    calls = []

    async def fake_run(sql, params=None, *, budget_s=None, label=None):
        calls.append((label, dict(params or {})))
        if label == "exporter-fields-span":
            raise C.NetflowError(503, "flow database unavailable", code="unavailable")
        return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(nh, "run_query", fake_run)
    asyncio.run(nh.exporter_fields("10.0.0.1", NOW, timedelta(minutes=15)))
    assert calls[1][1]["ts_since"] == NOW - timedelta(minutes=15) - timedelta(hours=3)


def test_check_reuses_the_field_scan_but_not_an_empty_one(monkeypatch):
    nh._fields_cache.clear()
    n = {"calls": 0, "records": 0}

    async def fake_fields(ip, now, window, *, max_records=None):
        n["calls"] += 1
        return {"records": n["records"], "capabilities": []}

    monkeypatch.setattr(nh, "exporter_fields", fake_fields)
    asyncio.run(nh.cached_exporter_fields("10.0.0.9", NOW, timedelta(minutes=15)))
    asyncio.run(nh.cached_exporter_fields("10.0.0.9", NOW, timedelta(minutes=15)))
    assert n["calls"] == 2, "an exporter's first records must show at the next poll"
    n["records"] = 10
    asyncio.run(nh.cached_exporter_fields("10.0.0.9", NOW, timedelta(minutes=15)))
    asyncio.run(nh.cached_exporter_fields("10.0.0.9", NOW, timedelta(minutes=15)))
    assert n["calls"] == 3, "polls within 30 s share one scan"
    nh._fields_cache.clear()


def test_bounded_field_scan_on_scratch_clickhouse(monkeypatch):
    """Real queries: the span comes from flow_exporter_minutes and the bounded
    scan still finds every record of the exporter (another exporter's rows in
    the same minutes are not counted)."""
    if not (os.environ.get("NETFLOW_TEST_CLICKHOUSE") or os.environ.get("ZENPLUS_NETFLOW_TEST_CH")):
        pytest.skip("scratch ClickHouse not configured")
    from netflow_scratch_ch import flow_db, scratch_client
    from test_netflow_health import RAW_COLS, TRACKED_ALL

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    with flow_db(client) as db:
        monkeypatch.setattr(C, "get_ch_client", thread_client)
        monkeypatch.setattr(C, "CH_DB", db)
        now = nh.now_utc().replace(microsecond=0)
        recs = []
        for i, exp in enumerate(["10.78.1.1"] * 30 + ["10.78.1.2"] * 50):
            t = now - timedelta(minutes=3, seconds=i)
            ms = int(t.timestamp() * 1000)
            recs.append([t, t + timedelta(seconds=1), ipaddress.IPv4Address(exp),
                         ipaddress.IPv4Address("10.0.0.1"), ipaddress.IPv4Address("192.0.2.1"), 40000, 443, 6,
                         0x18, 1000, 2, 1, ms - 500, ms, 3, 4, TRACKED_ALL, 4, "ipfix", 10, 0])
        client.insert(f"{db}.flow_records", recs, column_names=RAW_COLS)
        f = asyncio.run(nh.exporter_fields("10.78.1.1", now, timedelta(minutes=15), max_records=50_000))
        assert f["records"] == 30 and f["tracked"] == 30
        assert {c["key"]: c["status"] for c in f["capabilities"]}["tcp_flags"] == "all"
        small = asyncio.run(nh.exporter_fields("10.78.1.2", now, timedelta(minutes=15), max_records=20))
        assert small["records"] == 20 and small["sampled_records"] is True
        none = asyncio.run(nh.exporter_fields("10.78.1.3", now, timedelta(minutes=15)))
        assert none["records"] == 0


# ── X-09: sampling disclosure names every sampled exporter ───────────────────


def test_sampling_note_puts_the_heaviest_first_and_counts_the_rest():
    cov = det.Coverage(exporters={
        ip: det.ExporterCoverage(exporter=ip, records=10, max_sampling=rate)
        for ip, rate in (("::1", 256), ("127.0.0.62", 100), ("127.0.0.63", 100), ("127.0.0.67", 512),
                         ("127.0.0.61", 1))
    })
    out = det.Outcome(kind="syn_scan", status="ok")
    det._sampling_note(cov, out)
    assert [r["exporter"] for r in out.reduced_sensitivity] == ["127.0.0.67", "::1", "127.0.0.62", "127.0.0.63"]
    note = out.notes[0]
    assert "127.0.0.67 1:512" in note, "the most heavily sampled exporter is never left out"
    assert "and 1 more" in note
    few = det.Outcome(kind="syn_scan", status="ok")
    det._sampling_note(det.Coverage(exporters={"a": det.ExporterCoverage(exporter="a", max_sampling=10)}), few)
    assert "more" not in few.notes[0] and "a 1:10" in few.notes[0]


# ── FE-09: reverse DNS off means no lookup from the suggestions ──────────────


class _Result:
    def all(self):
        return []

    def first(self):
        return None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDB:
    async def execute(self, *a, **k):
        return _Result()

    def begin_nested(self):
        return _Nested()


def _suggest(monkeypatch, enabled: bool):
    from app.api.v1.netflow import settings as S

    lookups = []

    async def cfg():
        return {"enabled": enabled, "deadline_s": 0.5}

    def resolve(ips, deadline=2.0):
        lookups.append(list(ips))
        return {ip: None for ip in ips}

    async def no_scope(db, user):
        return None

    async def no_configs(db, user):
        return False

    async def no_owner(db, ips):
        return {}

    async def no_facts(db, ids):
        return {}

    monkeypatch.setattr(S, "rdns_config", cfg)
    monkeypatch.setattr(E, "_resolve_hostnames", resolve)
    monkeypatch.setattr(E.scoping, "visible_tags", no_scope)
    monkeypatch.setattr(E, "_can_read_configs", no_configs)
    monkeypatch.setattr(E, "resolve_exporter_owners", no_owner)
    monkeypatch.setattr(E, "_device_facts", no_facts)
    vis = FlowVisibility(restricted=False, exporters=None, user_id="u", permissions=frozenset({"netflow.view"}))
    user = SimpleNamespace(id=uuid.uuid4(), role="admin", scope_tags=[])
    out = asyncio.run(E.exporter_suggestions("198.18.231.4", user=user, vis=vis, db=_FakeDB()))
    return out, lookups


def test_suggestions_send_no_ptr_query_when_reverse_dns_is_off(monkeypatch):
    out, lookups = _suggest(monkeypatch, enabled=False)
    assert lookups == [], "reverse DNS is off: no PTR query may leave the appliance"
    assert out["reverse_dns"] is None and out["sources"]["reverse_dns_status"] == "disabled"


def test_suggestions_resolve_when_reverse_dns_is_on(monkeypatch):
    out, lookups = _suggest(monkeypatch, enabled=True)
    assert lookups == [["198.18.231.4"]]
    assert out["sources"]["reverse_dns_status"] == "no_record"


# ── CR-10: a downloaded feed is never echoed back ────────────────────────────


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"status":"degraded","secret":"internal"}\nnot-a-prefix\n'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FeedDB:
    def __init__(self, url):
        self.url = url
        self.statuses = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if sql.startswith("SELECT id, source, url"):
            return SimpleNamespace(first=lambda: ("f1", "url" if self.url else "upload", self.url))
        if "last_status" in sql:
            self.statuses.append(params["s"])
        return SimpleNamespace(first=lambda: None)


def test_fetched_feed_content_is_not_echoed_but_uploads_keep_samples(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _FeedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Loopback is refused by default (SSRF guard); allow it explicitly for this test server only.
        monkeypatch.setenv(ti.FETCH_ALLOW_ENV, "127.0.0.1/32")
        url = f"http://127.0.0.1:{server.server_port}/feed.txt"
        db = _FeedDB(url)
        res = asyncio.run(ti.refresh_feed(db, "f1"))
        assert res["ok"] is False and res["stats"]["invalid"] == 2
        assert res["stats"]["samples_invalid"] == [], "fetched content is never echoed"
        assert "degraded" not in repr(res) and "secret" not in repr(db.statuses)
        up = _FeedDB(None)
        res2 = asyncio.run(ti.refresh_feed(up, "f1", content="not-a-prefix\n"))
        assert res2["stats"]["samples_invalid"] == ["not-a-prefix"], "an upload's own text may be shown"
        # Without the allowance, the same loopback URL is refused before any request.
        monkeypatch.delenv(ti.FETCH_ALLOW_ENV)
        refused = asyncio.run(ti.refresh_feed(_FeedDB(url), "f1"))
        assert refused["ok"] is False and "public internet address" in refused["status"]
    finally:
        server.shutdown()


# ── Live API (integrated stack) ──────────────────────────────────────────────

LIVE_API = os.environ.get("NETFLOW_TEST_API")
LIVE_TOKEN = os.environ.get("NETFLOW_TEST_TOKEN")
live = pytest.mark.skipif(not (LIVE_API and LIVE_TOKEN), reason="live API not configured")


def _live(method, path, **kw):
    import requests

    r = requests.request(method, f"{LIVE_API}/api/v1{path}", headers={"Authorization": f"Bearer {LIVE_TOKEN}"},
                         timeout=60, **kw)
    return r


@live
def test_live_check_of_a_silent_exporter_says_not_receiving():
    status = _live("GET", "/netflow/exporters/status")
    if status.status_code == 401:
        pytest.skip("live token expired")
    rows = status.json()["exporters"]
    silent = next((r for r in rows if r["state"] == "silent" and r.get("last_received_at")), None)
    if silent is None:
        pytest.skip("no silent exporter on the live stack")
    d = _live("GET", f"/netflow/exporters/{silent['exporter']}/check").json()
    by = {c["id"]: c for c in d["checks"]}
    assert d["overall"] == "stopped"
    assert by["datagrams"]["status"] == "fail" and by["datagrams"]["detail"].startswith("Not receiving")


@live
def test_live_reverse_dns_switch_is_honoured_by_suggestions():
    enr = _live("GET", "/netflow/settings/enrichment")
    if enr.status_code == 401:
        pytest.skip("live token expired")
    body = enr.json()
    cfg = (body.get("rdns") or body.get("data", {}).get("rdns"))
    try:
        assert _live("PUT", "/netflow/settings/enrichment/rdns",
                     json={"enabled": False, "deadline_s": cfg["deadline_s"]}).status_code == 200
        _live("POST", "/netflow/settings/enrichment/rdns/flush")
        before = _live("GET", "/netflow/settings/enrichment").json()
        sug = _live("GET", "/netflow/exporter-devices/198.18.231.250/suggestions")
        after = _live("GET", "/netflow/settings/enrichment").json()
        cache = lambda e: (e.get("rdns") or e.get("data", {}).get("rdns"))["cache"]  # noqa: E731
        assert sug.status_code == 200 and sug.json()["sources"]["reverse_dns_status"] == "disabled"
        assert cache(after)["no_record"] == cache(before)["no_record"]
        assert cache(after)["names"] == cache(before)["names"] and cache(after)["pending"] == 0
    finally:
        _live("PUT", "/netflow/settings/enrichment/rdns", json={"enabled": cfg["enabled"], "deadline_s": cfg["deadline_s"]})
