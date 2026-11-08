"""Synthetic scenario read models and run timing.

* range parsing (default 24 h, from/to, 90-day cap), bucket size, outage folding
  and secret masking are pure;
* a real scenario run against a local HTTP server records the per-step phase
  timing (connect, send, wait, download), response size and server address;
* the ClickHouse queries run against embedded ClickHouse (``chdb``) when it is
  installed, over a disposable table with the production DDL (migrate-058);
  otherwise that test is skipped.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.services import apm_synthetic_stats as st
from app.services.apm_synthetic_service import _Timing, run_scenario

UTC = timezone.utc
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def test_range_defaults_to_24_hours_and_caps_at_retention():
    since, until = st.resolve_range(None, None, None, now=NOW)
    assert until == NOW.replace(tzinfo=None) and until - since == timedelta(hours=24)
    since, until = st.resolve_range(6, None, None, now=NOW)
    assert until - since == timedelta(hours=6)
    since, until = st.resolve_range(None, NOW - timedelta(days=400), NOW - timedelta(days=1), now=NOW)
    assert until - since == st.MAX_RANGE
    with pytest.raises(ValueError):
        st.resolve_range(None, NOW, NOW - timedelta(hours=1), now=NOW)


def test_bucket_size_is_round_and_never_finer_than_the_run_interval():
    day = (NOW - timedelta(hours=24), NOW)
    assert st.bucket_seconds(*day, interval=300) == 900          # 86400/120 = 720 -> 900
    assert st.bucket_seconds(*day, interval=60) == 900
    assert st.bucket_seconds(NOW - timedelta(hours=1), NOW, interval=300) == 300
    assert st.bucket_seconds(NOW - timedelta(days=30), NOW, interval=60) == 21600


def test_iso_always_carries_utc():
    assert st.iso(datetime(2026, 11, 8, 9, 20)) == "2026-11-08T09:20:00+00:00"
    assert st.iso(None) is None


def test_outages_fold_consecutive_failures_newest_first():
    t = [NOW - timedelta(minutes=50 - 5 * i) for i in range(10)]
    pts = [(t[0], True, "", ""), (t[1], False, "Login", "status 503 eq 200"), (t[2], False, "Login", "x"),
           (t[3], True, "", ""), (t[4], True, "", ""), (t[5], False, "OTP", "timeout after 30s")]
    out = st.outages(pts, until=t[9])
    assert len(out) == 2
    assert out[0]["ongoing"] and out[0]["failed_step"] == "OTP" and out[0]["failed_runs"] == 1
    first = out[1]
    assert not first["ongoing"] and first["failed_runs"] == 2 and first["error"] == "status 503 eq 200"
    assert first["duration_s"] == 600


def test_read_only_users_never_see_scenario_secrets():
    m = {"id": "1", "variables": {"password": "hunter2"}, "steps": [
        {"name": "Login", "headers": {"Authorization": "Bearer abc", "Accept": "application/json",
                                      "X-API-Key": "k"}, "body": '{"pass":"{{password}}"}'}]}
    out = st.mask_scenario(m)
    blob = json.dumps(out)
    assert "hunter2" not in blob and "Bearer abc" not in blob and '"k"' not in blob and "{{password}}" not in blob
    assert out["steps"][0]["headers"]["Accept"] == "application/json" and out["secrets_masked"]
    assert m["variables"]["password"] == "hunter2"                  # the stored scenario is untouched


def test_timing_phases_add_up_and_ignore_unknown_events():
    t = _Timing()
    for ev in ("connection.connect_tcp.started", "connection.connect_tcp.complete",
               "http11.send_request_headers.started", "http11.send_request_headers.complete",
               "http11.receive_response_headers.started", "http11.receive_response_headers.complete",
               "http11.receive_response_body.started", "http11.receive_response_body.complete",
               "http11.response_closed.started", "bogus"):
        t.record(ev, {})
    assert set(t.result()) == {"connect_ms", "tls_ms", "send_ms", "ttfb_ms", "download_ms"}
    assert all(v >= 0 for v in t.result().values())


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = b'{"ok": true, "token": "t-1"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.send_response(503)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"busy")

    def log_message(self, *a):
        pass


def test_a_real_run_records_phase_timing_size_and_server_address():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        result = asyncio.run(run_scenario({"config": {"steps": [
            {"name": "Token", "method": "GET", "url": base + "/token",
             "extract": [{"var": "tok", "from": "json", "path": "token"}]},
            {"name": "Verify", "method": "POST", "url": base + "/verify?t={{tok}}",
             "assertions": [{"type": "status_code", "operator": "eq", "value": 200}]},
        ]}, "timeout": 5}))
    finally:
        server.shutdown()
    first, second = result["steps"]
    assert first["ok"] and first["response_bytes"] == 28 and first["content_type"] == "application/json"
    assert first["server_ip"] == "127.0.0.1" and first["timing"]["connect_ms"] >= 0
    assert set(first["timing"]) == {"connect_ms", "tls_ms", "send_ms", "ttfb_ms", "download_ms"}
    assert second["timing"]["connect_ms"] == 0          # the session reuses the connection
    assert not second["ok"] and second["status_code"] == 503 and result["failed_step"] == "Verify"
    assert second["url"].endswith("t=t-1") and second["body_snippet"] == "busy"


# ── The ClickHouse queries, on embedded ClickHouse ───────────────────────────

ROOT = Path(__file__).resolve().parents[2]


def _literal(v):
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_literal(x) for x in v) + ")"
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _Chdb:
    """clickhouse_connect-shaped adapter (client-side %(name)s binding)."""

    def __init__(self, session):
        self.s = session

    def query(self, sql, parameters=None):
        sql = re.sub(r"%\((\w+)\)s", lambda m: _literal((parameters or {})[m.group(1)]), sql)
        out = self.s.query(sql, "JSONCompact")
        data = json.loads(out.bytes() or b'{"data": [], "meta": []}')
        rows = []
        for row in data["data"]:
            conv = []
            for v, meta in zip(row, data["meta"]):
                if meta["type"].startswith("DateTime") and isinstance(v, str):
                    v = datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
                elif meta["type"].startswith(("Float", "Nullable(Float")) and isinstance(v, str):
                    v = float(v)
                elif meta["type"].startswith(("UInt64", "Int64")) and isinstance(v, str):
                    v = int(v)
                conv.append(v)
            rows.append(conv)
        return _Result(rows)


@pytest.fixture
def ch(tmp_path):
    pytest.importorskip("chdb", reason="embedded ClickHouse (chdb) not installed")
    from chdb import session as chs
    s = chs.Session(str(tmp_path / "ch"))
    s.query("CREATE DATABASE IF NOT EXISTS zenplus")
    ddl = (ROOT / "scripts" / "migrate-058-apm-synthetics-clickhouse.sql").read_text()
    s.query(ddl.split("CREATE TABLE IF NOT EXISTS zenplus.apm_ingest_stats")[0].split("-- Per-run")[1]
            .split("\n", 1)[1].strip().rstrip(";"))
    yield s, _Chdb(s)
    s.cleanup() if hasattr(s, "cleanup") else None


def _step(name, ok, code, ms, timing=True):
    s = {"name": name, "method": "POST", "url": f"https://raha.example/{name}", "ok": ok, "status_code": code,
         "ms": ms, "error": "" if ok else f"status {code} eq 200", "asserts": []}
    if timing:
        s["timing"] = {"connect_ms": 5.0, "tls_ms": 10.0, "send_ms": 1.0, "ttfb_ms": ms - 20, "download_ms": 4.0}
    return s


def test_queries_on_embedded_clickhouse(ch):
    session, client = ch
    mid = uuid.uuid4()
    base = datetime(2026, 11, 8, 0, 0)
    rows = []
    for i in range(288):                                  # 24 h at 300 s
        ts = base + timedelta(seconds=300 * i)
        fail = i in (100, 101, 200)
        steps = [_step("Generate Token", not fail, 503 if fail else 200, 280.0, timing=i > 10)]
        if not fail:
            steps += [_step("VerifyOTP", True, 200, 320.0), _step("Dashboard", True, 200, 80.0)]
        rows.append((str(mid), ts, "down" if fail else "up", 0 if fail else 1, 680 if not fail else 280, 3,
                     0 if fail else 3, "Generate Token" if fail else "", "status 503 eq 200" if fail else "",
                     json.dumps(steps)))
    values = ", ".join("(" + ", ".join(_literal(v) for v in r) + ")" for r in rows)
    session.query("INSERT INTO zenplus.apm_synthetic_results (monitor_id, timestamp, status, success, total_ms,"
                  " steps_total, steps_passed, failed_step, error, steps_json) VALUES " + values)
    since, until = base, base + timedelta(hours=24)

    per = st.scenario_stats(client, [str(mid)], since, until)[str(mid)]
    assert per["runs"] == 288 and per["failures"] == 3 and per["uptime_pct"] == round(285 / 288 * 100, 2)
    assert per["last_error"] == "status 503 eq 200" and per["last_failure_at"].startswith("2026-11-08T16:40")
    assert len(per["spark"]) == st.SPARK_BUCKETS and min(x for x in per["spark"] if x is not None) < 100

    sm = st.summary(client, str(mid), since, until, interval=300)
    t = sm["totals"]
    assert t["runs"] == 288 and t["failed"] == 3 and t["outages"] == 2 and t["downtime_s"] == 900
    assert sm["range"]["bucket_seconds"] == 900 and len(sm["series"]) == 96
    names = [s["name"] for s in sm["steps"]]
    assert names == ["Generate Token", "VerifyOTP", "Dashboard"]
    gen, otp = sm["steps"][0], sm["steps"][1]
    assert gen["executed"] == 288 and gen["failed"] == 3 and 503 in gen["status_codes"]
    assert otp["skipped"] == 3 and otp["pass_pct"] == 100.0 and otp["timing"]["tls_ms"] == 10.0
    assert gen["top_errors"][0] == {"error": "status 503 eq 200", "count": 3,
                                    "last_seen": "2026-11-08T16:40:00+00:00"}
    assert len(gen["series"]) == 96 and sum(p["failed"] for p in gen["series"]) == 3
    assert sm["errors"][0]["count"] == 3 and sm["outages"][0]["start"].startswith("2026-11-08T16:40")

    page = st.runs(client, str(mid), since, until, status="failed", limit=2)
    assert page["total"] == 3 and len(page["runs"]) == 2 and not page["runs"][0]["success"]
    assert page["runs"][0]["steps"] == [{"name": "Generate Token", "ok": False, "status_code": 503, "ms": 280.0}]
    assert st.runs(client, str(mid), since, until, step="Generate Token")["total"] == 3
    assert st.runs(client, str(mid), since, until, limit=500)["total"] == 288   # no 200-run cap

    at = datetime.fromisoformat(page["runs"][0]["timestamp"])
    detail = st.run_detail(client, str(mid), at)
    assert detail["failed_step"] == "Generate Token" and detail["steps"][0]["timing"]["ttfb_ms"] == 260.0
    assert st.run_detail(client, str(mid), at + timedelta(seconds=1)) is None


def test_saving_masked_placeholders_keeps_the_stored_secrets():
    from app.api.v1.apm_synthetics import MonitorUpdate, _keep_masked_secrets
    stored = {"variables": {"password": "hunter2", "user": "demo"}, "steps": [
        {"name": "Login", "method": "POST", "url": "https://a.example/login",
         "headers": {"Authorization": "Bearer abc"}, "body": '{"p":"{{password}}"}'}]}
    sent = MonitorUpdate(name="x", variables={"password": st.MASK, "user": "changed"}, steps=[
        {"name": "Login", "method": "POST", "url": "https://a.example/login",
         "headers": {"Authorization": st.MASK, "Accept": "text/html"}, "body": st.MASK}])
    out = _keep_masked_secrets(sent, stored)
    assert out.variables == {"password": "hunter2", "user": "changed"}
    assert out.steps[0].headers == {"Authorization": "Bearer abc", "Accept": "text/html"}
    assert out.steps[0].body == '{"p":"{{password}}"}'
