"""NetFlow settings (FE-09, CR-12, CR-13): api/v1/netflow/settings.py.

Layers:
  * unit -- CIDR parsing (IPv4 + IPv6, host bits, zone ids, IPv4-mapped),
    group validation (names, colours, tags, parents and cycles, duplicate and
    redundant CIDRs), overlap detection (nested vs unrelated), import parsing,
    application-rule validation (ranges, portless protocols, priorities, wide
    ranges) and shadowing warnings, address-range SQL literals, upload
    unpacking (plain / gzip / tar.gz, size bounds) and GeoIP database status;
  * live API (NETFLOW_B7_API, e.g. http://127.0.0.1:8117, and NETFLOW_B7_PG,
    the API's PostgreSQL, e.g. postgresql+asyncpg://postgres@127.0.0.1:55441/b7_zenplus):
    a viewer, a NetFlow manager (operator) and the administrator exercise the
    IP-group CRUD (403 / 400 fields / 409 / overlaps / nesting / audit rows /
    cache invalidation seen by the ``group=`` filter), import + export, the
    application-rule CRUD, preview and classifier test (cache invalidation
    seen by the ``app=`` filter), enrichment settings, the GeoIP upload
    validation, the visibility notes and the audit history. Test users,
    groups and rules are removed afterwards.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import ipaddress
import json
import os
import shutil
import sys
import tarfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.netflow import settings as S  # noqa: E402
from app.services import geoip, netflow_classify  # noqa: E402

N = ipaddress.ip_network


def _group(gid, name, cidrs, parent=None):
    return {"id": gid, "name": name, "description": None, "cidrs": cidrs, "color": None, "tags": [],
            "parent_id": parent, "created_at": None, "updated_at": None, "created_by": None, "updated_by": None}


# ── CIDR parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,net,note", [
    ("10.0.0.0/8", "10.0.0.0/8", None),
    ("192.0.2.7", "192.0.2.7/32", None),
    ("10.0.0.5/24", "10.0.0.0/24", "host bits"),
    ("2001:db8::/32", "2001:db8::/32", None),
    ("2001:DB8:0:0::1/64", "2001:db8::/64", "host bits"),
    ("::ffff:10.1.0.0/112", "10.1.0.0/16", "IPv4-mapped"),
    ("  172.16.0.0/12 ", "172.16.0.0/12", None),
])
def test_parse_cidr_accepts_both_families(raw, net, note):
    got, err, why = S.parse_cidr(raw)
    assert err is None and str(got) == net
    assert (why is None) if note is None else (note in why)


@pytest.mark.parametrize("raw,fragment", [
    ("", "Empty"),
    ("fe80::1%eth0", "zone ids"),
    ("10.0.0", "not an IP"),
    ("10.0.0.0/33", "not an IP"),
    ("hello", "not an IP"),
    ("010.0.0.1", "not an IP"),
])
def test_parse_cidr_rejects_garbage(raw, fragment):
    got, err, _ = S.parse_cidr(raw)
    assert got is None and fragment in err


def test_range_predicates_use_parsed_literals_only():
    assert S._net_pred("s", N("10.0.0.0/8")) == "(s BETWEEN toIPv6('::ffff:10.0.0.0') AND toIPv6('::ffff:10.255.255.255'))"
    assert S._net_pred("d", N("2001:db8::/126")) == "(d BETWEEN toIPv6('2001:db8::') AND toIPv6('2001:db8::3'))"
    assert S._nets_pred("s", []) == "0"
    both = S._nets_pred("a", [N("10.0.0.0/8"), N("2001:db8::/32")])
    assert both.startswith("((a BETWEEN") and " OR " in both


def test_address_count_collapses_and_labels():
    c = S.address_count([N("10.0.0.0/24"), N("10.0.0.128/25"), N("10.0.1.0/24"), N("2001:db8::/48"), N("2001:db8:5::/48"), N("2001:db8:5:1::/64")])
    assert c["ipv4"] == 512 and c["ipv6_prefixes"] == 2
    assert c["label"] == "512 IPv4 addresses · 2 IPv6 prefixes"
    assert S.address_count([N("192.0.2.1/32")])["label"] == "1 IPv4 address"
    assert S.address_count([])["label"] == "No addresses"


# ── IP-group validation ──────────────────────────────────────────────────────

GROUPS = [
    _group("11111111-1111-4111-8111-111111111111", "Internal LAN", ["10.0.0.0/8"]),
    _group("22222222-2222-4222-8222-222222222222", "HQ", ["10.20.0.0/16"], "11111111-1111-4111-8111-111111111111"),
    _group("33333333-3333-4333-8333-333333333333", "HQ floor 1", ["10.20.1.0/24"], "22222222-2222-4222-8222-222222222222"),
    _group("44444444-4444-4444-8444-444444444444", "Backup", ["10.20.1.128/25", "2001:db8:40::/48"]),
]


def test_validate_group_normalises_and_warns():
    body = S.IpGroupIn(name="  Lab \x07 net ", cidrs=["10.9.0.5/16", "10.9.0.0/16", "10.9.1.0/24", "", "2001:db8:9::/48"],
                       color="#1BAF7A", tags=["site", "Site", " lab ", ""], description="  ")
    row, errors, warnings = S.validate_group(body, GROUPS, None)
    assert not errors
    assert row["name"] == "Lab   net"                     # control characters become spaces
    assert row["cidrs"] == ["10.9.0.0/16", "10.9.1.0/24", "2001:db8:9::/48"]
    assert row["tags"] == ["site", "lab"] and row["color"] == "#1BAF7A" and row["description"] is None
    assert any("host bits" in w for w in warnings)
    assert any("listed twice" in w for w in warnings)
    assert any("10.9.1.0/24 is inside 10.9.0.0/16" in w for w in warnings)


@pytest.mark.parametrize("kw,field,fragment", [
    ({"name": "", "cidrs": ["10.0.0.0/8"]}, "name", "required"),
    ({"name": "x" * 121, "cidrs": ["10.0.0.0/8"]}, "name", "120"),
    ({"name": "internal lan", "cidrs": ["10.0.0.0/8"]}, "name", "already exists"),
    ({"name": "A", "cidrs": []}, "cidrs", "at least one"),
    ({"name": "A", "cidrs": ["10.0.0.0/8", "nope", "1.2.3"]}, "cidrs", "not an IP"),
    ({"name": "A", "cidrs": ["10.0.0.0/8"], "color": "blue"}, "color", "hex"),
    ({"name": "A", "cidrs": ["10.0.0.0/8"], "tags": ["t" * 65]}, "tags", "64"),
    ({"name": "A", "cidrs": ["10.0.0.0/8"], "parent_id": "not-a-uuid"}, "parent_id", "Unknown"),
    ({"name": "A", "cidrs": ["10.0.0.0/8"], "parent_id": str(uuid.uuid4())}, "parent_id", "Unknown"),
])
def test_validate_group_field_errors(kw, field, fragment):
    _row, errors, _w = S.validate_group(S.IpGroupIn(**kw), GROUPS, None)
    assert field in errors and fragment in errors[field], errors


def test_validate_group_rename_and_cycles():
    lan = GROUPS[0]["id"]
    # Renaming a group to its own name (any case) is not a duplicate.
    _r, errors, _w = S.validate_group(S.IpGroupIn(name="INTERNAL LAN", cidrs=["10.0.0.0/8"]), GROUPS, lan)
    assert not errors
    # A group cannot move under itself or its own descendants.
    for parent in (lan, GROUPS[1]["id"], GROUPS[2]["id"]):
        _r, errors, _w = S.validate_group(S.IpGroupIn(name="Internal LAN", cidrs=["10.0.0.0/8"], parent_id=parent),
                                          GROUPS, lan)
        assert "inside itself" in errors["parent_id"]
    # Moving a leaf under an unrelated group is fine.
    _r, errors, _w = S.validate_group(S.IpGroupIn(name="HQ floor 1", cidrs=["10.20.1.0/24"],
                                                  parent_id=GROUPS[3]["id"]), GROUPS, GROUPS[2]["id"])
    assert not errors


def test_whole_family_prefix_warns():
    _r, errors, warnings = S.validate_group(S.IpGroupIn(name="Everything", cidrs=["0.0.0.0/0", "::/0"]), GROUPS, None)
    assert not errors
    assert "0.0.0.0/0 matches every IPv4 address" in warnings and "::/0 matches every IPv6 address" in warnings


def test_overlaps_label_nesting_and_unrelated_groups():
    hq1 = GROUPS[2]
    out = S.overlaps_for(hq1["id"], S._nets_of(hq1["cidrs"]), GROUPS, hq1["parent_id"])
    by = {o["group"]: o for o in out}
    assert by["Internal LAN"]["nested"] is True and by["Internal LAN"]["relation"] == "inside"
    assert by["HQ"]["nested"] is True
    assert by["Backup"]["nested"] is False and by["Backup"]["relation"] == "contains"
    # IPv6 overlaps are found too; families never cross.
    draft = S.overlaps_for(None, [N("2001:db8::/32"), N("192.168.0.0/16")], GROUPS, None)
    assert [(o["group"], o["cidr"], o["relation"]) for o in draft] == [("Backup", "2001:db8::/32", "contains")]
    # A parent sees its descendants as nested.
    lan = GROUPS[0]
    nested = S.overlaps_for(lan["id"], S._nets_of(lan["cidrs"]), GROUPS, None)
    assert {o["group"]: o["nested"] for o in nested} == {"HQ": True, "HQ floor 1": True, "Backup": False}


def test_tree_helpers():
    assert S._descendants(GROUPS, GROUPS[0]["id"]) == {GROUPS[1]["id"], GROUPS[2]["id"]}
    assert S._ancestors(GROUPS, GROUPS[2]["parent_id"]) == [GROUPS[1]["id"], GROUPS[0]["id"]]
    loop = [_group("a", "A", [], "b"), _group("b", "B", [], "a")]
    assert S._ancestors(loop, "a") == ["a", "b"]            # a corrupt loop terminates


def test_import_entries_shapes_and_problems():
    entries, problems = S._import_entries([{"name": "A", "cidrs": ["10.0.0.0/8"]}])
    assert entries[0]["name"] == "A" and not problems
    entries, problems = S._import_entries({"format": "zenplus-netflow-ip-groups", "groups": [
        {"name": "A", "cidrs": ["10.0.0.0/8"], "parent": None, "tags": ["x"]},
        {"name": "a", "cidrs": []},
        {"name": "", "cidrs": []},
        {"name": "B", "cidrs": "10.0.0.0/8"},
        "junk",
    ]})
    assert [e["name"] for e in entries] == ["A"]
    assert any("more than once" in p for p in problems) and any("missing name" in p for p in problems)
    assert any("'cidrs' must be a list" in p for p in problems) and any("not an object" in p for p in problems)
    assert S._import_entries({"nope": 1})[1] == ["The file must be a list of groups or an object with a 'groups' list"]
    assert "At most" in S._import_entries([{"name": str(i), "cidrs": []} for i in range(S.MAX_GROUPS + 1)])[1][0]


# ── Application-rule validation ─────────────────────────────────────────────

def test_validate_rule_normalises():
    row, errors, warnings = S.validate_rule(S.AppRuleIn(application=" Payroll ", protocol=6, port_lo=9443, port_hi=9443,
                                                        cidrs=["10.0.10.7/24", "", "10.0.10.0/24"], priority=50))
    assert not errors
    assert row == {"application": "Payroll", "service": None, "category": None, "protocol": 6, "port_lo": 9443,
                   "port_hi": None, "cidrs": ["10.0.10.0/24"], "priority": 50, "enabled": True}
    assert any("host bits" in w for w in warnings)


@pytest.mark.parametrize("kw,field,fragment", [
    ({"application": "", "port_lo": 80}, "application", "required"),
    ({"application": "x" * 81, "port_lo": 80}, "application", "80"),
    ({"application": "A", "service": "s" * 81, "port_lo": 80}, "service", "80"),
    ({"application": "A", "category": "c" * 65, "port_lo": 80}, "category", "64"),
    ({"application": "A", "protocol": 256}, "protocol", "0-255"),
    ({"application": "A", "port_hi": 90}, "port_lo", "first port"),
    ({"application": "A", "port_lo": 0}, "port_lo", "1-65535"),
    ({"application": "A", "port_lo": 80, "port_hi": 70000}, "port_hi", "1-65535"),
    ({"application": "A", "port_lo": 90, "port_hi": 80}, "port_hi", "below"),
    ({"application": "A", "protocol": 1, "port_lo": 80}, "protocol", "no ports"),
    ({"application": "A", "protocol": 50, "port_lo": 500}, "protocol", "no ports"),
    ({"application": "A", "cidrs": ["10.0.0.0/8", "bad"]}, "cidrs", "not an IP"),
    ({"application": "A"}, "port_lo", "needs a port, a protocol or an address range"),
    ({"application": "A", "port_lo": 80, "priority": -1}, "priority", "0-100000"),
])
def test_validate_rule_field_errors(kw, field, fragment):
    _row, errors, _w = S.validate_rule(S.AppRuleIn(**kw))
    assert field in errors and fragment in errors[field], errors


def test_validate_rule_accepts_protocol_and_address_rules_and_warns():
    _row, errors, warnings = S.validate_rule(S.AppRuleIn(application="GRE tunnels", protocol=47))
    assert not errors and not warnings
    _row, errors, _w = S.validate_rule(S.AppRuleIn(application="Partner", cidrs=["2001:db8::/32"]))
    assert not errors
    _row, errors, warnings = S.validate_rule(S.AppRuleIn(application="Low", port_lo=5000, port_hi=9000,
                                                         priority=netflow_classify.BUILTIN_PRIORITY))
    assert not errors
    assert any("at or below the built-in rules" in w for w in warnings)
    assert any("4001 ports" in w for w in warnings)
    # SCTP / UDP-Lite / DCCP carry ports, so a port rule on them is allowed.
    for proto in (33, 132, 136):
        assert not S.validate_rule(S.AppRuleIn(application="X", protocol=proto, port_lo=9000))[1]


def test_rule_kind():
    assert S._rule_kind({"port_lo": 80, "cidrs": ["10.0.0.0/8"]}) == "port"
    assert S._rule_kind({"port_lo": None, "cidrs": ["10.0.0.0/8"]}) == "address"
    assert S._rule_kind({"port_lo": None, "cidrs": []}) == "protocol"


def test_rule_conflicts_name_the_rule_that_ranks_first():
    # Below the built-ins, TCP 443 stays "Web": the draft is shadowed.
    row, _e, _w = S.validate_rule(S.AppRuleIn(application="My portal", protocol=6, port_lo=443, priority=5000))
    warnings = S._rule_conflicts(row, [], None)
    assert warnings and "443/tcp" in warnings[0] and netflow_classify.APP_WEB in warnings[0]
    # At the default priority the operator rule wins: nothing to warn about.
    row, _e, _w = S.validate_rule(S.AppRuleIn(application="My portal", protocol=6, port_lo=443))
    assert S._rule_conflicts(row, [], None) == []
    # Another enabled operator rule with a lower number shadows it; a disabled one does not.
    other = {"id": "r1", "application": "Earlier", "service": None, "category": None, "protocol": 6,
             "port_lo": 400, "port_hi": 500, "cidrs": [], "priority": 10, "enabled": True}
    w = S._rule_conflicts(row, [other], None)
    assert w and "Earlier (db)" in w[0]
    assert S._rule_conflicts(row, [{**other, "enabled": False}], None) == []
    assert S._rule_conflicts(row, [other], "r1") == []          # editing that very rule
    # Protocol / address rules have no port shadowing.
    row, _e, _w = S.validate_rule(S.AppRuleIn(application="GRE", protocol=47))
    assert S._rule_conflicts(row, [], None) == []


def test_builtin_catalogue_is_read_only_view_of_the_classifier():
    cat = S._builtin_catalogue(netflow_classify.build_rules())
    by = {c["application"]: c for c in cat}
    web = by[netflow_classify.APP_WEB]
    ports = {p["port"]: p for p in web["ports"]}
    assert ports[443]["transports"] == ["TCP", "UDP"] or ports[443]["transports"] == ["TCP"]
    assert by[netflow_classify.APP_VPN]["protocols"], "tunnel protocols (GRE, ESP, ...) are listed"
    assert all(c["category"] for c in cat)
    assert [c["application"].lower() for c in cat] == sorted(c["application"].lower() for c in cat)


# ── Uploads and GeoIP status ─────────────────────────────────────────────────

def test_read_upload_unpacks_plain_gzip_and_tar():
    payload = b"\x00mmdb-bytes\xab\xcd\xefMaxMind.com"
    assert S._read_upload(payload) == payload
    assert S._read_upload(gzip.compress(payload)) == payload
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("GeoLite2-ASN_20260901/README.txt", b"readme"),
                           ("GeoLite2-ASN_20260901/GeoLite2-ASN.mmdb", payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    assert S._read_upload(buf.getvalue()) == payload
    empty = io.BytesIO()
    with tarfile.open(fileobj=empty, mode="w:gz") as tar:
        info = tarfile.TarInfo("README")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="no .mmdb"):
        S._read_upload(empty.getvalue())


def test_read_upload_bounds_decompression(monkeypatch):
    monkeypatch.setattr(S, "MAX_GEOIP_BYTES", 1024)
    with pytest.raises(ValueError, match="too large"):
        S._read_upload(gzip.compress(b"MMDB" + b"x" * 50_000))


def _real_mmdb_dir() -> str | None:
    for d in (os.environ.get("NETFLOW_GEOIP_DIR"), "/opt/zenplus/data/geoip"):
        if d and os.path.exists(os.path.join(d, "asn.mmdb")):
            return d
    return None


def test_geoip_status_reports_missing_invalid_and_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(geoip, "GEOIP_DIR", str(tmp_path))
    st = S.geoip_status()
    assert st["writable"] is True and st["available"] is False
    assert {d["kind"]: d["present"] for d in st["databases"]} == {"country": False, "asn": False}
    (tmp_path / "country.mmdb").write_bytes(b"definitely not a database")
    bad = {d["kind"]: d for d in S.geoip_status()["databases"]}["country"]
    assert bad["present"] is True and bad["valid"] is False and "Not a readable MaxMind database" in bad["error"]
    src = _real_mmdb_dir()
    if src is None:
        pytest.skip("no DB-IP / GeoLite2 asn.mmdb on this host")
    shutil.copy(os.path.join(src, "asn.mmdb"), tmp_path / "asn.mmdb")
    (tmp_path / "asn.mmdb.stamp").write_text("2026-09\n")
    asn = {d["kind"]: d for d in S.geoip_status()["databases"]}["asn"]
    assert asn["valid"] is True and asn["build_date"].endswith("Z") and isinstance(asn["age_days"], int)
    assert asn["stamp"] == "2026-09" and "asn" in asn["database_type"].lower()
    assert asn["sample"]["ip"] == "1.1.1.1" and asn["stale"] == (asn["age_days"] > S.GEOIP_STALE_DAYS)


def test_enrichment_settings_defaults_and_bounds():
    class Row(tuple):
        pass

    class Res:
        def __init__(self, row):
            self.row = row

        def first(self):
            return self.row

    class FakeDB:
        def __init__(self, row):
            self.row = row

        async def execute(self, *_a, **_k):
            return Res(self.row)

    async def load(row):
        return await S.load_enrichment_settings(FakeDB(row))

    assert asyncio.run(load(None)) == {"rdns": S.RDNS_DEFAULTS, "updated_at": None}
    got = asyncio.run(load(Row(({"rdns": {"enabled": False, "deadline_s": 0.5}}, None))))
    assert got["rdns"] == {"enabled": False, "deadline_s": 0.5}
    # Out-of-range or mistyped stored values fall back to the defaults.
    got = asyncio.run(load(Row((json.dumps({"rdns": {"enabled": "no", "deadline_s": 99}}), None))))
    assert got["rdns"] == S.RDNS_DEFAULTS


# ── Live API ─────────────────────────────────────────────────────────────────

API = os.environ.get("NETFLOW_B7_API")
PG = os.environ.get("NETFLOW_B7_PG")
ADMIN = (os.environ.get("NETFLOW_B7_USER", "admin"), os.environ.get("NETFLOW_B7_PASS", "admin123"))
PASS = "B7-Settings-Passw0rd!"
USERS = {"b7_set_viewer": ("viewer", []), "b7_set_mgr": ("operator", [])}
PREFIX = "b7t-"


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


def _cleanup_sql():
    names = list(USERS)
    return [
        ("DELETE FROM netflow_ip_groups WHERE name LIKE :p", {"p": PREFIX + "%"}),
        ("DELETE FROM netflow_app_rules WHERE application LIKE :p", {"p": PREFIX + "%"}),
        ("DELETE FROM audit_logs WHERE actor_username = ANY(:u)", {"u": names}),
        ("DELETE FROM users WHERE username = ANY(:u)", {"u": names}),
    ]


@pytest.fixture(scope="module")
def tokens():
    from app.core.security import hash_password

    _run_pg(_cleanup_sql())
    _run_pg([("INSERT INTO users (username, email, password_hash, role, scope_tags) VALUES "
              "(:u, :m, :h, :r, CAST(:s AS jsonb))",
              {"u": u, "m": f"{u}@example.test", "h": hash_password(PASS), "r": role, "s": json.dumps(scope)})
             for u, (role, scope) in USERS.items()])
    out = {u: _login(u) for u in USERS}
    out["admin"] = _login(*ADMIN)
    yield out
    _run_pg(_cleanup_sql())


def _req(method, headers, path, **kw):
    import requests

    return requests.request(method, f"{API}/api/v1/netflow{path}", headers=headers, timeout=60, **kw)


def _audit(action, resource_id=None):
    sql = "SELECT action, metadata FROM audit_logs WHERE action = :a"
    params = {"a": action}
    if resource_id:
        sql += " AND resource_id = :r"
        params["r"] = resource_id
    return _run_pg([(sql + " ORDER BY created_at DESC", params)])[0]


@live
def test_ip_group_crud_permissions_validation_and_audit(tokens):
    viewer, mgr = tokens["b7_set_viewer"], tokens["b7_set_mgr"]
    listing = _req("GET", viewer, "/settings/ip-groups")
    assert listing.status_code == 200 and listing.json()["can_manage"] is False
    assert _req("GET", mgr, "/settings/ip-groups").json()["can_manage"] is True
    body = {"name": PREFIX + "lab", "cidrs": ["10.77.0.0/16", "2001:db8:77::/48"], "color": "#1baf7a", "tags": ["lab"]}
    assert _req("POST", viewer, "/settings/ip-groups", json=body).status_code == 403
    # Field errors come back per field.
    r = _req("POST", mgr, "/settings/ip-groups", json={**body, "cidrs": ["10.77.0.0/16", "nope"], "color": "red"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_value"
    assert set(r.json()["fields"]) == {"cidrs", "color"}
    r = _req("POST", mgr, "/settings/ip-groups", json=body)
    assert r.status_code == 201, r.text
    lab = r.json()["item"]
    assert lab["cidrs"] == ["10.77.0.0/16", "2001:db8:77::/48"] and lab["created_by"] == "b7_set_mgr"
    assert lab["addresses"] == {"ipv4": 65536, "ipv6_prefixes": 1, "label": "65,536 IPv4 addresses · 1 IPv6 prefix"}
    assert _audit("netflow.ip_group.create", lab["id"])
    # Duplicate names (any case) are a field error before the database sees them.
    dup = _req("POST", mgr, "/settings/ip-groups", json={**body, "name": (PREFIX + "LAB")})
    assert dup.status_code == 400 and "already exists" in dup.json()["fields"]["name"]
    # A sub-group: nested overlap is labelled, an unrelated one warns.
    r = _req("POST", mgr, "/settings/ip-groups", json={"name": PREFIX + "lab-servers", "cidrs": ["10.77.1.0/24"],
                                                       "parent_id": lab["id"]})
    assert r.status_code == 201
    child = r.json()["item"]
    assert child["parent_name"] == PREFIX + "lab" and child["depth"] == 1
    assert {o["group"]: o["nested"] for o in child["overlaps"]}.get(PREFIX + "lab") is True
    r = _req("POST", mgr, "/settings/ip-groups", json={"name": PREFIX + "other", "cidrs": ["10.77.1.128/25"]})
    other = r.json()["item"]
    assert any(o["group"] == PREFIX + "lab-servers" and not o["nested"] for o in other["overlaps"])
    # The parent's effective ranges include the child; listing shows children.
    items = {g["name"]: g for g in _req("GET", mgr, "/settings/ip-groups").json()["items"]}
    assert items[PREFIX + "lab"]["children"] == [PREFIX + "lab-servers"]
    # Cycles are refused; stale copies conflict.
    r = _req("PUT", mgr, f"/settings/ip-groups/{lab['id']}", json={**body, "parent_id": child["id"]})
    assert r.status_code == 400 and "inside itself" in r.json()["fields"]["parent_id"]
    r = _req("PUT", mgr, f"/settings/ip-groups/{lab['id']}",
             json={**body, "description": "edited", "expected_updated_at": lab["updated_at"]})
    assert r.status_code == 200 and r.json()["item"]["description"] == "edited"
    r = _req("PUT", mgr, f"/settings/ip-groups/{lab['id']}",
             json={**body, "description": "stale", "expected_updated_at": lab["updated_at"]})
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    before_after = _audit("netflow.ip_group.update", lab["id"])[0][1]
    assert before_after["after"]["description"] == "edited"
    # Unknown ids are 404; deleting a parent detaches its children.
    assert _req("PUT", mgr, f"/settings/ip-groups/{uuid.uuid4()}", json=body).status_code == 404
    assert _req("DELETE", mgr, "/settings/ip-groups/not-a-uuid").status_code == 404
    assert _req("DELETE", viewer, f"/settings/ip-groups/{lab['id']}").status_code == 403
    r = _req("DELETE", mgr, f"/settings/ip-groups/{lab['id']}")
    assert r.status_code == 200 and r.json()["detached_children"] == [PREFIX + "lab-servers"]
    items = {g["name"]: g for g in _req("GET", mgr, "/settings/ip-groups").json()["items"]}
    assert PREFIX + "lab" not in items and items[PREFIX + "lab-servers"]["parent_id"] is None
    assert _audit("netflow.ip_group.delete", lab["id"])


@live
def test_group_writes_reach_the_group_filter_immediately(tokens):
    mgr = tokens["b7_set_mgr"]
    name = PREFIX + "filter-probe"
    q = {"hours": 1, "group": name, "envelope": 1}
    unknown = _req("GET", mgr, "/settings/ip-groups/usage", params=q)
    assert unknown.status_code == 400 and "group" in unknown.text.lower()
    r = _req("POST", mgr, "/settings/ip-groups", json={"name": name, "cidrs": ["10.0.0.0/8"]})
    assert r.status_code == 201
    gid = r.json()["item"]["id"]
    ok = _req("GET", mgr, "/settings/ip-groups/usage", params=q)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["meta"]["filters"].get("group") == name
    row = next(i for i in body["data"]["items"] if i["id"] == gid)
    assert set(row) == {"id", "name", "hosts", "bytes", "flows"}
    _req("DELETE", mgr, f"/settings/ip-groups/{gid}")
    assert _req("GET", mgr, "/settings/ip-groups/usage", params=q).status_code == 400


@live
def test_group_usage_and_preview(tokens):
    viewer = tokens["b7_set_viewer"]
    r = _req("GET", viewer, "/settings/ip-groups/usage", params={"hours": 24, "envelope": 1})
    assert r.status_code == 200
    data, meta = r.json()["data"], r.json()["meta"]
    assert meta["window"]["source"] in ("raw", "rollup", "hybrid") and "server_time" in meta
    assert data["total"]["bytes"] >= data["ungrouped"]["bytes"] >= 0
    for item in data["items"]:
        assert item["bytes"] <= data["total"]["bytes"]
    # Legacy shape without the envelope.
    assert "items" in _req("GET", viewer, "/settings/ip-groups/usage", params={"hours": 1}).json()
    # A draft preview: normalised CIDRs, problems, overlaps and traffic.
    r = _req("POST", viewer, "/settings/ip-groups/preview", params={"hours": 24, "envelope": 1},
             json={"name": "draft", "cidrs": ["10.0.0.5/8", "bogus", "2001:db8::/32"]})
    assert r.status_code == 200
    p = r.json()["data"]
    assert "bogus" in p["errors"]["cidrs"] and p["cidrs"] == ["10.0.0.0/8", "2001:db8::/32"]
    assert any("host bits" in w for w in p["warnings"])
    t = p["traffic"]
    assert t is not None and 0 <= t["bytes"] <= t["total_bytes"] and len(t["top_hosts"]) <= 10
    assert t["hosts"] >= len(t["top_hosts"])
    if t["top_hosts"]:
        assert all(ipaddress.ip_address(h["ip"]) for h in t["top_hosts"])
    # Nothing valid: no traffic query at all.
    r = _req("POST", viewer, "/settings/ip-groups/preview", json={"cidrs": ["nope"]})
    assert r.status_code == 200 and r.json()["traffic"] is None


@live
def test_import_export_round_trip(tokens):
    viewer, mgr = tokens["b7_set_viewer"], tokens["b7_set_mgr"]
    doc = {"format": "zenplus-netflow-ip-groups", "version": 1, "groups": [
        {"name": PREFIX + "imp-parent", "cidrs": ["10.88.0.0/16"], "color": "#2a78d6", "tags": ["site"]},
        {"name": PREFIX + "imp-child", "cidrs": ["10.88.1.0/24"], "parent": PREFIX + "imp-parent"},
    ]}
    assert _req("POST", viewer, "/settings/ip-groups/import", json={"document": doc}).status_code == 403
    dry = _req("POST", mgr, "/settings/ip-groups/import", json={"document": doc, "dry_run": True}).json()
    assert dry["dry_run"] and sorted(dry["created"]) == sorted(g["name"] for g in doc["groups"]) and not dry["problems"]
    names = {g["name"] for g in _req("GET", mgr, "/settings/ip-groups").json()["items"]}
    assert PREFIX + "imp-parent" not in names                     # a dry run writes nothing
    applied = _req("POST", mgr, "/settings/ip-groups/import", json={"document": doc, "dry_run": False})
    assert applied.status_code == 200, applied.text
    items = {g["name"]: g for g in _req("GET", mgr, "/settings/ip-groups").json()["items"]}
    assert items[PREFIX + "imp-child"]["parent_name"] == PREFIX + "imp-parent"
    assert _audit("netflow.ip_groups.import")
    # Re-import: unchanged; a change is an update.
    again = _req("POST", mgr, "/settings/ip-groups/import", json={"document": doc, "dry_run": True}).json()
    assert sorted(again["unchanged"]) == sorted(g["name"] for g in doc["groups"]) and not again["created"]
    doc2 = json.loads(json.dumps(doc))
    doc2["groups"][1]["cidrs"] = ["10.88.2.0/24"]
    upd = _req("POST", mgr, "/settings/ip-groups/import", json={"document": doc2, "dry_run": True}).json()
    assert upd["updated"] == [PREFIX + "imp-child"]
    # Any bad entry rejects the whole file (400 when applying).
    bad = {"groups": doc["groups"] + [{"name": PREFIX + "imp-bad", "cidrs": ["300.1.1.1"]},
                                       {"name": PREFIX + "imp-orphan", "cidrs": ["10.0.0.0/8"], "parent": "nowhere"}]}
    r = _req("POST", mgr, "/settings/ip-groups/import", json={"document": bad, "dry_run": False})
    assert r.status_code == 400 and len(r.json()["result"]["problems"]) == 2
    loop = {"groups": [{"name": PREFIX + "l1", "cidrs": ["10.1.0.0/16"], "parent": PREFIX + "l2"},
                       {"name": PREFIX + "l2", "cidrs": ["10.2.0.0/16"], "parent": PREFIX + "l1"}]}
    assert any("loops" in p for p in _req("POST", mgr, "/settings/ip-groups/import",
                                          json={"document": loop}).json()["problems"])
    # Export carries every group with parents by name, and imports back cleanly.
    exp = _req("GET", viewer, "/settings/ip-groups/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    exported = exp.json()
    assert exported["format"] == "zenplus-netflow-ip-groups"
    child = next(g for g in exported["groups"] if g["name"] == PREFIX + "imp-child")
    assert child["parent"] == PREFIX + "imp-parent"
    rt = _req("POST", mgr, "/settings/ip-groups/import", json={"document": exported, "dry_run": True}).json()
    assert not rt["problems"] and not rt["created"] and not rt["updated"] and not rt["deleted"]


@live
def test_app_rule_crud_preview_and_classifier(tokens):
    viewer, mgr = tokens["b7_set_viewer"], tokens["b7_set_mgr"]
    app = PREFIX + "Secure portal"
    rule = {"application": app, "category": "Business apps", "service": "Portal", "protocol": 6, "port_lo": 443,
            "priority": 10}
    assert _req("POST", viewer, "/settings/app-rules", json=rule).status_code == 403
    r = _req("POST", mgr, "/settings/app-rules", json={**rule, "protocol": 1})
    assert r.status_code == 400 and "no ports" in r.json()["fields"]["protocol"]
    # Preview before saving: TCP 443 traffic in the last hour moves from Web to the new application.
    pv = _req("POST", viewer, "/settings/app-rules/preview", params={"hours": 1, "envelope": 1}, json={"rule": rule})
    assert pv.status_code == 200, pv.text
    p, meta = pv.json()["data"], pv.json()["meta"]
    assert meta["window"]["source"] == "raw"
    assert p["total"]["bytes"] >= p["changed"]["bytes"] >= 0
    if p["changed"]["bytes"]:
        assert all(c["to"] == app for c in p["changes"])
        assert any(c["from"] == netflow_classify.APP_WEB for c in p["changes"])
        assert all(e["port"] == 443 for c in p["changes"] for e in c["examples"])
    assert _req("POST", viewer, "/settings/app-rules/preview", json={}).status_code == 400
    # The classifier test shows current vs candidate.
    t = _req("POST", viewer, "/settings/applications/test",
             json={"protocol": 6, "src_port": 51514, "dst_port": 443, "rule": rule}).json()
    assert t["current"]["application"] == netflow_classify.APP_WEB and t["candidate"]["application"] == app
    assert t["candidate"]["service_port"] == 443 and t["candidate"]["direction"] == "request"
    bad = _req("POST", viewer, "/settings/applications/test", json={"protocol": 6, "src_ip": "10.0.0.0/8"})
    assert bad.status_code == 400 and "src_ip" in bad.json()["fields"]
    # Save it: the classifier (app= filter) knows the name at once.
    q = {"hours": 1, "app": app, "envelope": 1}
    assert _req("GET", mgr, "/settings/ip-groups/usage", params=q).status_code == 400
    r = _req("POST", mgr, "/settings/app-rules", json=rule)
    assert r.status_code == 201, r.text
    saved = r.json()["item"]
    assert saved["kind"] == "port" and saved["protocol_name"] == "TCP" and saved["created_by"] == "b7_set_mgr"
    assert _audit("netflow.app_rule.create", saved["id"])
    assert _req("GET", mgr, "/settings/ip-groups/usage", params=q).status_code == 200
    assert app in _req("GET", viewer, "/settings/applications").json()["applications"]
    now = _req("POST", viewer, "/settings/applications/test", json={"protocol": 6, "src_port": 51514, "dst_port": 443}).json()
    assert now["current"]["application"] == app and now["candidate"] is None
    # Removal preview: the reverse change.
    rm = _req("POST", viewer, "/settings/app-rules/preview", params={"hours": 1}, json={"replace_id": saved["id"]}).json()
    assert all(c["from"] == app for c in rm["changes"])
    # Disable with PATCH: classification falls back to Web.
    r = _req("PATCH", mgr, f"/settings/app-rules/{saved['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["item"]["enabled"] is False
    back = _req("POST", viewer, "/settings/applications/test", json={"protocol": 6, "src_port": 51514, "dst_port": 443}).json()
    assert back["current"]["application"] == netflow_classify.APP_WEB
    # PUT with a stale copy conflicts; a fresh one updates.
    r = _req("PUT", mgr, f"/settings/app-rules/{saved['id']}", json={**rule, "expected_updated_at": saved["updated_at"]})
    assert r.status_code == 409
    listing = _req("GET", mgr, "/settings/app-rules").json()
    cur = next(i for i in listing["items"] if i["id"] == saved["id"])
    r = _req("PUT", mgr, f"/settings/app-rules/{saved['id']}",
             json={**rule, "port_hi": 444, "enabled": True, "expected_updated_at": cur["updated_at"]})
    assert r.status_code == 200 and r.json()["item"]["port_hi"] == 444
    # A lower-ranked rule on the same port is told who wins.
    r = _req("POST", mgr, "/settings/app-rules", json={**rule, "application": PREFIX + "Shadowed", "priority": 50})
    assert r.status_code == 201 and any(app in w for w in r.json()["warnings"])
    _req("DELETE", mgr, f"/settings/app-rules/{r.json()['item']['id']}")
    assert _req("DELETE", viewer, f"/settings/app-rules/{saved['id']}").status_code == 403
    assert _req("DELETE", mgr, f"/settings/app-rules/{saved['id']}").status_code == 200
    assert _req("DELETE", mgr, f"/settings/app-rules/{saved['id']}").status_code == 404
    assert _audit("netflow.app_rule.delete", saved["id"])


@live
def test_application_catalogue_and_exporter_names(tokens):
    viewer = tokens["b7_set_viewer"]
    cat = _req("GET", viewer, "/settings/applications").json()
    assert cat["builtin"] and cat["builtin_priority"] == netflow_classify.BUILTIN_PRIORITY
    assert len(cat["precedence"]) == 4 and netflow_classify.APP_WEB in cat["applications"]
    r = _req("GET", viewer, "/settings/applications/exporter-names", params={"hours": 24, "envelope": 1})
    assert r.status_code == 200
    data = r.json()["data"]
    cov = data["coverage"]
    assert cov["records"] >= cov["with_app_id"] >= 0
    assert cov["share"] is None or 0 <= cov["share"] <= 1
    for item in data["items"]:
        assert int(item["app_id"]) == (item["engine"] << 32) | item["selector"]
        assert item["updated_at"] is None or item["updated_at"].endswith("Z")


@live
def test_enrichment_settings_and_geoip_upload_validation(tokens):
    viewer, mgr = tokens["b7_set_viewer"], tokens["b7_set_mgr"]
    st = _req("GET", viewer, "/settings/enrichment").json()
    assert st["can_manage"] is False and {"enabled", "deadline_s", "cache", "resolvers"} <= set(st["rdns"])
    assert {d["kind"] for d in st["geoip"]["databases"]} == {"country", "asn"}
    original = {"enabled": st["rdns"]["enabled"], "deadline_s": st["rdns"]["deadline_s"]}
    assert _req("PUT", viewer, "/settings/enrichment/rdns", json={"enabled": False}).status_code == 403
    r = _req("PUT", mgr, "/settings/enrichment/rdns", json={"enabled": True, "deadline_s": 60})
    assert r.status_code == 400 and "deadline_s" in r.json()["fields"]
    try:
        r = _req("PUT", mgr, "/settings/enrichment/rdns", json={"enabled": False, "deadline_s": 1.5})
        assert r.status_code == 200 and r.json()["rdns"] == {"enabled": False, "deadline_s": 1.5}
        assert _req("GET", viewer, "/settings/enrichment").json()["rdns"]["enabled"] is False
        assert _audit("netflow.enrichment.update")[0][1]["after"] == {"enabled": False, "deadline_s": 1.5}
    finally:
        _req("PUT", tokens["admin"], "/settings/enrichment/rdns", json=original)
    t = _req("POST", mgr, "/settings/enrichment/rdns/test", json={"ip": "127.0.0.1"}).json()
    assert t["status"] in ("ok", "no_record", "pending") and t["ip"] == "127.0.0.1"
    assert _req("POST", mgr, "/settings/enrichment/rdns/test", json={"ip": "10.0.0.0/8"}).status_code == 400
    assert _req("POST", viewer, "/settings/enrichment/rdns/flush").status_code == 403
    assert "dropped" in _req("POST", mgr, "/settings/enrichment/rdns/flush").json()
    # GeoIP upload: validation never touches the installed files.
    files = {"file": ("x.mmdb", b"not a database", "application/octet-stream")}
    assert _req("POST", viewer, "/settings/enrichment/geoip", data={"kind": "asn"}, files=files).status_code == 403
    r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "city"}, files=files)
    assert r.status_code == 400 and "kind" in r.json()["fields"]
    r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "asn"}, files=files)
    assert r.status_code == 400 and "MaxMind" in r.json()["fields"]["file"]
    r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "asn"},
             files={"file": ("x.mmdb.gz", b"\x1f\x8b\x08junk", "application/gzip")})
    assert r.status_code == 400 and "file" in r.json()["fields"]
    r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "asn"}, files={"file": ("e.mmdb", b"", "x/y")})
    assert r.status_code == 400 and "empty" in r.json()["fields"]["file"]
    after = _req("GET", viewer, "/settings/enrichment").json()["geoip"]["databases"]
    assert [(d["kind"], d["size"], d["build_date"]) for d in after] == \
        [(d["kind"], d["size"], d["build_date"]) for d in st["geoip"]["databases"]]


@live
def test_geoip_upload_installs_a_valid_database(tokens):
    """Re-installs the API's own country database (gzip'ed): same content,
    so other users of the directory see no change except the stamp."""
    mgr = tokens["b7_set_mgr"]
    st = _req("GET", mgr, "/settings/enrichment").json()["geoip"]
    country = next(d for d in st["databases"] if d["kind"] == "country")
    path = os.path.join(st["directory"], "country.mmdb")
    if not (country["valid"] and st["writable"] and os.access(path, os.R_OK)):
        pytest.skip("the API's GeoIP directory has no readable, valid country database")
    stamp_path = path + ".stamp"
    stamp = Path(stamp_path).read_text() if os.path.exists(stamp_path) else None
    data = Path(path).read_bytes()
    try:
        # An ASN database offered as the country database is refused.
        asn_path = os.path.join(st["directory"], "asn.mmdb")
        if os.path.exists(asn_path):
            r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "country"},
                     files={"file": ("asn.mmdb", Path(asn_path).read_bytes(), "application/octet-stream")})
            assert r.status_code == 400 and "not a country database" in r.json()["fields"]["file"]
        r = _req("POST", mgr, "/settings/enrichment/geoip", data={"kind": "country"},
                 files={"file": ("country.mmdb.gz", gzip.compress(data, 1), "application/gzip")})
        assert r.status_code == 200, r.text
        installed = next(d for d in r.json()["databases"] if d["kind"] == "country")
        assert installed["valid"] and installed["size"] == len(data) and installed["stamp"].startswith("upload:")
        assert _audit("netflow.enrichment.geoip_upload", "country")
        assert not [f for f in os.listdir(st["directory"]) if ".upload-" in f]   # no temp files left
    finally:
        if stamp is not None:
            Path(stamp_path).write_text(stamp)


@live
def test_visibility_notes_and_history(tokens):
    viewer, mgr = tokens["b7_set_viewer"], tokens["b7_set_mgr"]
    r = _req("GET", viewer, "/settings/visibility", params={"hours": 24, "envelope": 1})
    assert r.status_code == 200
    data, meta = r.json()["data"], r.json()["meta"]
    assert meta["window"]["source"] == "raw"
    tot = data["totals"]
    assert tot["bytes"] >= tot["tunnel_bytes"] >= 0
    for k in ("tunnel_share", "nat_share", "app_id_share", "legacy_share"):
        assert tot[k] is None or 0 <= tot[k] <= 1
    assert sum(e["records"] for e in data["exporters"]) == tot["records"]
    assert set(data["notes"]) == {"tunnels", "nat", "app_id"}
    # Longer than 7 days is refused (raw scan bound).
    assert _req("GET", viewer, "/settings/visibility", params={"hours": 24 * 8}).status_code == 400
    assert _req("GET", viewer, "/settings/history", params={"resource": "ip_group"}).status_code == 403
    h = _req("GET", mgr, "/settings/history", params={"resource": "ip_group", "limit": 5})
    assert h.status_code == 200 and len(h.json()["items"]) <= 5
    for item in h.json()["items"]:
        assert item["action"].startswith("netflow.ip_group")


def test_rdns_config_is_cached_and_bounded(monkeypatch):
    calls = []

    async def fake_load(db=None):
        calls.append(db)
        return {"rdns": {"enabled": False, "deadline_s": 0.5}, "updated_at": None}

    monkeypatch.setattr(S, "load_enrichment_settings", fake_load)
    monkeypatch.setitem(S._rdns_config_cache, "value", None)
    first = asyncio.run(S.rdns_config())
    second = asyncio.run(S.rdns_config())
    assert first == second == {"enabled": False, "deadline_s": 0.5} and len(calls) == 1
    first["enabled"] = True                                   # callers get a copy
    assert asyncio.run(S.rdns_config())["enabled"] is False
    monkeypatch.setitem(S._rdns_config_cache, "at", 0.0)      # expired -> re-read
    asyncio.run(S.rdns_config())
    assert len(calls) == 2
    monkeypatch.setitem(S._rdns_config_cache, "value", None)
