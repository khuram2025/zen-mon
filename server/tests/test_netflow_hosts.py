"""NetFlow host profile (CR-14, FE-09): api/v1/netflow/hosts.py.

Layers:
  * unit -- network class twin of the SQL classifier, bucket choice, the
    series filler (measured zero only where collection was running, unknown
    elsewhere), IP-group membership (own CIDR vs via sub-group, most specific
    first), the rDNS "resolved at" clock and section error mapping;
  * live API (NETFLOW_B7_API + NETFLOW_B7_PG, see test_netflow_settings.py):
    input validation, the full profile shape with its envelope and the legacy
    shape, UDT identity gated by udt.view / udt.view_users, threat-intel and
    findings sections from fixtures, and exporter scoping -- a viewer whose tag
    scope owns exactly one exporter gets 404 for hosts that exporter never saw
    and, for hosts it did see, only that exporter's flows and findings. All
    fixtures (users, device, exporter mapping, UDT rows, feed, findings) are
    removed afterwards.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.netflow import common  # noqa: E402
from app.api.v1.netflow import hosts as H  # noqa: E402
from app.api.v1.netflow.common import NetflowError  # noqa: E402
from app.services import netflow_groups  # noqa: E402

UTC = timezone.utc


# ── Unit ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,cls", [
    ("10.1.2.3", "Private 10/8"),
    ("8.8.8.8", "Public"),
    ("2001:4860:4860::8888", "Public"),
    ("198.51.100.7", "Documentation / special-use"),
    ("203.0.113.9", "Documentation / special-use"),
    ("198.18.0.1", "Documentation / special-use"),
    ("2001:db8::1", "Documentation / special-use"),
])
def test_netclass_of_matches_the_filter_classes(ip, cls):
    assert H.netclass_of(ip) == cls


def test_pick_bucket_targets_about_120_points_on_the_source_grain():
    assert H.pick_bucket(3600, 1) == 60
    assert H.pick_bucket(24 * 3600, 1) == 900
    assert H.pick_bucket(24 * 3600, 3600) == 3600            # hourly rollup cannot be split
    assert H.pick_bucket(7 * 86400, 3600) == 7200
    assert H.pick_bucket(93 * 86400, 3600) == 86400
    assert H.pick_bucket(60, None) == 60


def test_fill_series_distinguishes_zero_from_unknown():
    start = datetime(2026, 9, 30, 10, 7, tzinfo=UTC)
    end = datetime(2026, 9, 30, 11, 7, tzinfo=UTC)
    bucket = 900
    b = lambda h, m: int(datetime(2026, 9, 30, h, m, tzinfo=UTC).timestamp())  # noqa: E731
    rows = {b(10, 15): (100, 200, 1, 2, 3)}
    covered = {b(10, 0), b(10, 15), b(10, 30)}
    pts = H.fill_series(rows, start, end, bucket, covered)
    assert [p["t"] for p in pts] == ["2026-09-30T10:00:00.000Z", "2026-09-30T10:15:00.000Z", "2026-09-30T10:30:00.000Z",
                                     "2026-09-30T10:45:00.000Z", "2026-09-30T11:00:00.000Z"]
    assert pts[0]["partial"] is True and pts[-1]["partial"] is True and pts[1]["partial"] is False
    assert pts[1]["bytes_in"] == 100 and pts[1]["bytes_out"] == 200 and pts[1]["flows"] == 3
    assert pts[2]["bytes_in"] == 0 and pts[2]["covered"] is True            # measured zero
    assert pts[3]["bytes_in"] is None and pts[3]["covered"] is False         # unknown, not zero
    # Coverage unreadable: every empty bucket is unknown; traffic rows stay.
    pts = H.fill_series(rows, start, end, bucket, None)
    assert [p["bytes_in"] for p in pts] == [None, 100, None, None, None]


def test_groups_section_lists_own_and_inherited_membership():
    N = ipaddress.ip_network
    G = netflow_groups.Group
    netflow_groups.set_groups_for_tests([
        G("p", "Internal LAN", (N("10.0.0.0/8"),)),
        G("c", "Data center", (N("10.30.0.0/24"),), parent_id="p"),
        G("b", "Backup servers", (N("10.30.0.32/27"),)),
        G("v6", "Branch v6", (N("2001:db8:40::/48"),)),
        G("e", "Empty parent", ()),
        G("ec", "Empty child", (N("10.30.0.0/16"),), parent_id="e"),
    ])
    try:
        sec = H._groups_section("10.30.0.40")
        assert sec["primary"] == "Backup servers"
        names = [(i["name"], i["cidr"], i["via_subgroup"]) for i in sec["items"]]
        assert names[0] == ("Backup servers", "10.30.0.32/27", False)
        assert ("Data center", "10.30.0.0/24", False) in names and ("Internal LAN", "10.0.0.0/8", False) in names
        assert ("Empty parent", None, True) in names                     # only through its sub-group
        assert H._groups_section("2001:db8:40::5")["primary"] == "Branch v6"
        assert H._groups_section("192.0.2.1") == {"primary": None, "items": []}
    finally:
        netflow_groups.set_groups_for_tests([])
        netflow_groups.invalidate_groups_cache()


def test_rdns_resolved_at_reads_the_cache_age():
    ip = "192.0.2.250"
    with common._rdns_lock:
        common._rdns_cache[ip] = (time.monotonic() + common._RDNS_TTL - 120, "host.example")
    try:
        at = H._rdns_resolved_at(ip)
        assert abs((datetime.now(UTC) - at).total_seconds() - 120) < 5
        with common._rdns_lock:
            common._rdns_cache[ip] = (time.monotonic() + common._RDNS_NEGATIVE_TTL - 30, None)
        at = H._rdns_resolved_at(ip)
        assert abs((datetime.now(UTC) - at).total_seconds() - 30) < 5      # negative answers use their own TTL
    finally:
        with common._rdns_lock:
            common._rdns_cache.pop(ip, None)
    assert H._rdns_resolved_at(ip) is None


def test_section_error_never_leaks_exception_text():
    timeout = NetflowError(504, "The query took too long", code="query_timeout", error_id="nf-12345678")
    assert H._section_error(timeout) == {"status": "timeout", "error_id": "nf-12345678",
                                         "message": timeout.detail}
    out = H._section_error(RuntimeError("Code: 60. DB::Exception: Table zenplus.secret does not exist"))
    assert out["status"] == "failed" and out["error_id"].startswith("nf-")
    assert "DB::Exception" not in out["message"] and out["error_id"] in out["message"]


def test_geoip_section_explains_private_and_reserved(monkeypatch):
    out = asyncio.run(H._geoip_section("10.1.2.3"))
    assert out["status"] == "not_applicable" and "Private 10/8" in out["message"]
    out = asyncio.run(H._geoip_section("::ffff:192.168.1.1"))
    assert out["status"] == "not_applicable"
    # Outside the private classes but not routed: never called "private".
    for special in ("198.18.255.254", "2001:db8::443", "192.0.2.7"):
        out = asyncio.run(H._geoip_section(special))
        assert out["status"] == "not_applicable" and out["message"].startswith("Special-purpose address"), special
    monkeypatch.setattr(H.geoip, "available", lambda: False)
    out = asyncio.run(H._geoip_section("8.8.8.8"))
    assert out["status"] == "no_database" and out["country"] is None


# ── Live API ─────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B7_API")
PG = os.environ.get("NETFLOW_B7_PG")
ADMIN = (os.environ.get("NETFLOW_B7_USER", "admin"), os.environ.get("NETFLOW_B7_PASS", "admin123"))
PASS = "B7-Hosts-Passw0rd!"
SCOPE_TAG = "b7t-host-scope"
USERS = {"b7_host_viewer": ("viewer", []), "b7_host_mgr": ("operator", []),
         "b7_host_scoped": ("viewer", [SCOPE_TAG])}
DEVICE_ID = "b7b7b7b7-0000-4000-8000-0000000000aa"
SWITCH_ID = "b7b7b7b7-0000-4000-8000-0000000000bb"
UDT_IP = "192.0.2.77"          # TEST-NET-1: never in flows; identity sections only
UDT_MAC = "02:b7:00:00:00:77"
NEVER_IP = "198.18.255.254"    # benchmarking range: never in flows


def _api_up() -> bool:
    if not (API and PG):
        return False
    import requests

    try:
        requests.get(f"{API}/api/v1/health", timeout=3)
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="set NETFLOW_B7_API and NETFLOW_B7_PG")


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

    return requests.request(method, f"{API}/api/v1/netflow{path}", headers=headers, timeout=90, **kw)


def _cleanup():
    return [
        ("DELETE FROM netflow_findings WHERE fingerprint LIKE 'b7t-%'", {}),
        ("DELETE FROM netflow_ti_feeds WHERE name LIKE 'b7t-%'", {}),
        ("DELETE FROM udt_user_logins WHERE user_name = 'b7t.user'", {}),
        ("DELETE FROM udt_endpoints WHERE mac = CAST(:m AS macaddr)", {"m": UDT_MAC}),
        ("DELETE FROM netflow_exporter_devices WHERE device_id = CAST(:d AS uuid)", {"d": DEVICE_ID}),
        ("DELETE FROM devices WHERE id IN (CAST(:d AS uuid), CAST(:s AS uuid))", {"d": DEVICE_ID, "s": SWITCH_ID}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": list(USERS)}),
    ]


def _busy_host(admin):
    """A host with traffic in the last hours and the exporters that saw it."""
    for ip in ("10.30.0.40", "10.20.3.32", "10.20.1.33"):
        r = _req("GET", admin, f"/hosts/{ip}", params={"hours": 6})
        if r.status_code == 200 and (r.json()["seen"] or {}).get("flows") and r.json()["exporters"]["items"]:
            return ip, r.json()
    pytest.skip("no busy simulator host in the scratch flows")


@pytest.fixture(scope="module")
def env():
    from app.core.security import hash_password

    _run_pg(_cleanup())
    admin = _login(*ADMIN)
    ip, profile = _busy_host(admin)
    mapped = {r[0] for r in _run_pg([("SELECT host(exporter_ip) FROM netflow_exporter_devices", {})])[0]}
    candidates = [e["exporter"] for e in profile["exporters"]["items"]
                  if e["exporter"] and ":" not in e["exporter"] and e["exporter"] not in mapped]
    if not candidates:
        pytest.skip("no unmapped IPv4 exporter saw the busy host")
    exporter = candidates[0]
    stmts = [
        ("INSERT INTO devices (id, hostname, ip_address, tags) VALUES (CAST(:d AS uuid), 'b7t-scope-router', "
         "CAST('10.255.77.1' AS inet), CAST(:t AS jsonb)), (CAST(:s AS uuid), 'b7t-access-switch', "
         "CAST('10.255.77.2' AS inet), '[]'::jsonb)",
         {"d": DEVICE_ID, "s": SWITCH_ID, "t": json.dumps([SCOPE_TAG])}),
    ]
    stmts += [("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
               "(:u, :m, :h, :r, CAST(:s AS jsonb))",
               {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": role, "s": json.dumps(scope)})
              for u, (role, scope) in USERS.items()]
    now = datetime.now(UTC)
    stmts += [
        ("WITH e AS (INSERT INTO udt_endpoints (mac, vendor, hostname, ip_address, endpoint_type, first_seen, last_seen) "
         "VALUES (CAST(:m AS macaddr), 'B7 Test Corp', 'b7t-pc', CAST(:ip AS inet), 'workstation', :f, :l) RETURNING id), "
         "loc AS (INSERT INTO udt_endpoint_locations (endpoint_id, device_id, if_index, vlan_id, is_direct, active, "
         "first_seen, last_seen) SELECT id, CAST(:s AS uuid), 7, 77, true, true, :f, :l FROM e RETURNING endpoint_id) "
         "INSERT INTO udt_user_logins (user_name, user_domain, endpoint_id, ip, hostname, event_id, logon_type, event_time) "
         "SELECT 'b7t.user', 'LAB', endpoint_id, CAST(:ip AS inet), 'b7t-pc', 4624, 2, :u FROM loc",
         {"m": UDT_MAC, "ip": UDT_IP, "s": SWITCH_ID, "f": now - timedelta(days=3), "l": now - timedelta(minutes=2),
          "u": now - timedelta(hours=1)}),
        ("WITH f AS (INSERT INTO netflow_ti_feeds (name, source, category, enabled, last_refreshed_at, last_status, "
         "entry_count) VALUES ('b7t-feed', 'upload', 'scanner', true, :r, 'ok', 1) RETURNING id) "
         "INSERT INTO netflow_ti_entries (feed_id, prefix) SELECT id, CAST('192.0.2.0/24' AS cidr) FROM f",
         {"r": now - timedelta(hours=2)}),
        ("INSERT INTO netflow_findings (fingerprint, kind, severity, confidence, status, title, src_ip, dst_ip, "
         "exporter_ip, first_seen, last_seen, occurrences) VALUES "
         "('b7t-own', 'volume_spike', 'warning', 'medium', 'open', 'b7t finding on the scoped exporter', "
         "CAST(:h AS inet), CAST('192.0.2.1' AS inet), CAST(:e AS inet), :f, :l, 2), "
         "('b7t-other', 'volume_spike', 'warning', 'medium', 'open', 'b7t finding elsewhere', "
         "CAST(:h AS inet), CAST('192.0.2.2' AS inet), CAST('203.0.113.250' AS inet), :f, :l, 2), "
         "('b7t-udt', 'threat_intel', 'critical', 'high', 'acknowledged', 'b7t finding on the UDT host', "
         "CAST(:u AS inet), NULL, NULL, :f, :l, 1), "
         "('b7t-closed', 'threat_intel', 'critical', 'high', 'resolved', 'b7t resolved', "
         "NULL, CAST(:u AS inet), NULL, :f, :l, 1)",
         {"h": ip, "e": exporter, "u": UDT_IP, "f": now - timedelta(hours=3), "l": now - timedelta(minutes=5)}),
    ]
    _run_pg(stmts)
    # The link goes through B6's mapping API so the visibility cache is invalidated.
    r = _req("PUT", admin, f"/exporter-devices/{exporter}", json={"device_id": DEVICE_ID, "note": "b7t scope test"})
    assert r.status_code == 200, r.text
    tokens = {u: _login(u) for u in USERS}
    tokens["admin"] = admin
    yield {"tokens": tokens, "host": ip, "exporter": exporter}
    _req("DELETE", admin, f"/exporter-devices/{exporter}")
    _run_pg(_cleanup())


@live
def test_rejects_anything_but_one_address(env):
    viewer = env["tokens"]["b7_host_viewer"]
    for bad in ("10.0.0.0%2F8", "not-an-ip", "300.1.1.1", "fe80::1%25eth0"):
        r = _req("GET", viewer, f"/hosts/{bad}")
        assert r.status_code in (400, 404), (bad, r.status_code)
        if r.status_code == 400:
            assert r.json()["code"] == "invalid_value" and "ip" in r.json()["fields"]


@live
def test_profile_shape_envelope_and_legacy(env):
    admin, ip = env["tokens"]["admin"], env["host"]
    r = _req("GET", admin, f"/hosts/{ip}", params={"hours": 24, "envelope": 1})
    assert r.status_code == 200, r.text
    data, meta = r.json()["data"], r.json()["meta"]
    assert meta["window"]["source"] in ("raw", "rollup", "hybrid") and meta["filters"].get("talker") == ip
    assert data["ip"] == ip and data["version"] == 4 and data["netclass"]
    seen = data["seen"]
    assert seen["flows"] > 0 and seen["first_seen"] <= seen["last_seen"] and seen["source"] == "rollup"
    ts = data["timeseries"]
    assert ts["bucket_s"] > 0 and ts["points"]
    for p in ts["points"]:
        assert (p["bytes_in"] is None) == (not p["covered"])           # unknown only where not covered
    assert ts["totals"]["bytes_in"] + ts["totals"]["bytes_out"] > 0
    peers = data["peers"]
    assert peers["total"] >= len(peers["items"]) and len(peers["items"]) <= H.PEERS_LIMIT
    assert all(p["ip"] != ip for p in peers["items"])
    assert sum(p["bytes"] for p in peers["items"]) <= ts["totals"]["bytes_in"] + ts["totals"]["bytes_out"]
    assert data["applications"]["items"] and data["exporters"]["items"]
    assert {p["role"] for p in data["ports"]["items"]} <= {"server", "client", "unknown"}
    assert data["interfaces"]["source"] == "raw"
    for i in data["interfaces"]["items"]:
        assert i["ifindex"] > 0 and i["direction"] in ("in", "out")
    assert data["rdns"]["status"] in ("ok", "no_record", "pending", "disabled")
    assert data["geoip"]["status"] == "not_applicable"                 # a private simulator address
    assert all(s["status"] == "ok" for s in data["sections"].values()), data["sections"]
    # Legacy shape without envelope=1.
    legacy = _req("GET", admin, f"/hosts/{ip}", params={"hours": 1}).json()
    assert legacy["ip"] == ip and "meta" not in legacy
    # A talker filter is replaced by the host (with a warning), other filters narrow.
    r = _req("GET", admin, f"/hosts/{ip}", params={"hours": 1, "talker": "10.0.0.1", "protocol": 6, "envelope": 1})
    assert r.status_code == 200
    assert any(w["code"] == "talker_replaced" for w in r.json()["meta"]["warnings"])
    assert all(p["protocol"] == 6 for p in r.json()["data"]["ports"]["items"])


@live
def test_unseen_address_is_a_profile_for_admins(env):
    admin = env["tokens"]["admin"]
    r = _req("GET", admin, f"/hosts/{NEVER_IP}", params={"hours": 1})
    assert r.status_code == 200
    d = r.json()
    assert d["seen"]["flows"] == 0 and d["seen"]["first_seen"] is None
    assert d["peers"]["items"] == [] and d["timeseries"]["totals"]["bytes_in"] == 0


@live
def test_udt_identity_ti_and_findings(env):
    t = env["tokens"]
    mgr = _req("GET", t["b7_host_mgr"], f"/hosts/{UDT_IP}", params={"hours": 1}).json()
    ident = mgr["identity"]
    assert ident["status"] == "ok"
    assert ident["endpoint"]["mac"] == UDT_MAC and ident["endpoint"]["vendor"] == "B7 Test Corp"
    assert ident["endpoint"]["current_ip"] is True
    assert ident["location"]["switch"] == "b7t-access-switch" and ident["location"]["vlan_id"] == 77
    assert ident["user"]["status"] == "ok" and ident["user"]["user_name"] == "b7t.user"
    assert ident["user"]["source"] == "domain controller logon"
    # A plain viewer has udt.view but not udt.view_users: no user name.
    viewer = _req("GET", t["b7_host_viewer"], f"/hosts/{UDT_IP}", params={"hours": 1}).json()
    assert viewer["identity"]["endpoint"]["mac"] == UDT_MAC
    assert viewer["identity"]["user"]["status"] == "forbidden" and "user_name" not in viewer["identity"]["user"]
    ti = mgr["threat_intel"]
    assert ti["status"] == "ok" and any(h["feed"] == "b7t-feed" and h["category"] == "scanner" for h in ti["host_hits"])
    f = mgr["findings"]
    assert f["status"] == "ok" and f["counts"]["acknowledged"] >= 1 and f["counts"]["resolved"] >= 1
    titles = {i["title"]: i for i in f["items"]}
    assert "b7t finding on the UDT host" in titles and "b7t resolved" not in titles   # open/acknowledged only
    assert titles["b7t finding on the UDT host"]["role"] == "source"


@live
def test_scoped_viewer_sees_only_its_exporter(env):
    t, ip, exporter = env["tokens"], env["host"], env["exporter"]
    scoped = t["b7_host_scoped"]
    # Hosts its exporter never saw do not exist for it -- same answer as never seen at all.
    assert _req("GET", scoped, f"/hosts/{NEVER_IP}").status_code == 404
    assert _req("GET", scoped, f"/hosts/{UDT_IP}").status_code == 404
    r = _req("GET", scoped, f"/hosts/{ip}", params={"hours": 6, "envelope": 1})
    assert r.status_code == 200, r.text
    data, meta = r.json()["data"], r.json()["meta"]
    assert meta["restricted"] is True
    assert {e["exporter"] for e in data["exporters"]["items"]} == {exporter}
    assert {i["exporter"] for i in data["interfaces"]["items"]} <= {exporter}
    full = _req("GET", t["admin"], f"/hosts/{ip}", params={"hours": 6}).json()
    assert data["seen"]["flows"] <= full["seen"]["flows"]
    assert data["timeseries"]["totals"]["bytes_out"] <= full["timeseries"]["totals"]["bytes_out"]
    titles = {i["title"] for i in data["findings"]["items"]}
    assert "b7t finding on the scoped exporter" in titles and "b7t finding elsewhere" not in titles
    admin_titles = {i["title"] for i in full["findings"]["items"]}
    assert {"b7t finding on the scoped exporter", "b7t finding elsewhere"} <= admin_titles
