"""The software catalog API (GET /api/v1/inventory/software, /publishers, /{product_key}, /{product_key}/installations).

Requests go through the real application against the seeded fleet of ``inventory_fleet`` (33 software products, 24
of them outside the default-hidden categories) with the clock pinned. The oracle for every number is plain SQL over
the fleet tables, written independently of ``inventory_software_query``. The last part is T-UI2 for the Software
pages: every count the pages turn into a link equals the ``total`` of the list the link opens.
"""

from __future__ import annotations

import asyncio
from datetime import timezone
from typing import Any

import pytest

from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from app.services import inventory_software_query as isq
from inventory_fleet import (
    EDGE_SERVERS, SCOPED_TAG, add_software_rows, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template,
    pinned_clock, sessions,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
HIDDEN = ("update", "driver", "os-component", "runtime")


@pytest.fixture
def client(fleet_db):
    with api_client(fleet_db, role="t_sw_view", permissions=["inventory.view"]) as c:
        yield c


def get(client, path: str, expect: int = 200, **params) -> Any:
    resp = client.get(API + path, params={k: v for k, v in params.items() if v is not None})
    assert resp.status_code == expect, (path, params, resp.status_code, resp.text[:400])
    return resp.json()


def catalog_all(client, **params) -> list[dict]:
    """Every row of a catalog query, read page by page."""
    out, page = [], 1
    while True:
        body = get(client, "/software", page=page, page_size=7, **params)
        out += body["items"]
        if len(out) >= body["total"] or not body["items"]:
            return out
        page += 1


def sql_products(db, scope_servers: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Per product key, straight from the software table: category, publisher key, installs, hosts, versions, first seen."""
    where, args = "", []
    if scope_servers is not None:
        where, args = "AND s.display_name = ANY($1::text[])", [scope_servers]
    rows = db.query(f"""
        SELECT w.name_key, max(w.category), max(w.publisher_key), count(*), count(DISTINCT w.server_id),
               count(DISTINCT w.version) FILTER (WHERE btrim(w.version) <> ''), min(w.first_seen_at)
          FROM server_software_inventory w JOIN servers s ON s.id = w.server_id
         WHERE w.name_key IS NOT NULL {where} GROUP BY 1""", *args)
    return {r[0]: {"category": r[1] or "other", "publisher": r[2], "installs": r[3], "hosts": r[4], "versions": r[5], "first_seen": r[6]}
            for r in rows}


def visible(products: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in products.items() if v["category"] not in HIDDEN}


# ════════════════════════════════════════════════════════════════════
# The catalog
# ════════════════════════════════════════════════════════════════════

def test_catalog_envelope_and_defaults(client, pinned_clock):
    body = get(client, "/software")
    assert set(body) == {"generated_at", "scope", "items", "total", "page", "page_size", "sort", "order", "hidden_products",
                         "hidden_installs", "unmatched_titles", "hidden_categories", "data_status"}
    assert body["data_status"] == {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}
    assert (body["page"], body["page_size"], body["sort"], body["order"]) == (1, 50, "hosts", "desc")
    assert body["generated_at"] == "2026-10-08T12:00:00Z" and body["scope"] == {"restricted": False, "tags": []}
    assert body["hidden_categories"] == ["driver", "os-component", "runtime", "update"]
    row = body["items"][0]
    assert set(row) == {"product_key", "name", "publisher", "publisher_key", "category", "norm_status", "hosts", "installs",
                        "version_count", "latest_version", "distribution", "first_seen", "last_change"}
    assert set(row["distribution"]) == {"top", "other"} and set(row["distribution"]["other"]) == {"versions", "hosts"}
    hosts = [r["hosts"] for r in body["items"]]
    assert hosts == sorted(hosts, reverse=True)
    # without sort, order follows the column: counts descending, everything else A to Z
    assert get(client, "/software", sort="name")["order"] == "asc" and get(client, "/software", sort="version_count")["order"] == "desc"


def test_every_figure_of_every_product_matches_sql(fleet_db, client, pinned_clock):
    oracle = sql_products(fleet_db)
    rows = catalog_all(client, hidden="include")
    assert {r["product_key"] for r in rows} == set(oracle) and len(rows) == 33
    for r in rows:
        o = oracle[r["product_key"]]
        assert (r["hosts"], r["installs"], r["version_count"], r["category"], r["publisher_key"]) == \
               (o["hosts"], o["installs"], o["versions"], o["category"], o["publisher"]), r["product_key"]
        assert r["first_seen"] == o["first_seen"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert r["norm_status"] == "normalized"


def test_the_default_catalog_is_the_overview_software_card(client, pinned_clock):
    """summary.software.products / installs / publishers link to /inventory/software: the lists must have those sizes."""
    summary = get(client, "/summary")["software"]
    body = get(client, "/software", page_size=500)
    assert body["total"] == summary["products"] == 24
    assert sum(r["installs"] for r in body["items"]) == summary["installs"]
    assert get(client, "/software/publishers", limit=200)["distinct"] == summary["publishers"]
    # the summary's top and sprawl rows are catalog rows
    by_key = {r["product_key"]: r for r in body["items"]}
    for top in summary["top"] + summary["sprawl"]:
        assert (by_key[top["product_key"]]["hosts"], by_key[top["product_key"]]["version_count"]) == (top["hosts"], top["versions"])
        assert by_key[top["product_key"]]["name"] == top["name"]


@pytest.mark.parametrize("sort,order", [(s, o) for s in isq.CATALOG_SORTS for o in ("asc", "desc")])
def test_every_sort_is_a_total_order_that_pages_cleanly(client, pinned_clock, sort, order):
    full = catalog_all(client, sort=sort, order=order, hidden="include")
    keys = [r["product_key"] for r in full]
    assert len(keys) == len(set(keys)) == 33                       # paging at 7 never repeats or skips a row
    big = get(client, "/software", sort=sort, order=order, hidden="include", page_size=500)["items"]
    assert [r["product_key"] for r in big] == keys                  # page size does not change the order
    value = {
        "name": lambda r: r["product_key"], "publisher": lambda r: (r["publisher"] or r["publisher_key"] or "").lower(),
        "category": lambda r: r["category"], "hosts": lambda r: r["hosts"], "version_count": lambda r: r["version_count"],
        "first_seen": lambda r: r["first_seen"],
        "latest_version": lambda r: iq._natural_key(r["latest_version"]),
    }[sort]
    have = [r for r in full if sort != "latest_version" or r["latest_version"] is not None]
    seq = [value(r) for r in have]
    assert seq == sorted(seq, reverse=(order == "desc")), (sort, order)
    # ties are broken by the product key, ascending, in both directions
    for a, b in zip(have, have[1:]):
        if value(a) == value(b):
            assert a["product_key"] < b["product_key"]


@pytest.mark.parametrize("candidates,slack", [(1, 0), (2, 0), (5, 1), (64, 14)])
def test_hosts_order_is_exact_however_few_candidates_are_tried_first(client, pinned_clock, monkeypatch, candidates, slack):
    """sort=hosts reads exact host counts for a candidate set only and widens until the page is provably final (hosts
    can never exceed installs or the visible servers). Starting from one candidate must give the same pages as
    reading all 33 products: the fleet has products whose installs exceed their hosts (VC++: 72 rows, 46 servers)."""
    monkeypatch.setattr(isq, "_CANDIDATE_MIN", candidates)
    monkeypatch.setattr(isq, "_CANDIDATE_SLACK", slack)
    oracle = sorted(sql_products_hosts(client), key=lambda kv: (-kv[1], kv[0]))
    for page_size in (1, 3, 10, 33):
        got = []
        for page in range(1, 40):
            items = get(client, "/software", sort="hosts", order="desc", hidden="include", page=page, page_size=page_size)["items"]
            got += [(r["product_key"], r["hosts"]) for r in items]
            if not items:
                break
        assert got == oracle, (candidates, slack, page_size)
    asc = [(r["product_key"], r["hosts"]) for r in get(client, "/software", sort="hosts", order="asc", hidden="include", page_size=500)["items"]]
    assert asc == sorted(oracle, key=lambda kv: (kv[1], kv[0]))


def test_the_first_page_by_hosts_does_not_read_every_product(client, pinned_clock, monkeypatch):
    """The point of the candidate bound: three products by hosts are proven from nine products' exact counts, not 33."""
    monkeypatch.setattr(isq, "_CANDIDATE_MIN", 1)
    monkeypatch.setattr(isq, "_CANDIDATE_SLACK", 0)
    get(client, "/software", sort="hosts", order="desc", hidden="include", page_size=3)
    (table,) = isq._tables.values()
    assert 3 <= len(table.hosts) <= 12 < 33


def sql_products_hosts(client) -> list[tuple[str, int]]:
    """(key, hosts) of every product by the other route: one assets query per product."""
    keys = [r["product_key"] for r in get(client, "/software", sort="name", hidden="include", page_size=500)["items"]]
    return [(k, get(client, "/assets", product=k, page_size=1)["total"]) for k in keys]


def test_paging_past_the_end_is_an_empty_page_with_the_real_total(client, pinned_clock):
    body = get(client, "/software", page=9, page_size=50)
    assert body["items"] == [] and body["total"] == 24 and body["page"] == 9


def test_hidden_categories_are_a_toggle_and_a_footer(fleet_db, client, pinned_clock):
    oracle = sql_products(fleet_db)
    noise = {k: v for k, v in oracle.items() if v["category"] in HIDDEN}
    shown = get(client, "/software", page_size=500)
    everything = get(client, "/software", hidden="include", page_size=500)
    assert shown["total"] == 24 and everything["total"] == 33
    assert shown["hidden_products"] == everything["hidden_products"] == len(noise) == 9
    assert shown["hidden_installs"] == sum(v["installs"] for v in noise.values())
    assert everything["total"] == shown["total"] + shown["hidden_products"]           # "Show 9 hidden entries" adds exactly them
    assert not {r["category"] for r in shown["items"]} & set(HIDDEN)
    # a category asked for by name is never hidden
    only = get(client, "/software", category="update", page_size=500)
    assert only["total"] == 3 and {r["category"] for r in only["items"]} == {"update"}


def test_category_is_repeatable_and_validated(client, pinned_clock):
    both = get(client, "/software", category=["security", "database"], page_size=500)
    assert {r["category"] for r in both["items"]} == {"security", "database"}
    assert both["total"] == get(client, "/software", category="security")["total"] + get(client, "/software", category="database")["total"] == 2
    # X1: endpoint protection is a category of its own (engines and sensors); 'security' holds the other tooling
    ep = get(client, "/software", category="endpoint-protection", page_size=500)
    assert {r["product_key"] for r in ep["items"]} == {"crowdstrike-falcon-sensor", "microsoft-defender-for-endpoint"}
    assert get(client, "/software", category=["security", "endpoint-protection"])["total"] == 3
    bad = get(client, "/software", expect=422, category="shoes")
    assert "category must be one of" in bad["detail"]
    for params in ({"hidden": "maybe"}, {"sort": "bogus"}, {"order": "sideways"}, {"page": 0}, {"page_size": 501}, {"unmatched": "perhaps"}):
        get(client, "/software", expect=422, **params)


def test_text_search_matches_words_of_the_key_name_and_publisher(client, pinned_clock):
    keys = lambda **p: {r["product_key"] for r in get(client, "/software", hidden="include", page_size=500, **p)["items"]}   # noqa: E731
    assert keys(q="chrome") == {"google-chrome"}
    assert keys(q="Google Chrome") == {"google-chrome"}                    # words, not the slug
    assert keys(q="visual c++") == {"microsoft-visual-c-plus-plus-2013-redistributable",
                                    "microsoft-visual-c-plus-plus-2015-2022-redistributable"}    # the catalog rules' product names
    assert "microsoft-edge" in keys(q="microsoft") and "google-chrome" not in keys(q="microsoft")   # publisher
    assert keys(q="no such thing") == set()
    assert keys(q="%") == set() and keys(q="'; drop table servers; --") == set()


def test_publisher_filter_and_the_publisher_facet_agree(client, pinned_clock):
    facet = get(client, "/software/publishers", limit=200)
    assert facet["distinct"] == 21 and facet["total"] == 24
    assert sum(i["products"] for i in facet["items"]) == facet["total"]
    for item in facet["items"]:
        listed = get(client, "/software", publisher=item["value"], page_size=500)
        assert listed["total"] == item["products"], item["value"]                # the facet count is the list total
        assert sum(r["installs"] for r in listed["items"]) == item["installs"]
    by_value = {i["value"]: i for i in facet["items"]}
    assert by_value["microsoft"]["label"] == "Microsoft" and by_value["microsoft"]["products"] == 4
    assert [i["products"] for i in facet["items"]] == sorted((i["products"] for i in facet["items"]), reverse=True)
    assert get(client, "/software", publisher="MICROSOFT")["total"] == 4          # keys match case-insensitively
    assert get(client, "/software", publisher="nobody")["total"] == 0


def test_publisher_hosts_are_servers_with_any_of_the_publishers_products(fleet_db, client, pinned_clock):
    facet = get(client, "/software/publishers", limit=200)
    for item in facet["items"]:
        n = fleet_db.scalar(
            "SELECT count(DISTINCT w.server_id) FROM server_software_inventory w WHERE w.publisher_key = $1 "
            "AND w.category <> ALL($2::text[])", item["value"], list(HIDDEN))
        assert item["hosts"] == n, item["value"]


def test_publisher_facet_follows_the_filters_except_its_own(client, pinned_clock):
    base = get(client, "/software/publishers", limit=200)
    browsers = get(client, "/software/publishers", category="browser", limit=200)
    assert {i["value"] for i in browsers["items"]} == {"google", "microsoft", "mozilla"}
    assert browsers["total"] == get(client, "/software", category="browser")["total"] == 3
    with_noise = get(client, "/software/publishers", hidden="include", limit=200)
    assert with_noise["total"] == 33 and with_noise["total"] > base["total"]
    assert get(client, "/software/publishers", limit=3)["distinct"] == 21 and len(get(client, "/software/publishers", limit=3)["items"]) == 3
    get(client, "/software/publishers", expect=422, limit=0)
    get(client, "/software/publishers", expect=422, limit=201)


def test_a_publisher_the_alias_table_does_not_know_is_labelled_by_its_raw_text_everywhere(fleet_db_fresh, pinned_clock):
    add_software_rows(fleet_db_fresh, [
        {"server": "srv-w22-01", "title": "Acme Widget Pro 3.1.4", "vendor": "Acme Gadgets Ltd", "version": "3.1.4"},
        {"server": "srv-w22-02", "title": "Acme Gizmo 1.0", "vendor": "ACME Gadgets Ltd.", "version": "1.0"},
        {"server": "srv-w22-03", "title": "Acme Gizmo 1.1", "vendor": "Acme Gadgets Ltd", "version": "1.1"}])
    pub = fleet_db_fresh.scalar("SELECT publisher_key FROM server_software_inventory WHERE package_name = 'Acme Widget Pro 3.1.4'")
    assert fleet_db_fresh.scalar("SELECT count(DISTINCT publisher_key) FROM server_software_inventory WHERE vendor ILIKE 'acme gadgets%'") == 1
    raw = fleet_db_fresh.scalar("SELECT min(vendor) FROM server_software_inventory WHERE publisher_key = $1", pub)
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        facet = {i["value"]: i for i in get(c, "/software/publishers", limit=200)["items"]}
        assert facet[pub]["label"] == raw and facet[pub]["products"] == 2 and facet[pub]["hosts"] == 3
        rows = get(c, "/software", q="acme", page_size=50)["items"]
        assert {r["publisher"] for r in rows} == {raw} and len(rows) == 2
        assert {get(c, f"/software/{r['product_key']}")["header"]["publisher"] for r in rows} == {raw}
        # a known publisher keeps the alias table's spelling
        assert facet["microsoft"]["label"] == "Microsoft"


def test_the_publisher_facet_counts_hosts_within_the_filtered_products(fleet_db, client, pinned_clock):
    facet = {i["value"]: i for i in get(client, "/software/publishers", category="browser", limit=200)["items"]}
    for pub, item in facet.items():
        n = fleet_db.scalar(
            "SELECT count(DISTINCT w.server_id) FROM server_software_inventory w WHERE w.publisher_key = $1 AND w.category = 'browser'", pub)
        assert item["hosts"] == n and item["products"] >= 1, pub
    microsoft = facet["microsoft"]
    assert microsoft["products"] == 1 and microsoft["hosts"] == get(client, "/assets", product="microsoft-edge", page_size=1)["total"]


def test_last_change_is_the_newest_software_event_of_the_product(fleet_db, client, pinned_clock):
    rows = {r["product_key"]: r for r in get(client, "/software", page_size=500, hidden="include")["items"]}
    for key, row in rows.items():
        newest = fleet_db.scalar(
            "SELECT max(observed_at) FROM inventory_change_events WHERE subject_key = $1 AND category = 'software'", key)
        assert row["last_change"] == (newest.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None), key
    assert rows["google-chrome"]["last_change"] == "2026-10-08T09:00:00Z"
    assert rows["veeam-backup-and-replication"]["last_change"] is None


# ── version order and ranks ──────────────────────────────────────────

def test_versions_of_a_product_are_ranked_per_scheme(client, pinned_clock):
    body = get(client, "/software/git")
    v = body["versions"]
    assert body["header"]["mixed_schemes"] is True
    assert [(x["version"], x["scheme"], x["rank"], x["generation"]) for x in v] == [
        ("2.47.0", "semverish", 1, "N"), ("2.46.2", "semverish", 2, "N-1"), ("2.43.0", "semverish", 3, "older"),
        ("1:2.34.1-1ubuntu1.11", "dpkg", 1, "N"), ("1:2.34.1-1ubuntu1.10", "dpkg", 2, "N-1")]
    assert body["header"]["latest_version"] == "2.47.0"                       # the newest of the group with more hosts
    assert body["header"]["version_count"] == len(v) == 5
    assert body["unversioned"] == {"hosts": 0, "installs": 0, "address": isq.NO_VERSION}


def test_the_comparator_orders_what_a_string_sort_gets_wrong(client, pinned_clock):
    chrome = get(client, "/software/google-chrome")["versions"]
    assert [x["version"] for x in chrome] == ["152.0.8001.12", "151.0.7922.110", "150.0.7871.50"]
    vcpp = get(client, "/software/microsoft-visual-c-plus-plus-2015-2022-redistributable")["versions"]
    assert [x["version"] for x in vcpp] == ["14.40.33810", "14.38.33135", "14.36.32532"]      # numerically, not 14.4 < 14.36
    assert [x["generation"] for x in vcpp] == ["N", "N-1", "older"]
    # the 2013 redistributable is a product of its own (one key per year), so its 12.0 build is not 'older' than 14.x
    vcpp13 = get(client, "/software/microsoft-visual-c-plus-plus-2013-redistributable")["versions"]
    assert [(x["version"], x["generation"]) for x in vcpp13] == [("12.0.40664", "N")]
    sevenzip = get(client, "/software/7-zip")["versions"]
    assert [x["version"] for x in sevenzip] == ["24.08", "24.05", "23.01", "22.01"]      # 24.08 > 24.05 > 23.01 numerically


def test_a_product_with_awkward_versions(fleet_db_fresh, pinned_clock):
    """Spellings of one version are one row (7.2 and 7.2.0), a pre-release is below its release, an unparseable version
    has no rank, a blank version is not a version, and the catalog row agrees with the product page."""
    versions = ["1.0", "1.0.0", "1.9", "1.10", "2.0rc1", "2.0", "nightly-build", "", None]
    add_software_rows(fleet_db_fresh, [
        {"server": f"srv-w22-{i + 1:02d}", "title": "Acme Widget Pro", "vendor": "Acme Inc", "version": v}
        for i, v in enumerate(versions)])
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        page = get(c, "/software/acme-widget-pro")
        v = {x["version"]: x for x in page["versions"]}
        assert set(v) == {"1.0.0", "1.9", "1.10", "2.0rc1", "2.0", "nightly-build"}      # 1.0 is 1.0.0 (the longer spelling labels it); blank and NULL are not versions
        assert [x["version"] for x in page["versions"]][:2] == ["2.0", "2.0rc1"]
        assert v["2.0"]["rank"] == 1 and v["2.0rc1"]["rank"] == 2 and v["1.10"]["rank"] == 3 and v["1.9"]["rank"] == 4
        assert v["1.0.0"]["rank"] == 5 and v["1.0.0"]["generation"] == "older"
        assert (v["1.0.0"]["hosts"], v["1.0.0"]["installs"]) == (2, 2)               # both spellings, two servers
        assert v["nightly-build"]["rank"] is None and v["nightly-build"]["generation"] is None
        assert [x["version"] for x in page["versions"]][-1] == "nightly-build"
        assert page["unversioned"] == {"hosts": 2, "installs": 2, "address": isq.NO_VERSION}
        assert page["header"]["version_count"] == 6 and page["header"]["latest_version"] == "2.0"
        row = next(r for r in get(c, "/software", q="acme widget")["items"])
        assert row["version_count"] == 6 and row["latest_version"] == "2.0" and row["hosts"] == 9
        # the version filter of the installations: the blank rows are reachable as '__none__', every spelling by any spelling
        assert get(c, "/software/acme-widget-pro/installations", version=isq.NO_VERSION)["total"] == 2
        assert get(c, "/software/acme-widget-pro/installations", version="1.0")["total"] == 2


def test_the_catalog_version_bar_is_the_eight_busiest_versions_newest_first(fleet_db_fresh, pinned_clock):
    # 20 versions, one host each, except 3.4.0 / 3.5.0 / 3.6.0 which three hosts run
    rows = [{"server": name, "title": "Many Versions Tool", "vendor": "Acme Inc", "version": f"3.{minor}.0"}
            for minor, name in enumerate([f"srv-w22-{i:02d}" for i in range(1, 19)] + ["srv-w19-01", "srv-w19-02"])]
    rows += [{"server": f"srv-w19-{i:02d}", "title": "Many Versions Tool", "vendor": "Acme Inc", "version": f"3.{4 + i % 3}.0"}
             for i in range(3, 9)]
    add_software_rows(fleet_db_fresh, rows)
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        row = get(c, "/software", q="many versions")["items"][0]
        bar = row["distribution"]
        assert row["version_count"] == 20 and row["hosts"] == 26 and len(bar["top"]) == 8
        ranks = [t["rank"] for t in bar["top"]]
        assert ranks == sorted(ranks) and None not in ranks                       # newest first by comparator
        busiest = {t["version"]: t["hosts"] for t in bar["top"] if t["hosts"] == 3}
        assert set(busiest) == {"3.4.0", "3.5.0", "3.6.0"}                       # the busiest versions are in
        assert [t["version"] for t in bar["top"] if t["hosts"] == 1] == ["3.19.0", "3.18.0", "3.17.0", "3.16.0", "3.15.0"]
        assert bar["other"] == {"versions": 12, "hosts": 12}
        page = get(c, f"/software/{row['product_key']}")
        assert len(page["versions"]) == 20
        assert sum(t["hosts"] for t in bar["top"]) + bar["other"]["hosts"] == sum(v["hosts"] for v in page["versions"]) == 26


# ── normalisation status ─────────────────────────────────────────────

CORPUS = [
    ("Google Chrome", "Google LLC", "hklm"), ("Acme Widget Pro 3.1.4", "Acme Inc", "hklm"),
    ("Mystery Tool", None, "hklm"), ("Foo Utility 2.0", "Microsoft Corporation", "hklm"),
    ("Security Update for Microsoft Windows (KB5034439)", "Microsoft", "hklm"), ("Intel(R) Chipset Device Software", "Intel Corporation", "hklm"),
    ("Microsoft Windows Language Pack - English", "Microsoft", "hklm"), ("libssl3", "Ubuntu Developers <ubuntu-devel@lists.ubuntu.com>", "dpkg"),
    ("some-package", None, "rpm"), ("Java 8 Update 391", "Oracle Corporation", "hklm"), ("", "Adobe", "hklm"), ("7-Zip 24.05 (x64)", "Igor Pavlov", "hklm"),
]


@pytest.mark.parametrize("title,vendor,source", CORPUS)
def test_the_derived_status_matches_the_real_normaliser(title, vendor, source):
    """The rows store the key, the publisher key and the category but not the status; ``norm_status`` recovers it.
    If the pipeline changes so that this derivation is wrong, this test fails instead of a pill showing the wrong thing."""
    key = norm.normalize_software(title, vendor, None, source=source)
    assert isq.norm_status(key.name_key, key.category, key.publisher_key) == key.status, (title, key)


def test_products_without_a_publisher_are_the_unknown_publisher(fleet_db_fresh, pinned_clock):
    add_software_rows(fleet_db_fresh, [
        {"server": "srv-w22-01", "title": "Mystery Tool", "vendor": None, "version": "1.0"},
        {"server": "srv-w22-02", "title": "Mystery Tool", "vendor": None, "version": "1.1"},
        {"server": "srv-w22-02", "title": "Orphan Utility", "vendor": None, "version": "2.0"},
        {"server": "srv-w22-03", "title": "Orphan Utility", "vendor": "", "version": "2.0"}])
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        facet = {i["value"]: i for i in get(c, "/software/publishers", limit=200)["items"]}
        assert "unknown" in facet and facet["unknown"]["label"] == "Unknown"
        listed = get(c, "/software", publisher="unknown", page_size=50)
        assert facet["unknown"]["products"] == listed["total"] >= 2
        assert {r["publisher"] for r in listed["items"]} == {None}
        assert facet["unknown"]["installs"] == sum(r["installs"] for r in listed["items"])
        expected_hosts = fleet_db_fresh.scalar(
            "SELECT count(DISTINCT server_id) FROM server_software_inventory WHERE publisher_key IS NULL AND name_key IS NOT NULL")
        assert facet["unknown"]["hosts"] == expected_hosts == 3
        assert get(c, "/software", publisher="UNKNOWN")["total"] == listed["total"]


def test_the_derived_status_matches_the_normaliser_for_every_product_of_the_fleet(fleet_db):
    for key, category, publisher, title, vendor, source in fleet_db.query(
            "SELECT DISTINCT ON (name_key) name_key, category, publisher_key, package_name, vendor, source "
            "FROM server_software_inventory ORDER BY name_key, package_name"):
        real = norm.normalize_software(title, vendor, None, source=source)
        assert isq.norm_status(key, category, publisher) == real.status == "normalized", key


def test_the_unmatched_queue(fleet_db_fresh, pinned_clock):
    add_software_rows(fleet_db_fresh, [
        {"server": "srv-w22-01", "title": "Acme Widget Pro 3.1.4", "vendor": "Acme Inc", "version": "3.1.4"},
        {"server": "srv-w22-02", "title": "Acme Widget Pro 3.1.5", "vendor": "Acme Inc", "version": "3.1.5"},
        {"server": "srv-w22-01", "title": "Mystery Tool", "vendor": None, "version": "1.0"},
        {"server": "srv-w22-03", "title": "Foo Utility 2.0", "vendor": "Microsoft Corporation", "version": "2.0"},
    ])
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        rows = {r["product_key"]: r for r in get(c, "/software", hidden="include", page_size=500)["items"]}
        assert rows["acme-widget-pro"]["norm_status"] == "unmatched" and rows["acme-widget-pro"]["hosts"] == 2
        assert rows["mystery-tool"]["norm_status"] == "unmatched"
        assert rows["microsoft-foo-utility"]["norm_status"] == "publisher_only"
        queue = get(c, "/software", unmatched="true", page_size=500)
        assert {r["product_key"] for r in queue["items"]} == {"acme-widget-pro", "mystery-tool"}
        # the footer count is the size of the list its "Review" link opens, with the same other filters
        plain = get(c, "/software", page_size=1)
        assert plain["unmatched_titles"] == queue["total"] == 2
        assert get(c, "/software", category="productivity")["unmatched_titles"] == 0
        # the product page and the catalog agree, and the evidence shows what the normaliser made of the raw titles
        page = get(c, "/software/acme-widget-pro")
        assert page["header"]["norm_status"] == "unmatched" and page["header"]["rule_id"] is None
        assert {(e["package_name"], e["hosts"]) for e in page["evidence"]["items"]} == {("Acme Widget Pro 3.1.4", 1), ("Acme Widget Pro 3.1.5", 1)}
        assert all(e["status"] == "unmatched" and e["rule_id"] is None for e in page["evidence"]["items"])
        assert get(c, "/software/microsoft-foo-utility")["header"]["norm_status"] == "publisher_only"
        assert get(c, "/software/google-chrome")["header"]["rule_id"] == "chrome"       # the pack rule, not the key


# ── the table cache ──────────────────────────────────────────────────

def test_a_restricted_callers_table_is_cached_for_a_minute_and_never_served_stale(fleet_db_fresh, pinned_clock, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(isq, "_monotonic", lambda: clock["t"])
    with api_client(fleet_db_fresh, role="t_sw_scoped", permissions=["inventory.view"], scope_tags=("prod", "dev")) as c:
        figures = lambda: [(r["hosts"], r["installs"]) for r in get(c, "/software", q="zoom")["items"]]   # noqa: E731
        before = figures()
        assert before and before[0][1] > 0
        fleet_db_fresh.execute("DELETE FROM server_software_inventory WHERE name_key = 'zoom'")
        clock["t"] += 30
        assert figures() == before                                                     # still the cached figures
        clock["t"] += 31
        assert figures() == []                                                         # a minute later: re-read, at once


@pytest.mark.asyncio
async def test_the_whole_estate_table_is_served_stale_for_a_while_a_fresh_one_is_built(fleet_db_fresh, pinned_clock, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(isq, "_monotonic", lambda: clock["t"])
    zoom = lambda body: [r["installs"] for r in body["items"]]            # noqa: E731
    async with sessions(fleet_db_fresh) as sm:
        async def look(scope=None):
            async with sm() as s:
                return await isq.catalog(s, isq.CatalogFilters(q="zoom"), scope=scope)
        first = await look()
        assert zoom(first) == [7]
        fleet_db_fresh.execute("DELETE FROM server_software_inventory WHERE name_key = 'zoom'")
        clock["t"] += 90                                                  # past the minute, inside the five
        stale = await look()
        assert zoom(stale) == [7] and stale["generated_at"] == first["generated_at"]          # served at once, from the old table
        await settle()                                                     # the background build finishes
        assert zoom(await look()) == []                                    # and the next caller sees it
        # a restricted caller never gets a stale table
        clock["t"] += 0
        scoped_first = await look(["prod", "dev"])
        assert zoom(scoped_first) == []
        fleet_db_fresh.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, norm_rev) "
                               "SELECT id, 'Zoom', '9.9.9', 'Zoom Video Communications, Inc.', 'zoom', 'zoom', 'productivity', 1 FROM servers WHERE display_name = 'srv-w22-01'")
        clock["t"] += 90
        again = zoom(await look(["prod", "dev"]))
        assert again == zoom(await look(["prod", "dev"])) and again != [7]          # fresh at once, whatever the scope holds
        # past five minutes even the whole-estate caller waits for the new table
        clock["t"] += 400
        assert zoom(await look()) == [1]
        assert not isq._inflight


async def settle() -> None:
    """Wait for the table builds that are running."""
    for task in list(isq._inflight.values()):
        await task


@pytest.mark.asyncio
async def test_a_failed_background_build_keeps_the_old_table_and_the_next_caller_builds_again(fleet_db_fresh, pinned_clock, monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(isq, "_monotonic", lambda: clock["t"])
    real = isq._load_table
    state = {"fail": False}

    async def flaky(db, scope):
        if state["fail"]:
            raise RuntimeError("database away")
        return await real(db, scope)

    monkeypatch.setattr(isq, "_load_table", flaky)
    async with sessions(fleet_db_fresh) as sm:
        async def look():
            async with sm() as s:
                return await isq.catalog(s, isq.CatalogFilters(), scope=None, page_size=1)
        first = await look()
        state["fail"] = True
        clock["t"] += 90
        assert (await look())["total"] == first["total"]                  # stale, and the rebuild fails quietly
        import asyncio
        for _ in range(100):
            if "could not be built" in caplog.text:
                break
            await asyncio.sleep(0.02)
        assert "could not be built" in caplog.text and not isq._inflight
        state["fail"] = False
        clock["t"] += 400
        assert (await look())["total"] == first["total"]


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_build(fleet_db, pinned_clock, monkeypatch):
    calls = []
    real = isq._load_table

    async def counting(db, scope):
        calls.append(scope)
        await asyncio.sleep(0.05)
        return await real(db, scope)

    monkeypatch.setattr(isq, "_load_table", counting)
    async with sessions(fleet_db) as sm:
        async def one():
            async with sm() as s:
                return await isq.catalog(s, isq.CatalogFilters(), scope=None, page_size=5)
        a, b, c = await asyncio.gather(one(), one(), one())
    assert len(calls) == 1 and a["total"] == b["total"] == c["total"] == 24


@pytest.mark.asyncio
async def test_a_failed_build_is_not_cached(fleet_db, pinned_clock, monkeypatch):
    real = isq._load_table
    state = {"fail": True}

    async def flaky(db, scope):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("boom")
        return await real(db, scope)

    monkeypatch.setattr(isq, "_load_table", flaky)
    async with sessions(fleet_db) as sm:
        async with sm() as s:
            with pytest.raises(RuntimeError):
                await isq.catalog(s, isq.CatalogFilters(), scope=None)
        async with sm() as s:
            assert (await isq.catalog(s, isq.CatalogFilters(), scope=None))["total"] == 24


HOSTILE = ["\x00", "a\x00b", "'; DROP TABLE servers; --", "%", "\\", "\u00e9" * 300, "\U0001F600", "\n\r\t", "{{7*7}}", ":q", "$1", "[]{}"]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_text_is_data_never_a_server_error(client, value, pinned_clock):
    """NUL bytes, quotes, wildcards, bind-looking text and oversized strings in every text parameter: 200, 404 or 422, never 5xx,
    and the data is untouched."""
    from urllib.parse import quote, urlencode

    def call(path: str, **params):
        return client.get(f"{API}{path}?{urlencode(params)}")

    for path, param in (("/software", "q"), ("/software", "publisher"), ("/software", "category"), ("/software/publishers", "q"),
                        ("/software/git/installations", "q"), ("/software/git/installations", "version")):
        resp = call(path, **{param: value})
        assert resp.status_code in (200, 422), (path, param, repr(value), resp.status_code, resp.text[:200])
    for suffix in ("", "/installations"):
        assert client.get(f"{API}/software/{quote(value, safe='')}{suffix}").status_code in (404, 422), repr(value)
    assert get(client, "/software", page_size=1)["total"] == 24


# ════════════════════════════════════════════════════════════════════
# The product page
# ════════════════════════════════════════════════════════════════════

def test_product_page_shape_and_header_agree_with_the_catalog_row(client, pinned_clock):
    body = get(client, "/software/google-chrome")
    assert set(body) == {"generated_at", "scope", "header", "versions", "unversioned", "evidence", "data_status"}
    h = body["header"]
    row = next(r for r in get(client, "/software", q="chrome")["items"] if r["product_key"] == "google-chrome")
    for k in ("product_key", "name", "publisher", "publisher_key", "category", "norm_status", "hosts", "installs",
              "version_count", "latest_version", "first_seen", "last_change"):
        assert h[k] == row[k], k
    assert (h["name"], h["publisher"], h["category"], h["rule_id"]) == ("Google Chrome", "Google", "browser", "chrome")
    assert h["mixed_schemes"] is False
    assert set(body["versions"][0]) == {"version", "hosts", "installs", "scheme", "rank", "generation"}


def test_the_versions_of_a_product_page_are_all_of_them_with_their_hosts(fleet_db, client, pinned_clock):
    for key in ("google-chrome", "git", "microsoft-visual-c-plus-plus-2015-2022-redistributable",
                "microsoft-visual-c-plus-plus-2013-redistributable", "java-se-runtime", "7-zip"):
        page = get(client, f"/software/{key}")
        oracle = {r[0]: (r[1], r[2]) for r in fleet_db.query(
            "SELECT version, count(DISTINCT server_id), count(*) FROM server_software_inventory WHERE name_key = $1 GROUP BY 1", key)}
        assert {v["version"]: (v["hosts"], v["installs"]) for v in page["versions"]} == oracle, key
        assert page["header"]["version_count"] == len(oracle) == len(page["versions"])
        assert page["header"]["installs"] == sum(i for _h, i in oracle.values())


def test_evidence_lists_the_raw_names_that_map_to_the_product(fleet_db, client, pinned_clock):
    page = get(client, "/software/microsoft-visual-c-plus-plus-2015-2022-redistributable")
    ev = page["evidence"]
    oracle = fleet_db.query(
        "SELECT package_name, vendor, count(DISTINCT server_id), count(*) FROM server_software_inventory WHERE name_key = $1 GROUP BY 1, 2",
        "microsoft-visual-c-plus-plus-2015-2022-redistributable")
    assert ev["total"] == len(oracle) == len(ev["items"]) and ev["truncated"] is False
    assert {(e["package_name"], e["vendor"], e["hosts"], e["installs"]) for e in ev["items"]} == set(oracle)
    assert sum(e["installs"] for e in ev["items"]) == page["header"]["installs"]
    assert all(e["status"] == "normalized" and e["rule_id"] for e in ev["items"])
    hosts = [e["hosts"] for e in ev["items"]]
    assert hosts == sorted(hosts, reverse=True)
    java = get(client, "/software/java-se-runtime")["evidence"]
    assert {e["package_name"] for e in java["items"]} == {"Java 8 Update 391", "Java 8 Update 401", "Java 8 Update 421"}


def test_evidence_is_capped(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(isq, "EVIDENCE_LIMIT", 3)
    add_software_rows(fleet_db_fresh, [
        {"server": "srv-w22-01", "title": f"Acme Toolbox {n}.0", "vendor": "Acme Inc", "version": f"{n}.0"} for n in range(6)])
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        for key in [r["product_key"] for r in get(c, "/software", q="acme toolbox")["items"]][:1]:
            ev = get(c, f"/software/{key}")["evidence"]
            assert len(ev["items"]) == 3 and ev["total"] > 3 and ev["truncated"] is True


def test_unknown_products_and_hostile_keys_are_404_with_one_body(client, pinned_clock):
    from urllib.parse import quote
    unknown = client.get(f"{API}/software/no-such-product")
    assert unknown.status_code == 404 and unknown.json() == {"detail": "Product not found"}
    for key in ("%", "'; DROP TABLE servers; --", "a" * 400, "é" * 300, "%00"):
        resp = client.get(f"{API}/software/{quote(key, safe='')}")
        assert resp.status_code == 404 and resp.json() == unknown.json(), key
        resp = client.get(f"{API}/software/{quote(key, safe='')}/installations")
        assert resp.status_code == 404 and resp.json() == unknown.json(), key
    assert client.get(f"{API}/software/publishers").status_code == 200       # the fixed path is not a product key


def test_a_key_with_a_plus_sign_is_addressable(fleet_db_fresh, pinned_clock):
    add_software_rows(fleet_db_fresh, [{"server": "srv-w22-01", "title": "C++ Toolkit Alpha", "vendor": "Acme Inc", "version": "1.0"}])
    key = fleet_db_fresh.scalar("SELECT name_key FROM server_software_inventory WHERE package_name = 'C++ Toolkit Alpha'")
    with api_client(fleet_db_fresh, role="t_sw_view", permissions=["inventory.view"]) as c:
        assert get(c, f"/software/{key}")["header"]["product_key"] == key


# ════════════════════════════════════════════════════════════════════
# Installations
# ════════════════════════════════════════════════════════════════════

def test_installations_envelope_rows_and_totals(fleet_db, client, pinned_clock):
    body = get(client, "/software/java-se-runtime/installations", page_size=500)
    assert set(body) == {"generated_at", "product_key", "version", "items", "total", "page", "page_size", "sort", "order"}
    assert body["total"] == len(body["items"]) == 24 == get(client, "/software/java-se-runtime")["header"]["installs"]
    assert set(body["items"][0]) == {"asset_key", "asset_id", "asset_name", "package_name", "version", "architecture", "source",
                                     "install_date", "first_seen_at", "updated_at"}
    oracle = {(r[0], r[1], r[2]) for r in fleet_db.query(
        "SELECT s.display_name, w.package_name, w.version FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
        "WHERE w.name_key = 'java-se-runtime'")}
    assert {(r["asset_name"], r["package_name"], r["version"]) for r in body["items"]} == oracle
    row = body["items"][0]
    assert row["asset_key"] == f"server:{row['asset_id']}"
    names = [r["asset_name"].lower() for r in body["items"]]
    assert names == sorted(names)


def test_installations_filters(client, pinned_clock):
    all_rows = get(client, "/software/google-chrome/installations", page_size=500)["items"]
    for version in {r["version"] for r in all_rows}:
        got = get(client, "/software/google-chrome/installations", version=version, page_size=500)
        assert got["total"] == sum(1 for r in all_rows if r["version"] == version) and got["version"] == version
        assert {r["version"] for r in got["items"]} == {version}
    assert get(client, "/software/google-chrome/installations", version="9.9.9")["total"] == 0
    assert get(client, "/software/google-chrome/installations", q="srv-w22-0")["total"] == sum(1 for r in all_rows if "srv-w22-0" in r["asset_name"])
    assert get(client, "/software/google-chrome/installations", q="152.0")["total"] == sum(1 for r in all_rows if "152.0" in r["version"])


@pytest.mark.parametrize("sort", isq.INSTALL_SORTS)
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_installations_sort_keys(client, pinned_clock, sort, order):
    first = get(client, "/software/7-zip/installations", sort=sort, order=order, page_size=500)
    keys = [(r["asset_name"], r["package_name"]) for r in first["items"]]
    paged = []
    for page in range(1, 6):
        paged += [(r["asset_name"], r["package_name"]) for r in get(client, "/software/7-zip/installations", sort=sort, order=order, page=page, page_size=8)["items"]]
    assert paged == keys and len(set(keys)) == first["total"] == 34       # a total order: no row repeated or skipped
    if sort not in ("version",):
        col = {"asset_name": "asset_name", "package_name": "package_name"}.get(sort, sort)
        values = [r[col] for r in first["items"] if r[col] is not None]
        key = (lambda s: s.lower()) if sort in ("asset_name", "package_name") else (lambda s: s)
        assert [key(v) for v in values] == sorted((key(v) for v in values), reverse=(order == "desc"))


def test_installations_sort_by_version_follows_the_comparator(client, pinned_clock):
    asc = get(client, "/software/7-zip/installations", sort="version", order="asc", page_size=500)["items"]
    desc = get(client, "/software/7-zip/installations", sort="version", order="desc", page_size=500)["items"]
    order = ["22.01", "23.01", "24.05", "24.08"]
    assert [order.index(r["version"]) for r in asc] == sorted(order.index(r["version"]) for r in asc)
    assert [order.index(r["version"]) for r in desc] == sorted((order.index(r["version"]) for r in desc), reverse=True)
    mixed = get(client, "/software/git/installations", sort="version", order="desc", page_size=500)["items"]
    assert [r["version"] for r in mixed][:1] == ["2.47.0"]                                    # semverish group first, newest first


def test_installations_errors_and_paging(client, pinned_clock):
    for params in ({"sort": "bogus"}, {"order": "up"}, {"page": 0}, {"page_size": 0}, {"page_size": 501}):
        get(client, "/software/git/installations", expect=422, **params)
    body = get(client, "/software/git/installations", page=9, page_size=50)
    assert body["items"] == [] and body["total"] == 19


# ════════════════════════════════════════════════════════════════════
# Scope
# ════════════════════════════════════════════════════════════════════

def scoped(db, tags=(SCOPED_TAG,)):
    return api_client(db, role="t_sw_scoped", permissions=["inventory.view"], scope_tags=tuple(tags))


def test_a_scoped_catalog_counts_only_the_visible_servers(fleet_db, pinned_clock):
    oracle = sql_products(fleet_db, list(EDGE_SERVERS))
    assert 0 < len(oracle) < 33
    with scoped(fleet_db) as c:
        body = get(c, "/software", hidden="include", page_size=500)
        assert body["scope"] == {"restricted": True, "tags": [SCOPED_TAG]}
        assert {r["product_key"] for r in body["items"]} == set(oracle)         # a product with no visible host is absent
        for r in body["items"]:
            o = oracle[r["product_key"]]
            assert (r["hosts"], r["installs"], r["version_count"]) == (o["hosts"], o["installs"], o["versions"]), r["product_key"]
            assert r["hosts"] <= len(EDGE_SERVERS)
        assert body["total"] == len(oracle)
        shown = get(c, "/software", page_size=500)
        assert shown["total"] == len(visible(oracle))
        assert shown["hidden_installs"] == sum(v["installs"] for v in oracle.values() if v["category"] in HIDDEN)
        facet = get(c, "/software/publishers", limit=200)
        assert facet["total"] == shown["total"] and sum(i["products"] for i in facet["items"]) == shown["total"]
        for item in facet["items"]:
            n = fleet_db.scalar(
                "SELECT count(DISTINCT w.server_id) FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
                "WHERE w.publisher_key = $1 AND w.category <> ALL($2::text[]) AND s.display_name = ANY($3::text[])",
                item["value"], list(HIDDEN), list(EDGE_SERVERS))
            assert item["hosts"] == n
    with api_client(fleet_db, role="t_sw_all", permissions=["inventory.view"]) as c:
        whole = get(c, "/software", hidden="include", page_size=500)
    assert whole["total"] == 33 and sum(r["hosts"] for r in whole["items"]) > sum(r["hosts"] for r in body["items"])


def test_a_scoped_product_page_and_installations(fleet_db, pinned_clock):
    oracle = sql_products(fleet_db, list(EDGE_SERVERS))
    with scoped(fleet_db) as c:
        for key in ("google-chrome", "git"):
            page = get(c, f"/software/{key}")
            o = oracle[key]
            assert (page["header"]["hosts"], page["header"]["installs"], page["header"]["version_count"]) == (o["hosts"], o["installs"], o["versions"])
            rows = get(c, f"/software/{key}/installations", page_size=500)
            assert rows["total"] == o["installs"]
            assert {r["asset_name"] for r in rows["items"]} <= set(EDGE_SERVERS)
            ev = page["evidence"]
            assert sum(e["installs"] for e in ev["items"]) == o["installs"]
            assert sum(v["hosts"] for v in page["versions"]) >= o["hosts"]
        # versions that only out-of-scope servers have do not appear
        whole_versions = {r[0] for r in fleet_db.query("SELECT DISTINCT version FROM server_software_inventory WHERE name_key = 'google-chrome'")}
        mine = {v["version"] for v in get(c, "/software/google-chrome")["versions"]}
        assert mine <= whole_versions


def test_an_out_of_scope_product_is_a_404_that_looks_like_an_unknown_one(fleet_db, pinned_clock):
    oracle = sql_products(fleet_db, list(EDGE_SERVERS))
    hidden_from_edge = [k for k in sql_products(fleet_db) if k not in oracle]
    assert hidden_from_edge, "the fixture needs a product no edge server has"
    with scoped(fleet_db) as c:
        unknown = c.get(f"{API}/software/no-such-product")
        assert unknown.status_code == 404
        for key in hidden_from_edge:
            for suffix in ("", "/installations"):
                resp = c.get(f"{API}/software/{key}{suffix}")
                assert resp.status_code == 404 and resp.json() == unknown.json()
                assert resp.headers.get("content-type") == unknown.headers.get("content-type")
    with api_client(fleet_db, role="t_sw_all", permissions=["inventory.view"]) as c:
        for key in hidden_from_edge:
            assert c.get(f"{API}/software/{key}").status_code == 200


def test_a_scope_that_matches_nothing_sees_an_empty_catalog(fleet_db, pinned_clock):
    with scoped(fleet_db, ("no-such-tag",)) as c:
        body = get(c, "/software", hidden="include")
        assert body["items"] == [] and body["total"] == 0 and body["hidden_products"] == 0 and body["unmatched_titles"] == 0
        assert get(c, "/software/publishers")["items"] == []
        get(c, "/software/google-chrome", expect=404)


def test_the_catalog_cache_never_serves_one_scope_to_another(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_sw_all", permissions=["inventory.view"]) as c:
        whole = get(c, "/software", hidden="include", page_size=500)
    with scoped(fleet_db) as c:
        mine = get(c, "/software", hidden="include", page_size=500)
        again = get(c, "/software", hidden="include", page_size=500)
    with scoped(fleet_db, ("team-a",)) as c:
        theirs = get(c, "/software", hidden="include", page_size=500)
    with api_client(fleet_db, role="t_sw_all", permissions=["inventory.view"]) as c:
        assert get(c, "/software", hidden="include", page_size=500) == whole
    assert mine == again and mine != whole and theirs != mine and theirs["scope"]["tags"] == ["team-a"]


def test_inventory_view_alone_reads_the_software_inventory(fleet_db, pinned_clock):
    """Doc 02 section 11 / D3: inventory.view alone shows the software inventory within tag scope (no servers.view needed)."""
    with api_client(fleet_db, role="t_inv_only", permissions=["inventory.view"]) as c:
        for path in ("/software", "/software/publishers", "/software/git", "/software/git/installations"):
            assert c.get(API + path).status_code == 200, path


# ════════════════════════════════════════════════════════════════════
# T-UI2 for the Software pages: every count the pages link to equals the total of the list it opens
# ════════════════════════════════════════════════════════════════════

def assets_total(client, **params) -> int:
    return get(client, "/assets", page_size=1, **params)["total"]


def test_drill_down_hosts_per_product_and_per_version(client, pinned_clock):
    rows = catalog_all(client, hidden="include")
    assert len(rows) == 33
    for r in rows:
        # catalog "Hosts" -> /inventory/assets?product=<key>
        assert assets_total(client, product=r["product_key"]) == r["hosts"], r["product_key"]
        # the version bar of the row and the version rows of the product page -> &product_version=<version>
        page = get(client, f"/software/{r['product_key']}")
        assert page["header"]["hosts"] == r["hosts"] and len(page["versions"]) == r["version_count"]
        for v in page["versions"]:
            assert assets_total(client, product=r["product_key"], product_version=v["version"]) == v["hosts"], (r["product_key"], v["version"])
        for t in r["distribution"]["top"]:
            assert assets_total(client, product=r["product_key"], product_version=t["version"]) == t["hosts"]
        # installs -> the installations list
        assert get(client, f"/software/{r['product_key']}/installations", page_size=1)["total"] == r["installs"] == page["header"]["installs"]
        # a version row's installs -> the installations list filtered by that version
        for v in page["versions"]:
            assert get(client, f"/software/{r['product_key']}/installations", version=v["version"], page_size=1)["total"] == v["installs"]


def test_drill_down_footers_and_category_counts(fleet_db, client, pinned_clock):
    everything = {r["product_key"]: r for r in get(client, "/software", hidden="include", page_size=500)["items"]}
    for category in sorted({r["category"] for r in everything.values()}):
        in_category = [r for r in everything.values() if r["category"] == category]
        listed = get(client, "/software", category=category, hidden="include", page_size=500)
        assert listed["total"] == len(in_category), category                         # "Security (4)" -> the list of 4
        assert sum(r["installs"] for r in listed["items"]) == sum(r["installs"] for r in in_category)
    shown = get(client, "/software", page_size=1)
    assert get(client, "/software", hidden="include", page_size=1)["total"] == shown["total"] + shown["hidden_products"]
    assert get(client, "/software", unmatched="true", page_size=1)["total"] == shown["unmatched_titles"]
    # hidden entries of one category: the footer counts them under the same other filters
    only_updates = get(client, "/software", category="update", hidden="include", page_size=1)
    assert only_updates["hidden_products"] == 0 or only_updates["hidden_products"] == only_updates["total"]
    q_view = get(client, "/software", q="microsoft", page_size=500)
    q_all = get(client, "/software", q="microsoft", hidden="include", page_size=500)
    assert q_all["total"] == q_view["total"] + q_view["hidden_products"] and q_view["hidden_installs"] > 0
