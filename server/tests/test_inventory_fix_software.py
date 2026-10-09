"""Regression tests of the Phase 1 review for the software catalog (be-b fix round).

Each test names the finding it pins: F5 (a cold caller held a pooled connection while the shared build needed another),
F7 (ranking versions and building the table ran on the event loop; sort by latest version at more than 10,000
products), F10 (expired tables were never evicted; the export filled each table with every product's versions), BN-09
(a version spelled "unknown" collided with the no-version sentinel), the equivalence-key merge (a version row's count equals
the Assets list it links to), X2 (``data_status``) and the catalog warmer.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import inventory_export as export
from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from app.services import inventory_software_query as isq
from app.services import versioning
from inventory_fleet import (
    EDGE_SERVERS, SCOPED_TAG, add_software_rows, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock,
    sessions,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
CF = isq.CatalogFilters


def get(client, path: str, expect: int = 200, **params) -> Any:
    resp = client.get(API + path, params={k: v for k, v in params.items() if v is not None})
    assert resp.status_code == expect, (path, params, resp.status_code, resp.text[:300])
    return resp.json()


@pytest.fixture
def viewer(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_fx_sw", permissions=["inventory.view"]) as c:
        yield c


def seed_products(db, products: int, versions: int) -> None:
    """``products`` x ``versions`` software rows (keys ``perf-product-<g>``, versions ``1.<v>.<g % 5>``), spread over the servers,
    in one statement. The capture triggers are suppressed, so the rows add no events."""
    db.psql(f"""
        BEGIN;
        SET LOCAL zenplus.inventory_suppress = 'on';
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor, install_date, updated_at, first_seen_at,
                                               source, name_key, publisher_key, category, norm_rev)
        SELECT s.id, 'Perf Product ' || g || ' build ' || v, '1.' || v || '.' || (g % 5), 'Perf Vendor', now(), now(), now(),
               'hklm', 'perf-product-' || g, 'perf-vendor', 'other', {norm.RULEPACK_REV}
          FROM generate_series(1, {products}) g
          CROSS JOIN generate_series(0, {versions - 1}) v
          JOIN LATERAL (SELECT id FROM servers ORDER BY id OFFSET (g + v) % 40 LIMIT 1) s ON true;
        COMMIT;""")


# ════════════════════════════════════════════════════════════════════
# The equivalence key: a version row is the Assets list it links to
# ════════════════════════════════════════════════════════════════════

def add_spellings(db) -> None:
    """Product ``acme-widget-pro``: 7.2 and 7.2.0 on different servers, BOTH on one server (a 32 and a 64 bit title), V1 and v1,
    a version spelled 'unknown' in two cases, and rows with no version at all (NULL and '')."""
    versions = ["7.2", "7.2.0", "V1", "v1", "unknown", "Unknown", None, "", "9.0"]
    add_software_rows(db, [{"server": f"srv-w22-{i + 1:02d}", "title": "Acme Widget Pro", "vendor": "Acme Inc", "version": v}
                           for i, v in enumerate(versions)])
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, name_key, publisher_key, category, "
               "norm_rev, source) SELECT server_id, package_name || ' (x86)', '7.2.0', vendor, name_key, publisher_key, category, "
               "norm_rev, source FROM server_software_inventory WHERE name_key = 'acme-widget-pro' AND version = '7.2'")


def test_equivalence_a_version_row_counts_every_spelling_once_per_host(fleet_db_fresh, pinned_clock, viewer):
    add_spellings(fleet_db_fresh)
    page = get(viewer, "/software/acme-widget-pro")
    by = {v["version"]: v for v in page["versions"]}
    labels = [v["version"] for v in page["versions"]]
    # 7.2 and 7.2.0 are one row, labelled with the longer spelling; V1 and v1 are one row; unknown and Unknown are one row
    assert sorted(l.lower() for l in labels) == ["7.2.0", "9.0", "unknown", "v1"]
    seven = by["7.2.0"]
    assert seven["installs"] == 3                                  # '7.2', '7.2.0' and the (x86) title of the first server
    assert seven["hosts"] == 2                                     # the server that holds both spellings is ONE host
    assert page["header"]["version_count"] == len(labels) == 4
    assert page["unversioned"] == {"hosts": 2, "installs": 2, "address": isq.NO_VERSION}     # NULL and '' are one bucket


def test_equivalence_every_version_row_equals_the_total_of_the_list_it_links_to(fleet_db_fresh, pinned_clock, viewer):
    add_spellings(fleet_db_fresh)
    page = get(viewer, "/software/acme-widget-pro")
    for v in page["versions"]:
        landed = get(viewer, "/assets", product="acme-widget-pro", product_version=v["version"])["total"]
        assert landed == v["hosts"], v                                      # hosts == the Assets list behind the number
        installed = get(viewer, "/software/acme-widget-pro/installations", version=v["version"])["total"]
        assert installed == v["installs"], v                                # installs == the installations behind it
    # a spelling that is not the label lists the same rows
    assert get(viewer, "/software/acme-widget-pro/installations", version="7.2")["total"] == 3
    assert get(viewer, "/software/acme-widget-pro/installations", version="7.2.0")["total"] == 3
    assert get(viewer, "/software/acme-widget-pro/installations", version="v1")["total"] == 2
    assert get(viewer, "/software/acme-widget-pro/installations", version="V1")["total"] == 2


def test_equivalence_merged_hosts_follow_the_callers_scope(fleet_db_fresh, pinned_clock):
    """The exact host count of a merged version is taken over the servers the caller can see, like every other figure."""
    inside = list(EDGE_SERVERS)[:2]
    outside = [r[0] for r in fleet_db_fresh.query(
        "SELECT display_name FROM servers WHERE NOT (tags @> to_jsonb($1::text)) ORDER BY display_name LIMIT 2", SCOPED_TAG)]
    spellings = ["3.0", "3.0.0"]
    add_software_rows(fleet_db_fresh, [{"server": name, "title": "Acme Widget Pro", "vendor": "Acme Inc", "version": spellings[i % 2]}
                                       for i, name in enumerate(inside + outside)])
    with api_client(fleet_db_fresh, role="t_fx_wide", permissions=["inventory.view"]) as c:
        page = get(c, "/software/acme-widget-pro")
        assert [(v["version"], v["hosts"], v["installs"]) for v in page["versions"]] == [("3.0.0", 4, 4)]
        assert get(c, "/assets", product="acme-widget-pro", product_version="3.0")["total"] == 4
    with api_client(fleet_db_fresh, role="t_fx_scoped", permissions=["inventory.view"], scope_tags=(SCOPED_TAG,)) as c:
        page = get(c, "/software/acme-widget-pro")
        assert [(v["version"], v["hosts"], v["installs"]) for v in page["versions"]] == [("3.0.0", 2, 2)]
        assert get(c, "/assets", product="acme-widget-pro", product_version="3.0")["total"] == 2
        assert get(c, "/software/acme-widget-pro/installations", version="3.0")["total"] == 2


def test_equivalence_the_catalog_row_counts_versions_by_key_and_agrees_with_the_summary(fleet_db_fresh, pinned_clock, viewer):
    add_spellings(fleet_db_fresh)
    row = next(r for r in get(viewer, "/software", q="acme widget")["items"])
    assert row["product_key"] == "acme-widget-pro" and row["version_count"] == 4
    assert {(b["version"].lower(), b["hosts"]) for b in row["distribution"]["top"]} == {("7.2.0", 2), ("v1", 2), ("unknown", 2), ("9.0", 1)}
    # the sort by version count and the figure shown agree (the count is the number of keys, read by the database)
    ordered = get(viewer, "/software", sort="version_count", order="desc", page_size=500)["items"]
    counts = [r["version_count"] for r in ordered]
    assert counts == sorted(counts, reverse=True) and counts[0] >= 4
    by_latest = get(viewer, "/software", sort="latest_version", order="desc", q="acme widget")["items"]
    assert by_latest[0]["latest_version"] == "9.0"


@pytest.mark.asyncio
async def test_equivalence_the_merge_groups_spellings_exactly_as_the_database_does(fleet_db):
    """The Assets list matches ``product_version`` with the database's key, so the version rows must be merged with it too:
    ``_spelling_keys`` asks the database, and ``merge_spellings`` groups by that answer. (For ASCII text the Python function
    ``versioning.equivalence_key`` gives the same keys; for letters the database's locale cannot lower-case it may not.)"""
    ascii_corpus = ["7.2", "7.2.0", "7.2.00", "7.20", "1.10", "1.100", "V1", "v1", "v1.0", "V1.0.0", " 1.0 ", "1.0", "1.0\t", ".0", "0",
                    "0.0", "0.0.0", "", "   ", "unknown", "UNKNOWN", "10.0", "10", "2.0rc1", "2.0RC1", "2.0.0rc1", "1.0.0-rc1", "1.0-rc1"]
    corpus = ascii_corpus + ["\u00e91", "\u00c91", "\u0131", "I"]
    async with sessions(fleet_db) as sm:
        async with sm() as s:
            keys = await isq._spelling_keys(s, corpus)
    for raw in ascii_corpus:
        assert keys[raw] == versioning.equivalence_key(raw), repr(raw)
    stats = [isq.VersionStat(v, 1, 1, "semverish") for v in corpus]
    merged, multi = isq.merge_spellings(stats, keys)
    groups: dict[Any, list[str]] = {}
    for v in corpus:
        groups.setdefault(keys[v], []).append(v)
    assert len(merged) == len(groups)
    assert sorted(len(sp) for _i, sp in multi) == sorted(len(g) for g in groups.values() if len(g) > 1)
    labels = {m.version for m in merged}
    assert "7.2.00" in labels and "7.2" not in labels and "7.2.0" not in labels      # the longest spelling labels its key
    assert None in labels and "" not in labels and "   " not in labels             # the no-version spellings are one entry, label None


# ════════════════════════════════════════════════════════════════════
# BN-09: 'unknown' is a version, the no-version bucket has its own address
# ════════════════════════════════════════════════════════════════════

def test_bn09_a_version_spelled_unknown_is_listed_by_its_own_name(fleet_db_fresh, pinned_clock, viewer):
    add_spellings(fleet_db_fresh)
    page = get(viewer, "/software/acme-widget-pro")
    unknown = next(v for v in page["versions"] if v["version"].lower() == "unknown")
    assert (unknown["hosts"], unknown["installs"]) == (2, 2)
    listed = get(viewer, "/software/acme-widget-pro/installations", version="unknown")
    assert listed["total"] == 2 and {r["version"] for r in listed["items"]} == {"unknown", "Unknown"}   # the literal rows, never the blank ones
    assert get(viewer, "/assets", product="acme-widget-pro", product_version="unknown")["total"] == unknown["hosts"]
    blank = get(viewer, "/software/acme-widget-pro/installations", version=page["unversioned"]["address"])
    assert blank["total"] == page["unversioned"]["installs"] == 2
    assert {r["version"] for r in blank["items"]} <= {None, ""}
    assert page["unversioned"]["address"] == isq.NO_VERSION == "__none__"


def test_bn09_a_product_without_a_literal_unknown_lists_nothing_for_it(fleet_db_fresh, pinned_clock, viewer):
    """The old alias is gone: 'unknown' no longer means 'no version' (that would hide the collision again)."""
    add_software_rows(fleet_db_fresh, [{"server": "srv-w22-01", "title": "Acme Widget Pro", "vendor": "Acme Inc", "version": None}])
    assert get(viewer, "/software/acme-widget-pro/installations", version="unknown")["total"] == 0
    assert get(viewer, "/software/acme-widget-pro/installations", version=isq.NO_VERSION)["total"] == 1
    assert get(viewer, "/software/acme-widget-pro")["unversioned"]["address"] == isq.NO_VERSION


def test_bn09_the_export_of_installations_uses_the_same_addresses(fleet_db_fresh, pinned_clock):
    add_spellings(fleet_db_fresh)
    with api_client(fleet_db_fresh, role="t_fx_exp", permissions=["inventory.view", "inventory.export"], persist_user=True) as c:
        for version, rows in (("unknown", 2), (isq.NO_VERSION, 2), ("7.2", 3)):
            resp = c.get(f"{API}/export", params={"dataset": "installations", "product": "acme-widget-pro", "version": version})
            assert resp.status_code == 200 and resp.headers["X-Export-Rows"] == str(rows), version


# ════════════════════════════════════════════════════════════════════
# X2: data_status on the catalog responses
# ════════════════════════════════════════════════════════════════════

def test_x2_the_catalog_publishers_and_product_carry_data_status(fleet_db_fresh, pinned_clock, viewer):
    clean = {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}
    assert get(viewer, "/software")["data_status"] == clean
    assert get(viewer, "/software/publishers")["data_status"] == clean
    assert get(viewer, "/software/git")["data_status"] == clean
    fleet_db_fresh.execute("UPDATE server_software_inventory SET norm_rev = NULL WHERE name_key = 'git'")
    pending = fleet_db_fresh.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL")
    assert pending > 0
    for path in ("/software", "/software/publishers", "/software/google-chrome"):
        status = get(viewer, path)["data_status"]
        assert status["normalising"] is True and status["pending_software_rows"] == pending, path


def test_x2_an_empty_catalog_with_pending_rows_is_not_an_empty_estate(fleet_db_fresh, pinned_clock, viewer):
    """Before the first normalisation every software row is unkeyed and the catalog lists nothing: data_status tells that
    apart from an estate without software."""
    fleet_db_fresh.execute("UPDATE server_software_inventory SET name_key = NULL, category = NULL, publisher_key = NULL, norm_rev = NULL")
    body = get(viewer, "/software")
    assert body["total"] == 0 and body["items"] == []
    assert body["data_status"]["normalising"] is True and body["data_status"]["pending_software_rows"] > 0
    fleet_db_fresh.execute("DELETE FROM server_software_inventory")
    isq.clear_caches()
    iq.clear_caches()
    assert get(viewer, "/software")["data_status"]["pending_software_rows"] == 0


def test_x2_data_status_is_scope_aware(fleet_db_fresh, pinned_clock):
    fleet_db_fresh.execute("UPDATE server_software_inventory SET norm_rev = NULL WHERE server_id IN "
                           "(SELECT id FROM servers WHERE NOT (tags @> to_jsonb($1::text)))", SCOPED_TAG)
    outside = fleet_db_fresh.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL")
    assert outside > 0
    with api_client(fleet_db_fresh, role="t_fx_scoped", permissions=["inventory.view"], scope_tags=(SCOPED_TAG,)) as c:
        status = get(c, "/software")["data_status"]
    assert status["pending_software_rows"] == 0 and status["normalising"] is False       # the pending rows are on servers it cannot see


# ════════════════════════════════════════════════════════════════════
# F5: a caller waiting for a shared build gives its connection back
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("what", ["catalog", "publishers", "mixed"])
async def test_f5_n_cold_callers_on_a_pool_of_n_all_succeed(fleet_db_fresh, pinned_clock, what):
    """Each cold caller used to hold its pooled connection while waiting for the build, which needs one more: N callers on a
    pool of N all waited for ``pool_timeout`` and failed."""
    n = 3
    engine = create_async_engine(fleet_db_fresh.url, pool_size=n, max_overflow=0, pool_timeout=6)
    try:
        async def call(i: int):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                if what == "publishers" or (what == "mixed" and i % 2):
                    return (await isq.publishers(s, CF(), scope=None))["total"]
                return (await isq.catalog(s, CF(), scope=None, page_size=5))["total"]

        started = time.perf_counter()
        totals = await asyncio.gather(*(call(i) for i in range(n)))
        assert len(set(totals)) == 1 or what == "mixed"
        assert time.perf_counter() - started < 5.5, "no caller may wait for the pool timeout"
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_f5_the_planner_settings_survive_the_wait(fleet_db_fresh, pinned_clock):
    """Giving the connection back drops the transaction-local settings (jit off, custom plans): the request sets them again."""
    seen: list[str] = []
    real = isq._ensure_detail

    async def spy(db, table, scope, keys, **kw):
        seen.append((await db.execute(__import__("sqlalchemy").text("SHOW jit"))).scalar())
        return await real(db, table, scope, keys, **kw)

    isq._ensure_detail, saved = spy, isq._ensure_detail
    try:
        async with sessions(fleet_db_fresh) as sm:
            async with sm() as s:
                await isq.catalog(s, CF(), scope=None, page_size=3)
    finally:
        isq._ensure_detail = saved
    assert seen and set(seen) == {"off"}


@pytest.mark.asyncio
async def test_f5_a_caller_that_goes_away_does_not_cancel_the_shared_build(fleet_db_fresh, pinned_clock, monkeypatch):
    # The shared build is held at a gate, so the cancel deterministically lands while the caller waits for it (a cancel
    # that lands in the caller's own first statement is a platform-wide situation and made a fixed sleep flaky).
    gate = asyncio.Event()
    original = isq._load_table

    async def gated(db, scope):
        await gate.wait()
        return await original(db, scope)

    monkeypatch.setattr(isq, "_load_table", gated)
    async with sessions(fleet_db_fresh) as sm:
        async def first():
            async with sm() as s:
                return await isq.catalog(s, CF(), scope=None, page_size=1)

        task = asyncio.ensure_future(first())
        for _ in range(1000):
            if isq._inflight:
                break
            await asyncio.sleep(0.005)
        assert isq._inflight, "the shared build never started"
        await asyncio.sleep(0.1)                       # the caller has released its connection and waits on the build
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        builds = list(isq._inflight.values())
        assert builds and not any(b.cancelled() or b.done() for b in builds), "the caller's cancel must not touch the build"
        gate.set()
        for build in builds:
            await build                                # the build finishes on its own
        async with sm() as s:
            assert (await isq.catalog(s, CF(), scope=None, page_size=1))["total"] > 0


# ════════════════════════════════════════════════════════════════════
# F7: more than 10,000 products; nothing CPU-bound on the event loop
# ════════════════════════════════════════════════════════════════════

class LoopLag:
    """How long the event loop was away from its other tasks: a 2 ms ticker records its worst delay."""

    def __init__(self):
        self.worst = 0.0
        self._stop = False

    async def run(self):
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            self.worst = max(self.worst, now - last - 0.002)
            last = now

    def stop(self):
        self._stop = True


@pytest.mark.asyncio
async def test_f7_sorting_twelve_thousand_products_by_latest_version_is_exact_and_leaves_the_loop_free(fleet_db_fresh, pinned_clock):
    products, versions = 12_000, 5
    seed_products(fleet_db_fresh, products, versions)
    lag = LoopLag()
    async with sessions(fleet_db_fresh) as sm:
        ticker = asyncio.ensure_future(lag.run())
        started = time.perf_counter()
        async with sm() as s:
            top = await isq.catalog(s, CF(q="perf product", hidden="include"), scope=None, sort="latest_version", order="desc", page_size=100)
        async with sm() as s:
            counts = await isq.catalog(s, CF(q="perf product", hidden="include"), scope=None, sort="version_count", order="desc", page_size=100)
        elapsed = time.perf_counter() - started
        lag.stop()
        await ticker
    assert top["total"] == products
    perf = top["items"]
    assert len(perf) == 100 and all(r["product_key"].startswith("perf-product-") for r in perf)
    # the newest of every perf product is 1.4.<g % 5>: the order is by that version (descending), ties by key
    expected = lambda r: (1, 4, int(r["product_key"].rsplit("-", 1)[1]) % 5)           # noqa: E731
    assert [r["latest_version"] for r in perf] == sorted((r["latest_version"] for r in perf), key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    assert all(tuple(int(x) for x in r["latest_version"].split(".")) == expected(r) for r in perf)
    assert top["items"][0]["latest_version"] == "1.4.4"
    assert all(r["version_count"] == versions for r in counts["items"])
    assert elapsed < 20, elapsed
    assert lag.worst < 0.75, f"the event loop stalled for {lag.worst:.3f}s"
    print(f"F7: {products} products x {versions} versions, two sorts in {elapsed:.2f}s, worst loop stall {lag.worst * 1000:.0f} ms")


@pytest.mark.asyncio
async def test_f7_a_catalog_wider_than_the_ranking_cap_is_refused_with_a_hint(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(isq, "MAX_RANKED_PRODUCTS", 30)
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            with pytest.raises(iq.InvalidFilter, match="narrow the catalog"):
                await isq.catalog(s, CF(hidden="include"), scope=None, sort="latest_version")
        async with sm() as s:           # narrowed to fewer products than the cap: fine
            body = await isq.catalog(s, CF(q="chrome"), scope=None, sort="latest_version")
            assert body["total"] >= 1
        async with sm() as s:           # the version count needs no ranking, so no cap
            assert (await isq.catalog(s, CF(hidden="include"), scope=None, sort="version_count"))["total"] > 30


def test_f7_the_cap_is_a_422_through_the_route(fleet_db_fresh, pinned_clock, monkeypatch, viewer):
    monkeypatch.setattr(isq, "MAX_RANKED_PRODUCTS", 5)
    resp = viewer.get(f"{API}/software", params={"sort": "latest_version", "hidden": "include"})
    assert resp.status_code == 422 and "narrow the catalog" in resp.json()["detail"]
    assert viewer.get(f"{API}/software", params={"sort": "latest_version", "q": "git"}).status_code == 200


@pytest.mark.asyncio
async def test_f7_concurrent_identical_sorts_share_one_read(fleet_db_fresh, pinned_clock, monkeypatch):
    """The newest-version read had no single flight: N identical requests ran it N times."""
    calls = []
    real = isq._latest_many

    def counting(rows):
        calls.append(len(rows))
        time.sleep(0.05)
        return real(rows)

    monkeypatch.setattr(isq, "_latest_many", counting)
    async with sessions(fleet_db_fresh) as sm:
        async def one():
            async with sm() as s:
                return await isq.catalog(s, CF(hidden="include"), scope=None, sort="latest_version", page_size=5)
        await asyncio.gather(one(), one(), one(), one())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_f7_the_ranking_runs_in_the_worker_pool_not_on_the_loop(fleet_db_fresh, pinned_clock, monkeypatch):
    threads: set[str] = set()
    real = isq.rank_versions

    def spy(stats):
        threads.add(threading.current_thread().name)
        return real(stats)

    monkeypatch.setattr(isq, "rank_versions", spy)
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            await isq.catalog(s, CF(), scope=None, page_size=5)
        async with sm() as s:
            await isq.catalog(s, CF(hidden="include"), scope=None, sort="latest_version")
    assert threads and all(t.startswith("inv-catalog") for t in threads), threads


# ════════════════════════════════════════════════════════════════════
# F10: eviction and what a table keeps
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_f10_expired_tables_are_evicted(fleet_db_fresh, pinned_clock, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(isq, "_monotonic", lambda: clock["t"])
    async with sessions(fleet_db_fresh) as sm:
        async def look(scope):
            async with sm() as s:
                return await isq.catalog(s, CF(), scope=scope, page_size=1)
        await look(["prod"])
        await look(None)
        assert {k[0] for k in isq._tables} == {"scoped", "all"}
        clock["t"] += 61
        await look(["dev"])                                  # another scope is asked for: the minute-old restricted table goes
        assert [k for k in isq._tables if k[0] == "scoped"] == [("scoped", ("dev",))]
        assert ("all",) in isq._tables                       # the whole-estate table may still be served stale
        clock["t"] += 400
        assert isq.evict_expired() >= 2
        assert not isq._tables
    assert isq.evict_expired() == 0


@pytest.mark.asyncio
async def test_f10_the_export_leaves_no_version_lists_behind(fleet_db_fresh, pinned_clock):
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            plan = await export.plan_export(s, export.ExportParams(dataset="software", software=CF(hidden="include")), scope=None)
            bind = s.bind
        body = b"".join([c async for c in export.stream_csv(bind, plan)]).decode("utf-8-sig")
    assert len(body.strip().split("\r\n")) == plan.rows + 1 > 10
    kept = sum(len(t.versions) for t in isq._tables.values())
    assert kept <= 1, f"the export kept {kept} version lists"            # (the one-row count page of the plan)
    # while a page read for a person is kept, up to the cap
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            await isq.catalog(s, CF(hidden="include"), scope=None, sort="name", order="asc", page_size=20)
    assert 10 < max(len(t.versions) for t in isq._tables.values()) <= isq._DETAIL_CAP


@pytest.mark.asyncio
async def test_f10_a_table_keeps_at_most_the_cap_and_a_page_still_gets_all_its_rows(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(isq, "_DETAIL_CAP", 8)
    async with sessions(fleet_db_fresh) as sm:
        async with sm() as s:
            body = await isq.catalog(s, CF(hidden="include"), scope=None, sort="name", order="asc", page_size=20)
    assert len(body["items"]) == 20 and all(r["distribution"] for r in body["items"])
    assert max(len(t.versions) for t in isq._tables.values()) <= 8


@pytest.mark.asyncio
async def test_f10_the_publisher_views_a_table_keeps_are_bounded(fleet_db_fresh, pinned_clock, monkeypatch):
    monkeypatch.setattr(isq, "_PUB_SIGNATURES_MAX", 3)
    async with sessions(fleet_db_fresh) as sm:
        for q in ("a", "b", "c", "d", "e", "f"):
            async with sm() as s:
                await isq.publishers(s, CF(q=q), scope=None)
    assert max(len(t.pub_hosts) for t in isq._tables.values()) <= 3


# ════════════════════════════════════════════════════════════════════
# The catalog warmer
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_warmer_publishes_a_table_that_already_holds_the_default_views(fleet_db_fresh, pinned_clock, monkeypatch):
    async with sessions(fleet_db_fresh) as sm:
        assert await isq.warm_once(sm) is True
        table = isq._tables[("all",)]
        assert table.hosts and table.versions                # the default page's exact figures are there
        loads = []
        real = isq._load_table

        async def counting(db, scope):
            loads.append(scope)
            return await real(db, scope)

        monkeypatch.setattr(isq, "_load_table", counting)
        async with sm() as s:
            default = await isq.catalog(s, CF(), scope=None)
        async with sm() as s:
            pubs = await isq.publishers(s, CF(), scope=None)
        assert loads == [] and default["total"] > 0 and pubs["total"] > 0          # no request built or measured the table
        assert default["items"] == (await _cold_catalog(sm))["items"]               # and it says what a cold build says


async def _cold_catalog(sm):
    isq.clear_caches()
    async with sm() as s:
        return await isq.catalog(s, CF(), scope=None)


@pytest.mark.asyncio
async def test_warmer_leaves_a_young_table_alone_and_stops_for_an_idle_one(fleet_db_fresh, pinned_clock, monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(isq, "_monotonic", lambda: clock["t"])
    monkeypatch.setitem(isq._last_asked, "all", clock["t"])
    async with sessions(fleet_db_fresh) as sm:
        assert await isq.warm_once(sm) is True
        first = isq._tables[("all",)]
        clock["t"] += 10
        assert await isq.warm_once(sm) is False and isq._tables[("all",)] is first          # younger than WARM_REFRESH_AFTER_S
        clock["t"] += 25                                                                     # 35 s old
        assert await isq.warm_once(sm) is True and isq._tables[("all",)] is not first        # replaced before it can expire
        # nobody asks for the catalog for half an hour: the warmer rests, and the table lapses
        clock["t"] += isq.WARM_IDLE_S + 1
        assert await isq.warm_once(sm) is False and ("all",) not in isq._tables
        # a request wakes it again
        async with sm() as s:
            await isq.catalog(s, CF(), scope=None, page_size=1)
        assert isq._last_asked["all"] == clock["t"]
        clock["t"] += 35
        assert await isq.warm_once(sm) is True


@pytest.mark.asyncio
async def test_warmer_loop_survives_a_failed_pass_and_stops_when_cancelled(monkeypatch, caplog):
    calls = []

    async def flaky(factory):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database away")
        return True

    monkeypatch.setattr(isq, "warm_once", flaky)
    monkeypatch.setattr(isq, "WARM_JITTER_S", 0)
    task = asyncio.ensure_future(isq.catalog_warmer_loop(start_delay=0, interval=0.01, session_factory=lambda: None))
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3 and "software catalog warm-up failed" in caplog.text


@pytest.mark.asyncio
async def test_warmer_loop_does_nothing_when_switched_off(monkeypatch):
    monkeypatch.setenv(isq.WARM_ENV, "0")
    called = []

    async def never(factory):
        called.append(1)

    monkeypatch.setattr(isq, "warm_once", never)
    monkeypatch.setattr(isq, "WARM_JITTER_S", 0)
    await asyncio.wait_for(isq.catalog_warmer_loop(start_delay=0, interval=0.01, session_factory=lambda: None), 2)
    assert called == []


def test_the_warmer_is_started_with_the_other_inventory_loops():
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "catalog_warmer_loop" in main and re.search(r'"inventory_catalog_warmer"', main), "started at startup and cancelled at shutdown"
