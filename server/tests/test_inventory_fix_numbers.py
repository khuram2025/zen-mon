"""Regression tests of the Phase 1 review for the numbers the Inventory pages show (be-a fix round).

BN-04 (case variants split groups whose counts differ from the list they link to), BN-06 and UP-09 (spellings of one version
and the latest / previous drill), BN-02 (version sprawl above 60,000 software rows), BN-10 (publishers and blank versions),
BN-11 (version sort order), BN-12 (the Overview's OS figures are the OS page's row counts), UP-05 (a server and its unlinked
same-IP device are two assets), UI-04 (unknown-model rows). The principle behind all of them: a number on a page is the
``total`` of the list it links to, whatever the data looks like.
"""

from __future__ import annotations

import contextlib

import pytest

from app.services import inventory_query as iq
from app.services import inventory_service
from app.services import versioning
from inventory_fleet import (
    add_assets, aid, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)
from test_inventory_api import _assert_links, get, hardware_links, os_links, summary_asset_links

API = "/api/v1/inventory"


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


def view(db):
    return api_client(db, role="t_fix_num", permissions=["inventory.view"])


def total(c, **params) -> int:
    return get(c, "/assets", page_size=1, **params)["total"]


# ── the version equivalence key (BN-06 / UP-09) ───────────────────────

@pytest.mark.parametrize("a,b", [("7.2", "7.2.0"), ("7.2.0", "7.2.00"), ("10.0", "10"), ("V1.0", "v1"), (" 3.1 ", "3.1.0.0"),
                                 ("14.38.33135", "14.38.33135.0")])
def test_equivalent_spellings_share_a_key(a, b):
    assert versioning.equivalence_key(a) == versioning.equivalence_key(b)


@pytest.mark.parametrize("a,b", [("7.2", "7.20"), ("1.10", "1.1"), ("1.100", "1.1"), ("7.2.1", "7.2"), ("1.0-rc1", "1.0"), ("8", "80"),
                                 (".0", "0"), (".0", "1"), (".0", ".0.0.1"), ("..0", ".0"), (".00", ".0")])
def test_different_versions_keep_different_keys(a, b):
    assert versioning.equivalence_key(a) != versioning.equivalence_key(b)


def test_nb07_a_non_blank_version_never_has_the_empty_key_of_a_blank_one():
    for raw in (".0", "..0", ".00", ".0.0", " .0 ", "-.0", "v.0", ".", ".."):
        key = versioning.equivalence_key(raw)
        assert key and key != "", raw
    assert versioning.equivalence_key(".0") == ".0" and versioning.equivalence_key("..0") == "."
    assert versioning.equivalence_key(".0.0") == ".0" and versioning.equivalence_key("1.0.0") == "1"
    assert versioning.equivalence_key("  ") is None and versioning.equivalence_key("") is None


def test_blank_and_non_text_have_no_key():
    assert versioning.equivalence_key(None) is None and versioning.equivalence_key("  ") is None
    assert versioning.equivalence_key("\t\n") is None
    assert versioning.equivalence_key(7) is None


def test_python_and_sql_keys_agree(fleet_db):
    """Counts (Python) and filters / GROUP BYs (SQL) must never disagree about which spellings are one version.
    (ASCII only: PostgreSQL's lower() follows the database locale for other letters; no real version string has any.)"""
    corpus = ["\t7.2.0\n", "7.2", "7.2.0", "7.2.00", "10.0", "10", "0", "0.0", "1.0.0.0", "V1.0", "v1", " 3.1 ", "1.10", "1.100", "1.0-rc1",
              "17.9.4a", "10.0.20348.587", "14.38.33135.0", "2022", "x.0", "1.0.0-rc1", "9.4", "A.B.0",
              # NB-07: a spelling made only of zero parts keeps its first character, so it is never the empty key of a blank
              ".0", ".00", "..0", ".0.0", ".0.0.0", " .0 ", "..", ".", ".0\n.0", "0.0.0", "-.0", "v.0"]
    sql = "SELECT " + versioning.EQUIVALENCE_KEY_SQL.format(c="CAST($1 AS text)")
    for v in corpus:
        assert fleet_db.scalar(sql, v) == versioning.equivalence_key(v), v


# ── BN-06 / UP-09: spellings of one version ───────────────────────────

ACME = dict(os_family="other", os_product="Acme OS", os_release="7.2")
SPELLINGS = [("acme-a", "7.2.0"), ("acme-b", "7.2"), ("acme-c", "7.1.9"), ("acme-d", "7.0.1"), ("acme-e", "weird-build"), ("acme-f", None)]


@pytest.fixture
def acme(fleet_db_fresh, pinned_clock):
    add_assets(fleet_db_fresh, devices=[{"name": n, "vendor": "Acme", "model": "A1", "os_version": v, **ACME} for n, v in SPELLINGS])
    return fleet_db_fresh


def _release(tree, product="Acme OS", release="7.2"):
    fam = next(f for f in tree["families"] if f["family"] == "other")
    prod = next(p for p in fam["products"] if p["product"] == product)
    return next(r for r in prod["releases"] if r["release"] == release)


def test_bn06_equal_spellings_are_one_version_node_and_the_latest_drill_equals_its_count(acme):
    """BN-06: currency merged 7.2 and 7.2.0 but the latest link matched one spelling (claimed 2, landed on 1), and which
    spelling was 'latest_version' depended on row order. One node, the longer spelling, and the drill equals the count."""
    with view(acme) as c:
        tree = get(c, "/os")
        rel = _release(tree)
        versions = {v["version"]: v["count"] for v in rel["versions"]}
        assert versions["7.2.0"] == 2 and "7.2" not in versions, "one node for both spellings, labelled with the longer one"
        assert rel["distinct_versions"] == 4 and rel["currency"]["latest"] == 2 and rel["currency"]["latest_version"] == "7.2.0"
        assert rel["currency"]["previous_version"] == "7.1.9" and rel["currency"]["previous"] == 1
        assert rel["currency"]["unknown"] == 2                                                # weird-build and no version
        base = {"os_family": "other", "os_product": "Acme OS", "os_release": "7.2"}
        assert total(c, **base, os_version="7.2.0") == total(c, **base, os_version="7.2") == 2, "the filter matches the node"
        assert total(c, **base, os_version="unknown") == 1
        n = _assert_links(c, os_links(tree), min_links=10)                                    # every node and currency figure
        assert n > 10
        tree_again = get(c, "/os")
        assert _release(tree_again) == rel, "deterministic"


def test_bn06_the_firmware_version_count_of_a_model_merges_spellings(acme):
    with view(acme) as c:
        h = get(c, "/hardware")
        a1 = next(m for m in h["network"]["models"] if m["vendor"] == "Acme" and m["model"] == "A1")
        assert a1["firmware_versions"] == 4              # 7.2(.0), 7.1.9, 7.0.1, weird-build
        vendor = next(v for v in h["network"]["vendors"] if v["vendor"] == "Acme")
        assert vendor["firmware_versions"] == 4


@pytest.mark.asyncio
async def test_bn06_currency_orders_levels_and_never_merges_by_the_comparator(pinned_clock):
    # two different keys that the comparator would call equal stay two levels, longer spelling first (a total order)
    cur, order = iq._currency("semverish", [("1.0.0-rc1", 3), ("1.0-rc1", 2), ("0.9", 1)])
    assert order[:2] == ["1.0.0-rc1", "1.0-rc1"] and cur["latest"] == 3 and cur["previous"] == 2 and cur["older"] == 1
    cur, order = iq._currency("win_build", [("10.0.20348.2700", 4), ("10.0.20348.587", 1), ("10.0.17763.1", 2)])
    assert cur["latest"] == 4 and cur["previous"] == 1 and cur["older"] == 2


def test_up09_a_trailing_zero_does_not_make_a_second_software_version(fleet_db_fresh, pinned_clock):
    """UP-09: 14.38.33135 (MSI) and 14.38.33135.0 (bundle) are one version for the sprawl count."""
    from inventory_fleet import add_software_rows
    add_software_rows(fleet_db_fresh, [
        {"server": "srv-w22-01", "title": "Acme Widget", "vendor": "Acme", "version": "2.1"},
        {"server": "srv-w22-02", "title": "Acme Widget", "vendor": "Acme", "version": "2.1.0"},
        {"server": "srv-w22-03", "title": "Acme Widget", "vendor": "Acme", "version": "2.1.0.0"},
        {"server": "srv-w22-04", "title": "Acme Widget", "vendor": "Acme", "version": "2.2"}])
    with view(fleet_db_fresh) as c:
        s = get(c, "/summary")
        key = fleet_db_fresh.scalar("SELECT name_key FROM server_software_inventory WHERE package_name = 'Acme Widget' LIMIT 1")
        row = [p for p in s["software"]["top"] + s["software"]["sprawl"] if p["product_key"] == key]
        assert not row or row[0]["versions"] == 2


# ── BN-04: case variants ──────────────────────────────────────────────

CASE_DEVICES = [
    dict(name="cv-a1", vendor="Cisco", model="C9200", os_family="ios-xe", os_product="IOS XE", os_release="17.9", os_version="17.9.4a",
         location="HQ / rack 1", serial="S1"),
    dict(name="cv-a2", vendor="Cisco", model="C9200", os_family="ios-xe", os_product="IOS XE", os_release="17.9", os_version="17.9.4a",
         location="HQ / rack 1", serial="S2"),
    dict(name="cv-a3", vendor="CISCO", model="c9200", os_family="ios-xe", os_product="ios xe", os_release="17.9", os_version="17.9.4a",
         location="hq / rack 1", serial="S3"),
    dict(name="cv-b1", vendor="Acme Networks", model="AX-100", os_family="other", os_product="AcmeOS", os_release="2.0", os_version="2.0.1",
         location="Annex", serial="S4"),
    dict(name="cv-b2", vendor="ACME NETWORKS", model="ax-100", os_family="other", os_product="ACMEOS", os_release="2.0", os_version="2.0.1",
         location="ANNEX", serial=None),
    dict(name="cv-b3", vendor="acme networks", model="Ax-100", os_family="other", os_product="acmeos", os_release="2.0", os_version="2.0.1",
         location="annex", serial="S6"),
    dict(name="cv-c1", vendor="Acme Networks", model=None, os_family="other", os_product="AcmeOS", os_release="2.0", os_version="2.0.1",
         location=None, serial="S7"),
]


@pytest.fixture
def cased(fleet_db_fresh, pinned_clock):
    add_assets(fleet_db_fresh, devices=CASE_DEVICES)
    return fleet_db_fresh


def test_bn04_facet_counts_equal_the_list_after_choosing_the_value(cased):
    """BN-04: 'Cisco' 3 and 'CISCO' 1 were two facet rows whose links both listed 4. One row per lower-case key."""
    with view(cased) as c:
        facets = get(c, "/assets/facets")["facets"]
        for name in ("os_product", "vendor", "model", "site"):
            items = facets[name]["items"]
            keys = [i["value"].lower() for i in items]
            assert len(keys) == len(set(keys)), (name, keys)
            for i in items:
                assert total(c, **{name: i["value"]}) == i["count"], (name, i)
        vendors = {i["value"]: i["count"] for i in facets["vendor"]["items"]}
        assert vendors["Acme Networks"] == 4                       # the spelling most devices use labels the group
        cisco = next(v for k, v in vendors.items() if k.lower() == "cisco")
        assert cisco >= 3


def test_bn04_a_tie_between_spellings_is_labelled_by_byte_order_not_by_row_order(fleet_db_fresh, pinned_clock):
    add_assets(fleet_db_fresh, devices=[
        dict(name="t-1", vendor="Zeta Corp", model="Z1"), dict(name="t-2", vendor="ZETA CORP", model="Z1")])
    with view(fleet_db_fresh) as c:
        for _ in range(3):
            vendors = [i["value"] for i in get(c, "/assets/facets")["facets"]["vendor"]["items"] if i["value"].lower() == "zeta corp"]
            assert vendors == ["ZETA CORP"]


def test_bn04_the_os_hardware_and_summary_numbers_still_equal_their_lists(cased):
    with view(cased) as c:
        _assert_links(c, os_links(get(c, "/os")), min_links=20)
        _assert_links(c, os_links(get(c, "/os", kind="device")), min_links=10)
        for vendor in (None, "Cisco", "acme networks"):
            h = get(c, "/hardware", vendor=vendor)
            _assert_links(c, hardware_links(h), min_links=10)
            if vendor:
                assert h["matrix"]["vendor"].lower() == vendor.lower()
        _assert_links(c, summary_asset_links(get(c, "/summary")), min_links=30)
        h = get(c, "/hardware")
        names = [v["vendor"] for v in h["network"]["vendors"] if v["vendor"]]
        assert len({n.lower() for n in names}) == len(names), "one vendor row per vendor"
        models = [(m["vendor"].lower(), (m["model"] or "").lower()) for m in h["network"]["models"]]
        assert len(models) == len(set(models))


def test_bn04_the_matrix_merges_case_variants_of_a_model_and_a_release(fleet_db_fresh, pinned_clock):
    add_assets(fleet_db_fresh, devices=[
        dict(name="m-1", vendor="Mx", model="M1", os_family="other", os_release="1.0"),
        dict(name="m-2", vendor="MX", model="m1", os_family="other", os_release="1.0"),
        dict(name="m-3", vendor="Mx", model="M1", os_family="other", os_release="Beta")])
    with view(fleet_db_fresh) as c:
        h = get(c, "/hardware", vendor="mx")
        mx = h["matrix"]
        assert [m["count"] for m in mx["models"]] == [3] and sorted(mx["releases"]) == ["1.0", "Beta"]
        _assert_links(c, hardware_links(h), min_links=5)
        assert total(c, vendor="MX", model="M1", os_release="beta") == 1


# ── UI-04: unknown-model rows ──────────────────────────────────────────

def test_ui04_unknown_model_rows_are_explicit_and_the_counts_add_up(cased):
    with view(cased) as c:
        net = get(c, "/hardware")["network"]
        acme = next(v for v in net["vendors"] if v["vendor"] == "Acme Networks")
        assert acme["models"] == acme["models_known"] == 1 and acme["unknown_model_devices"] == 1 and acme["count"] == 4
        rows = [m for m in net["models"] if m["vendor"] == "Acme Networks"]
        assert [(m["model"], m["unknown"], m["count"]) for m in rows] == [("AX-100", False, 3), (None, True, 1)] or \
            [(m["model"], m["unknown"], m["count"]) for m in rows] == [("ax-100", False, 3), (None, True, 1)] or \
            sorted((m["unknown"], m["count"]) for m in rows) == [(False, 3), (True, 1)]
        assert net["models_known"] == sum(1 for m in net["models"] if not m["unknown"])
        assert net["unknown_model_devices"] == sum(m["count"] for m in net["models"] if m["unknown"]) + sum(
            v["count"] for v in net["vendors"] if v["vendor"] is None and v["unknown_model_devices"])
        s = get(c, "/summary")
        assert s["hardware"]["models"] == net["models_known"]


# ── BN-11: version sort ───────────────────────────────────────────────

def test_bn11_versions_sort_by_their_numbers_not_their_text(fleet_db_fresh, pinned_clock):
    versions = ["7.2.5", "7.10.1", "7.100.0", "7.4.1", "6.4.15", "10.0.20348.587", "10.0.20348.2700", "6.3.9600", "2022"]
    add_assets(fleet_db_fresh, devices=[dict(name=f"vs-{i}", vendor="Vs", model="V1", os_family="other", os_product="VsOS", os_version=v)
                                        for i, v in enumerate(versions)])
    want = sorted(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    with view(fleet_db_fresh) as c:
        for sort in ("os_version", "os"):
            asc = [r["os_version"] for r in get(c, "/assets", os_product="VsOS", sort=sort, page_size=50)["items"]]
            assert asc == want, (sort, asc)
            desc = [r["os_version"] for r in get(c, "/assets", os_product="VsOS", sort=sort, order="desc", page_size=50)["items"]]
            assert desc == want[::-1]


# ── BN-12: the Overview's OS figures are the OS page's rows ────────────

def test_bn12_overview_os_figures_count_the_nodes_of_the_os_page(fleet_db, pinned_clock):
    with view(fleet_db) as c:
        s, tree = get(c, "/summary"), get(c, "/os")
    releases = [(f["family"], p["product"], r["release"]) for f in tree["families"] for p in f["products"] for r in p["releases"]]
    assert s["os"]["families"] == len(tree["families"]) and s["os"]["releases"] == len(releases)
    assert any(f["family"] == "unknown" for f in tree["families"]) and any(r[2] is None for r in releases), \
        "the unknown family and a missing release are nodes of their own, and counted"


# ── BN-02 / BN-10: the Overview's software figures ────────────────────

def _bulk_software(db, servers: int = 200) -> None:
    """The review's scenario: 100 products installed everywhere in 2 versions, 900 products on 50 hosts, 'javalike' on 30 hosts in
    30 distinct versions and 'midsprawl' on every host in 3 versions (65,000+ rows, above the exact-count threshold)."""
    rev = iq.inventory_normalize.RULEPACK_REV
    db.psql(f"""
        SET zenplus.inventory_suppress = 'on';
        INSERT INTO servers (display_name, hostname, os_type, collection_mode, status, tags)
        SELECT 'bulk-' || g, 'bulk-' || g, 'windows', 'agent', 'healthy', '["bulk"]'::jsonb FROM generate_series(1, {servers}) g;
        CREATE TEMP TABLE _b AS SELECT id, row_number() OVER (ORDER BY display_name) AS n FROM servers WHERE display_name LIKE 'bulk-%';
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source)
        SELECT b.id, 'common-' || lpad(p::text, 3, '0'), 'v' || (1 + b.n % 2), 'Acme', 'common-' || lpad(p::text, 3, '0'), 'acme', 'other', {rev}, 'hklm'
          FROM _b b, generate_series(1, 100) p;
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source)
        SELECT b.id, 'rare-' || lpad(p::text, 4, '0'), 'v1', 'Acme', 'rare-' || lpad(p::text, 4, '0'), 'acme', 'other', {rev}, 'hklm'
          FROM _b b, generate_series(1, 900) p WHERE (b.n + p) % 4 = 0;
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source)
        SELECT b.id, 'javalike', 'j' || b.n, 'Acme', 'javalike', 'acme', 'other', {rev}, 'hklm' FROM _b b WHERE b.n <= 30;
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source)
        SELECT b.id, 'midsprawl', 'm' || (b.n % 3), 'Acme', 'midsprawl', 'acme', 'other', {rev}, 'hklm' FROM _b b;
        UPDATE servers SET inventory_baselined_at = now() WHERE display_name LIKE 'bulk-%';
    """)


@pytest.mark.asyncio
async def test_bn02_version_sprawl_is_exact_above_the_exact_count_threshold(fleet_db_fresh, pinned_clock):
    """BN-02: above 60,000 visible rows only the 64 most installed products were ranked, so 'javalike' (30 versions) and
    'midsprawl' (3) were missing from the Overview while the catalog and the product pages listed them."""
    db = fleet_db_fresh
    _bulk_software(db)
    hidden = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    assert db.scalar("SELECT count(*) FROM server_software_inventory WHERE NOT (category = ANY($1))", hidden) > iq._EXACT_SOFTWARE_ROWS
    brute = db.query(
        """SELECT name_key, count(DISTINCT server_id), count(DISTINCT version), count(*) FROM server_software_inventory
            WHERE NOT (category = ANY($1)) AND name_key IS NOT NULL GROUP BY name_key""", hidden)
    want = sorted((r for r in brute if r[2] > 1), key=lambda r: (-r[2], -r[3], r[0]))[:8]
    async with session(db) as s:
        sm = await iq.summary(s, scope=None, use_cache=False)
    got = [(p["product_key"], p["versions"], p["hosts"]) for p in sm["software"]["sprawl"]]
    assert got == [(r[0], r[2], r[1]) for r in want]
    assert got[0][:2] == ("javalike", 30) and ("midsprawl", 3) in [g[:2] for g in got]
    # the host ranking stays exact on the candidate path as well
    by_hosts = sorted(brute, key=lambda r: (-r[1], -r[3], r[0]))[:8]
    assert [(p["product_key"], p["hosts"]) for p in sm["software"]["top"]] == [(r[0], r[1]) for r in by_hosts]
    assert sm["software"]["products"] == len(brute) and sm["software"]["installs"] == sum(r[3] for r in brute)


@pytest.mark.asyncio
async def test_bn02_beyond_the_exact_threshold_the_ranking_says_what_it_covers(fleet_db_fresh, pinned_clock, monkeypatch):
    """When the version pass is bounded (the design size, 2 million rows: reading them all costs the whole cold budget of
    the summary) the response says which products were ranked, and the ranking within them is exact."""
    db = fleet_db_fresh
    _bulk_software(db)
    monkeypatch.setattr(iq, "_SPRAWL_EXACT_ROWS", 1_000)
    monkeypatch.setattr(iq, "_SPRAWL_MIN_CANDIDATES", 10)
    monkeypatch.setattr(iq, "_SPRAWL_ROW_SHARE", 0.30)
    async with session(db) as s:
        sm = await iq.summary(s, scope=None, use_cache=False)
    basis = sm["software"]["sprawl_basis"]
    assert basis["scope"] == "top_installed" and 10 <= basis["candidates"] < basis["products"] == sm["software"]["products"]
    hidden = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    rows = db.query(
        """SELECT name_key, count(*) n, count(DISTINCT version) v, count(DISTINCT server_id) h FROM server_software_inventory
            WHERE NOT (category = ANY($1)) AND name_key IS NOT NULL GROUP BY name_key""", hidden)
    pool = {r[0] for r in sorted(rows, key=lambda r: (-r[1], r[0]))[:basis["candidates"]]}
    want = sorted((r for r in rows if r[0] in pool and r[2] > 1), key=lambda r: (-r[2], -r[1], r[0]))[:8]
    assert [(p["product_key"], p["versions"]) for p in sm["software"]["sprawl"]] == [(r[0], r[2]) for r in want]
    # the host ranking is exact whatever the version pass covers
    by_hosts = sorted(rows, key=lambda r: (-r[3], -r[1], r[0]))[:8]
    assert [(p["product_key"], p["hosts"]) for p in sm["software"]["top"]] == [(r[0], r[3]) for r in by_hosts]


def test_the_http_summary_carries_every_new_field(fleet_db, pinned_clock):
    """The response models must not drop the fields this round added (FastAPI filters a response by its model)."""
    with view(fleet_db) as c:
        s = get(c, "/summary")
    assert set(s["software"]["sprawl_basis"]) == {"scope", "candidates", "products"}
    assert set(s["software"]["security_agent_coverage"]) == {"covered", "missing", "unknown", "total"}
    assert "not_applicable" in s["coverage"] and set(s["health"]["not_collected_by_design"]) == {"count"}
    assert set(s["data_status"]) == {"normalising", "pending_software_rows", "pending_identity_rows"}
    with view(fleet_db) as c:
        h = get(c, "/hardware", vendor="Cisco")
    assert {"models_known", "unknown_model_devices"} <= set(h["network"])
    assert {"models_known", "unknown_model_devices", "not_collected"} <= set(h["network"]["vendors"][0])
    assert {"unknown", "not_collected"} <= set(h["network"]["models"][0]) and "unknown" in h["matrix"]["models"][0]


@pytest.mark.asyncio
async def test_bn02_a_small_estate_is_ranked_over_all_its_products(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        sm = await iq.summary(s, scope=None, use_cache=False)
    assert sm["software"]["sprawl_basis"] == {"scope": "all_products", "candidates": sm["software"]["products"],
                                              "products": sm["software"]["products"]}


def test_bn10_publishers_and_blank_versions_agree_with_the_software_pages(fleet_db_fresh, pinned_clock):
    """BN-10: a product with no publisher made /software/publishers list an 'unknown' group the Overview did not count, and
    blank versions were counted as versions."""
    from inventory_fleet import add_software_rows
    db = fleet_db_fresh
    rev = iq.inventory_normalize.RULEPACK_REV
    add_software_rows(db, [{"server": f"srv-w22-{i:02d}", "title": "Blank Ver Tool", "vendor": "Acme", "version": str(i)}
                           for i in range(1, 13)])
    key = db.scalar("SELECT name_key FROM server_software_inventory WHERE package_name = 'Blank Ver Tool' LIMIT 1")
    for i, (v, srv) in enumerate((("", "srv-w19-01"), ("  ", "srv-w19-02"))):
        db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source) "
                   "SELECT s.id, 'Blank Ver Tool', $1, 'Acme', $2, NULL, 'other', $3, 'hklm' FROM servers s WHERE s.display_name = $4",
                   v, key, rev, srv)
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev, source) "
               "SELECT s.id, 'No Publisher Tool', '3', NULL, 'no-publisher-tool', NULL, 'other', $1, 'hklm' FROM servers s WHERE s.display_name = 'srv-w22-05'", rev)
    with view(db) as c:
        s = get(c, "/summary")
        pubs = get(c, "/software/publishers")
        assert s["software"]["publishers"] == pubs["distinct"], "an unpublished product is one 'unknown' publisher on both pages"
        assert any(i["value"] == "unknown" for i in pubs["items"])
        row = next(p for p in s["software"]["sprawl"] if p["product_key"] == key)
        assert row["versions"] == 12, "blank versions are not versions"
        assert get(c, f"/software/{key}")["header"]["version_count"] == 12


# ── UP-05: a server and its unlinked same-IP device ───────────────────

def test_up05_only_a_link_makes_a_server_and_a_device_one_asset(fleet_db_fresh, pinned_clock):
    """UP-05, as built: 'counted once' holds for a LINKED pair (servers.device_id). A device that merely shares a server's IP
    is not paired automatically: the pairing would have to be one rule in every reader of the link (the daily snapshots, the
    export) and a guess from an address that can be reused. Both rows are listed, and every total still equals its list."""
    db = fleet_db_fresh
    with view(db) as c:
        before = total(c)
    add_assets(db, devices=[dict(name="web-02-dev", ip="10.250.7.42", vendor="Acme", model="W1", os_family="other")],
               servers=[dict(name="web-02", ip="10.250.7.42")])
    with view(db) as c:
        assert total(c) == before + 2, "two rows: the server and the unlinked device"
        db.execute("UPDATE servers SET device_id = $1 WHERE display_name = 'web-02'", aid("device", "web-02-dev"))
        iq.clear_caches()
        assert total(c) == before + 1, "linked: one asset"
        assert get(c, "/summary")["assets"]["total"] == before + 1
