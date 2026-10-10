"""Regression tests of the Phase 1 review for the change feed (be-a fix round).

Each test names the finding it pins: BN-01 / F2 / FE-06 (scoped ``kind=device`` was an HTTP 500), BN-03 (a device linked by
two servers was counted twice), BN-13 (the pairing statistic and ``q``), F4 / BN-08 (extreme dates were an HTTP 500),
FE-03 (browser zone ids the host cannot open), FE-05 / BN-05 / FE-08 / UP-08 / DT-04 (``first_change_at``, ``history_start``
and ``retention_days``). The oracles are plain SQL over the seeded fleet, independent of the code under test.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest
from fastapi.testclient import TestClient

from app.services import inventory_export as export
from app.services import inventory_query as iq
from inventory_fleet import (
    CAPTURE_STARTED, FIXED_NOW, LINKED_SERVERS, SCOPED_TAG, aid, api_client, fleet_db, fleet_db_fresh,
    fleet_info, fleet_template, pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
UTC = timezone.utc
LONG_AGO = "2020-01-01T00:00:00Z"


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


def client(db, tags=(), role_tags=(), export_perm=False):
    perms = ["inventory.view"] + (["inventory.export"] if export_perm else [])
    return api_client(db, role="t_fix_chg", permissions=perms, scope_tags=tuple(tags), role_scope_tags=tuple(role_tags),
                      persist_user=export_perm)


def http(c) -> TestClient:
    """The same app, answering a server error with its 500 instead of raising it (what a browser would get)."""
    return TestClient(c.app, raise_server_exceptions=False)


def visible_device_events(db, tags, since=datetime(2020, 1, 1, tzinfo=UTC)) -> int:
    """Events of the devices a scope can see (devices that are nobody's twin and carry a tag, plus twins of visible
    servers), counting each device once."""
    wanted = [t.lower() for t in tags]
    has_tag = "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({c}, '[]'::jsonb)) t WHERE lower(t) = ANY($1::text[]))"
    return db.scalar(
        "SELECT count(*) FROM inventory_change_events e WHERE e.asset_kind = 'device' AND e.observed_at >= $2 "
        "AND e.asset_id IN (SELECT d.id FROM devices d WHERE NOT EXISTS (SELECT 1 FROM servers x WHERE x.device_id = d.id) AND "
        + has_tag.format(c="d.tags") + " UNION SELECT s.device_id FROM servers s WHERE s.device_id IS NOT NULL AND "
        + has_tag.format(c="s.tags") + ")", wanted, since)


def visible_events(db, tags, kind=None, since=datetime(2020, 1, 1, tzinfo=UTC)) -> int:
    wanted = [t.lower() for t in tags]
    has_tag = "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({c}, '[]'::jsonb)) t WHERE lower(t) = ANY($1::text[]))"
    servers = db.scalar(
        "SELECT count(*) FROM inventory_change_events e WHERE e.asset_kind = 'server' AND e.observed_at >= $2 AND e.asset_id IN "
        "(SELECT s.id FROM servers s WHERE " + has_tag.format(c="s.tags") + ")", wanted, since)
    devices = visible_device_events(db, tags, since)
    return {"server": servers, "device": devices, None: servers + devices}[kind]


# ── BN-01 / F2 / FE-06 ────────────────────────────────────────────────

SCOPES = [(SCOPED_TAG,), ("prod",), ("prod", "dev"), ("team-b",), ("no-such-tag",)]


@pytest.mark.parametrize("tags", SCOPES)
@pytest.mark.parametrize("via", ["own", "role"])
def test_bn01_scoped_changes_by_kind_over_http(fleet_db, pinned_clock, tags, via):
    """BN-01 / F2 / FE-06: a scoped caller asking for kind=device got HTTP 500 (the UNION named its columns after the
    unaliased device branch). Every kind answers 200 and equals an independent count; server + device = all."""
    own, role = (tags, ()) if via == "own" else ((), tags)
    with client(fleet_db, tags=own, role_tags=role) as c:
        h = http(c)
        totals = {}
        for kind in ("device", "server", None):
            params = {"since": LONG_AGO, "page_size": 200, **({"kind": kind} if kind else {})}
            r = h.get(f"{API}/changes", params=params)
            assert r.status_code == 200, (tags, kind, r.status_code, r.text[:200])
            body = r.json()
            totals[kind] = body["total"]
            assert body["total"] == visible_events(fleet_db, tags, kind), (tags, kind)
            assert len(body["items"]) == min(body["total"], 200) and len({i["id"] for i in body["items"]}) == len(body["items"])
            if kind:
                assert {i["asset_kind"] for i in body["items"]} <= {kind}
        assert totals["device"] + totals["server"] == totals[None]
        # the chips and the daily series of the device view add up to its total too
        r = h.get(f"{API}/changes", params={"since": LONG_AGO, "kind": "device", "page_size": 1}).json()
        assert r["stats"]["total"] == r["total"] == sum(p["total"] for p in r["series"]) or len(r["series"]) == iq.MAX_SERIES_DAYS


def test_bn01_the_scoped_device_view_is_not_vacuous(fleet_db, pinned_clock):
    assert visible_device_events(fleet_db, (SCOPED_TAG,)) > 0 and visible_device_events(fleet_db, ("prod",)) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [(SCOPED_TAG,), ("prod",), ("prod", "dev")])
async def test_bn01_the_export_of_scoped_device_changes(fleet_db, pinned_clock, tags):
    """The changes export reaches the same code through ``plan_export`` (the function the route calls) and ``stream_csv``."""
    params = export.ExportParams(dataset="changes", changes=iq.ChangeFilters(kind="device", since=datetime(2020, 1, 1, tzinfo=UTC)))
    async with session(fleet_db) as s:
        plan = await export.plan_export(s, params, scope=list(tags))
        body = b"".join([chunk async for chunk in export.stream_csv(s.bind, plan)]).decode("utf-8-sig")
    expected = visible_device_events(fleet_db, tags)
    assert plan.total == plan.rows == expected > 0
    lines = [ln for ln in body.split("\r\n") if ln]
    assert len(lines) == expected + 1                                     # the header and one line per event
    assert all("device:" in ln for ln in lines[1:])


def test_bn01_the_export_route_is_a_200_for_a_scoped_caller(fleet_db, pinned_clock):
    with client(fleet_db, tags=(SCOPED_TAG,), export_perm=True) as c:
        r = http(c).get(f"{API}/export", params={"dataset": "changes", "kind": "device", "since": LONG_AGO})
        assert r.status_code == 200, r.text[:200]
        assert r.headers["X-Export-Rows"] == str(visible_device_events(fleet_db, (SCOPED_TAG,)))


# ── BN-03 ─────────────────────────────────────────────────────────────

def _add_duplicate_server_for(db, server_name: str, name: str) -> None:
    db.execute(
        "INSERT INTO servers (display_name, hostname, os_type, collection_mode, status, tags, device_id) "
        "SELECT $2, $2, 'windows', 'snmp', 'healthy', s.tags, s.device_id FROM servers s WHERE s.display_name = $1",
        server_name, name)


@pytest.mark.asyncio
async def test_bn03_a_device_linked_by_two_servers_is_counted_once(fleet_db_fresh, fleet_info, pinned_clock):
    """BN-03: servers.device_id is not unique. Two server records linking one device made the scoped feed, its stats, the
    summary's changes_7d and the export list that device's events twice."""
    db = fleet_db_fresh
    name = LINKED_SERVERS[2]                      # an edge server whose SNMP twin has a firmware event
    before = {}
    for tags in ((SCOPED_TAG,), ("prod", "dev")):
        before[tags] = visible_events(db, tags)
    _add_duplicate_server_for(db, name, "dupe-" + name)
    async with session(db) as s:
        for tags in ((SCOPED_TAG,), ("prod", "dev")):
            res = await iq.changes(s, scope=list(tags), params=iq.ChangeFilters(since=datetime(2020, 1, 1, tzinfo=UTC)),
                                   page_size=500, tz="UTC")
            ids = [i["id"] for i in res["items"]]
            assert len(ids) == len(set(ids)), "an event appears once"
            assert res["total"] == len(ids) == res["stats"]["total"] == sum(p["total"] for p in res["series"])
            assert res["total"] == visible_events(db, tags) == before[tags] + 0
        # the summary's seven-day figure and the unrestricted feed agree with a scope that sees everything
        sm = await iq.summary(s, scope=[SCOPED_TAG], use_cache=False)
        feed = await iq.changes(s, scope=[SCOPED_TAG], params=iq.ChangeFilters(since=FIXED_NOW - timedelta(days=7)), tz="UTC")
        assert sm["changes_7d"]["total"] == feed["total"]
        # the export counts the device once as well
        params = export.ExportParams(dataset="changes", changes=iq.ChangeFilters(since=datetime(2020, 1, 1, tzinfo=UTC)))
        plan = await export.plan_export(s, params, scope=[SCOPED_TAG])
        assert plan.total == before[(SCOPED_TAG,)]


@pytest.mark.asyncio
async def test_bn03_the_visible_set_is_distinct(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    _add_duplicate_server_for(db, LINKED_SERVERS[2], "dupe-a")
    _add_duplicate_server_for(db, LINKED_SERVERS[2], "dupe-b")
    async with session(db) as s:
        visible = await iq._visible_assets(s, [SCOPED_TAG], iq.ChangeFilters(), None)
    assert len(visible) == len(set(visible))


# ── BN-13 ─────────────────────────────────────────────────────────────

def _insert_pair(db, server: str, old_title: str, new_title: str, key: str, old_v: str, new_v: str, at: datetime) -> None:
    sid = str(aid("server", server))
    for action, title, o, n in (("removed", old_title, old_v, None), ("added", new_title, None, new_v)):
        db.execute(
            "INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, asset_name, category, action, subject, "
            "subject_key, old_value, new_value, detail) VALUES ($1, 'server', $2, $3, 'software', $4, $5, $6, $7, $8, '{}'::jsonb)",
            at, sid, server, action, title, key, o, n)


@pytest.mark.asyncio
async def test_bn13_the_pair_statistic_follows_the_pairs_the_list_labels(fleet_db_fresh, pinned_clock):
    """BN-13: q matching one half of an upgrade pair hid it from stats.software.paired while the list still labelled the
    visible half by its partner. The statistic counts a pair when either half matches and reads both halves."""
    db = fleet_db_fresh
    at = FIXED_NOW - timedelta(hours=2, seconds=7)
    _insert_pair(db, "srv-w22-01", "QProd 1.0", "QProd 9.9.9", "qprod", "1.0", "9.9.9", at)
    async with session(db) as s:
        base = await iq.changes(s, scope=None, params=iq.ChangeFilters(subject_key="qprod"), tz="UTC")
        only_new = await iq.changes(s, scope=None, params=iq.ChangeFilters(subject_key="qprod", q="9.9.9"), tz="UTC")
        neither = await iq.changes(s, scope=None, params=iq.ChangeFilters(subject_key="qprod", q="no-such-text"), tz="UTC")
        scoped = await iq.changes(s, scope=["prod", "dev", "pci", "team-a", "team-b", SCOPED_TAG],
                                  params=iq.ChangeFilters(subject_key="qprod", q="9.9.9"), tz="UTC")
    assert base["stats"]["software"]["paired"] == {"upgraded": 1, "downgraded": 0}
    assert [i["pair"]["label"] for i in base["items"]] == ["upgrade", "upgrade"]
    assert [i["action"] for i in only_new["items"]] == ["added"]
    assert only_new["items"][0]["pair"]["label"] == "upgrade", "the list labels the visible half by its partner"
    assert only_new["stats"]["software"]["paired"] == {"upgraded": 1, "downgraded": 0}, "the statistic agrees with the label"
    assert neither["items"] == [] and neither["stats"]["software"]["paired"] == {"upgraded": 0, "downgraded": 0}
    assert scoped["stats"]["software"]["paired"] == {"upgraded": 1, "downgraded": 0}


def test_bn13_a_long_q_is_rejected_or_truncated_never_a_server_error(fleet_db, pinned_clock):
    assert len(iq.ChangeFilters(q="x" * 250).normalized().q) == iq.MAX_TEXT          # the service truncates
    assert len(iq.AssetFilters(q="x" * 250).normalized().q) == iq.MAX_TEXT
    with client(fleet_db) as c:
        h = http(c)
        for path in ("/changes", "/assets", "/assets/facets"):
            assert h.get(API + path, params={"q": "x" * 250}).status_code == 422          # the route refuses, as the contract says
            assert h.get(API + path, params={"q": "x" * 200}).status_code == 200


# ── F4 / BN-08 ────────────────────────────────────────────────────────

EXTREMES = [
    {"until": "9999-12-31T23:59:59Z"},
    {"until": "9999-12-31T23:59:59.999999Z", "tz": "Asia/Tokyo"},
    {"since": "2026-10-01T00:00:00Z", "until": "9999-12-31T23:59:59Z", "tz": "Asia/Tokyo"},
    {"since": "9999-12-31T23:59:59Z", "tz": "Asia/Tokyo"},
    {"since": "0001-01-01T00:00:00Z", "tz": "Pacific/Kiritimati"},
    {"since": "0001-01-01T00:00:00+14:00"},
    {"since": "1969-12-31T23:59:59Z"},
    {"until": "2100-01-02T00:00:00Z"},
    # BN-08 residual (round 2): pydantic raised a bare ValueError for year 0 from inside FastAPI's own validation, before the
    # route's range check, so every one of these was an HTTP 500 until the routes took the text and parsed it themselves.
    {"since": "0000-01-01T00:00:00Z"},
    {"since": "0000-01-01"},
    {"until": "0000-12-31T23:59:59Z"},
    {"since": "0000-01-01T00:00:00Z", "until": "0000-01-01T00:00:00Z"},
    {"since": "0000-06-15T12:00:00+14:00", "tz": "Pacific/Kiritimati"},
    {"since": "2026-10-01T00:00:00Z", "until": "0000-01-01T00:00:00Z"},
    {"since": "99999-01-01T00:00:00Z"},
    {"since": "2026-13-45T00:00:00Z"},
    {"since": "not a date"},
    {"until": "1e400"},
    {"since": "253402300800000"},
    {"since": "-62167219200"},
    {"since": "x" * 64},
]


@pytest.mark.parametrize("params", EXTREMES)
def test_f4_extreme_dates_are_422_not_500(fleet_db, pinned_clock, params):
    """F4 / BN-08: ``astimezone`` and the day loop overflowed at the ends of the calendar: HTTP 500 on /changes and on the
    changes export. They are now the existing InvalidFilter (422)."""
    with client(fleet_db, export_perm=True) as c:
        h = http(c)
        assert h.get(f"{API}/changes", params=params).status_code == 422, params
        assert h.get(f"{API}/export", params={"dataset": "changes", **{k: v for k, v in params.items() if k != "tz"}}).status_code == 422, params


def test_f4_the_edges_of_the_range_still_work(fleet_db, pinned_clock):
    with client(fleet_db) as c:
        h = http(c)
        for params in ({"since": "1970-01-01T00:00:00Z"}, {"since": "2000-01-01T00:00:00Z", "until": "2099-12-31T00:00:00Z"},
                       {"until": "2099-01-01T00:00:00Z", "tz": "Pacific/Kiritimati"}):
            r = h.get(f"{API}/changes", params={"page_size": 1, **params})
            assert r.status_code == 200, (params, r.text[:200])
            body = r.json()
            assert len(body["series"]) <= iq.MAX_SERIES_DAYS
            latest_local_day = (FIXED_NOW + timedelta(hours=14)).date().isoformat()        # Pacific/Kiritimati is UTC+14
            assert body["series"] == [] or body["series"][-1]["day"] <= latest_local_day, "the series ends at the present"


@pytest.mark.asyncio
async def test_f4_a_far_until_still_returns_the_events_and_a_series_ending_today(fleet_db, pinned_clock):
    async with session(fleet_db) as s:
        res = await iq.changes(s, scope=None, params=iq.ChangeFilters(since=FIXED_NOW - timedelta(days=7),
                                                                       until=datetime(2090, 1, 1, tzinfo=UTC)), tz="UTC")
        plain = await iq.changes(s, scope=None, params=iq.ChangeFilters(since=FIXED_NOW - timedelta(days=7)), tz="UTC")
    assert res["total"] == plain["total"] > 0 and res["series"] == plain["series"]
    assert sum(p["total"] for p in res["series"]) == res["total"]


# ── FE-03 ─────────────────────────────────────────────────────────────

LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Asia/Rangoon": "Asia/Yangon",
}


@pytest.fixture
def image_without_backward_links(monkeypatch):
    """A host whose tz database lacks the 'backward' links (Ubuntu 24.04 without tzdata-legacy): the legacy ids do not open."""
    real = iq.ZoneInfo

    def zone(name):
        if name in LEGACY_ZONES or name.startswith("Mars/"):
            raise ZoneInfoNotFoundError(name)
        return real(name)

    monkeypatch.setattr(iq, "ZoneInfo", zone)


@pytest.mark.parametrize("legacy,canonical", sorted(LEGACY_ZONES.items()))
def test_fe03_browser_zone_ids_the_host_cannot_open_still_answer(fleet_db, pinned_clock, image_without_backward_links, legacy, canonical):
    """FE-03: Chrome reports Asia/Calcutta, Asia/Katmandu, Europe/Kiev ... and the Changes page got a 422 for them. A known
    alias is read as the zone it links to (the user keeps local days) and nothing is reported as a fallback."""
    with client(fleet_db) as c:
        body = c.get(f"{API}/changes", params={"tz": legacy, "page_size": 1}).json()
    assert body["window"]["tz"] == canonical and body["window"]["tz_fallback"] is False


def test_fe03_an_unknown_but_well_formed_zone_falls_back_to_utc_and_says_so(fleet_db, pinned_clock, image_without_backward_links):
    with client(fleet_db) as c:
        r = c.get(f"{API}/changes", params={"tz": "Mars/Olympus", "page_size": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["window"]["tz"] == "UTC" and body["window"]["tz_fallback"] is True
        ok = c.get(f"{API}/changes", params={"tz": "Pacific/Auckland", "page_size": 1}).json()
        assert ok["window"]["tz"] == "Pacific/Auckland" and ok["window"]["tz_fallback"] is False
        for bad in ("not a zone!", "../etc/passwd", "", "A" * 65, "Europe//Kiev", "a b"):
            code = c.get(f"{API}/changes", params={"tz": bad}).status_code
            assert code == 422 or bad == "", (bad, code)


def test_fe03_the_fallback_series_is_cut_in_utc(fleet_db, pinned_clock, image_without_backward_links):
    with client(fleet_db) as c:
        utc = c.get(f"{API}/changes", params={"tz": "UTC", "page_size": 1}).json()
        fb = c.get(f"{API}/changes", params={"tz": "Mars/Olympus", "page_size": 1}).json()
    assert fb["series"] == utc["series"] and fb["total"] == utc["total"]


# ── FE-05 / BN-05 / FE-08 / UP-08 / DT-04 ─────────────────────────────

@pytest.mark.asyncio
async def test_fe05_view_all_with_first_change_at_lands_on_the_flyout_total(fleet_db, fleet_info, pinned_clock):
    """FE-05 / BN-05: the flyout counted every event but the link opened the Changes page with its seven-day default, so
    'View all 3' landed on 2. first_change_at is the since that makes the landed total equal the number."""
    checked = 0
    async with session(fleet_db) as s:
        for kind, ids in (("server", fleet_info.server_ids), ("device", fleet_info.device_ids)):
            for name, uid in ids.items():
                d = await iq.asset_detail(s, kind, uid, scope=None)
                if d is None:
                    continue                                   # a server's SNMP twin is not an asset of its own
                ch = d["changes"]
                if ch["total"] == 0:
                    assert ch["first_change_at"] is None
                    continue
                since = datetime.fromisoformat(ch["first_change_at"].replace("Z", "+00:00"))
                landed = await iq.changes(s, scope=None, params=iq.ChangeFilters(asset=f"{kind}:{uid}", since=since), tz="UTC")
                assert landed["total"] == ch["total"], (name, ch["total"], landed["total"])
                oldest = min(datetime.fromisoformat(i["observed_at"].replace("Z", "+00:00")) for i in landed["items"])
                assert since <= oldest
                checked += 1
    assert checked >= 40
    # the cases the review found: an event older than the seven-day default window
    async with session(fleet_db) as s:
        d = await iq.asset_detail(s, "server", fleet_info.server_ids["srv-w22-01"], scope=None)
        default_window = await iq.changes(s, scope=None, params=iq.ChangeFilters(asset=f"server:{fleet_info.server_ids['srv-w22-01']}"), tz="UTC")
    assert d["changes"]["total"] == 3 and default_window["total"] == 2


@pytest.mark.asyncio
async def test_fe08_history_start_is_the_capture_start_not_the_first_event(fleet_db, pinned_clock):
    """FE-08 / UP-08 / DT-04: the footnote named the first event's day (and probed only 790 days back). The response
    carries the install-wide capture start (migration 158) and the retention."""
    async with session(fleet_db) as s:
        res = await iq.changes(s, scope=None, params=iq.ChangeFilters(since=FIXED_NOW - timedelta(days=60)), tz="UTC")
        scoped = await iq.changes(s, scope=[SCOPED_TAG], tz="UTC")
        first_event = fleet_db.scalar("SELECT min(observed_at) FROM inventory_change_events")
    assert res["history_start"] == iq.iso(CAPTURE_STARTED) and res["retention_days"] == 400
    assert datetime.fromisoformat(res["history_start"].replace("Z", "+00:00")) < first_event, "capture began before the first event"
    assert scoped["history_start"] == res["history_start"], "install-wide, the same for every caller"


@pytest.mark.asyncio
async def test_fe08_retention_follows_the_setting_and_a_missing_capture_row_is_null(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    db.execute("INSERT INTO system_settings (key, value) VALUES ('inventory', '{\"change_retention_days\": 1100}'::jsonb) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    db.execute("DELETE FROM system_settings WHERE key = 'inventory.capture_started_at'")
    async with session(db) as s:
        res = await iq.changes(s, scope=None, tz="UTC")
    assert res["retention_days"] == 1100 and res["history_start"] is None


def test_fe05_the_http_response_carries_the_new_fields(fleet_db, fleet_info, pinned_clock):
    with client(fleet_db) as c:
        ch = c.get(f"{API}/changes", params={"page_size": 1}).json()
        d = c.get(f"{API}/assets/server/{fleet_info.server_ids['srv-w22-01']}").json()
    assert ch["history_start"] and ch["retention_days"] == 400 and ch["window"]["tz_fallback"] is False
    assert d["changes"]["first_change_at"] and d["changes"]["total"] == 3
