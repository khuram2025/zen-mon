"""Regression tests of the Phase 1 review for endpoint protection and the normalisation backlog (X1, X2: UP-01, UI-01, DT-02,
UP-02, UP-12, UP-13).

'Endpoint protection present' used to mean 'a software row of the broad category security', which the ZenPlus Agent itself,
VPN clients, scanners and password managers satisfy, so the coverage line could not show a gap. It is now: a software row of the
category ``endpoint-protection`` (antivirus, EPP and EDR engines and sensors) or a running Defender service (WinDefend, Sense).
A server is *missing* only when its software list is complete and normalised and shows neither; a truncated list, a list not yet
normalised and no list at all are *unknown*. ``data_status`` tells the pages how much is still being normalised.
"""

from __future__ import annotations

import contextlib

import pytest

from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from inventory_fleet import (
    DEFENDER_SERVICE_SERVERS, EDGE_SERVERS, NO_SECURITY_SERVERS, SCOPED_TAG, SENSE_STOPPED_SERVERS, TRUNCATED_SERVERS,
    ZENPLUS_ONLY_SERVERS, add_assets, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template,
    pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)
from test_inventory_api import get

REV = norm.RULEPACK_REV


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


def view(db, tags=()):
    return api_client(db, role="t_fix_ep", permissions=["inventory.view"], scope_tags=tuple(tags))


def names_of(c, **params) -> set[str]:
    return {r["name"] for r in get(c, "/assets", page_size=500, **params)["items"]}


def oracle(db) -> dict[str, tuple[str, str | None]]:
    """(state, reason) per server, from the raw tables with the rule written out again in plain SQL and Python (X1, X9, X10)."""
    out = {}
    for name, baselined, truncated, ep, defender, waiting, stuck, services in db.query(
            """SELECT s.display_name, s.inventory_baselined_at IS NOT NULL, s.software_truncated,
                      EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.category = 'endpoint-protection'),
                      EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id
                                AND v.service_name IN ('WinDefend', 'Sense') AND lower(v.state) = 'running'),
                      EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id
                                AND (w.norm_rev IS NULL OR w.norm_rev < $1)
                                AND (w.source IS NOT NULL OR s.os_type = 'windows' OR s.os_name ~* '\\mwindows\\M')),
                      EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.norm_rev IS NULL
                                AND NOT (w.source IS NOT NULL OR s.os_type = 'windows'
                                         OR COALESCE(s.os_name ~* '\\mwindows\\M', false))),
                      (SELECT count(*) FROM server_service_inventory v WHERE v.server_id = s.id)
                 FROM servers s""", REV):
        if ep or defender:
            out[name] = ("present", None)
        elif not baselined or stuck:
            out[name] = ("unknown", "no_software_list")
        elif truncated:
            out[name] = ("unknown", "list_truncated")
        elif waiting:
            out[name] = ("unknown", "normalising")
        elif services == 0:
            out[name] = ("unknown", "no_service_list")
        else:
            out[name] = ("missing", None)
    return out


def oracle_states(db) -> dict[str, str]:
    """present / missing / unknown per server, from the raw tables."""
    return {name: state for name, (state, _reason) in oracle(db).items()}


def test_the_fleet_has_every_state(fleet_db, pinned_clock):
    states = oracle_states(fleet_db)
    assert set(states.values()) == {"present", "missing", "unknown"}


def test_the_list_filter_and_summary_agree_with_the_tables(fleet_db, pinned_clock):
    states = oracle_states(fleet_db)
    with view(fleet_db) as c:
        for state in ("present", "missing", "unknown"):
            assert names_of(c, security_agent=state) == {n for n, v in states.items() if v == state}, state
        assert names_of(c, health="missing_security_agent") == {n for n, v in states.items() if v == "missing"}
        sec = get(c, "/summary")["software"]["security_agent_coverage"]
    assert sec == {"covered": sum(v == "present" for v in states.values()), "missing": sum(v == "missing" for v in states.values()),
                   "unknown": sum(v == "unknown" for v in states.values()), "total": len(states)}


@pytest.mark.parametrize("group,state", [
    (ZENPLUS_ONLY_SERVERS, "missing"),            # UP-01: the agent's own entry is not protection
    (DEFENDER_SERVICE_SERVERS, "present"),        # built-in Defender has no Uninstall entry: the running service is the evidence
    (SENSE_STOPPED_SERVERS, "missing"),           # a stopped sensor protects nothing
    (TRUNCATED_SERVERS, "unknown"),               # UP-13: a cut list cannot prove the absence
])
def test_the_cases_of_the_review(fleet_db, fleet_info, pinned_clock, group, state):
    with view(fleet_db) as c:
        for name in group:
            row = get(c, "/assets", q=name)["items"][0]
            assert row["name"] == name
            listed = {s for s in ("present", "missing", "unknown") if name in names_of(c, security_agent=s)}
            assert listed == {state}, (name, listed)
            flyout = get(c, f"/assets/server/{fleet_info.server_ids[name]}")["server"]["software"]["security_agent"]
            assert flyout["state"] == state, (name, flyout)
            assert flyout["present"] == {"present": True, "missing": False, "unknown": None}[state]
            assert (name in names_of(c, health="missing_security_agent")) == (state == "missing")


def test_zenplus_agent_alone_does_not_cover_a_server(fleet_db, fleet_info, pinned_clock):
    name = ZENPLUS_ONLY_SERVERS[0]
    assert fleet_db.scalar("SELECT count(*) FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
                           "WHERE s.display_name = $1 AND w.package_name = 'ZenPlus Agent'", name) == 1
    assert fleet_db.scalar("SELECT w.category FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
                           "WHERE s.display_name = $1 AND w.package_name = 'ZenPlus Agent'", name) != "endpoint-protection"
    with view(fleet_db) as c:
        sec = get(c, f"/assets/server/{fleet_info.server_ids[name]}")["server"]["software"]["security_agent"]
    assert sec == {"present": False, "state": "missing", "reason": None, "products": [], "services": []}


def test_the_defender_service_is_named_in_the_flyout(fleet_db, fleet_info, pinned_clock):
    with view(fleet_db) as c:
        sec = get(c, f"/assets/server/{fleet_info.server_ids[DEFENDER_SERVICE_SERVERS[0]]}")["server"]["software"]["security_agent"]
    assert sec["present"] is True and sec["services"] == ["WinDefend"] and sec["products"] == []


def test_a_vpn_client_is_the_broad_category_and_does_not_count(fleet_db, pinned_clock):
    """FortiClient files under 'security' (a VPN client): the servers that carry nothing else are missing."""
    only_forti = {r[0] for r in fleet_db.query(
        """SELECT s.display_name FROM servers s WHERE EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id
                    AND w.category = 'security')
              AND NOT EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.server_id = s.id AND w.category = 'endpoint-protection')
              AND NOT EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id AND lower(v.state) = 'running'
                    AND v.service_name IN ('WinDefend', 'Sense'))
              AND s.inventory_baselined_at IS NOT NULL AND NOT s.software_truncated""")}
    assert only_forti
    with view(fleet_db) as c:
        assert only_forti <= names_of(c, health="missing_security_agent")


def test_a_running_state_is_matched_without_regard_to_case(fleet_db_fresh, fleet_info, pinned_clock):
    db = fleet_db_fresh
    name = SENSE_STOPPED_SERVERS[0]
    assert oracle_states(db)[name] == "missing"
    db.execute("UPDATE server_service_inventory SET state = 'Running' WHERE service_name = 'Sense' AND server_id IN "
               "(SELECT id FROM servers WHERE display_name = $1)", name)
    with view(db) as c:
        assert name in names_of(c, security_agent="present")


# ── the backlog: not yet normalised is unknown, never missing (UP-02, UP-12) ─────

@pytest.fixture
def upgrading(fleet_db_fresh, fleet_info, pinned_clock):
    """The first minutes after the upgrade: three servers' software rows have no key yet, as migration 157 leaves them."""
    db = fleet_db_fresh
    victims = ("srv-w22-01", "srv-w22-02", NO_SECURITY_SERVERS[5])
    db.execute("UPDATE server_software_inventory SET norm_rev = NULL, name_key = NULL, category = NULL, publisher_key = NULL "
               "WHERE server_id IN (SELECT id FROM servers WHERE display_name = ANY($1::text[]))", list(victims))
    iq.clear_caches()
    return db, victims


def test_servers_with_unkeyed_rows_are_unknown_not_missing(upgrading, fleet_info):
    db, victims = upgrading
    with view(db) as c:
        unknown = names_of(c, security_agent="unknown")
        assert set(victims) <= unknown
        assert not set(victims) & names_of(c, health="missing_security_agent")
        assert not set(victims) & names_of(c, security_agent="present")
        flyout = get(c, f"/assets/server/{fleet_info.server_ids[victims[0]]}")["server"]["software"]["security_agent"]
        assert flyout["state"] == "unknown" and flyout["reason"] == "normalising" and flyout["present"] is None
    assert oracle_states(db)[victims[0]] == "unknown"


def test_data_status_counts_the_backlog_and_clears_when_it_is_done(upgrading, fleet_info):
    db, victims = upgrading
    rows = db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL")
    assert rows > 20
    db.execute("UPDATE inventory_identity SET norm_rev = NULL WHERE asset_kind = 'server' AND asset_id IN "
               "(SELECT id FROM servers WHERE display_name = ANY($1::text[]))", list(victims))
    with view(db) as c:
        s = get(c, "/summary")
    assert s["data_status"] == {"normalising": True, "pending_software_rows": rows, "pending_identity_rows": 3}
    # the loop catches up
    db.execute("UPDATE server_software_inventory SET norm_rev = $1, name_key = 'k-' || package_name, category = 'other' "
               "WHERE norm_rev IS NULL", REV)
    db.execute("UPDATE inventory_identity SET norm_rev = $1 WHERE norm_rev IS NULL", REV)
    iq.clear_caches()
    with view(db) as c:
        assert get(c, "/summary")["data_status"] == {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}


def test_rows_keyed_by_an_older_revision_count_as_pending_until_they_are_rekeyed(fleet_db_fresh, fleet_info, pinned_clock):
    """A rule-pack bump leaves old-revision rows whose category is the old pack's (CrowdStrike was 'security'): until the loop
    re-keys them the server must not read as unprotected."""
    db = fleet_db_fresh
    name = "srv-w22-01"
    assert oracle_states(db)[name] == "present"
    db.execute("UPDATE server_software_inventory SET norm_rev = $2, category = CASE WHEN category = 'endpoint-protection' "
               "THEN 'security' ELSE category END WHERE server_id IN (SELECT id FROM servers WHERE display_name = $1)", name, REV - 1)
    iq.clear_caches()
    with view(db) as c:
        assert name in names_of(c, security_agent="unknown") and name not in names_of(c, health="missing_security_agent")
        ds = get(c, "/summary")["data_status"]
        assert ds["normalising"] is True and ds["pending_software_rows"] > 0
    db.execute("UPDATE server_software_inventory w SET norm_rev = $1 FROM servers s WHERE s.id = w.server_id AND s.display_name = $2",
               REV, name)
    db.execute("UPDATE server_software_inventory SET category = 'endpoint-protection' WHERE name_key = 'crowdstrike-falcon-sensor' "
               "AND server_id IN (SELECT id FROM servers WHERE display_name = $1)", name)
    iq.clear_caches()
    with view(db) as c:
        assert name in names_of(c, security_agent="present")
        assert get(c, "/summary")["data_status"]["normalising"] is False


@pytest.mark.asyncio
async def test_data_status_is_a_helper_the_catalog_can_call_and_it_is_scope_aware(upgrading):
    db, victims = upgrading
    async with session(db) as s:
        everyone = await iq.data_status(s, scope=None)
        edge = await iq.data_status(s, scope=[SCOPED_TAG])
        nobody = await iq.data_status(s, scope=["no-such-tag"])
    visible_victims = [v for v in victims if v in EDGE_SERVERS]
    expected_edge = db.scalar(
        "SELECT count(*) FROM server_software_inventory w JOIN servers s ON s.id = w.server_id WHERE w.norm_rev IS NULL "
        "AND s.display_name = ANY($1::text[])", list(EDGE_SERVERS))
    assert everyone["normalising"] is True and everyone["pending_software_rows"] > edge["pending_software_rows"] == expected_edge
    assert nobody == {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}
    assert set(everyone) == {"normalising", "pending_software_rows", "pending_identity_rows"} and visible_victims is not None


def test_a_baselined_server_of_another_os_type_with_unkeyed_rows_is_unknown(fleet_db_fresh, pinned_clock):
    """UP-12: pre-157 rows (source NULL) of a server whose os_type is not 'windows' are never keyed until its next upload; it was
    flagged 'without a security agent' for as long as it stayed silent."""
    db = fleet_db_fresh
    add_assets(db, servers=[dict(name="odd-os", baselined=True, mode="agent")])
    db.execute("UPDATE servers SET os_type = 'other' WHERE display_name = 'odd-os'")
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor) SELECT id, 'Legacy Tool', '1', 'v' "
               "FROM servers WHERE display_name = 'odd-os'")
    db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) SELECT id, 'W32Time', 'running' "
               "FROM servers WHERE display_name = 'odd-os'")         # a service list exists: the unkeyed row is the only reason
    with view(db) as c:
        assert "odd-os" in names_of(c, security_agent="unknown") and "odd-os" not in names_of(c, health="missing_security_agent")


# ── the helper the flyout and the SQL share their answer through ────────────

def test_security_state_is_the_single_rule():
    base = dict(endpoint_rows=0, defender_services=0, baselined=True, truncated=False, pending_rows=0, unkeyable_rows=0, service_rows=3)
    f = lambda **kw: iq.security_assessment(**{**base, **kw})        # noqa: E731
    assert f(endpoint_rows=1, baselined=False, truncated=True, pending_rows=5, service_rows=0) == ("present", None)
    assert f(defender_services=1) == ("present", None)
    assert f() == ("missing", None)
    assert f(baselined=False) == ("unknown", "no_software_list")
    assert f(unkeyable_rows=1) == ("unknown", "no_software_list")                     # X10: rows the loop never selects
    assert f(truncated=True) == ("unknown", "list_truncated")
    assert f(pending_rows=1) == ("unknown", "normalising")
    assert f(service_rows=0) == ("unknown", "no_service_list")                        # X9: no service list to look for Defender in
    # the first reason that applies wins, in this order
    assert f(baselined=False, truncated=True, pending_rows=1, service_rows=0) == ("unknown", "no_software_list")
    assert f(truncated=True, pending_rows=1, service_rows=0) == ("unknown", "list_truncated")
    assert f(pending_rows=1, service_rows=0) == ("unknown", "normalising")
    assert iq.security_state(**{**base, "service_rows": 0}) == "unknown"
    assert set(iq.SECURITY_UNKNOWN_REASONS) == {"no_software_list", "list_truncated", "normalising", "no_service_list"}


def test_every_server_has_the_same_answer_in_the_list_and_in_its_flyout(fleet_db, fleet_info, pinned_clock):
    answers = oracle(fleet_db)
    with view(fleet_db) as c:
        for name, (state, reason) in answers.items():
            flyout = get(c, f"/assets/server/{fleet_info.server_ids[name]}")["server"]["software"]["security_agent"]
            assert (flyout["state"], flyout["reason"]) == (state, reason), name
