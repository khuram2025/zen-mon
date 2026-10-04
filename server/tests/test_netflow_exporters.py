"""NetFlow exporter -> device mapping (WP B6, FE-08): api/v1/netflow/exporters.py.

Findings covered: FE-08 (searchable mapping with evidence, impact preview,
history, guidance), X-16 (mapping writes need netflow.manage, an exporter the
caller can see and a target device in scope; 404 on a missing link without an
audit row; the visibility cache follows writes), X-46 (an explicit mapping
wins over a management-IP match in the listing and for visibility).

Layers:
  * pure helpers (configuration evidence, guidance, hostname matching) — always;
  * live API with its database (NETFLOW_B6_API, e.g. http://127.0.0.1:8116,
    and NETFLOW_B6_PG, e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/zenplus):
    the full link / re-link / unlink lifecycle with an unscoped manager, a
    team-scoped manager, a scoped viewer and a viewer, plus suggestions from
    management address, NCM configuration and discovery sysName. Test users,
    devices, configurations, discovery rows, mappings, settings and their
    audit rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.netflow import exporters as X  # noqa: E402


# ── Configuration evidence ────────────────────────────────────────────────────

IOS = """hostname edge-1
!
interface Loopback0
 description flow export source
 ip address 10.0.0.1 255.255.255.255
!
interface GigabitEthernet0/0/0
 ip address 10.0.0.10 255.255.255.0
!
router bgp 65000
 neighbor 10.0.0.1 remote-as 65001
!
flow exporter ZENPLUS
 destination 192.0.2.10
 source Loopback0
"""


def test_config_evidence_tells_interface_addresses_from_mentions():
    hits = X.config_evidence(IOS, "10.0.0.1")
    assert hits == [
        {"line": 5, "kind": "interface_address", "interface": "Loopback0"},
        {"line": 11, "kind": "mention", "interface": None},
    ], "10.0.0.10 must not match 10.0.0.1"
    assert X.config_evidence(IOS, "10.0.0.10") == [{"line": 8, "kind": "interface_address",
                                                    "interface": "GigabitEthernet0/0/0"}]
    assert X.config_evidence(IOS, "192.0.2.10") == [{"line": 14, "kind": "mention", "interface": None}], \
        "this appliance as flow destination is a mention, not ownership"
    assert X.config_evidence(IOS, "10.0.0.2") == []
    assert X.config_evidence(IOS, "not-an-ip") == []


def test_config_evidence_other_syntaxes():
    junos = "set interfaces lo0 unit 0 family inet address 10.1.1.1/32\nset routing-options router-id 10.1.1.1\n"
    assert X.config_evidence(junos, "10.1.1.1") == [
        {"line": 1, "kind": "interface_address", "interface": "lo0.0"},
        {"line": 2, "kind": "mention", "interface": None},
    ]
    forti = 'config system interface\n    edit "port1"\n        set ip 10.2.2.2 255.255.255.0\n    next\nend\n'
    assert X.config_evidence(forti, "10.2.2.2") == [{"line": 3, "kind": "interface_address", "interface": "port1"}]
    v6 = "interface Loopback0\n ipv6 address 2001:db8::1/128\n!\nipv6 route ::/0 2001:db8::10\n"
    assert X.config_evidence(v6, "2001:db8::1") == [{"line": 2, "kind": "interface_address", "interface": "Loopback0"}]
    many = "\n".join(f"ip route 0.0.0.0 0.0.0.0 10.3.3.3 {i}" for i in range(20))
    assert len(X.config_evidence(many, "10.3.3.3")) == 5, "evidence is bounded"


def test_guidance_for_devices():
    assert X.guidance_for({"snmp_enabled": True, "interfaces": 4, "interfaces_with_speed": 2}) == []
    codes = lambda d: [g["code"] for g in X.guidance_for(d)]  # noqa: E731
    assert codes({"snmp_enabled": False, "interfaces": 0, "interfaces_with_speed": 0}) == ["no_snmp"]
    assert codes({"snmp_enabled": True, "interfaces": 0, "interfaces_with_speed": 0}) == ["no_interfaces"]
    assert codes({"snmp_enabled": True, "interfaces": 3, "interfaces_with_speed": 0}) == ["no_speeds"]
    assert codes({"snmp_enabled": False, "interfaces": 3, "interfaces_with_speed": 0}) == ["no_snmp", "no_speeds"]


def test_hostname_matching():
    assert X._hostname_matches("edge-1.example.com.", "EDGE-1")
    assert X._hostname_matches("edge-1", "edge-1.corp.local")
    assert not X._hostname_matches("edge-10", "edge-1")
    assert not X._hostname_matches("", "edge-1") and not X._hostname_matches("edge-1", None)


# ── Live API ──────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B6_API")
PG = os.environ.get("NETFLOW_B6_PG")
ADMIN = (os.environ.get("NETFLOW_B6_USER", "admin"), os.environ.get("NETFLOW_B6_PASS", "admin123"))
PASS = "B6-Mapping-Passw0rd!"
TAG_A, TAG_B = "b6t-map-team-a", "b6t-map-team-b"
USERS = {
    "b6t_x_viewer": ("viewer", []),
    "b6t_x_mgr": ("operator", []),
    "b6t_x_mgr_a": ("operator", [TAG_A]),
    "b6t_x_scoped": ("viewer", [TAG_A]),
    "b6t_x_ro": ("read_only", []),         # netflow.view without ncm.view
}
EXP = "127.0.0.76"
DEV_A = "b6f00000-0000-4000-8000-0000000000a1"   # team A, SNMP, interfaces with a speed
DEV_B = "b6f00000-0000-4000-8000-0000000000b1"   # team B, management address = EXP
DEV_C = "b6f00000-0000-4000-8000-0000000000c1"   # team A, no SNMP, config assigns EXP to Loopback0
PROFILE = "b6t-map-discovery"


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


def _data(r):
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _listed(headers, **params):
    return {e["exporter_ip"]: e for e in _data(_req("GET", headers, "/exporter-devices", params={"search": EXP, **params}))}


@pytest.fixture(scope="module")
def tokens():
    from app.core.security import hash_password

    names = list(USERS)
    devices = [DEV_A, DEV_B, DEV_C]
    cleanup = [
        ("DELETE FROM netflow_exporter_devices WHERE host(exporter_ip) = :ip", {"ip": EXP}),
        ("DELETE FROM netflow_exporter_settings WHERE host(exporter_ip) = :ip", {"ip": EXP}),
        ("DELETE FROM audit_logs WHERE resource_type IN ('netflow_exporter_settings', 'netflow_exporter_device') "
         "AND resource_id = :ip", {"ip": EXP}),
        ("DELETE FROM discovery_profiles WHERE name = :n", {"n": PROFILE}),
        ("DELETE FROM device_configs WHERE device_id = ANY(CAST(:d AS uuid[]))", {"d": devices}),
        ("DELETE FROM devices WHERE id = ANY(CAST(:d AS uuid[]))", {"d": devices}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
    ]
    _run_pg(cleanup)
    config = (f"hostname b6t-map-c\n!\ninterface Loopback0\n ip address {EXP} 255.255.255.255\n!\n"
              "flow exporter ZENPLUS\n destination 192.0.2.10\n source Loopback0\n")
    setup = [("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
              "(:u, :m, :h, :r, CAST(:s AS jsonb))",
              {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": role, "s": json.dumps(scope)})
             for u, (role, scope) in USERS.items()]
    setup += [("INSERT INTO devices (id, hostname, ip_address, device_type, tags, status, snmp_enabled) VALUES "
               "(CAST(:id AS uuid), :h, CAST(:ip AS inet), 'router', CAST(:t AS jsonb), 'up', :snmp)",
               {"id": d, "h": h, "ip": ip, "t": json.dumps([t]), "snmp": snmp})
              for d, h, ip, t, snmp in ((DEV_A, "b6t-map-a", "10.66.9.1", TAG_A, True),
                                        (DEV_B, "b6t-map-b", EXP, TAG_B, True),
                                        (DEV_C, "b6t-map-c", "10.66.9.3", TAG_A, False))]
    setup += [("INSERT INTO device_interfaces (device_id, if_index, if_name, if_speed) VALUES "
               "(CAST(:d AS uuid), :i, :n, :s)", {"d": DEV_A, "i": i, "n": f"Gi0/{i}", "s": s})
              for i, s in ((1, 1_000_000_000), (2, None))]
    setup.append(("INSERT INTO device_configs (device_id, config_type, content, content_hash, size_bytes, line_count, "
                  "captured_by) VALUES (CAST(:d AS uuid), 'running', :c, 'b6t', :n, 8, 'manual')",
                  {"d": DEV_C, "c": config, "n": len(config)}))
    setup.append(("WITH p AS (INSERT INTO discovery_profiles (name) VALUES (:n) RETURNING id), "
                  "r AS (INSERT INTO discovery_runs (profile_id, trigger_type) SELECT id, 'manual' FROM p RETURNING id, profile_id) "
                  "INSERT INTO discovery_results_v2 (run_id, profile_id, ip_address, sys_name) "
                  "SELECT id, profile_id, CAST(:ip AS inet), 'B6T-MAP-A.example.test' FROM r",
                  {"n": PROFILE, "ip": EXP}))
    _run_pg(setup)
    out = {u: _login(u) for u in USERS}
    out["admin"] = _login(*ADMIN)
    yield out
    _run_pg(cleanup)


def _wait_for_ip_match(headers, timeout_s=25.0):
    """Devices are inserted behind the API's back (not through it), so its
    15 s ownership cache learns about them only on expiry; wait for that."""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        row = _listed(headers).get(EXP)
        if row and row["source"] == "ip-match":
            return row
        if time.monotonic() > deadline:
            return row
        time.sleep(1)


def _audit_count():
    return _run_pg([("SELECT count(*) FROM audit_logs WHERE resource_type = 'netflow_exporter_device' "
                     "AND resource_id = :ip", {"ip": EXP})])[0][0][0]


@live
def test_mapping_lifecycle_scoping_and_precedence(tokens):
    admin, mgr, mgr_a = tokens["admin"], tokens["b6t_x_mgr"], tokens["b6t_x_mgr_a"]
    viewer, scoped = tokens["b6t_x_viewer"], tokens["b6t_x_scoped"]
    # A settings row lists the exporter even before it sends anything.
    assert _req("PUT", admin, f"/exporters/{EXP}/settings", json={"display_name": "b6t exporter"}).status_code == 200

    row = _wait_for_ip_match(admin)
    assert row["source"] == "ip-match" and row["device_id"] == DEV_B and row["display_name"] == "b6t exporter"
    assert row["flows"] == 0 and row["last_seen"] is None
    assert EXP in _listed(admin, unmapped=0) and EXP not in _listed(admin, unmapped=1)
    # Team A cannot see team B's exporter, so it can neither inspect nor claim it (X-16).
    assert EXP not in _listed(scoped)
    assert _req("GET", scoped, f"/exporter-devices/{EXP}/suggestions").status_code == 404
    assert _req("PUT", mgr_a, f"/exporter-devices/{EXP}", json={"device_id": DEV_A}).status_code == 404
    assert _req("PUT", viewer, f"/exporter-devices/{EXP}", json={"device_id": DEV_A}).status_code == 403
    assert _req("PUT", mgr, f"/exporter-devices/{EXP}", json={"device_id": "not-a-uuid"}).status_code == 404
    assert _req("PUT", mgr, f"/exporter-devices/{EXP}", json={"device_id": "b6f00000-0000-4000-8000-00000000dead"}).status_code == 404
    assert _req("PUT", mgr, "/exporter-devices/not-an-ip", json={"device_id": DEV_A}).status_code == 400
    assert _audit_count() == 0, "refused writes are not audited"

    # The impact preview names the address match the link overrides.
    imp = _data(_req("GET", mgr, f"/exporter-devices/{EXP}/impact", params={"device_id": DEV_A}))
    assert imp["current_owner"]["device_id"] == DEV_B and imp["current_owner"]["source"] == "ip-match"
    assert imp["overrides_ip_match"] == {"device_id": DEV_B, "hostname": "b6t-map-b"}
    assert imp["unchanged"] is False and imp["target"]["interfaces"] == 2 and imp["target"]["interfaces_with_speed"] == 1
    assert imp["visibility"]["current_tags"] == [TAG_B] and imp["visibility"]["target_tags"] == [TAG_A]
    assert "overrides_ip_match" in {g["code"] for g in imp["guidance"]}

    r = _req("PUT", mgr, f"/exporter-devices/{EXP}", json={"device_id": DEV_A, "note": "loopback of edge A"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "mapped" and r.json()["device_hostname"] == "b6t-map-a"
    # X-46: the explicit link wins over the management-address match.
    row = _listed(admin)[EXP]
    assert row["source"] == "mapped" and row["device_id"] == DEV_A and row["note"] == "loopback of edge A"
    assert row["overrides_device_id"] == DEV_B and row["overrides_device_hostname"] == "b6t-map-b"
    # The visibility cache follows the write: team A sees it immediately, and its manager may re-link it
    # within the team, but not to a device outside the team.
    assert EXP in _listed(scoped)
    assert EXP in {e["exporter"] for e in _data(_req("GET", scoped, "/exporters/status"))["exporters"]}
    assert _req("PUT", mgr_a, f"/exporter-devices/{EXP}", json={"device_id": DEV_B}).status_code == 404
    assert _req("PUT", mgr_a, f"/exporter-devices/{EXP}", json={"device_id": DEV_C}).status_code == 200
    assert _data(_req("GET", admin, f"/exporter-devices/{EXP}/impact", params={"device_id": DEV_C}))["unchanged"] is True

    hist = _data(_req("GET", admin, f"/exporter-devices/{EXP}/history"))
    mapping = [h for h in hist if h["kind"] == "mapping"]
    assert [h["details"]["device_hostname"] for h in mapping] == ["b6t-map-c", "b6t-map-a"], "newest first"
    assert mapping[0]["details"]["previous_device_hostname"] == "b6t-map-a" and mapping[0]["actor"] == "b6t_x_mgr_a"
    assert mapping[1]["details"]["previous_device_id"] == DEV_B and mapping[1]["details"]["previous_source"] == "ip-match"
    assert any(h["kind"] == "settings" for h in hist)
    # A scoped reader sees the history without the out-of-scope device's name.
    shist = [h for h in _data(_req("GET", scoped, f"/exporter-devices/{EXP}/history")) if h["kind"] == "mapping"]
    assert shist[1]["details"]["previous_device_id"] is None
    assert shist[1]["details"]["previous_device_hostname"] == "a device outside your scope"

    # Unlink: back to the address match (and out of team A's sight); a second unlink is a 404 without audit.
    assert _req("DELETE", viewer, f"/exporter-devices/{EXP}").status_code == 403
    assert _req("DELETE", mgr, f"/exporter-devices/{EXP}").status_code == 204
    before = _audit_count()
    assert _req("DELETE", mgr, f"/exporter-devices/{EXP}").status_code == 404
    assert _audit_count() == before
    row = _listed(admin)[EXP]
    assert row["source"] == "ip-match" and row["device_id"] == DEV_B and row["overrides_device_id"] is None
    assert EXP not in _listed(scoped)
    assert _req("DELETE", admin, f"/exporters/{EXP}/settings").status_code == 204


@live
def test_suggestions_rank_evidence(tokens):
    import time

    deadline = time.monotonic() + 25     # the API's ownership cache may predate the test devices
    while True:
        s = _data(_req("GET", tokens["admin"], f"/exporter-devices/{EXP}/suggestions"))
        if s["current_owner"] or time.monotonic() > deadline:
            break
        time.sleep(1)
    assert s["exporter"] == EXP and s["current_owner"]["device_id"] == DEV_B
    by = {c["device_id"]: c for c in s["candidates"]}
    assert {DEV_A, DEV_B, DEV_C} <= set(by)
    assert {e["kind"] for e in by[DEV_B]["evidence"]} == {"management_ip"} and by[DEV_B]["current_owner"] is True
    assert by[DEV_B]["confidence"] == "high"
    c = by[DEV_C]
    assert c["evidence"][0]["kind"] == "config_interface" and "Loopback0" in c["evidence"][0]["text"]
    assert c["evidence"][0]["line"] == 4 and "ip address" not in json.dumps(c["evidence"]), "line numbers, never config text"
    assert [g["code"] for g in c["guidance"]] == ["no_snmp"]
    assert {e["kind"] for e in by[DEV_A]["evidence"]} == {"sys_name"}
    assert by[DEV_A]["confidence"] == "medium"
    order = [x["device_id"] for x in s["candidates"]]
    assert order.index(DEV_B) < order.index(DEV_C) < order.index(DEV_A), "ranked by evidence strength"
    assert s["sources"]["ncm"]["permitted"] is True and s["sources"]["ncm"]["available"] is True
    assert s["sources"]["discovery"] is True
    # Without ncm.view there is no configuration evidence (and no hint of what a config says).
    ro = _data(_req("GET", tokens["b6t_x_ro"], f"/exporter-devices/{EXP}/suggestions"))
    assert ro["sources"]["ncm"]["permitted"] is False
    assert "config_interface" not in {e["kind"] for c in ro["candidates"] for e in c["evidence"]}
    assert DEV_C not in {c["device_id"] for c in ro["candidates"]}
