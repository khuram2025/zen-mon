"""GET /api/v1/inventory/trends and GET / PUT /api/v1/inventory/settings.

Trends read ``inventory_daily_snapshots`` (the fleet harness seeds 14 days up to the pinned date with one missing
day); settings are ``system_settings['inventory']``. The audit trail is read back from ``audit_logs``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from app.services import inventory_service as svc
from app.services import inventory_trends_settings as its
from inventory_fleet import (
    FIXED_NOW, SCOPED_TAG, SNAPSHOT_DAYS, SNAPSHOT_GAP_DAY, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template,
    pinned_clock, sessions,
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)

API = "/api/v1/inventory"
TODAY = FIXED_NOW.date()


def get(client, path: str, expect: int = 200, **params) -> Any:
    resp = client.get(API + path, params={k: v for k, v in params.items() if v is not None})
    assert resp.status_code == expect, (path, params, resp.status_code, resp.text[:400])
    return resp.json()


def put(client, body: Any, expect: int = 200) -> Any:
    resp = client.put(API + "/settings", json=body)
    assert resp.status_code == expect, (body, resp.status_code, resp.text[:400])
    return resp.json()


@pytest.fixture
def viewer(fleet_db):
    with api_client(fleet_db, role="t_tr_view", permissions=["inventory.view"]) as c:
        yield c


def audit_rows(db) -> list[dict[str, Any]]:
    return [{"action": r[0], "actor": r[1], "role": r[2], "type": r[3], "id": r[4], "metadata": r[5]} for r in db.query(
        "SELECT action, actor_username, actor_role, resource_type, resource_id, metadata FROM audit_logs ORDER BY created_at, id")]


# ════════════════════════════════════════════════════════════════════
# Trends
# ════════════════════════════════════════════════════════════════════

def test_trends_envelope_and_a_series(fleet_db, viewer, pinned_clock):
    body = get(viewer, "/trends", metric="assets")
    assert set(body) == {"generated_at", "restricted", "enabled", "metric", "dimension", "days", "window", "first_day",
                         "days_with_data", "dimensions_total", "truncated", "series"}
    assert body["restricted"] is False and body["enabled"] is True and body["metric"] == "assets" and body["days"] == 30
    assert body["window"] == {"since": (TODAY - timedelta(days=29)).isoformat(), "until": TODAY.isoformat()}
    assert body["first_day"] == (TODAY - timedelta(days=SNAPSHOT_DAYS - 1)).isoformat()
    assert [s["dimension"] for s in body["series"]] == ["device", "server"] and body["dimensions_total"] == 2   # highest latest value first
    oracle = {(d.isoformat(), dim): v for d, dim, v in fleet_db.query(
        "SELECT day, dimension, value FROM inventory_daily_snapshots WHERE metric = 'assets'")}
    for s in body["series"]:
        assert {(p["day"], s["dimension"]): p["value"] for p in s["points"]} == {k: v for k, v in oracle.items() if k[1] == s["dimension"]}
        assert [p["day"] for p in s["points"]] == sorted(p["day"] for p in s["points"])
    assert body["days_with_data"] == SNAPSHOT_DAYS - 1


def test_a_day_without_a_snapshot_is_a_gap_never_a_zero(viewer, pinned_clock):
    body = get(viewer, "/trends", metric="coverage", days=SNAPSHOT_DAYS)
    gap = (TODAY - timedelta(days=SNAPSHOT_GAP_DAY)).isoformat()
    for s in body["series"]:
        days = [p["day"] for p in s["points"]]
        assert gap not in days and len(days) == SNAPSHOT_DAYS - 1
        assert all(p["value"] >= 0 for p in s["points"])
    assert body["days_with_data"] == SNAPSHOT_DAYS - 1


@pytest.mark.parametrize("days,expected", [(1, 1), (3, 3), (4, 4), (5, 5), (6, 5), (7, 6), (30, 13), (400, 13)])
def test_the_window_is_the_last_n_local_days_today_included(viewer, pinned_clock, days, expected):
    body = get(viewer, "/trends", metric="assets", dimension="server", days=days)
    assert len(body["series"]) == 1 and len(body["series"][0]["points"]) == expected          # 10-03 is the gap, 5 days back
    assert body["window"]["until"] == TODAY.isoformat()
    assert body["window"]["since"] == (TODAY - timedelta(days=days - 1)).isoformat()
    assert body["series"][0]["points"][-1]["day"] == TODAY.isoformat()


def test_dimension_filter_is_exact_and_an_unknown_dimension_is_an_empty_series(viewer, pinned_clock):
    one = get(viewer, "/trends", metric="health", dimension="stale")
    assert one["dimension"] == "stale" and [s["dimension"] for s in one["series"]] == ["stale"]
    none = get(viewer, "/trends", metric="health", dimension="nope")
    assert none["series"] == [{"dimension": "nope", "points": []}] and none["days_with_data"] == 0
    assert get(viewer, "/trends", metric="health")["dimensions_total"] == 7
    odd = get(viewer, "/trends", metric="os_release", dimension="windows/Windows Server/2022")
    assert len(odd["series"][0]["points"]) == SNAPSHOT_DAYS - 1


@pytest.mark.parametrize("metric", svc.SNAPSHOT_METRICS)
def test_every_metric_family_the_loop_writes_can_be_read(viewer, pinned_clock, metric):
    body = get(viewer, "/trends", metric=metric)
    assert body["series"] and all(s["points"] for s in body["series"])


def test_trends_validation(viewer):
    assert "metric" in json.dumps(get(viewer, "/trends", expect=422, metric="bogus")["detail"])
    get(viewer, "/trends", expect=422)
    for days in (0, 401, -1):
        get(viewer, "/trends", expect=422, metric="assets", days=days)
    get(viewer, "/trends", expect=422, metric="assets", days="many")
    for value in ("\x00", "'; DROP TABLE servers; --", "%", "é" * 300):
        resp = viewer.get(f"{API}/trends", params={"metric": "assets", "dimension": value.replace("\x00", "")})
        assert resp.status_code in (200, 422)


def test_the_series_of_a_metric_is_capped_and_says_so(fleet_db_fresh, pinned_clock):
    rows = [(TODAY, "os_release", f"linux/Distro {n:03d}/1", 100 + n) for n in range(70)]
    for day, metric, dim, value in rows:
        fleet_db_fresh.execute("INSERT INTO inventory_daily_snapshots VALUES ($1, $2, $3, $4)", day, metric, dim, value)
    with api_client(fleet_db_fresh, role="t_tr_view", permissions=["inventory.view"]) as c:
        body = get(c, "/trends", metric="os_release")
        assert body["truncated"] is True and body["dimensions_total"] == 74 and len(body["series"]) == its.MAX_TREND_SERIES
        latest = [s["points"][-1]["value"] for s in body["series"]]
        assert latest == sorted(latest, reverse=True)
        assert get(c, "/trends", metric="os_release", dimension="linux/Distro 000/1")["truncated"] is False


def test_first_day_is_the_oldest_snapshot_even_outside_the_window(fleet_db_fresh, pinned_clock):
    fleet_db_fresh.execute("INSERT INTO inventory_daily_snapshots VALUES ($1, 'assets', 'server', 40)", TODAY - timedelta(days=200))
    with api_client(fleet_db_fresh, role="t_tr_view", permissions=["inventory.view"]) as c:
        body = get(c, "/trends", metric="assets", days=7)
        assert body["first_day"] == (TODAY - timedelta(days=200)).isoformat()
        assert all(p["day"] >= body["window"]["since"] for s in body["series"] for p in s["points"])
        assert get(c, "/trends", metric="assets", days=400)["days_with_data"] == SNAPSHOT_DAYS


def test_today_is_the_appliance_local_day(fleet_db_fresh, pinned_clock):
    """At 12:00 UTC it is already the next day in Kiritimati (UTC+14): the window ends there, and today has no snapshot yet."""
    fleet_db_fresh.execute(
        "INSERT INTO system_settings (key, value) VALUES ('company', '{\"timezone\": \"Pacific/Kiritimati\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    with api_client(fleet_db_fresh, role="t_tr_view", permissions=["inventory.view"]) as c:
        body = get(c, "/trends", metric="assets", dimension="server", days=2)
    assert body["window"]["until"] == (TODAY + timedelta(days=1)).isoformat()
    assert [p["day"] for p in body["series"][0]["points"]] == [TODAY.isoformat()]


def test_a_restricted_caller_gets_no_series_and_the_database_is_not_asked(fleet_db, pinned_clock, monkeypatch):
    queries: list[str] = []
    real = iq._run

    async def spy(db, sql, params):
        queries.append(sql)
        return await real(db, sql, params)

    monkeypatch.setattr(iq, "_run", spy)
    with api_client(fleet_db, role="t_tr_scoped", permissions=["inventory.view"], scope_tags=(SCOPED_TAG,)) as c:
        for metric in svc.SNAPSHOT_METRICS:
            body = get(c, "/trends", metric=metric)
            assert body["restricted"] is True and body["series"] == [] and body["days_with_data"] == 0 and body["first_day"] is None
            assert body["dimensions_total"] == 0
        assert get(c, "/trends", metric="assets", dimension="server")["series"] == []
    assert not [q for q in queries if "inventory_daily_snapshots" in q]


def test_role_level_scope_restricts_trends_too_and_system_admin_is_never_restricted(fleet_db, pinned_clock):
    with api_client(fleet_db, role="t_tr_team", permissions=["inventory.view"], role_scope_tags=(SCOPED_TAG,)) as c:
        assert get(c, "/trends", metric="assets")["restricted"] is True
    with api_client(fleet_db, role="t_tr_admin", permissions=["system.admin"], scope_tags=(SCOPED_TAG,)) as c:
        body = get(c, "/trends", metric="assets")
        assert body["restricted"] is False and len(body["series"]) == 2


def test_switching_trends_off_withholds_them_but_keeps_the_figures(fleet_db_fresh, pinned_clock):
    with api_client(fleet_db_fresh, role="t_tr_mgr", permissions=["inventory.view", "inventory.manage"]) as c:
        put(c, {"trends_enabled": False})
        body = get(c, "/trends", metric="assets")
        assert body["enabled"] is False and body["series"] == [] and body["restricted"] is False
        put(c, {"trends_enabled": True})
        assert get(c, "/trends", metric="assets")["series"]
    assert fleet_db_fresh.scalar("SELECT count(*) FROM inventory_daily_snapshots") > 0        # nothing was deleted


def test_the_endpoint_reads_what_the_snapshot_loop_really_writes(fleet_db_fresh, pinned_clock):
    """The metric and dimension names of the loop (``snapshot_day``) are the ones the endpoint serves: the figures it
    writes for today are the Overview's own (the freshness buckets use SQL ``now()``, so only the counts that do
    not depend on the clock are compared)."""
    import asyncio

    async def run():
        async with sessions(fleet_db_fresh) as sm:
            async with sm() as s:
                await svc.snapshot_day(s, TODAY, commit=True)
    asyncio.run(run())
    with api_client(fleet_db_fresh, role="t_tr_view", permissions=["inventory.view"]) as c:
        summary = get(c, "/summary")
        latest = lambda metric: {s["dimension"]: s["points"][-1] for s in get(c, "/trends", metric=metric, days=1)["series"]}   # noqa: E731
        assets = latest("assets")
        assert {d: p["value"] for d, p in assets.items()} == {"server": summary["assets"]["server"], "device": summary["assets"]["device"]}
        assert {p["day"] for p in assets.values()} == {TODAY.isoformat()}
        sw = latest("software_products")
        assert (sw["products"]["value"], sw["installs"]["value"]) == (summary["software"]["products"], summary["software"]["installs"])
        assert set(latest("coverage")) == {"fresh", "aging", "stale", "never", "not_applicable"}
        assert set(latest("changes")) == {"software/added", "software/removed", "software/changed", "os/changed", "firmware/changed",
                                          "hardware/changed"}
        assert sum(p["value"] for d, p in latest("os_release").items()) == summary["assets"]["total"]


# ════════════════════════════════════════════════════════════════════
# Settings
# ════════════════════════════════════════════════════════════════════

def test_get_settings_shape_defaults_and_limits(viewer):
    body = get(viewer, "/settings")
    assert set(body) == {"settings", "defaults", "limits", "categories", "updated_at", "changed", "adjusted"}
    assert body["settings"] == body["defaults"] == svc.merge_inventory_settings(None)
    assert body["updated_at"] is None and body["changed"] == [] and body["adjusted"] == []
    assert body["limits"]["stale_days"] == {"min": 1, "max": 365} and body["limits"]["snapshot_hour"] == {"min": 0, "max": 23}
    assert body["limits"]["change_retention_days"] == {"min": 30, "max": 3650} and body["limits"]["fresh_hours"] == {"min": 1, "max": 720}
    assert body["categories"] == list(norm.CATEGORIES) and "update" in body["settings"]["hide_categories_by_default"]


def test_who_may_read_and_who_may_write_settings(fleet_db_fresh):
    cases = [("t_set_view", ["inventory.view"], 200, 403), ("t_set_export", ["inventory.view", "inventory.export"], 200, 403),
             ("t_set_manage", ["inventory.view", "inventory.manage"], 200, 200), ("t_set_none", [], 403, 403),
             ("t_set_other", ["servers.view", "devices.manage", "settings.manage"], 403, 403)]
    for role, perms, read, write in cases:
        with api_client(fleet_db_fresh, role=role, permissions=perms) as c:
            assert c.get(API + "/settings").status_code == read, role
            assert c.put(API + "/settings", json={"stale_days": 7}).status_code == write, role
    with api_client(fleet_db_fresh, role="t_set_admin", permissions=["system.admin"]) as c:
        assert c.put(API + "/settings", json={"stale_days": 7}).status_code == 200


def test_unknown_keys_are_rejected_and_named(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        body = put(c, {"stale_days": 9, "zebra": 1, "apple": {"x": 1}}, expect=422)
        assert body["detail"] == "unknown settings: apple, zebra"
        assert put(c, {"nope": True}, expect=422)["detail"] == "unknown setting: nope"
        assert "unknown fresh_hours key: fleet" in put(c, {"fresh_hours": {"fleet": 3}}, expect=422)["detail"]
    assert fleet_db_fresh.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory'") == 0     # nothing was stored


@pytest.mark.parametrize("body,fragment", [
    ({"stale_days": "abc"}, "stale_days must be a number"), ({"stale_days": True}, "stale_days must be a number"),
    ({"stale_days": None}, "stale_days must be a number"), ({"snapshot_hour": [1]}, "snapshot_hour must be a number"),
    ({"change_retention_days": {}}, "change_retention_days must be a number"),
    ({"fresh_hours": 3}, "fresh_hours must be an object"), ({"fresh_hours": {"server": "x"}}, "fresh_hours.server must be a number"),
    ({"hide_categories_by_default": "update"}, "must be a list of category names"),
    ({"hide_categories_by_default": [1]}, "must be a list of category names"),
    ({"hide_categories_by_default": ["update", "shoes", "boots"]}, "unknown categories: boots, shoes"),
    ({"trends_enabled": "yes"}, "trends_enabled must be true or false"), ({"trends_enabled": 1}, "trends_enabled must be true or false"),
])
def test_wrong_types_are_422_with_a_reason(fleet_db_fresh, body, fragment):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        assert fragment in put(c, body, expect=422)["detail"]
    assert fleet_db_fresh.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory'") == 0


def test_a_body_that_is_not_an_object_is_422(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        for body in ([1, 2], "text", 7, None):
            assert c.put(API + "/settings", json=body).status_code == 422


def test_out_of_range_numbers_are_clamped_and_the_response_shows_what_was_stored(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        body = put(c, {"stale_days": 9999, "change_retention_days": 1, "snapshot_hour": 99, "fresh_hours": {"server": 5000, "device": 0}})
        s = body["settings"]
        assert s["stale_days"] == 365 and s["change_retention_days"] == 30 and s["snapshot_hour"] == 23
        assert s["fresh_hours"] == {"server": 720, "device": 1}
        assert set(body["adjusted"]) == {"stale_days", "change_retention_days", "snapshot_hour", "fresh_hours.server", "fresh_hours.device"}
        assert set(body["changed"]) == {"stale_days", "change_retention_days", "snapshot_hour", "fresh_hours"}
        # (this manage-only role cannot read the page: the database is the witness below)
    stored = fleet_db_fresh.scalar("SELECT value FROM system_settings WHERE key = 'inventory'")
    assert stored == s


def test_fresh_hours_stay_below_stale_days(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        s = put(c, {"stale_days": 1, "fresh_hours": {"server": 100}})["settings"]
    assert s["stale_days"] == 1 and s["fresh_hours"]["server"] == 24             # capped at stale_days * 24
    assert s["fresh_hours"]["device"] == 2


def test_a_partial_update_keeps_every_other_value(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_all", permissions=["inventory.view", "inventory.manage"]) as c:
        put(c, {"stale_days": 14, "fresh_hours": {"device": 6}, "hide_categories_by_default": ["update"]})
        s = put(c, {"snapshot_hour": 3})["settings"]
        assert s == {**svc.merge_inventory_settings(None), "stale_days": 14, "fresh_hours": {"server": 24, "device": 6},
                     "hide_categories_by_default": ["update"], "snapshot_hour": 3}
        assert get(c, "/settings")["settings"] == s
        s = put(c, {"fresh_hours": {"server": 48}})["settings"]
        assert s["fresh_hours"] == {"server": 48, "device": 6}                   # one kind at a time
        assert put(c, {"hide_categories_by_default": []})["settings"]["hide_categories_by_default"] == []     # hide nothing is a choice


def test_the_stored_blob_is_the_whole_merged_settings(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        put(c, {"trends_enabled": False})
    stored = fleet_db_fresh.scalar("SELECT value FROM system_settings WHERE key = 'inventory'")
    assert stored == {**svc.merge_inventory_settings(None), "trends_enabled": False}
    # and the loops and every other reader see it through the one validator
    assert fleet_db_fresh.scalar("SELECT updated_at IS NOT NULL FROM system_settings WHERE key = 'inventory'") is True


def products_per_category(db) -> dict[str, int]:
    """The oracle: distinct normalised products of the seeded fleet per category (a product's category is the row maximum)."""
    return {r[0]: int(r[1]) for r in db.query(
        "SELECT category, count(*) FROM (SELECT name_key, max(category) AS category FROM server_software_inventory "
        "WHERE name_key IS NOT NULL GROUP BY name_key) p GROUP BY category")}


def test_saving_drops_the_summary_cache_at_once(fleet_db_fresh, pinned_clock):
    per_category = products_per_category(fleet_db_fresh)
    hidden_by_default = sum(per_category.get(c, 0) for c in svc.merge_inventory_settings(None)["hide_categories_by_default"])
    assert hidden_by_default > 0                                                     # the fleet has products in hidden categories
    with api_client(fleet_db_fresh, role="t_set_all", permissions=["inventory.view", "inventory.manage"]) as c:
        before = get(c, "/summary")
        assert get(c, "/summary") == before                                          # cached
        put(c, {"hide_categories_by_default": []})
        after = get(c, "/summary")
        assert after["software"]["products"] == sum(per_category.values())
        assert before["software"]["products"] == sum(per_category.values()) - hidden_by_default
        assert after["software"]["hidden_categories"] == []
        put(c, {"fresh_hours": {"server": 1}})
        assert get(c, "/summary")["coverage"]["fresh_hours"]["server"] == 1


def test_the_catalog_footer_follows_the_hidden_categories_setting(fleet_db_fresh, pinned_clock):
    per_category = products_per_category(fleet_db_fresh)
    default_hidden = svc.merge_inventory_settings(None)["hide_categories_by_default"]
    with api_client(fleet_db_fresh, role="t_set_all", permissions=["inventory.view", "inventory.manage"]) as c:
        first = get(c, "/software", page_size=1)
        assert first["hidden_categories"] == sorted(default_hidden)
        assert first["hidden_products"] == sum(per_category.get(c, 0) for c in default_hidden) > 0
        put(c, {"hide_categories_by_default": ["update"]})
        body = get(c, "/software", page_size=1)
        assert body["hidden_categories"] == ["update"] and body["hidden_products"] == per_category.get("update", 0)
        assert body["total"] == sum(per_category.values()) - per_category.get("update", 0)


def test_every_change_is_audited_with_the_changed_keys_only(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"], username="settings.admin",
                    persist_user=True) as c:
        put(c, {"stale_days": 14, "snapshot_hour": 0, "trends_enabled": True})
        put(c, {"hide_categories_by_default": ["update", "driver"], "fresh_hours": {"device": 4}})
    rows = [r for r in audit_rows(fleet_db_fresh) if r["action"] == "inventory.settings.update"]
    assert len(rows) == 2
    first, second = rows
    assert first["metadata"] == {"changed": {"stale_days": {"from": 7, "to": 14}}}            # snapshot_hour and trends_enabled did not change
    assert second["metadata"] == {"changed": {
        "hide_categories_by_default": {"from": svc.merge_inventory_settings(None)["hide_categories_by_default"],
                                       "to": ["update", "driver"]},
        "fresh_hours": {"from": {"server": 24, "device": 2}, "to": {"server": 24, "device": 4}}}}
    assert (first["actor"], first["role"], first["type"], first["id"]) == ("settings.admin", "t_set_manage", "inventory_settings", "inventory")
    assert fleet_db_fresh.scalar("SELECT updated_by IS NOT NULL FROM system_settings WHERE key = 'inventory'") is True


def test_saving_the_same_values_changes_nothing_and_audits_nothing(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"], persist_user=True) as c:
        body = put(c, {"stale_days": 7, "fresh_hours": {"server": 24}})
        assert body["changed"] == [] and body["updated_at"] is None
        assert fleet_db_fresh.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory'") == 0
        put(c, {"stale_days": 10})
        stamp = fleet_db_fresh.scalar("SELECT updated_at FROM system_settings WHERE key = 'inventory'")
        again = put(c, {"stale_days": 10})
        assert again["changed"] == []
        assert fleet_db_fresh.scalar("SELECT updated_at FROM system_settings WHERE key = 'inventory'") == stamp
    assert len([r for r in audit_rows(fleet_db_fresh) if r["action"] == "inventory.settings.update"]) == 1


def test_a_rejected_request_is_not_audited(fleet_db_fresh):
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"], persist_user=True) as c:
        put(c, {"bogus": 1}, expect=422)
    with api_client(fleet_db_fresh, role="t_set_view", permissions=["inventory.view"], persist_user=True) as c:
        assert c.put(API + "/settings", json={"stale_days": 3}).status_code == 403
    assert [r for r in audit_rows(fleet_db_fresh) if r["action"] == "inventory.settings.update"] == []
    assert fleet_db_fresh.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory'") == 0


def test_a_user_who_is_not_in_the_users_table_can_still_save(fleet_db_fresh):
    """updated_by references users(id); the write must not fail for an actor the table does not know (the audit is best-effort)."""
    with api_client(fleet_db_fresh, role="t_set_manage", permissions=["inventory.manage"]) as c:
        assert put(c, {"stale_days": 9})["settings"]["stale_days"] == 9
    assert fleet_db_fresh.scalar("SELECT updated_by FROM system_settings WHERE key = 'inventory'") is None


def test_a_scoped_manager_changes_the_same_global_settings(fleet_db_fresh):
    """Settings are module configuration, not tagged data: a role-scoped manager saves them like anyone."""
    with api_client(fleet_db_fresh, role="t_set_scoped", permissions=["inventory.view", "inventory.manage"],
                    scope_tags=(SCOPED_TAG,)) as c:
        assert put(c, {"stale_days": 11})["settings"]["stale_days"] == 11
        assert get(c, "/settings")["settings"]["stale_days"] == 11
