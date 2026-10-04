"""NetFlow threat-intelligence IP reputation (WP B5, CR-10).

Feeds are prefix lists (URL feeds disabled until enabled, uploads for
air-gapped appliances); nothing ships built in. Parsing ignores over-broad and
special-use prefixes; matching is an IPv4/IPv6 interval index.

Tiers: pure unit tests; PostgreSQL feed storage/refresh
(``ZENPLUS_NETFLOW_TEST_PG``); the live API of this tree (``NETFLOW_TEST_API``
+ ``NETFLOW_TEST_TOKEN``).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import netflow_ti as ti  # noqa: E402


# ── Parsing ──────────────────────────────────────────────────────────────────

FEED = """\
# abuse.ch style comment
; spamhaus style comment
52.9.9.9
52.9.10.0/24 ; SBL123456
"203.0.114.0/23",botnet,2026-09-30
2a00:1450::/32  # a v6 prefix
::ffff:45.1.2.3
52.9.9.9
10.0.0.0/8
192.168.1.1
100.64.0.0/10
2001:db8::/32
0.0.0.0/0
12.0.0.0/7
2a00::/12
not-an-address
1.2.3.4-1.2.3.9
"""


def test_parse_feed_formats_and_safety():
    prefixes, stats = ti.parse_feed(FEED)
    assert prefixes == ["52.9.9.9/32", "52.9.10.0/24", "203.0.114.0/23", "2a00:1450::/32", "45.1.2.3/32"]
    assert stats.duplicates == 1
    assert stats.non_public == 4            # RFC 1918, CGNAT, documentation space never flag internal hosts
    assert stats.too_broad == 3             # 0/0, /7, v6 /12
    assert stats.invalid == 2 and stats.samples_invalid == ["not-an-address", "1.2.3.4-1.2.3.9"]
    assert stats.accepted == 5
    assert stats.summary().startswith("5 prefixes; 2 unreadable lines skipped; 4 private/special-use")
    one, s1 = ti.parse_feed("52.9.9.9\n")
    assert one == ["52.9.9.9/32"] and s1.summary() == "1 prefix"
    empty, s2 = ti.parse_feed("# nothing\n\n")
    assert empty == [] and s2.lines == 0


def test_parse_feed_caps_entries(monkeypatch):
    monkeypatch.setattr(ti, "MAX_ENTRIES", 3)
    prefixes, stats = ti.parse_feed("\n".join(f"52.9.{i}.1" for i in range(10)))
    assert len(prefixes) == 3 and stats.truncated and "truncated at 3" in stats.summary()


# ── Interval index ───────────────────────────────────────────────────────────

def _index():
    feeds = {"a": ti.FeedInfo("a", "C2 list", "botnet_c2"), "b": ti.FeedInfo("b", "Scanners", "scanner"),
             "c": ti.FeedInfo("c", "Tor", "tor_exit")}
    entries = [("a", "52.9.0.0/16"), ("b", "52.9.9.0/24"), ("b", "52.9.9.255/32"), ("c", "52.10.0.0/32"),
               ("a", "2a00:1450::/32"), ("c", "2a00:1450:4001::1/128"), ("zz", "99.0.0.0/8")]
    return ti.TiIndex(entries, feeds, version="v1")


def test_index_resolves_nested_prefixes_from_several_feeds():
    idx = _index()
    names = lambda ip: sorted(f.name for f in idx.lookup(ip))  # noqa: E731
    assert names("52.9.1.1") == ["C2 list"]
    assert names("52.9.9.7") == ["C2 list", "Scanners"]          # nested: both feeds
    assert names("52.9.9.255") == ["C2 list", "Scanners"]
    assert names("52.9.10.0") == ["C2 list"]                      # after the nested /24 closes
    assert names("52.10.0.0") == ["Tor"] and names("52.10.0.1") == []
    assert names("52.8.255.255") == [] and names("53.0.0.0") == []
    assert names("2a00:1450:4001::1") == ["C2 list", "Tor"]
    assert names("2a00:1450:4001::2") == ["C2 list"]
    assert names("2a00:1451::1") == []
    assert names("::ffff:52.9.9.7") == ["C2 list", "Scanners"]    # IPv4-mapped form
    assert names("99.1.1.1") == []                                 # entries of a feed that is not enabled
    assert names("bogus") == []
    assert idx.prefixes == 7 and not idx.empty
    assert ti.TiIndex([], {}).empty


def test_catalog_suggests_urls_without_shipping_data():
    for c in ti.CATALOG:
        assert c["url"].startswith("https://") and c["licence"] and c["category"] in ti.CATEGORIES
        assert set(c) == {"key", "name", "url", "category", "refresh_hours", "licence"}


# ── PostgreSQL storage and refresh ───────────────────────────────────────────

PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG (scratch DB with migrate-138)")


@needs_pg
def test_refresh_keeps_previous_entries_on_failure(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def ok_fetch(url):
        return "52.77.0.0/16\n52.78.1.1\n10.0.0.0/8\n"

    async def bad_fetch(url):
        raise ValueError("HTTP 503")

    async def junk_fetch(url):
        return "<html>blocked</html>\n"

    async def run():
        engine = create_async_engine(PG_DSN)
        name = f"b5-test-{uuid.uuid4().hex[:8]}"
        try:
            async with AsyncSession(engine) as db:
                fid = (await db.execute(text(
                    "INSERT INTO netflow_ti_feeds (name, source, url, category, enabled) "
                    "VALUES (:n, 'url', 'https://example.invalid/list.txt', 'malicious', TRUE) RETURNING id"),
                    {"n": name})).scalar()
                monkeypatch.setattr(ti, "fetch_url", ok_fetch)
                r1 = await ti.refresh_feed(db, fid)
                await db.commit()
                assert r1["ok"] and r1["entries"] == 2 and r1["stats"]["non_public"] == 1
                monkeypatch.setattr(ti, "fetch_url", bad_fetch)
                r2 = await ti.refresh_feed(db, fid)
                await db.commit()
                assert not r2["ok"] and r2["status"].startswith("Download failed (HTTP 503)")
                monkeypatch.setattr(ti, "fetch_url", junk_fetch)
                r3 = await ti.refresh_feed(db, fid)
                await db.commit()
                assert not r3["ok"] and r3["status"].startswith("No usable prefixes")
                row = (await db.execute(text("SELECT entry_count, last_status FROM netflow_ti_feeds WHERE id = :f"),
                                        {"f": fid})).first()
                n = (await db.execute(text("SELECT count(*) FROM netflow_ti_entries WHERE feed_id = :f"), {"f": fid})).scalar()
                assert row[0] == 2 and n == 2 and "previous entries kept" in row[1]
                # The index sees the enabled feed (rebuilt on change).
                ti.invalidate_index()
                ti._index_state.update(index=None, checked=0.0)
                idx = await ti.get_index(db)
                assert [f.name for f in idx.lookup("52.77.3.4")] == [name]
                await db.execute(text("UPDATE netflow_ti_feeds SET enabled = FALSE, updated_at = NOW() WHERE id = :f"), {"f": fid})
                await db.commit()
                ti.invalidate_index()
                ti._index_state.update(checked=0.0)
                assert (await ti.get_index(db)).lookup("52.77.3.4") == []
                await db.execute(text("DELETE FROM netflow_ti_feeds WHERE id = :f"), {"f": fid})
                await db.commit()
                assert (await db.execute(text("SELECT count(*) FROM netflow_ti_entries WHERE feed_id = :f"), {"f": fid})).scalar() == 0
        finally:
            ti._index_state.update(index=None, checked=0.0)
            await engine.dispose()

    asyncio.run(run())


# ── Live API ─────────────────────────────────────────────────────────────────

LIVE = os.environ.get("NETFLOW_TEST_API")
TOKEN = os.environ.get("NETFLOW_TEST_TOKEN")
live = pytest.mark.skipif(not (LIVE and TOKEN), reason="set NETFLOW_TEST_API and NETFLOW_TEST_TOKEN")
H = {"Authorization": f"Bearer {TOKEN}"}


def _url(path: str) -> str:
    return f"{LIVE}/api/v1/netflow{path}"


@live
def test_live_feed_lifecycle_upload_lookup_disable_delete():
    listing = requests.get(_url("/ti/feeds"), headers=H, timeout=30).json()
    assert listing["builtin"] is None and listing["catalog"] and "categories" in listing
    for f in listing["items"]:
        assert isinstance(f.get("active_findings"), int)
    name = f"b5 live {uuid.uuid4().hex[:6]}"
    r = requests.post(_url("/ti/feeds"), headers=H, timeout=30, json={
        "name": name, "source": "upload", "category": "scanner", "enabled": True,
        "content": "52.201.0.0/16\n2a03:9999::/32\n10.0.0.1\n"})
    assert r.status_code == 201, r.text
    feed = r.json()
    try:
        assert feed["entry_count"] == 2 and feed["enabled"] is True and feed["result"]["ok"]
        assert requests.post(_url("/ti/feeds"), headers=H, timeout=30, json={
            "name": name.upper(), "source": "upload", "content": "52.1.1.1"}).status_code == 409
        entries = requests.get(_url(f"/ti/feeds/{feed['id']}/entries"), headers=H, timeout=30).json()
        assert sorted(entries["entries"]) == ["2a03:9999::/32", "52.201.0.0/16"]
        look = requests.get(_url("/ti/lookup"), headers=H, timeout=30,
                            params={"ip": "52.201.3.4,2a03:9999::1,8.8.8.8"}).json()["results"]
        assert any(x["name"] == name for x in look["52.201.3.4"]) and any(x["name"] == name for x in look["2a03:9999::1"])
        assert not any(x["name"] == name for x in look["8.8.8.8"])
        assert requests.get(_url("/ti/lookup"), headers=H, params={"ip": "nope"}, timeout=30).status_code == 400
        # Replace the uploaded list; a URL refresh of an upload feed is refused.
        up = requests.post(_url(f"/ti/feeds/{feed['id']}/upload"), headers=H, timeout=30, json={"content": "52.202.0.1\n"})
        assert up.status_code == 200 and up.json()["entry_count"] == 1
        assert requests.post(_url(f"/ti/feeds/{feed['id']}/refresh"), headers=H, timeout=30).status_code == 400
        off = requests.patch(_url(f"/ti/feeds/{feed['id']}"), headers=H, timeout=30, json={"enabled": False})
        assert off.status_code == 200 and off.json()["enabled"] is False
        look2 = requests.get(_url("/ti/lookup"), headers=H, params={"ip": "52.202.0.1"}, timeout=30).json()["results"]
        assert not any(x["name"] == name for x in look2["52.202.0.1"])
        assert requests.patch(_url(f"/ti/feeds/{feed['id']}"), headers=H, timeout=30,
                              json={"category": "evil"}).status_code == 400
    finally:
        d = requests.delete(_url(f"/ti/feeds/{feed['id']}"), headers=H, timeout=30)
        assert d.status_code == 200


@live
def test_live_url_feed_is_not_downloaded_until_enabled():
    name = f"b5 url {uuid.uuid4().hex[:6]}"
    r = requests.post(_url("/ti/feeds"), headers=H, timeout=30, json={
        "name": name, "source": "url", "url": "https://example.invalid/never-fetched.txt", "category": "malicious"})
    assert r.status_code == 201, r.text
    feed = r.json()
    try:
        assert feed["enabled"] is False and feed["result"] is None and feed["entry_count"] == 0
        assert feed["last_refreshed_at"] is None
        bad = requests.post(_url("/ti/feeds"), headers=H, timeout=30, json={
            "name": name + "x", "source": "url", "url": "ftp://example.invalid/x"})
        assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)
    finally:
        requests.delete(_url(f"/ti/feeds/{feed['id']}"), headers=H, timeout=30)
