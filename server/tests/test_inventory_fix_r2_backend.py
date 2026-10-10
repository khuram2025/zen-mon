"""Regression tests of the Phase 1 re-check (fix round 2, be-d).

Every test names the finding it pins. Sections:

NB-01 / NQ-04   a version with a superscript digit made the natural-sort key raise (HTTP 500 on the Software catalog, the product
                page, the installations sort, both CSV exports and the OS tree)
BN-08           since/until of year 0000 were an HTTP 500 (pydantic raised before the route's range check)
NF-3            ``inventory_fleet.add_assets`` double-encoded ``tags`` (a scope test built on it could pass vacuously)
NL-02           the keep-stored-OS rule kept a MikroTik board name stored by rule-pack revision 1 through the revision-2 re-render
NF-BE-01        the OS export labelled an unparseable version 'older' where the OS page counts it 'unknown'
NB-03           the software CSV left its REPEATABLE READ transaction (a fan-out to other connections, a session closed to
                wait for a shared build) while the documentation promised one snapshot
NB-04           a version row's host count was summed across 20,000-triple statements, double counting a host on both sides of a cut
NF-BE-02        the summary's version and host counts ignored the hidden-category filter that its installs apply
NB-07           a version spelled '.0' had the empty equivalence key of a blank version and matched every blank-version row
UP-R2-02 / NL-05 / FV1-N3 / NQ-05   endpoint-protection rules: utilities filed as protection, common spellings read as none
                (revision 3 of the rule pack; the re-key is silent)

The oracles are plain SQL or plain Python over data the test inserted, independent of the code under test.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.services import host_metric_service as hms
from app.services import inventory_export as export
from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from app.services import inventory_software_query as isq
from app.services import versioning
from inventory_fleet import (
    SCOPED_TAG, add_assets, add_software_rows, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock,
    sessions,  # noqa: F401  (fixtures)
)
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures)
from test_inventory_service import add_device, event_count, ident, inv_db, tick  # noqa: F401  (inv_db is a fixture)
from test_inventory_fix_r2_endpoint_protection import add_row, add_server as add_ep_server, flyout, full_ticks, view
from test_inventory_fix_software import seed_products

API = "/api/v1/inventory"


@contextlib.contextmanager
def client(db, tags=(), export_perm=True):
    """An API client whose requests answer a server error with its 500 (what a browser would get), not by raising."""
    perms = ["inventory.view"] + (["inventory.export"] if export_perm else [])
    with api_client(db, role="t_r2_be", permissions=perms, scope_tags=tuple(tags), persist_user=export_perm) as c:
        yield TestClient(c.app, raise_server_exceptions=False)


def rows_of(response) -> list[dict]:
    text = response.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


# ════════════════════════════════════════════════════════════════════
# NB-01 / NQ-04: the natural-sort key is total
# ════════════════════════════════════════════════════════════════════

# Every one of these once broke a read path: '²' and '³' are str.isdigit() but not regex digits, so int() raised.
HOSTILE_VERSIONS = [
    "1.0²", "²", "2³", "1²3", "12.3²", "1.0⁰", "1.0¹", "１２３", "1.０", "٣.٤", "१२३.४", "v①", "1.0½", "1.๑", "1.0²-beta",
    "9" * 120 + ".1", "1." + "0" * 118 + "²", "", "  ", ".", "..0", "-", "0", "000", "0.0.0", "7.2", "7.10", "7.2.0", "V7.2",
]


def test_nb01_natural_key_is_total_over_hostile_text():
    """NB-01 / NQ-04: no input raises, in any of the three places that carry the key, and they order identically."""
    samples = HOSTILE_VERSIONS + ["9" * 400, "1." + "9" * 400, "9" * 5000, "1." + "9" * 5000 + "²", "\ud800", "x\x00y", "İ1", "ǅ2",
                                  "1" * 4300 + "²" + "2" * 4300]
    for raw in samples:
        a = versioning.natural_key(raw)
        b = iq._natural_key(raw)
        c = hms._natural_key(raw)
        assert b == (0, a) and c == a, repr(raw)[:60]
    assert iq._natural_key(None) == (1, ()) and versioning.natural_key(None) == () and versioning.natural_key(7) == ()
    # and a list of all of them sorts, in both directions, with None last (the key tuples compare with each other)
    for key in (iq._natural_key, hms._natural_key):
        sorted(samples + [None], key=key, reverse=True)
        sorted(samples, key=key)


def test_nb01_natural_key_orders_digit_runs_by_value_without_converting_them():
    order = ["0", "1", "1.0", "1.2", "1.10", "1.100", "2", "10", "10²", "9" * 400, "9" * 400 + "1", "1" * 4999 + "1"]
    keys = [versioning.natural_key(x) for x in order]
    assert keys == sorted(keys)                       # each is not greater than the next: the list is in natural order
    assert versioning.natural_key("007") == versioning.natural_key("7")                     # leading zeros do not count
    assert versioning.natural_key("V7.2") == versioning.natural_key("v7.2")                 # case does not count
    assert versioning.natural_key("7.2") < versioning.natural_key("7.10") < versioning.natural_key("17.9")
    # a digit that int() accepts but the ASCII class does not is text, so it sorts after every ASCII digit run
    assert versioning.natural_key("１") != versioning.natural_key("1")
    assert versioning.natural_key("1") < versioning.natural_key("１")


def test_nb01_the_parsers_survive_a_5000_digit_run():
    """No parser or comparator converts an unbounded digit run: input over 256 characters is refused (None), never an error
    (``int()`` itself refuses runs over 4,300 digits)."""
    long_run = "9" * 5000
    for scheme in versioning.SCHEMES:
        assert versioning.parse(scheme, long_run) is None
        assert versioning.compare(scheme, long_run, "1.0") is None
        assert versioning.canonicalize(scheme, long_run) is None
        assert versioning.release_cycle(scheme, long_run + "²") is None
        versioning.compare(scheme, "1." + "9" * 200, "1." + "9" * 200 + "²")      # inside the cap, still total
    assert versioning.equivalence_key(long_run + ".0") == long_run


def free_servers(db, title: str, count: int) -> list[str]:
    """``count`` fleet servers that hold no row titled ``title`` yet (the table is keyed by server and title)."""
    names = [r[0] for r in db.query(
        "SELECT display_name FROM servers s WHERE NOT EXISTS (SELECT 1 FROM server_software_inventory w "
        "WHERE w.server_id = s.id AND w.package_name = $1) ORDER BY display_name LIMIT $2", title, count)]
    assert len(names) == count
    return names


def _seed_hostile_software(db) -> None:
    """The same product ('Google Chrome') and a product of its own at every hostile version, one server each."""
    chrome = [v for v in HOSTILE_VERSIONS if v.strip()][:14]
    add_software_rows(db, [{"server": n, "title": "Google Chrome", "vendor": "Google LLC", "version": v}
                           for n, v in zip(free_servers(db, "Google Chrome", len(chrome)), chrome)])
    add_software_rows(db, [{"server": n, "title": "Zzz Rare Tool", "vendor": "Zzz Ltd", "version": v}
                           for n, v in zip(free_servers(db, "Zzz Rare Tool", 3), ["1.0²", "2³", "²"])])


def test_nb01_every_software_read_path_survives_a_superscript_version(fleet_db_fresh, pinned_clock):
    """NB-01: the catalog (every sort, both orders), the product page, the installations sort=version and the CSV exports used
    to answer 500 for everyone as soon as one agent reported '12.3²'."""
    _seed_hostile_software(fleet_db_fresh)
    with client(fleet_db_fresh) as h:
        for sort in isq.CATALOG_SORTS:
            for order in ("asc", "desc"):
                r = h.get(f"{API}/software", params={"sort": sort, "order": order, "page_size": 500})
                assert r.status_code == 200, (sort, order, r.status_code, r.text[:200])
        assert h.get(f"{API}/software").status_code == 200                                          # the default page
        found = {x["product_key"] for x in h.get(f"{API}/software", params={"q": "zzz rare tool"}).json()["items"]}
        assert "zzz-rare-tool" in found
        page = h.get(f"{API}/software/google-chrome")
        assert page.status_code == 200, page.text[:200]
        assert {v["version"] for v in page.json()["versions"]} >= {"1.0²", "2³", "12.3²"}
        assert h.get(f"{API}/software/zzz-rare-tool").status_code == 200
        for key in ("google-chrome", "zzz-rare-tool"):
            for sort in isq.INSTALL_SORTS:
                for order in ("asc", "desc"):
                    r = h.get(f"{API}/software/{key}/installations", params={"sort": sort, "order": order, "page_size": 500})
                    assert r.status_code == 200, (key, sort, order, r.status_code, r.text[:200])
            assert h.get(f"{API}/software/{key}/installations", params={"version": "1.0²"}).status_code == 200
        ex = h.get(f"{API}/export", params={"dataset": "software", "page_size": 500})
        assert ex.status_code == 200, ex.text[:200]
        assert any(r["product_key"] == "zzz-rare-tool" for r in rows_of(ex))
        for sort in ("name", "latest_version", "version_count"):
            assert h.get(f"{API}/export", params={"dataset": "software", "sort": sort}).status_code == 200, sort
        inst = h.get(f"{API}/export", params={"dataset": "installations", "product": "google-chrome", "sort": "version"})
        assert inst.status_code == 200, inst.text[:200]
        assert {r["version"] for r in rows_of(inst)} >= {"1.0²", "2³", "12.3²"}


def test_nb01_installations_sorted_by_version_is_ordered_not_merely_answered(fleet_db_fresh, pinned_clock):
    """The order is the comparator's for versions it can order, then the natural order for the rest (superscripts among them)."""
    versions = ["9.0", "10.0", "10.0²", "2³", "1.0²"]
    add_software_rows(fleet_db_fresh, [{"server": n, "title": "Google Chrome", "vendor": "Google LLC", "version": v}
                                       for n, v in zip(free_servers(fleet_db_fresh, "Google Chrome", len(versions)), versions)])
    with client(fleet_db_fresh) as h:
        r = h.get(f"{API}/software/google-chrome/installations", params={"sort": "version", "order": "asc", "page_size": 500})
        assert r.status_code == 200, r.text[:200]
        versions = [x["version"] for x in r.json()["items"] if x["package_name"] == "Google Chrome"]
        ranked = [v for v in versions if v in ("9.0", "10.0")]
        assert ranked == ["9.0", "10.0"]                       # 9 before 10, numerically
        assert set(versions) >= {"1.0²", "2³", "10.0²"}
        desc = h.get(f"{API}/software/google-chrome/installations", params={"sort": "version", "order": "desc", "page_size": 500})
        assert desc.status_code == 200
        assert [x["version"] for x in desc.json()["items"] if x["version"] in ("9.0", "10.0")] == ["10.0", "9.0"]


def test_nb01_nq04_the_os_tree_and_both_os_exports_survive_a_superscript_version(fleet_db_fresh, pinned_clock):
    """NQ-04: two Cisco IOS XE devices with '17.9.4' and '17.9.4²' made GET /inventory/os answer 500."""
    devices = []
    for i, (rel, ver) in enumerate([("17.9", "17.9.4"), ("17.9", "17.9.4²"), ("1.0²", "2³"), ("²", "²"), ("17.9", "１７.９.４"),
                                    ("17.9", "17.9." + "9" * 100), ("17.12", "17.12.1")]):
        devices.append({"name": f"r2-xe-{i}", "vendor": "Cisco", "model": "C9300", "os_family": "ios-xe", "os_product": "IOS XE",
                        "os_release": rel, "os_version": ver})
    add_assets(fleet_db_fresh, devices=devices)
    with client(fleet_db_fresh) as h:
        for params in ({}, {"kind": "device"}, {"kind": "server"}):
            r = h.get(f"{API}/os", params=params)
            assert r.status_code == 200, (params, r.status_code, r.text[:200])
        assert h.get(f"{API}/hardware").status_code == 200
        assert h.get(f"{API}/summary").status_code == 200
        ex = h.get(f"{API}/export", params={"dataset": "os"})
        assert ex.status_code == 200, ex.text[:200]
        assert any(r["version"] == "17.9.4²" for r in rows_of(ex))
        for sort in iq.SORT_KEYS:
            r = h.get(f"{API}/assets", params={"sort": sort, "page_size": 500, "os_family": "ios-xe"})
            assert r.status_code == 200, (sort, r.status_code, r.text[:200])
        assert h.get(f"{API}/export", params={"dataset": "assets", "sort": "os_version"}).status_code == 200


# ════════════════════════════════════════════════════════════════════
# BN-08 residual: since/until are text, parsed once
# ════════════════════════════════════════════════════════════════════

def test_bn08_parse_instant_never_raises_anything_but_invalid_filter():
    for raw in ["0000-01-01T00:00:00Z", "0000-01-01", "0000-12-31T23:59:59-14:00", "", " ", "x", "\x00", "9" * 64, "1e999",
                "2026-02-30T00:00:00Z", "2026-10-01T25:00:00Z", "+10000-01-01T00:00:00Z", "١٢٣٤-01-01", "2026-10-01T00:00:00+99:99"]:
        with pytest.raises(iq.InvalidFilter):
            iq.parse_instant("since", raw)
    assert iq.parse_instant("since", None) is None
    # the spellings the routes accepted before keep working
    for raw in ["2026-10-01T00:00:00Z", "2026-10-01T00:00:00.123Z", "2026-10-01T00:00:00+05:30", "2026-10-01", "2026-10-01 10:00:00",
                "1700000000", "1700000000.5"]:
        assert iq.parse_instant("since", raw) is not None, raw


def test_bn08_no_inventory_route_declares_a_date_time_parameter():
    """The fix is not specific to /changes and /export: no Inventory route lets FastAPI/pydantic parse a date by itself."""
    from app.main import app

    spec = app.openapi()
    offenders = []
    for path, ops in spec["paths"].items():
        if "/inventory" not in path:
            continue
        for method, op in ops.items():
            for param in op.get("parameters", []):
                schema = param.get("schema", {})
                variants = [schema] + list(schema.get("anyOf", [])) + list(schema.get("oneOf", []))
                if any(v.get("format") in ("date-time", "date") for v in variants):
                    offenders.append((method, path, param["name"]))
    assert offenders == []
    names = {p["name"] for p in spec["paths"]["/api/v1/inventory/changes"]["get"]["parameters"]}
    assert {"since", "until"} <= names                    # and the two parameters still exist


@pytest.mark.parametrize("params", [
    {"since": "0000-01-01T00:00:00Z"}, {"until": "0000-01-01"}, {"since": "0000-06-15T00:00:00+14:00", "tz": "Pacific/Kiritimati"},
    {"since": "2026-10-01T00:00:00Z", "until": "0000-12-31T23:59:59Z"},
])
def test_bn08_year_zero_is_422_on_changes_and_on_the_changes_export(fleet_db, pinned_clock, params):
    with client(fleet_db) as h:
        r = h.get(f"{API}/changes", params=params)
        assert r.status_code == 422, (params, r.status_code, r.text[:200])
        e = h.get(f"{API}/export", params={"dataset": "changes", **{k: v for k, v in params.items() if k != "tz"}})
        assert e.status_code == 422, (params, e.status_code, e.text[:200])
        # a bad date is refused before the export asks for a slot: the next export is not 429
        ok = h.get(f"{API}/export", params={"dataset": "changes", "since": "2026-10-01T00:00:00Z"})
        assert ok.status_code == 200, ok.text[:200]


def test_bn08_the_text_forms_still_work_and_a_long_value_is_422(fleet_db, pinned_clock):
    with client(fleet_db) as h:
        for params in ({"since": "2026-10-01"}, {"since": "2026-10-01 10:00:00"}, {"since": "1790000000"},
                       {"since": "2026-09-01T00:00:00Z", "until": "2026-10-01T00:00:00+02:00"}):
            r = h.get(f"{API}/changes", params={"page_size": 1, **params})
            assert r.status_code == 200, (params, r.text[:200])
        assert h.get(f"{API}/changes", params={"since": "2" * 65}).status_code == 422


# ════════════════════════════════════════════════════════════════════
# NF-3: add_assets stores tags as a jsonb array
# ════════════════════════════════════════════════════════════════════

def test_nf3_add_assets_tags_are_a_jsonb_array_that_a_scope_predicate_matches(fleet_db_fresh):
    add_assets(fleet_db_fresh,
               devices=[{"name": "nf3-dev", "tags": [SCOPED_TAG, "other"], "vendor": "Cisco", "model": "C9300"}],
               servers=[{"name": "nf3-srv", "tags": [SCOPED_TAG]}, {"name": "nf3-untagged"}])
    kinds = {r[0]: r[1] for r in fleet_db_fresh.query(
        "SELECT hostname, jsonb_typeof(tags) FROM devices WHERE hostname = 'nf3-dev' UNION ALL "
        "SELECT display_name, jsonb_typeof(tags) FROM servers WHERE display_name LIKE 'nf3-%'")}
    assert kinds == {"nf3-dev": "array", "nf3-srv": "array", "nf3-untagged": "array"}
    assert fleet_db_fresh.scalar("SELECT count(*) FROM devices WHERE tags ?| array[$1]", SCOPED_TAG) >= 1
    assert fleet_db_fresh.scalar("SELECT count(*) FROM servers WHERE display_name = 'nf3-srv' AND tags ?| array[$1]", SCOPED_TAG) == 1
    # and a scoped API user sees exactly the assets built with the tag, not zero
    with client(fleet_db_fresh, tags=(SCOPED_TAG,)) as h:
        names = {a["name"] for a in h.get(f"{API}/assets", params={"page_size": 500}).json()["items"]}
        assert {"nf3-dev", "nf3-srv"} <= names and "nf3-untagged" not in names


# ════════════════════════════════════════════════════════════════════
# NL-02: a rule-pack re-render is authoritative over the keep-stored-OS rule
# ════════════════════════════════════════════════════════════════════

MIKROTIK = dict(vendor="MikroTik", sys_object_id="1.3.6.1.4.1.14988.1", snmp_enabled=True)


def store_revision_1_state(db, device_id, *, os_version: str) -> None:
    """The identity row exactly as the first build's loop (rule-pack revision 1) left it for a RouterOS device whose profile
    regex had put a board name into the version: family routeros, version = the board name, source 'profile'."""
    db.execute(
        "UPDATE inventory_identity SET os_family = 'routeros', os_product = 'RouterOS', os_release = NULL, os_version = $2, "
        "os_build = NULL, os_edition = NULL, os_source = 'profile', norm_rev = 1, values_rev = 1 "
        "WHERE asset_kind = 'device' AND asset_id = $1", device_id, os_version)


@pytest.mark.asyncio
async def test_nl_02_a_board_name_stored_by_revision_1_is_cleared_by_the_revision_2_re_render(inv_db):
    """NL-02: rev 1 stored 'RB4011iGS+' as the RouterOS version; the rev 2 pack returns no version for it, but the keep rule
    (a missing source is a gap, not a change) kept the stored block, so only NEW devices were corrected."""
    board = add_device(inv_db, "mt-board", model="RB4011iGS+", os_version="RB4011iGS+", sys_descr="RouterOS RB4011iGS+", **MIKROTIK)
    hap = add_device(inv_db, "mt-hap", model="hAP ac^2", os_version="hAP ac^2", sys_descr="RouterOS hAP ac^2", **MIKROTIK)
    real = add_device(inv_db, "mt-real", model="RB5009UG+S+", os_version="7.12.1", sys_descr="RouterOS RB5009UG+S+IN", **MIKROTIK)
    await tick(inv_db)
    for dev in (board, hap, real):                                    # the state an appliance upgraded from revision 1 holds
        store_revision_1_state(inv_db, dev, os_version=inv_db.scalar("SELECT os_version FROM devices WHERE id = $1", dev))
    inv_db.execute("DELETE FROM inventory_change_events")
    assert (ident(inv_db, "device", board)["os_version"], ident(inv_db, "device", hap)["os_version"]) == ("RB4011iGS+", "hAP ac^2")

    result = await tick(inv_db)
    assert result.stale == 3

    for dev in (board, hap):
        row = ident(inv_db, "device", dev)
        assert (row["os_version"], row["os_source"], row["os_release"]) == (None, None, None), row
        assert row["os_family"] == "routeros" and revs(inv_db, dev) == (inventory_rev(), inventory_rev())
    kept = ident(inv_db, "device", real)
    assert (kept["os_version"], kept["os_release"], kept["os_source"]) == ("7.12.1", "7.12", "profile")
    assert event_count(inv_db) == 0, "a re-render writes no history"


def inventory_rev() -> int:
    return norm.RULEPACK_REV


def revs(db, device_id) -> tuple[int, int]:
    """(norm_rev, values_rev) of a device's identity row."""
    return tuple(db.query("SELECT norm_rev, values_rev FROM inventory_identity WHERE asset_kind = 'device' AND asset_id = $1",
                          device_id)[0])


@pytest.mark.asyncio
async def test_nl_02_within_one_revision_a_gap_still_keeps_the_stored_block(inv_db):
    """The keep rule's own job is unchanged: at the same revision a missing source is a gap, not a change."""
    dev = add_device(inv_db, "mt-gap", model="RB5009UG+S+", os_version="7.12.1", sys_descr="RouterOS RB5009UG+S+IN", **MIKROTIK)
    await tick(inv_db)
    before = ident(inv_db, "device", dev)
    assert before["os_version"] == "7.12.1" and revs(inv_db, dev) == (inventory_rev(), inventory_rev())
    inv_db.execute("UPDATE devices SET os_version = NULL, sys_descr = 'RouterOS RB5009UG+S+IN (gap)' WHERE id = $1", dev)
    result = await tick(inv_db)
    assert result.dirty == 1
    assert ident(inv_db, "device", dev) == before
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_nl_02_a_later_re_render_replaces_a_version_the_pack_still_accepts_without_an_event(inv_db, monkeypatch):
    """A re-render writes the new pack's value (also a changed one) and still no history."""
    dev = add_device(inv_db, "mt-moves", model="RB5009UG+S+", os_version="7.12.1", sys_descr="RouterOS RB5009UG+S+IN", **MIKROTIK)
    await tick(inv_db)
    monkeypatch.setattr(norm, "RULEPACK_REV", norm.RULEPACK_REV + 1)
    result = await tick(inv_db)
    assert result.stale == 1
    row = ident(inv_db, "device", dev)
    assert row["os_version"] == "7.12.1" and revs(inv_db, dev) == (norm.RULEPACK_REV, norm.RULEPACK_REV)
    assert event_count(inv_db) == 0


# ════════════════════════════════════════════════════════════════════
# UP-R2-02 / NL-05 / FV1-N3 / NQ-05: the endpoint-protection rules
# ════════════════════════════════════════════════════════════════════

# Titles that are NOT an antivirus, EPP or EDR engine, yet start like one.
NOT_AN_ENGINE = [
    "Malwarebytes Support Tool", "Malwarebytes Windows Firewall Control", "Malwarebytes Management Console", "Malwarebytes Nebula",
    "Check Point Endpoint Security VPN", "Check Point Endpoint Connect", "Check Point Capsule VPN", "Check Point Remote Access VPN",
    "FortiClient VPN", "ZenPlus Agent", "McAfee Agent", "ESET Management Agent", "Kaspersky Security Center Network Agent",
    "Cisco AnyConnect Secure Mobility Client", "Zscaler", "Sysmon64", "Wazuh Agent", "Nessus Agent (x64)", "Bitwarden",
    "Bitdefender GravityZone", "Google Chrome", "7-Zip 23.01 (x64)",
]
ENGINES = [
    "Malwarebytes", "Malwarebytes version 4.6.8.311", "Malwarebytes Endpoint Agent", "Malwarebytes Anti-Malware", "Malwarebytes Premium",
    "Check Point Endpoint Security", "Check Point Harmony Endpoint", "Cylance PROTECT", "CylancePROTECT", "Cylance Unified Agent",
    "BlackBerry Protect", "Comodo Client - Security", "COMODO Client Security", "Comodo Endpoint Security", "F-Secure Client",
    "F-Secure Client Security", "WithSecure Elements Agent", "CrowdStrike Windows Sensor", "SentinelOne Agent", "Sophos Endpoint Agent",
]


def software_rules() -> list[dict]:
    with open(norm._DATA_DIR / "software_rules.json", encoding="utf-8") as fh:
        return json.load(fh)["rules"]


@pytest.mark.parametrize("title", NOT_AN_ENGINE)
def test_up_r2_02_nl_05_a_utility_or_vpn_client_is_not_endpoint_protection(title):
    """UP-R2-02 (Malwarebytes Support Tool, Firewall Control) and NL-05 (Check Point Endpoint Security VPN): a server whose only
    such title is one of these must read as unprotected, not 'covered'."""
    assert norm.normalize_software(title, None, "1.0").category != iq.ENDPOINT_PROTECTION_CATEGORY


@pytest.mark.parametrize("title", ENGINES)
def test_fv1_n3_nq_05_the_common_spellings_of_an_engine_are_endpoint_protection(title):
    """FV1-N3 / NQ-05: 'Cylance PROTECT' (with a space), 'Comodo Client - Security' and 'F-Secure Client' read as no protection."""
    assert norm.normalize_software(title, None, "1.0").category == iq.ENDPOINT_PROTECTION_CATEGORY


def test_up_r2_02_no_endpoint_protection_pattern_matches_a_known_non_engine_title():
    """The guard behind test_UP01 (which reads product names) runs every endpoint-protection PATTERN against the titles that are
    not engines: the Malwarebytes and Check Point prefixes matched them."""
    for rule in software_rules():
        if rule["category"] != iq.ENDPOINT_PROTECTION_CATEGORY:
            continue
        rx = re.compile(rule["name_re"], re.IGNORECASE)
        for title in NOT_AN_ENGINE:
            assert not rx.search(title), (rule["id"], title)


def test_up_r2_02_check_point_vpn_client_has_a_rule_of_its_own():
    for title in ("Check Point Endpoint Security VPN", "Check Point Endpoint Connect"):
        key = norm.normalize_software(title, "Check Point Software Technologies Ltd.", "E88.20")
        assert key.category == "remote-access" and key.name_key == "check-point-vpn-client"


@pytest.mark.asyncio
async def test_up_r2_02_the_revision_3_re_key_is_silent_and_moves_the_state(fleet_db_fresh, pinned_clock):
    """Rows keyed by revision 2 re-key under revision 3 with no change event: the Malwarebytes utility stops counting as
    protection, 'Cylance PROTECT' starts to. In between, a stored endpoint-protection row still counts (present) and a server
    with rows waiting for the re-key is 'normalising', never 'missing'."""
    db = fleet_db_fresh
    util = add_ep_server(db, "r2-mwb-tool")
    add_row(db, util, "Malwarebytes Support Tool", source="hklm", norm_rev=norm.RULEPACK_REV - 1, category="endpoint-protection",
            name_key="malwarebytes")
    cyl = add_ep_server(db, "r2-cylance")
    add_row(db, cyl, "Cylance PROTECT", source="hklm", norm_rev=norm.RULEPACK_REV - 1, category="other", name_key="cylance-protect")
    events_before = db.scalar("SELECT count(*) FROM inventory_change_events")
    iq.clear_caches()
    with view(db) as c:
        assert flyout(c, util)["state"] == "present"
        assert (flyout(c, cyl)["state"], flyout(c, cyl)["reason"]) == ("unknown", "normalising")
    await full_ticks(db)
    iq.clear_caches()
    with view(db) as c:
        assert flyout(c, util)["state"] == "missing"
        f = flyout(c, cyl)
        assert f["state"] == "present" and f["products"] == ["Cylance PROTECT"]
    assert db.scalar("SELECT count(*) FROM inventory_change_events") == events_before
    assert db.scalar("SELECT min(norm_rev) FROM server_software_inventory") == norm.RULEPACK_REV


# ════════════════════════════════════════════════════════════════════
# NB-07: '.0' is a version, not a blank
# ════════════════════════════════════════════════════════════════════

def test_nb_07_dot_zero_hosts_installs_assets_installations_and_export_agree(fleet_db_fresh, pinned_clock):
    """NB-07: the '.0' version row said hosts 2, the Assets list behind it 6 and the installations list 8, because '.0' reduced
    to the empty key and also matched every row with no version (NULL, '' and blanks)."""
    db = fleet_db_fresh
    versions = [None, "", "  ", ".0", ".0", "..0", "1.0", "1.0.0"]
    add_software_rows(db, [{"server": n, "title": "Acme Nb07 Tool", "vendor": "Acme", "version": v}
                           for n, v in zip(free_servers(db, "Acme Nb07 Tool", len(versions)), versions)])
    key = db.scalar("SELECT name_key FROM server_software_inventory WHERE package_name = 'Acme Nb07 Tool' LIMIT 1")
    with client(fleet_db_fresh) as h:
        page = h.get(f"{API}/software/{key}").json()
        by = {v["version"]: v for v in page["versions"]}
        assert by[".0"]["hosts"] == 2 and by[".0"]["installs"] == 2
        assert page["unversioned"]["hosts"] == 3 and page["unversioned"]["installs"] == 3          # NULL, '' and '  '
        assert page["header"]["version_count"] == len(page["versions"]) == 3                        # '.0', '..0' and '1.0.0' (= '1.0')
        for v in page["versions"]:
            assets = h.get(f"{API}/assets", params={"product": key, "product_version": v["version"], "page_size": 500}).json()["total"]
            inst = h.get(f"{API}/software/{key}/installations", params={"version": v["version"]}).json()["total"]
            exported = rows_of(h.get(f"{API}/export", params={"dataset": "installations", "product": key, "version": v["version"]}))
            assert assets == v["hosts"] and inst == v["installs"] == len(exported), v
        none = h.get(f"{API}/software/{key}/installations", params={"version": isq.NO_VERSION}).json()["total"]
        assert none == page["unversioned"]["installs"] == 3
        # '..0' is its own version (the key '.'): it does not fall into '.0' either
        assert by["..0"]["hosts"] == 1


# ════════════════════════════════════════════════════════════════════
# NF-BE-01: the OS export and the OS page bucket a version the same way
# ════════════════════════════════════════════════════════════════════

def test_nf_be_01_the_os_export_buckets_agree_with_the_pages_build_currency(fleet_db_fresh, pinned_clock):
    """NF-BE-01: 'weird-build' (no parse) was 'older' in the CSV and 'unknown' on the page, so summing the CSV by bucket gave
    older 6 / unknown 1 against the page's older 5 / unknown 2."""
    versions = ["7.2.0"] * 5 + ["7.2A", "7.2a", "7.1.9.0", "7.1.09", "7.0.1", "weird-build", "build-2", None]
    add_assets(fleet_db_fresh, devices=[
        {"name": f"nfbe01-{i}", "vendor": "Acme", "model": "A1", "os_family": "other", "os_product": "Acme OS", "os_release": "7.2",
         "os_version": v} for i, v in enumerate(versions)])
    with client(fleet_db_fresh) as h:
        tree = h.get(f"{API}/os").json()
        release = next(r for f in tree["families"] for p in f["products"] if p["product"] == "Acme OS" for r in p["releases"]
                       if r["release"] == "7.2")
        cur = release["currency"]
        assert cur is not None and cur["unknown"] >= 3                       # weird-build, build-2 and the asset with no version
        exported = [r for r in rows_of(h.get(f"{API}/export", params={"dataset": "os"}))
                    if r["product"] == "Acme OS" and r["release"] == "7.2"]
    sums: dict[str, int] = {}
    for r in exported:
        sums[r["build_currency"]] = sums.get(r["build_currency"], 0) + int(r["assets"])
    assert {k: sums.get(k, 0) for k in ("latest", "previous", "older", "unknown")} == {
        k: cur[k] for k in ("latest", "previous", "older", "unknown")}
    by_version = {r["version"]: r["build_currency"] for r in exported}
    assert by_version["weird-build"] == "unknown" and by_version["build-2"] == "unknown" and by_version[""] == "unknown"
    assert by_version["7.2.0"] in ("latest", "previous")


def test_nf_be_01_version_bucket_is_the_one_rule():
    cur = {"latest": 2, "previous": 1, "older": 1, "unknown": 1, "latest_version": "7.2.0", "previous_version": "7.1.9"}
    assert iq.version_bucket("semverish", "7.2.0", cur) == "latest"
    assert iq.version_bucket("semverish", "7.1.9", cur) == "previous"
    assert iq.version_bucket("semverish", "7.0.1", cur) == "older"
    assert iq.version_bucket("semverish", "weird-build", cur) == "unknown"
    assert iq.version_bucket("semverish", None, cur) == "unknown"
    assert iq.version_bucket("semverish", "7.2.0", None) is None             # no currency: no bucket at all


# ════════════════════════════════════════════════════════════════════
# NF-BE-02: one set of rows in every figure of the summary's software block
# ════════════════════════════════════════════════════════════════════

def seed_mixed_category_product(db) -> str:
    """A name_key whose rows have two categories: 5 rows 'other' (versions 1.0 and 2.0) and 4 rows 'update' (6.0 to 9.0). This
    is a transitional state (rows keyed under an older pack), the only way a key gets two categories."""
    servers = free_servers(db, "Mixcat Tool", 9)
    rows = []
    for i, name in enumerate(servers):
        hidden = i >= 5
        rows.append((name, "1.0" if i < 3 else "2.0" if i < 5 else f"{i}.0", "update" if hidden else "other"))
    for name, version, category in rows:
        db.execute(
            "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source, norm_rev, category, name_key) "
            "SELECT id, 'Mixcat Tool', $2, 'Acme', 'hklm', $4, $3, 'mixcat' FROM servers WHERE display_name = $1",
            name, version, category, norm.RULEPACK_REV)
    return "mixcat"


@pytest.mark.asyncio
@pytest.mark.parametrize("forced_paths", [False, True], ids=["exact", "candidate_path"])
async def test_nf_be_02_the_sprawl_row_counts_versions_and_hosts_of_the_rows_the_installs_count(
        fleet_db_fresh, pinned_clock, monkeypatch, forced_paths):
    """NF-BE-02: the product's installs counted its 5 visible rows while its distinct versions counted all 9 (6 versions
    instead of 2) and, on the candidate path, its hosts counted the hidden rows too."""
    if forced_paths:
        monkeypatch.setattr(iq, "_EXACT_SOFTWARE_ROWS", 0)
        monkeypatch.setattr(iq, "_SPRAWL_EXACT_ROWS", 0)
    key = seed_mixed_category_product(fleet_db_fresh)
    hidden = ["update", "driver", "os-component", "runtime"]
    servers_with_software = fleet_db_fresh.scalar("SELECT count(DISTINCT server_id) FROM server_software_inventory")
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            block = await iq._software_block(s, None, hidden, servers_with_software, top_n=500)
            everything = await iq._software_block(s, None, [], servers_with_software, top_n=500)
    row = next(x for x in block["sprawl"] if x["product_key"] == key)
    assert row["versions"] == 2 and row["hosts"] == 5                       # 1.0 and 2.0 on the 5 visible rows
    top = next(x for x in block["top"] if x["product_key"] == key)
    assert top["hosts"] == 5 and top["versions"] == 2
    visible = fleet_db_fresh.scalar(
        "SELECT count(*) FROM server_software_inventory WHERE name_key IS NOT NULL AND COALESCE(category, 'other') <> ALL($1::text[])",
        hidden)
    assert block["installs"] == visible
    # with nothing hidden the same product has every row, host and version
    full = next(x for x in everything["sprawl"] if x["product_key"] == key)
    assert full["versions"] == 6 and full["hosts"] == 9


# ════════════════════════════════════════════════════════════════════
# NB-04: a version row's hosts are counted once per host, however the statements are cut
# ════════════════════════════════════════════════════════════════════

def test_nb_04_job_chunks_never_cut_through_a_job():
    jobs = [0, 0, 0, 1, 1, 2, 3, 3, 3, 3, 3, 4]
    for limit in (1, 2, 3, 4, 5, 7, 100):
        chunks = isq._job_chunks(jobs, limit)
        assert chunks[0][0] == 0 and chunks[-1][1] == len(jobs)
        assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))                    # contiguous, nothing lost or repeated
        for lo, hi in chunks:
            assert hi > lo
            assert hi == len(jobs) or jobs[hi] != jobs[hi - 1], (limit, lo, hi)          # a cut falls between two jobs
            last = jobs[hi - 1]
            assert hi - lo <= limit or (hi - lo) - jobs[lo:hi].count(last) < limit       # larger only by the job that straddled the cut
    assert isq._job_chunks([], 5) == []
    assert isq._job_chunks([7, 7, 7, 7, 7, 7], 2) == [(0, 6)]


@pytest.mark.parametrize("chunk", [1, 2, 3, 5])
def test_nb_04_a_host_with_several_spellings_of_one_version_counts_once_whatever_the_statement_size(fleet_db_fresh, pinned_clock, monkeypatch, chunk):
    """NB-04: with a statement size that cut between two spellings the host was counted once per statement (hosts 2 for one
    host), which broke the invariant versions[].hosts == the Assets total behind it."""
    monkeypatch.setattr(isq, "_MERGED_HOSTS_CHUNK", chunk)
    db = fleet_db_fresh
    servers = free_servers(db, "Acme Chunk Tool", 3)
    # server 0 holds three spellings of 7.1 (and a 32 bit title), servers 1 and 2 hold one spelling each
    spec = [(servers[0], "Acme Chunk Tool", "7.1"), (servers[0], "Acme Chunk Tool (x86)", "7.1.0"), (servers[0], "Acme Chunk Tool (arm)", "7.1.0.0"),
            (servers[1], "Acme Chunk Tool", "7.1"), (servers[2], "Acme Chunk Tool", "7.1.0.0"),
            (servers[1], "Acme Chunk Tool (x86)", "8.0"), (servers[2], "Acme Chunk Tool (x86)", "8.0.0")]
    for name, title, version in spec:
        db.execute(
            "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source, norm_rev, category, name_key) "
            "SELECT id, $2, $3, 'Acme', 'hklm', $4, 'other', 'acme-chunk-tool' FROM servers WHERE display_name = $1",
            name, title, version, norm.RULEPACK_REV)
    with client(fleet_db_fresh) as h:
        page = h.get(f"{API}/software/acme-chunk-tool").json()
        by = {v["version"].rstrip(".0") if v["version"] else None: v for v in page["versions"]}
        assert {k: (v["hosts"], v["installs"]) for k, v in by.items()} == {"7.1": (3, 5), "8": (2, 2)}
        assert page["header"]["hosts"] == 3
        for v in page["versions"]:
            landed = h.get(f"{API}/assets", params={"product": "acme-chunk-tool", "product_version": v["version"]}).json()["total"]
            assert landed == v["hosts"], (chunk, v)


# ════════════════════════════════════════════════════════════════════
# NB-03: the software export stays in its snapshot
# ════════════════════════════════════════════════════════════════════

ISOLATION_SQL = ("SELECT current_setting('transaction_isolation') AS iso, current_setting('transaction_read_only') AS ro, "
                 "current_setting('idle_in_transaction_session_timeout') AS idle")


def spy_on_detail_reads(monkeypatch, db, *, after_export_calls: int, between=None):
    """Wrap ``_detail_rows`` (every per-product read of a software page): note the transaction settings of the session it
    runs on, and run ``between()`` (a COMMITTED write on another connection) once ``after_export_calls`` reads of the file's pages
    have happened. The plan's own probe reads one product (page_size 1); a page of the file reads 15 or more at a time."""
    seen: list[dict] = []
    real = isq._detail_rows
    state = {"export": 0, "fired": False}

    async def spy(s, scope, keys, vis, **kw):
        of_the_file = len(keys) > 1
        if of_the_file:
            if state["export"] == after_export_calls and between is not None and not state["fired"]:
                state["fired"] = True
                between()
            state["export"] += 1
        out = await real(s, scope, keys, vis, **kw)
        row = (await iq._run(s, ISOLATION_SQL, {}))[0]
        seen.append({"export": of_the_file, "iso": row["iso"], "ro": row["ro"], "idle": row["idle"]})
        return out

    monkeypatch.setattr(isq, "_detail_rows", spy)
    return seen


def perf_installs(db) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in db.query(
        "SELECT name_key, count(*) FROM server_software_inventory WHERE name_key LIKE 'perf-product-%' GROUP BY 1")}


def test_nb_03_every_page_of_a_software_file_is_read_in_the_same_repeatable_read_transaction(fleet_db_fresh, pinned_clock, monkeypatch):
    """NB-03: with 24 or more products on a page the catalog read half of them on another READ COMMITTED connection, so a
    row committed while the file streamed showed in some products of one page and not in the others."""
    db = fleet_db_fresh
    seed_products(db, 60, 2)
    monkeypatch.setattr(export, "BATCH", 30)
    before = perf_installs(db)
    extra_server = db.scalar("SELECT display_name FROM servers WHERE NOT EXISTS (SELECT 1 FROM server_software_inventory w "
                             "WHERE w.server_id = servers.id AND w.name_key LIKE 'perf-product-%') ORDER BY display_name LIMIT 1")
    assert extra_server and len(before) == 60

    def commit_one_more_install_of_every_perf_product():
        db.execute(
            "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source, norm_rev, category, name_key) "
            "SELECT s.id, 'Perf Product ' || g || ' late', '9.9', 'Perf Vendor', 'hklm', $2, 'other', 'perf-product-' || g "
            "FROM servers s, generate_series(1, 60) g WHERE s.display_name = $1", extra_server, norm.RULEPACK_REV)

    seen = spy_on_detail_reads(monkeypatch, db, after_export_calls=2, between=commit_one_more_install_of_every_perf_product)
    with client(db) as h:
        r = h.get(f"{API}/export", params={"dataset": "software", "q": "perf product", "hidden": "include"})
    assert r.status_code == 200, r.text[:300]
    in_file = [x for x in seen if x["export"]]
    assert len(in_file) >= 4, "the file spans several pages, each read in two halves"
    assert {(x["iso"], x["ro"]) for x in in_file} == {("repeatable read", "on")}
    assert all(x["idle"] != "0" for x in in_file)                          # the backstop timeout is still on
    got = {row["product_key"]: int(row["installs"]) for row in rows_of(r) if row["product_key"].startswith("perf-product-")}
    assert len(got) == 60 and perf_installs(db) != before                   # the committed rows exist now ...
    assert got == before                                                    # ... and no page of the file shows them


def test_nb_03_a_shared_wait_does_not_end_the_transaction_of_the_file(fleet_db_fresh, pinned_clock, monkeypatch):
    """NB-03 (2): sort=latest_version waits for shared reads, which closed the export's own session (ending its transaction
    with its read-only and idle-timeout settings): every later page then ran as READ COMMITTED with no timeout."""
    db = fleet_db_fresh
    seed_products(db, 60, 2)
    monkeypatch.setattr(export, "BATCH", 30)
    seen = spy_on_detail_reads(monkeypatch, db, after_export_calls=99)
    with client(db) as h:
        isq.clear_caches()
        r = h.get(f"{API}/export", params={"dataset": "software", "q": "perf product", "hidden": "include", "sort": "latest_version"})
    assert r.status_code == 200, r.text[:300]
    in_file = [x for x in seen if x["export"]]
    assert len(in_file) >= 4
    assert {(x["iso"], x["ro"]) for x in in_file} == {("repeatable read", "on")} and all(x["idle"] != "0" for x in in_file)
    assert len(rows_of(r)) == 60


def test_nb_03_the_row_set_and_order_of_a_file_do_not_change_when_the_cached_table_goes_away(fleet_db_fresh, pinned_clock, monkeypatch):
    """The light table is fixed when the file starts: expiring or clearing the cache between pages cannot shift a page."""
    db = fleet_db_fresh
    seed_products(db, 60, 2)
    monkeypatch.setattr(export, "BATCH", 20)
    cleared = {"n": 0}
    real = isq._detail_rows

    async def clearing(s, scope, keys, vis, **kw):
        if s.info.get(iq.PINNED_KEY):
            cleared["n"] += 1
            isq.clear_caches()                    # the table of every scope is dropped between the reads of the file
        return await real(s, scope, keys, vis, **kw)

    monkeypatch.setattr(isq, "_detail_rows", clearing)
    with client(db) as h:
        r = h.get(f"{API}/export", params={"dataset": "software", "q": "perf product", "hidden": "include"})
    keys = [row["product_key"] for row in rows_of(r)]
    assert cleared["n"] >= 3
    assert len(keys) == len(set(keys)) == 60 and keys == sorted(keys)        # every product once, in the file's order
