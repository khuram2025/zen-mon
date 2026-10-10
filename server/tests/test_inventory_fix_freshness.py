"""Regression tests of the Phase 1 review for freshness and the health predicates (X3: UP-03, DT-03, UP-04).

Some assets cannot report an inventory by design: a ping-only device (SNMP off), a child managed through a controller (its
identity arrives from the controller sync, which stamps nothing) and a server in a collection mode that has no inventory upload.
They stay in the asset list, are counted and are drillable (``freshness=not_applicable``), but they are not 'never reported', not
in the fresh percentage, not in 'stale or never', and they do not stand in the way of the all-clear. The four identity health
predicates skip them as well. The oracles read the raw tables.
"""

from __future__ import annotations

import contextlib

from app.services import inventory_query as iq
from inventory_fleet import (
    CONTROLLER_MANAGED_DEVICES, NOT_APPLICABLE_SERVERS, add_assets, api_client, fleet_db, fleet_db_fresh, fleet_info,
    fleet_template, pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)
from test_inventory_api import get

API = "/api/v1/inventory"


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


def view(db, tags=()):
    return api_client(db, role="t_fix_fresh", permissions=["inventory.view"], scope_tags=tuple(tags))


def names_of(c, **params) -> set[str]:
    return {r["name"] for r in get(c, "/assets", page_size=500, **params)["items"]}


def oracle_not_applicable(db) -> set[str]:
    """Assets that cannot report and never did, straight from the tables."""
    devices = db.query(
        "SELECT d.hostname FROM devices d WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id) "
        "AND d.identity_updated_at IS NULL AND (NOT COALESCE(d.snmp_enabled, false) OR d.managed_by_device_id IS NOT NULL "
        "OR d.poll_mode = 'via_controller')")
    servers = db.query("SELECT display_name FROM servers WHERE inventory_updated_at IS NULL AND collection_mode <> 'agent'")
    return {r[0] for r in devices} | {r[0] for r in servers}


def test_the_rule_matches_the_tables_on_the_fleet(fleet_db, pinned_clock):
    expected = oracle_not_applicable(fleet_db)
    # ping-only devices, the two controller-managed ones and the two non-agent servers that never reported
    assert {"edge-unk-02", "edge-unk-04", *CONTROLLER_MANAGED_DEVICES, *NOT_APPLICABLE_SERVERS} <= expected
    with view(fleet_db) as c:
        assert names_of(c, freshness="not_applicable") == expected
        s = get(c, "/summary")
    assert s["coverage"]["not_applicable"] == len(expected) == s["health"]["not_collected_by_design"]["count"]


def test_an_agent_server_that_never_reported_is_still_never_and_a_non_agent_server_with_a_stamp_is_computed(fleet_db, pinned_clock):
    with view(fleet_db) as c:
        never = names_of(c, freshness="never")
        stale_or_never = names_of(c, freshness="stale_or_never")
    assert "srv-new-01" in never                                  # mode agent, registered, nothing received yet
    assert not (never | stale_or_never) & oracle_not_applicable(fleet_db), "cannot-report assets are not 'never'"
    rows = {x[0]: {"mode": x[1], "at": x[2]} for x in fleet_db.query(
        "SELECT display_name, collection_mode, inventory_updated_at FROM servers")}
    ssh_with_stamp = next(n for n, r in rows.items() if r["mode"] == "ssh" and r["at"] is not None)
    with view(fleet_db) as c:
        assert get(c, "/assets", q=ssh_with_stamp)["items"][0]["freshness"] in ("fresh", "aging", "stale")


def test_fresh_percentage_is_over_the_assets_that_can_report(fleet_db, pinned_clock):
    with view(fleet_db) as c:
        s = get(c, "/summary")
    cov = s["coverage"]
    reporting = s["assets"]["total"] - cov["not_applicable"]
    assert cov["fresh"] + cov["aging"] + cov["stale"] + cov["never"] == reporting
    assert cov["fresh_pct"] == round(100.0 * cov["fresh"] / reporting, 1)
    assert cov["fresh_pct"] > round(100.0 * cov["fresh"] / s["assets"]["total"], 1), "the base shrank"


def test_the_freshness_facet_counts_and_lists_the_fourth_state(fleet_db, pinned_clock):
    with view(fleet_db) as c:
        facet = {i["value"]: i["count"] for i in get(c, "/assets/facets")["facets"]["freshness"]["items"]}
        assert list(facet) == ["fresh", "aging", "stale", "never", "not_applicable"]
        for value, count in facet.items():
            assert get(c, "/assets", page_size=1, freshness=value)["total"] == count
        assert facet["not_applicable"] == len(oracle_not_applicable(fleet_db))
        health = {i["value"]: i["count"] for i in get(c, "/assets/facets")["facets"]["health"]["items"]}
        for key, count in health.items():
            assert get(c, "/assets", page_size=1, health=key)["total"] == count, key


def test_the_identity_predicates_skip_assets_that_cannot_report(fleet_db, pinned_clock):
    """UP-04: ping-only devices have no vendor, model, serial or OS and never will; they were most of four worklists."""
    skip = oracle_not_applicable(fleet_db)
    with view(fleet_db) as c:
        for health in ("missing_os_version", "missing_model", "missing_serial", "unknown_vendor"):
            assert not names_of(c, health=health) & skip, health
        # the Assets list still carries them, with their gaps visible as values
        listed = {r["name"]: r for r in get(c, "/assets", freshness="not_applicable", page_size=500)["items"]}
        assert set(listed) == skip
        assert listed["edge-unk-02"]["vendor"] is None and listed["edge-unk-02"]["freshness"] == "not_applicable"
        assert get(c, "/assets", health="unknown_vendor", page_size=1)["total"] + sum(
            1 for n in skip if listed[n]["vendor"] is None and listed[n]["kind"] == "device") == get(
            c, "/assets", vendor="unknown", kind="device", page_size=1)["total"]


def test_vendor_and_model_unknown_select_the_gaps_whatever_their_freshness(fleet_db, pinned_clock):
    with view(fleet_db) as c:
        everything = get(c, "/assets", page_size=500)["items"]
        no_vendor = names_of(c, vendor="unknown")
        no_model = names_of(c, model="unknown")
        assert no_vendor == {r["name"] for r in everything if r["vendor"] is None}
        assert no_model == {r["name"] for r in everything if r["model"] is None}
        # the worklists are the same assets, minus those that cannot report; devices only (S3)
        assert names_of(c, health="unknown_vendor") == {r["name"] for r in everything if r["vendor"] is None
                                                       and r["kind"] == "device" and r["freshness"] != "not_applicable"}
        assert names_of(c, health="unknown_vendor") < no_vendor
        assert names_of(c, health="missing_model") < no_model


def test_the_all_clear_is_reachable_when_only_assets_that_cannot_report_lack_a_report(fleet_db_fresh, pinned_clock):
    """DT-03: 'Inventory is complete' could never appear on an estate with one ping-only device or controller child."""
    db = fleet_db_fresh
    db.execute("DELETE FROM servers")
    db.execute("DELETE FROM devices")
    add_assets(db,
               devices=[dict(name="sw-ok", vendor="Cisco", model="C9200", serial="S1", os_family="ios-xe", os_product="IOS XE",
                             os_release="17.9", os_version="17.9.4a"),
                        dict(name="ping-only", snmp_enabled=False, identity_age_hours=None),
                        dict(name="fg-parent", vendor="Fortinet", model="FG-100F", serial="S2", os_family="fortios",
                             os_product="FortiOS", os_release="7.2", os_version="7.2.8"),
                        dict(name="ap-child", managed_by="fg-parent", vendor="Fortinet", model="FAP-231F", serial="S3",
                             os_family="fortiap", os_product="FortiAP", os_release="7.2", os_version="7.2.4",
                             identity_age_hours=None)],
               servers=[dict(name="srv-agent", mode="agent", os_family="windows", os_product="Windows Server", os_release="2022",
                             os_version="10.0.20348.2700", baselined=True),
                        dict(name="srv-snmp-only", mode="snmp", updated_age_hours=None)])
    with view(db) as c:
        s = get(c, "/summary")
        assert s["assets"]["total"] == 6 and s["coverage"]["not_applicable"] == 3
        assert s["coverage"]["never"] == 0 and s["coverage"]["fresh_pct"] == 100.0
        assert s["health"]["never"] == 0 and s["health"]["stale"] == 0
        assert s["health"]["not_collected_by_design"] == {"count": 3}
        assert names_of(c, freshness="not_applicable") == {"ping-only", "ap-child", "srv-snmp-only"}
        assert names_of(c, freshness="stale_or_never") == set()
    # a device that can report and has not is still 'never'
    db.execute("UPDATE devices SET identity_updated_at = NULL WHERE hostname = 'sw-ok'")
    iq.clear_caches()
    with view(db) as c:
        s = get(c, "/summary")
        assert s["health"]["never"] == 1 and names_of(c, freshness="never") == {"sw-ok"}


def test_a_scoped_caller_counts_only_the_visible_assets_that_cannot_report(fleet_db, pinned_clock):
    with view(fleet_db, tags=("prod",)) as c:
        s = get(c, "/summary")
        listed = names_of(c, freshness="not_applicable")
    assert s["coverage"]["not_applicable"] == len(listed)
    assert listed <= oracle_not_applicable(fleet_db)
    assert s["coverage"]["not_applicable"] < len(oracle_not_applicable(fleet_db))


def test_sort_by_freshness_puts_the_fourth_state_last(fleet_db, pinned_clock):
    rank = {"fresh": 0, "aging": 1, "stale": 2, "never": 3, "not_applicable": 4}
    with view(fleet_db) as c:
        items = get(c, "/assets", sort="freshness", page_size=500)["items"]
    assert [rank[r["freshness"]] for r in items] == sorted(rank[r["freshness"]] for r in items)
    assert items[-1]["freshness"] == "not_applicable"
