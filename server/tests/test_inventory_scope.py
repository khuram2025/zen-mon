"""Tag scope on every Inventory endpoint (doc 04 tests T10 to T12; principle 3: scope fails closed).

A scoped user sees only the assets whose tags share a name with their scope (their own ``scope_tags`` plus
their role's), in every list, every count and every aggregate; an out-of-scope asset is a 404 that cannot be
told apart from an unknown one; untagged assets are invisible to a scoped user; ``system.admin`` is never
scoped. The oracle for "what is visible" is plain SQL over the seeded fleet, independent of the code under
test. The software catalog, its product pages and installation lists, the export and the trend endpoint are covered
at the end of the file (the detailed behaviour is in test_inventory_software / _export / _trends_settings).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import inventory_query as iq
from app.services import inventory_service
from inventory_fleet import (
    EDGE_DEVICES, EDGE_SERVERS, LINKED_SERVERS, SCOPED_TAG, UNTAGGED_DEVICES, UNTAGGED_SERVERS, aid, api_client,
    fleet_db, fleet_db_fresh, fleet_info, fleet_template, pinned_clock,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"


def scoped(db, tags=(SCOPED_TAG,), **kw):
    return api_client(db, role="t_scope_view", permissions=["inventory.view"], scope_tags=tuple(tags), **kw)


def full(db):
    return api_client(db, role="t_scope_view", permissions=["inventory.view"])


def get(c, path: str, expect: int = 200, **params) -> Any:
    resp = c.get(API + path, params=params)
    assert resp.status_code == expect, (path, params, resp.status_code, resp.text[:300])
    return resp.json()


def visible_assets(db, tags=(SCOPED_TAG,)) -> dict[str, set[str]]:
    """What the scope can see, by SQL: servers carrying a scope tag, devices that are nobody's SNMP twin and carry one."""
    wanted = [t.lower() for t in tags]
    has_tag = ("EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE({c}, '[]'::jsonb)) t WHERE lower(t) = ANY($1::text[]))")
    servers = {r[0] for r in db.query(f"SELECT s.display_name FROM servers s WHERE {has_tag.format(c='s.tags')}", wanted)}
    devices = {r[0] for r in db.query(
        "SELECT d.hostname FROM devices d WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id) AND "
        + has_tag.format(c="d.tags"), wanted)}
    twins = {r[0] for r in db.query(
        f"SELECT d.hostname FROM devices d JOIN servers s ON s.device_id = d.id WHERE {has_tag.format(c='s.tags')}", wanted)}
    return {"servers": servers, "devices": devices, "twins": twins}


@pytest.fixture
def vis(fleet_db):
    return visible_assets(fleet_db)


def test_the_fixture_has_something_to_hide(fleet_db, vis):
    assert vis["servers"] == set(EDGE_SERVERS) and vis["devices"] == set(EDGE_DEVICES)
    assert vis["twins"] == {f"twin-{n}" for n in EDGE_SERVERS if n in LINKED_SERVERS} == {"twin-srv-w19-03"}
    total = fleet_db.scalar("SELECT (SELECT count(*) FROM servers) + (SELECT count(*) FROM devices s WHERE NOT EXISTS "
                            "(SELECT 1 FROM servers x WHERE x.device_id = s.id))")
    assert 0 < len(vis["servers"]) + len(vis["devices"]) < total / 4


# ── T10: every endpoint sees only in-scope assets ────────────────────

def test_assets_lists_exactly_the_visible_assets(fleet_db, vis, pinned_clock):
    with scoped(fleet_db) as c:
        body = get(c, "/assets", page_size=500)
    assert {r["name"] for r in body["items"]} == vis["servers"] | vis["devices"]
    assert body["total"] == len(vis["servers"]) + len(vis["devices"])
    assert all(SCOPED_TAG in [t.lower() for t in r["tags"]] for r in body["items"])


def test_untagged_assets_are_invisible_to_a_scoped_user(fleet_db, pinned_clock):
    with scoped(fleet_db) as c:
        names = {r["name"] for r in get(c, "/assets", page_size=500)["items"]}
        assert not names & set(UNTAGGED_SERVERS) and not names & set(UNTAGGED_DEVICES)
        for n in UNTAGGED_SERVERS[:2]:
            assert get(c, "/assets", q=n)["total"] == 0


def test_filters_cannot_widen_a_scope(fleet_db, vis, pinned_clock):
    inside = vis["servers"] | vis["devices"]
    with scoped(fleet_db) as c:
        for params in ({"tag": ["prod"]}, {"kind": "server"}, {"os_family": "windows"}, {"vendor": "Cisco"},
                       {"product": "google-chrome"}, {"freshness": "stale_or_never"}, {"health": "missing_serial"},
                       {"q": "srv-"}, {"site": "HQ"}, {"security_agent": "missing"}, {"module_class": "chassis"}):
            names = {r["name"] for r in get(c, "/assets", page_size=500, **params)["items"]}
            assert names <= inside, (params, sorted(names - inside))
        # asking for the other team's tag by name finds nothing, and so does naming one of their machines
        assert get(c, "/assets", tag=["team-a"])["total"] == 0
        assert get(c, "/assets", q="srv-w22-01")["total"] == 0


def test_facets_describe_the_visible_assets_only(fleet_db, vis, pinned_clock):
    rows = fleet_db.query(
        "SELECT s.tags FROM servers s WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(s.tags) t WHERE lower(t) = $1)", SCOPED_TAG)
    visible_tags = {t.lower() for (tags,) in rows for t in tags}
    with scoped(fleet_db) as c:
        f = get(c, "/assets/facets")
        listing = get(c, "/assets", page_size=500)["items"]
    assert f["total"] == len(listing)
    assert sum(i["count"] for i in f["facets"]["kind"]["items"]) == f["total"]
    assert sum(i["count"] for i in f["facets"]["freshness"]["items"]) == f["total"]
    for name, field in (("vendor", "vendor"), ("model", "model"), ("os_family", "os_family"), ("site", "site")):
        shown = {i["value"] for i in f["facets"][name]["items"]}
        assert shown <= {r[field] for r in listing}, name
    tag_values = {i["value"] for i in f["facets"]["tag"]["items"]}
    every_tag_in_scope = {t.lower() for r in listing for t in r["tags"]}
    assert tag_values == every_tag_in_scope and SCOPED_TAG in tag_values
    assert "team-a" not in tag_values and "team-b" not in tag_values and visible_tags <= tag_values


def test_summary_counts_visible_assets_only(fleet_db, vis, pinned_clock):
    n_srv, n_dev = len(vis["servers"]), len(vis["devices"])
    with scoped(fleet_db) as c:
        s = get(c, "/summary")
    assert s["scope"] == {"restricted": True, "tags": [SCOPED_TAG]}
    assert s["assets"] == {"total": n_srv + n_dev, "server": n_srv, "device": n_dev, "linked_devices": 1}
    cov = s["coverage"]
    assert cov["fresh"] + cov["aging"] + cov["stale"] + cov["never"] + cov["not_applicable"] == n_srv + n_dev
    edge = list(EDGE_SERVERS)
    hidden = list(inventory_service.DEFAULT_SETTINGS["hide_categories_by_default"])
    sw_rows = fleet_db.scalar(
        "SELECT count(*) FROM server_software_inventory w JOIN servers s ON s.id = w.server_id WHERE s.display_name = ANY($1::text[]) "
        "AND NOT (w.category = ANY($2::text[]))", edge, hidden)
    assert s["software"]["installs"] == sw_rows
    assert s["software"]["products"] == fleet_db.scalar(
        "SELECT count(DISTINCT w.name_key) FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
        "WHERE s.display_name = ANY($1::text[]) AND NOT (w.category = ANY($2::text[]))", edge, hidden)
    for row in s["software"]["top"] + s["software"]["sprawl"]:
        assert row["hosts"] <= n_srv
    assert sum(r["count"] for r in s["os"]["top"]) + s["os"]["unknown"] <= n_srv + n_dev
    assert s["hardware"]["devices"] == n_dev
    assert s["software"]["security_agent_coverage"]["total"] <= n_srv
    # the 7-day change counts are the visible assets' events only
    expected = fleet_db.scalar(
        """SELECT count(*) FROM inventory_change_events e WHERE e.observed_at >= $1::timestamptz - interval '7 days' AND (
              (e.asset_kind = 'server' AND e.asset_name = ANY($2::text[])) OR (e.asset_kind = 'device' AND e.asset_name = ANY($3::text[])))""",
        pinned_clock, edge, sorted(vis["devices"] | vis["twins"]))
    assert s["changes_7d"]["total"] == expected and expected > 0


def test_a_scoped_summary_never_equals_the_fleet_summary(fleet_db, pinned_clock):
    with full(fleet_db) as c:
        whole = get(c, "/summary")
    with scoped(fleet_db) as c:
        mine = get(c, "/summary")
    assert mine["assets"]["total"] < whole["assets"]["total"]
    assert mine["health"] != whole["health"] and mine["software"]["installs"] < whole["software"]["installs"]


def test_the_summary_cache_never_serves_one_scope_to_another(fleet_db, pinned_clock):
    iq.clear_caches()
    with full(fleet_db) as c:
        whole = get(c, "/summary")
    with scoped(fleet_db) as c:
        mine = get(c, "/summary")
        again = get(c, "/summary")
    with scoped(fleet_db, tags=("team-a",)) as c:
        theirs = get(c, "/summary")
    assert whole["scope"]["restricted"] is False
    assert mine == again and mine["scope"]["tags"] == [SCOPED_TAG]
    assert theirs["scope"]["tags"] == ["team-a"] and theirs["assets"] != mine["assets"]
    with full(fleet_db) as c:
        assert get(c, "/summary")["assets"]["total"] == whole["assets"]["total"]


def test_os_and_hardware_cover_the_visible_assets_only(fleet_db, vis, pinned_clock):
    n = len(vis["servers"]) + len(vis["devices"])
    with scoped(fleet_db) as c:
        tree = get(c, "/os")
        hw = get(c, "/hardware", vendor="Cisco")
        servers_tree = get(c, "/os", kind="server")
    assert tree["scope"] == {"restricted": True, "tags": [SCOPED_TAG]} and tree["total"] == n
    assert servers_tree["total"] == len(vis["servers"])
    assert hw["scope"]["restricted"] is True
    assert hw["network"]["devices"] == len(vis["devices"])
    assert hw["servers"]["total"] == len(vis["servers"])
    assert sum(v["count"] for v in hw["network"]["vendors"]) == len(vis["devices"])
    mods = fleet_db.scalar(
        "SELECT count(*) FROM device_entities e JOIN devices d ON d.id = e.device_id WHERE d.hostname = ANY($1::text[]) "
        "AND e.class IN ('chassis','backplane','module','powerSupply','fan','cpu','stack')", sorted(vis["devices"]))
    assert sum(g["count"] for g in hw["modules"]) == mods
    for cell_model in hw["matrix"]["models"]:
        assert cell_model["count"] <= len(vis["devices"])


def test_changes_are_asset_driven_and_cover_the_visible_assets_only(fleet_db, vis, pinned_clock):
    inside_servers, inside_devices = vis["servers"], vis["devices"] | vis["twins"]
    with scoped(fleet_db) as c:
        body = get(c, "/changes", page_size=500, since="2026-01-01T00:00:00Z")
        stats = get(c, "/changes", since="2026-01-01T00:00:00Z")["stats"]
    assert body["total"] == len(body["items"]) > 0
    for e in body["items"]:
        names = inside_servers if e["asset_kind"] == "server" else inside_devices
        assert e["asset_name"] in names, e
    expected = fleet_db.scalar(
        """SELECT count(*) FROM inventory_change_events e WHERE e.observed_at >= '2026-01-01' AND (
              (e.asset_kind = 'server' AND e.asset_name = ANY($1::text[])) OR (e.asset_kind = 'device' AND e.asset_name = ANY($2::text[])))""",
        sorted(inside_servers), sorted(inside_devices))
    assert body["total"] == expected and stats["total"] == expected
    assert sum(d["total"] for d in body["series"]) == expected
    # events of a machine that no longer exists carry no tags, so a scoped user never sees them
    assert fleet_db.scalar("SELECT count(*) FROM inventory_change_events WHERE asset_name = 'srv-retired-01'") == 1
    assert not [e for e in body["items"] if e["asset_name"] == "srv-retired-01"]
    with full(fleet_db) as c:
        assert [e for e in get(c, "/changes", page_size=500, since="2026-01-01T00:00:00Z")["items"] if e["asset_name"] == "srv-retired-01"]


def test_the_snmp_side_of_a_visible_server_follows_the_server_not_its_own_tags(fleet_db, vis, pinned_clock):
    """A twin's events show for the server's team (the machine is one asset); a twin tagged for the scope but whose
    server is not does not leak, because the server owns it."""
    with scoped(fleet_db) as c:
        names = {e["asset_name"] for e in get(c, "/changes", page_size=500, since="2026-01-01T00:00:00Z")["items"]}
    assert "twin-srv-w19-03" in names              # its server is edge
    assert "twin-srv-w22-09" not in names          # tagged edge itself, but its server is not
    assert fleet_db.scalar("SELECT count(*) FROM inventory_change_events WHERE asset_name = 'twin-srv-w22-09'") == 1


def test_the_changes_asset_filter_cannot_reach_an_out_of_scope_asset(fleet_db, fleet_info, pinned_clock):
    other = fleet_info.server_ids["srv-w22-01"]
    other_device = fleet_info.device_ids["fg-01"]
    with scoped(fleet_db) as c:
        for asset in (f"server:{other}", f"device:{other_device}"):
            body = get(c, "/changes", asset=asset, since="2026-01-01T00:00:00Z")
            assert body["items"] == [] and body["total"] == 0 and body["stats"]["total"] == 0
        mine = get(c, "/changes", asset=f"server:{fleet_info.server_ids[EDGE_SERVERS[0]]}", since="2026-01-01T00:00:00Z")
        assert mine["total"] > 0
    with full(fleet_db) as c:
        assert get(c, "/changes", asset=f"server:{other}", since="2026-01-01T00:00:00Z")["total"] > 0


# ── T11: an out-of-scope asset is a 404 that looks like any other ────

def test_out_of_scope_flyout_is_404_and_indistinguishable_from_unknown(fleet_db, fleet_info, pinned_clock):
    out_server, out_device = fleet_info.server_ids["srv-w22-01"], fleet_info.device_ids["fg-01"]
    in_server, in_device = fleet_info.server_ids[EDGE_SERVERS[0]], fleet_info.device_ids[EDGE_DEVICES[0]]
    with scoped(fleet_db) as c:
        unknown = c.get(f"{API}/assets/server/00000000-0000-0000-0000-00000000dead")
        assert unknown.status_code == 404
        for kind, ident in (("server", out_server), ("device", out_device)):
            resp = c.get(f"{API}/assets/{kind}/{ident}")
            assert resp.status_code == 404
            assert resp.json() == unknown.json()
            assert resp.headers.get("content-type") == unknown.headers.get("content-type")
        # an untagged asset and the tagged-for-scope twin of an out-of-scope server are equally absent
        for ident, kind in ((fleet_info.server_ids[UNTAGGED_SERVERS[0]], "server"),
                            (fleet_info.device_ids["twin-srv-w22-09"], "device")):
            assert c.get(f"{API}/assets/{kind}/{ident}").json() == unknown.json()
        assert get(c, f"/assets/server/{in_server}")["asset"]["name"] == EDGE_SERVERS[0]
        assert get(c, f"/assets/device/{in_device}")["asset"]["name"] == EDGE_DEVICES[0]


def test_a_visible_servers_flyout_never_lists_events_of_other_assets(fleet_db, fleet_info, pinned_clock):
    name = "srv-w19-03"
    with scoped(fleet_db) as c:
        body = get(c, f"/assets/server/{fleet_info.server_ids[name]}")
    assert {e["asset_name"] for e in body["changes"]["items"]} <= {name, f"twin-{name}"}


# ── Role scope, administrators, and failing closed ───────────────────

def test_a_role_level_scope_applies_to_every_member(fleet_db, vis, pinned_clock):
    with api_client(fleet_db, role="t_team_role", permissions=["inventory.view"], role_scope_tags=(SCOPED_TAG,)) as c:
        body = get(c, "/assets", page_size=500)
        assert {r["name"] for r in body["items"]} == vis["servers"] | vis["devices"]
        assert get(c, "/summary")["scope"] == {"restricted": True, "tags": [SCOPED_TAG]}


def test_user_and_role_scopes_are_united(fleet_db, vis, pinned_clock):
    other = visible_assets(fleet_db, ("team-a",))
    with api_client(fleet_db, role="t_team_role", permissions=["inventory.view"], role_scope_tags=(SCOPED_TAG,),
                    scope_tags=("team-a",)) as c:
        got = {r["name"] for r in get(c, "/assets", page_size=500)["items"]}
        assert get(c, "/summary")["scope"]["tags"] == sorted([SCOPED_TAG, "team-a"])
    assert got == vis["servers"] | vis["devices"] | other["servers"] | other["devices"]


def test_system_admin_is_never_scoped(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_admin", permissions=["system.admin"], scope_tags=(SCOPED_TAG,)) as c:
        assert get(c, "/summary")["scope"] == {"restricted": False, "tags": []}
        assert get(c, "/assets", page_size=1)["total"] == 145


def test_a_scope_that_matches_nothing_sees_nothing_and_nothing_fails(fleet_db, fleet_info, pinned_clock):
    with scoped(fleet_db, tags=("no-such-tag",)) as c:
        s = get(c, "/summary")
        assert s["assets"] == {"total": 0, "server": 0, "device": 0, "linked_devices": 0}
        assert s["coverage"]["fresh_pct"] is None and s["os"]["top"] == [] and s["software"]["installs"] == 0
        assert s["software"]["top"] == [] and s["hardware"]["top_models"] == []
        assert all(v == 0 for k, v in s["health"].items() if k != "not_collected_by_design")
        assert s["health"]["not_collected_by_design"] == {"count": 0}
        assert s["data_status"]["pending_software_rows"] == 0 and s["data_status"]["pending_identity_rows"] == 0
        assert get(c, "/assets")["items"] == [] and get(c, "/assets/facets")["total"] == 0
        assert get(c, "/os")["families"] == [] and get(c, "/hardware")["network"]["devices"] == 0
        assert get(c, "/changes", since="2026-01-01T00:00:00Z")["total"] == 0
        get(c, f"/assets/server/{fleet_info.server_ids['srv-w22-01']}", expect=404)


def test_tag_matching_ignores_case_on_both_sides(fleet_db_fresh, vis, pinned_clock):
    fleet_db_fresh.execute(
        "UPDATE servers SET tags = '[\"Prod\", \"TEAM-EDGE\"]'::jsonb WHERE display_name = $1", EDGE_SERVERS[0])
    with scoped(fleet_db_fresh, tags=("Team-Edge",)) as c:
        names = {r["name"] for r in get(c, "/assets", page_size=500)["items"]}
    assert EDGE_SERVERS[0] in names and names == vis["servers"] | vis["devices"]


def test_a_malformed_tag_column_hides_the_asset_instead_of_failing(fleet_db_fresh, vis, pinned_clock):
    """tags is jsonb; a scalar or an object where an array belongs must neither crash a list nor leak the row."""
    fleet_db_fresh.execute("UPDATE servers SET tags = '\"team-edge\"'::jsonb WHERE display_name = $1", EDGE_SERVERS[1])
    fleet_db_fresh.execute("UPDATE devices SET tags = '{\"team-edge\": true}'::jsonb WHERE hostname = $1", EDGE_DEVICES[1])
    with scoped(fleet_db_fresh) as c:
        names = {r["name"] for r in get(c, "/assets", page_size=500)["items"]}
        get(c, "/summary"), get(c, "/assets/facets"), get(c, "/os"), get(c, "/hardware")
        get(c, "/changes", since="2026-01-01T00:00:00Z")
    assert EDGE_SERVERS[1] not in names and EDGE_DEVICES[1] not in names
    with full(fleet_db_fresh) as c:                     # an unrestricted user still sees them, with an empty tag list
        rows = {r["name"]: r for r in get(c, "/assets", page_size=500)["items"]}
        assert rows[EDGE_SERVERS[1]]["tags"] == [] and rows[EDGE_DEVICES[1]]["tags"] == []


# ── T12: trends ──────────────────────────────────────────────────────

def test_restricted_users_are_told_so_for_the_widgets_that_must_not_show_fleet_wide_series(fleet_db, pinned_clock):
    """The Overview hides trend series when ``scope.restricted`` is true; the flag is on every aggregate response."""
    with scoped(fleet_db) as c:
        for path in ("/summary", "/os", "/hardware"):
            assert get(c, path)["scope"]["restricted"] is True, path
    with full(fleet_db) as c:
        for path in ("/summary", "/os", "/hardware"):
            assert get(c, path)["scope"] == {"restricted": False, "tags": []}, path


def test_the_trend_endpoint_gives_a_restricted_user_no_series(fleet_db, pinned_clock):
    """The snapshots are fleet-wide (no tags): a scoped caller gets 200 with restricted=true and nothing else, for
    every metric, and an unrestricted caller of the same role gets the series."""
    from app.services.inventory_service import SNAPSHOT_METRICS
    with scoped(fleet_db) as c:
        for metric in SNAPSHOT_METRICS:
            body = get(c, "/trends", metric=metric, days=400)
            assert body["restricted"] is True and body["series"] == [] and body["days_with_data"] == 0
        assert get(c, "/trends", metric="assets", dimension="server")["series"] == []
    with full(fleet_db) as c:
        assert get(c, "/trends", metric="assets")["series"]


# ── the software inventory ───────────────────────────────────────────

def _software_oracle(db, servers):
    return {r[0]: (r[1], r[2], r[3]) for r in db.query(
        "SELECT w.name_key, count(DISTINCT w.server_id), count(*), count(DISTINCT w.version) FILTER (WHERE btrim(w.version) <> '') "
        "FROM server_software_inventory w JOIN servers s ON s.id = w.server_id WHERE s.display_name = ANY($1::text[]) GROUP BY 1", sorted(servers))}


def test_the_catalog_counts_the_visible_servers_only(fleet_db, vis, pinned_clock):
    oracle = _software_oracle(fleet_db, vis["servers"])
    with scoped(fleet_db) as c:
        body = get(c, "/software", hidden="include", page_size=500)
    assert body["scope"] == {"restricted": True, "tags": [SCOPED_TAG]}
    assert {r["product_key"]: (r["hosts"], r["installs"], r["version_count"]) for r in body["items"]} == oracle
    assert 0 < len(oracle) < 31 and max(h for h, _i, _v in oracle.values()) <= len(vis["servers"])


def test_a_product_page_and_its_installations_hold_visible_servers_only(fleet_db, vis, pinned_clock):
    oracle = _software_oracle(fleet_db, vis["servers"])
    with scoped(fleet_db) as c:
        for key in sorted(oracle):
            page = get(c, f"/software/{key}")
            assert (page["header"]["hosts"], page["header"]["installs"], page["header"]["version_count"]) == oracle[key], key
            rows = get(c, f"/software/{key}/installations", page_size=500)["items"]
            assert {r["asset_name"] for r in rows} <= vis["servers"] and len(rows) == oracle[key][1]
            assert sum(e["installs"] for e in page["evidence"]["items"]) == oracle[key][1]


def test_a_product_nobody_in_scope_runs_is_a_404_like_an_unknown_one(fleet_db, vis, pinned_clock):
    oracle = _software_oracle(fleet_db, vis["servers"])
    outside = [r[0] for r in fleet_db.query("SELECT DISTINCT name_key FROM server_software_inventory") if r[0] not in oracle]
    assert outside
    with scoped(fleet_db) as c:
        unknown = c.get(f"{API}/software/no-such-product")
        assert unknown.status_code == 404
        for key in outside:
            for suffix in ("", "/installations"):
                resp = c.get(f"{API}/software/{key}{suffix}")
                assert (resp.status_code, resp.json()) == (404, unknown.json()), key
        assert get(c, "/software", q=outside[0].split("-")[0], hidden="include", page_size=500)["total"] == \
            sum(1 for k in oracle if outside[0].split("-")[0] in k)


def test_the_last_change_of_a_product_never_shows_other_teams_activity(fleet_db, vis, pinned_clock):
    """git's newest event is a retired server's (an asset whose tags are gone, so its visibility cannot be proven): an
    unrestricted caller sees it, a scoped one never does. Chrome's newest event is on a server outside the scope."""
    newest = lambda key, servers=None: fleet_db.scalar(          # noqa: E731
        "SELECT max(observed_at) FROM inventory_change_events WHERE subject_key = $1 AND category = 'software' "
        "AND ($2::text[] IS NULL OR asset_name = ANY($2::text[]))", key, servers)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None          # noqa: E731
    with full(fleet_db) as c:
        whole = {r["product_key"]: r["last_change"] for r in get(c, "/software", hidden="include", page_size=500)["items"]}
    with scoped(fleet_db) as c:
        mine = {r["product_key"]: r["last_change"] for r in get(c, "/software", hidden="include", page_size=500)["items"]}
        git_page = get(c, "/software/git") if "git" in mine else None
    edge = sorted(vis["servers"])
    assert whole["git"] == iso(newest("git")) == "2026-10-05T07:00:00Z"
    for key, got in mine.items():
        assert got == iso(newest(key, edge)), key
    assert mine["git"] != whole["git"]
    assert whole["google-chrome"] == "2026-10-08T09:00:00Z" and mine["google-chrome"] != whole["google-chrome"]
    if git_page is not None:
        assert git_page["header"]["last_change"] == mine["git"]


def test_the_software_export_is_scoped_like_the_lists(fleet_db, vis, pinned_clock):
    import csv, io
    with api_client(fleet_db, role="t_scope_export", permissions=["inventory.view", "inventory.export"], scope_tags=(SCOPED_TAG,)) as c:
        resp = c.get(f"{API}/export", params={"dataset": "software", "hidden": "include"})
        rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"), newline="")))
        listed = get(c, "/software", hidden="include", page_size=500, sort="name", order="asc")["items"]
        assert [r["product_key"] for r in rows] == [i["product_key"] for i in listed]
        assert [int(r["hosts"]) for r in rows] == [i["hosts"] for i in listed]
        resp = c.get(f"{API}/export", params={"dataset": "assets"})
        names = {r["name"] for r in csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"), newline=""))}
        assert names == vis["servers"] | vis["devices"]
