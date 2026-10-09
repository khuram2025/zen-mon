"""Regression tests of the Phase 1 re-check (fix round 2, be-d) for the normalisation backlog and endpoint protection.

UP-R2-01 / NB-02 / NL-01 / NQ-02 / DTN-02 / UP-12 residual (one defect, five reports)
    ``data_status`` counted software rows the normalise loop never selects, so 'Normalising existing software lists' could
    stay up for ever. It now asks the loop's own eligibility rule (``inventory_service.software_eligible_sql``), scoped and
    unscoped and in the older-revision probe, and a server whose unkeyed rows the loop never selects stays 'unknown' for
    endpoint protection with the reason ``no_software_list`` (X10), never ``normalising``.
NQ-01 / NL-03 (X9)
    A server with a complete, normalised software list and NO service rows (the APM-only agent profile, services switched
    off) was 'missing' endpoint protection: built-in Defender shows only as a running service, so with no service list the
    question cannot be answered. It is 'unknown' with the new reason ``no_service_list`` in the list filters, the Overview,
    the daily snapshot and the flyout.

The oracles are plain SQL and Python written out again from the rule, independent of the code under test.
"""

from __future__ import annotations

import contextlib
import itertools
import re

import pytest

from app.services import inventory_normalize as norm
from app.services import inventory_query as iq
from app.services import inventory_service as svc
from inventory_fleet import (
    NO_SERVICE_LIST_SERVERS, SCOPED_TAG, add_assets, api_client, fleet_db, fleet_db_fresh, fleet_info, fleet_template,
    pinned_clock, sessions,  # noqa: F401
)
from pg_fixture import pg_cluster  # noqa: F401  (fixture)
from test_inventory_api import get
from test_inventory_ingest import WIN_2022, add_server as ingest_add_server, sw, upload

REV = norm.RULEPACK_REV
ANYTHING = "t_r2_ep"


@contextlib.asynccontextmanager
async def session(db):
    async with sessions(db) as sm:
        async with sm() as s:
            yield s


def view(db, tags=()):
    return api_client(db, role=ANYTHING, permissions=["inventory.view"], scope_tags=tuple(tags))


def flyout(c, fleet_ids_or_id) -> dict:
    return get(c, f"/assets/server/{fleet_ids_or_id}")["server"]["software"]["security_agent"]


def names_of(c, **params) -> set[str]:
    return {r["name"] for r in get(c, "/assets", page_size=500, **params)["items"]}


async def full_ticks(db, rounds: int = 2) -> None:
    """The normalise loop run to idle, each round with the state of a freshly started worker."""
    async with sessions(db) as factory:
        for _ in range(rounds):
            await svc.run_normalise_tick(factory, state=svc.TickState())


def add_server(db, name, *, os_type="windows", os_name=None, tags=(), baselined=True, truncated=False, services=1) -> str:
    """A server with an identity row at the current revision (nothing for the identity backlog to do), baselined, with
    ``services`` plain service rows (W32Time and friends) unless 0. Returns its id."""
    add_assets(db, servers=[dict(name=name, baselined=baselined, tags=list(tags))])
    db.execute("UPDATE servers SET os_type = $2, os_name = $3, software_truncated = $4 WHERE display_name = $1",
               name, os_type, os_name, truncated)
    sid = db.scalar("SELECT id FROM servers WHERE display_name = $1", name)
    for i in range(services):
        db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, $2, 'running')",
                   sid, f"svc-{i}")
    return str(sid)


def add_row(db, sid, package, *, source=None, norm_rev=None, category=None, name_key=None) -> None:
    """A software row exactly as given (suppressed capture triggers): no normaliser, no inline key."""
    db.execute(
        "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source, norm_rev, category, name_key) "
        "VALUES ($1, $2, '1.0', 'Vendor', $3, $4, $5, $6)", sid, package, source, norm_rev, category, name_key)


# ════════════════════════════════════════════════════════════════════
# The one eligibility rule: three renderings, the same answer
# ════════════════════════════════════════════════════════════════════

SERVER_KINDS = [("windows", None), ("other", None), ("linux", "Windows Server 2019 Standard"), ("linux", "Ubuntu 22.04.4 LTS"),
                ("other", "WINDOWS 11 Pro"), ("linux", "Awindowsb"), ("unknown", "")]
ROW_KINDS = list(itertools.product((None, "hklm"), (None, REV - 1, REV)))      # (source, norm_rev)


def eligible_py(os_type, os_name, source) -> bool:
    """The loop's rule in plain Python: a source, or a Windows machine by os_type or by the word in its OS name."""
    return source is not None or os_type == "windows" or bool(os_name and re.search(r"\bwindows\b", os_name, re.IGNORECASE))


def seed_matrix(db) -> dict[str, dict]:
    """Every (os_type, os_name) x (source, norm_rev) combination as a server holding one row of each row kind."""
    out = {}
    for n, (os_type, os_name) in enumerate(SERVER_KINDS):
        name = f"r2-matrix-{n}"
        sid = add_server(db, name, os_type=os_type, os_name=os_name, tags=[f"mx{n % 2}"])
        for src, rev in ROW_KINDS:
            add_row(db, sid, f"P {src} {rev}", source=src, norm_rev=rev, category="other" if rev is not None else None,
                    name_key=f"p-{src}-{rev}" if rev is not None else None)
        out[name] = {"id": sid, "os_type": os_type, "os_name": os_name, "tag": f"mx{n % 2}"}
    return out


@pytest.mark.asyncio
async def test_up_r2_01_the_three_renderings_of_the_eligibility_rule_agree_with_the_plain_rule(fleet_db_fresh):
    """UP-R2-01 / X10: the EXISTS form (loop, backlog), the join form (queries that join servers) and a plain Python rule
    select the same rows, including servers without an OS name (NOT of the predicate must not be NULL)."""
    seed_matrix(fleet_db_fresh)
    exists_form = {(r[0], r[1]) for r in fleet_db_fresh.query(
        f"SELECT s.server_id, s.package_name FROM server_software_inventory s WHERE {svc.software_eligible_sql('s')}")}
    join_form = {(r[0], r[1]) for r in fleet_db_fresh.query(
        f"SELECT w.server_id, w.package_name FROM server_software_inventory w JOIN servers x ON x.id = w.server_id "
        f"WHERE {svc.software_eligible_sql('w', server='x')}")}
    not_exists = {(r[0], r[1]) for r in fleet_db_fresh.query(
        f"SELECT s.server_id, s.package_name FROM server_software_inventory s WHERE NOT {svc.software_eligible_sql('s')}")}
    not_join = {(r[0], r[1]) for r in fleet_db_fresh.query(
        f"SELECT w.server_id, w.package_name FROM server_software_inventory w JOIN servers x ON x.id = w.server_id "
        f"WHERE NOT {svc.software_eligible_sql('w', server='x')}")}
    every = {(r[0], r[1]) for r in fleet_db_fresh.query("SELECT server_id, package_name FROM server_software_inventory")}
    plain = {(r[0], r[1]) for r in fleet_db_fresh.query(
        "SELECT w.server_id, w.package_name, w.source, s.os_type, s.os_name FROM server_software_inventory w "
        "JOIN servers s ON s.id = w.server_id") if eligible_py(r[3], r[4], r[2])}
    assert exists_form == join_form == plain
    assert not_exists == not_join == every - plain         # never NULL: a row is eligible or not, nothing in between
    assert not_exists, "the matrix must contain rows the loop never selects"


@pytest.mark.asyncio
async def test_up_r2_01_data_status_equals_the_loops_backlog_equals_the_plain_rule(fleet_db_fresh, pinned_clock):
    """UP-R2-01 / NB-02 / NL-01 / NQ-02 / DTN-02: for every mix of server kind, source and revision the banner count is the
    count of the rows the loop will pick up (backlog), unscoped and scoped; the older-revision rows of a server the loop
    never selects do not count and do not keep the stale-revision probe on its slow path."""
    db = fleet_db_fresh
    servers = seed_matrix(db)
    expected_total = 0
    expected_by_tag: dict[str, int] = {}
    for info in servers.values():
        for src, rev in ROW_KINDS:
            if rev in (None, REV - 1) and eligible_py(info["os_type"], info["os_name"], src):
                expected_total += 1
                expected_by_tag[info["tag"]] = expected_by_tag.get(info["tag"], 0) + 1
    assert expected_total > 0 and len(expected_by_tag) == 2
    async with session(db) as s:
        everyone = await iq.data_status(s, scope=None)
        backlog = await svc.normalise_backlog(s)
        scoped = {tag: await iq.data_status(s, scope=[tag]) for tag in expected_by_tag}
    assert everyone["pending_software_rows"] == backlog.software_rows == expected_total
    assert everyone["normalising"] is True
    for tag, n in expected_by_tag.items():
        assert scoped[tag]["pending_software_rows"] == n, tag
    # And the old-revision probe: eligible stale rows exist here, so the probe is on
    async with session(db) as s:
        assert await iq._software_rev_pending(s) is True


@pytest.mark.asyncio
async def test_up_r2_01_a_non_windows_server_with_source_null_rows_is_not_normalising_after_a_full_tick(fleet_db_fresh, pinned_clock):
    """The acceptance case of the finding: after the loop has run to idle, rows it never selects leave data_status at
    normalising false everywhere (summary, catalog, publishers, product page; unscoped and scoped), and the server stays
    'unknown' for endpoint protection with the reason no_software_list."""
    db = fleet_db_fresh
    edge = add_server(db, "r2-odd-edge", os_type="linux", os_name="Ubuntu 22.04.4 LTS", tags=[SCOPED_TAG])
    plain = add_server(db, "r2-odd-null-name", os_type="other", os_name=None)
    for sid in (edge, plain):
        add_row(db, sid, "Legacy Tool")
        add_row(db, sid, "Other Legacy Tool")
    windows_by_name = add_server(db, "r2-win-by-name", os_type="linux", os_name="Windows Server 2019 Standard")
    add_row(db, windows_by_name, "Google Chrome")                                          # the loop keys this one (UP-12)
    async with session(db) as s:
        before = await iq.data_status(s, scope=None)
    assert before["pending_software_rows"] == 1 and before["normalising"] is True      # only the Windows-by-name row counts
    await full_ticks(db)
    unkeyed = {r[0] for r in db.query("SELECT s.display_name FROM server_software_inventory w JOIN servers s ON s.id = w.server_id "
                                      "WHERE w.norm_rev IS NULL")}
    assert unkeyed == {"r2-odd-edge", "r2-odd-null-name"}, "the loop leaves rows it does not select unkeyed"
    async with session(db) as s:
        backlog = await svc.normalise_backlog(s)
        assert backlog.software_rows == 0 and backlog.identity_rows == 0
        for scope in (None, [SCOPED_TAG], ["no-such-tag"]):
            assert await iq.data_status(s, scope=scope) == {"normalising": False, "pending_software_rows": 0, "pending_identity_rows": 0}
    iq.clear_caches()
    for tags in ((), (SCOPED_TAG,)):
        with view(db, tags) as c:
            assert get(c, "/summary")["data_status"]["normalising"] is False
            assert get(c, "/software")["data_status"]["normalising"] is False
            assert get(c, "/software/publishers")["data_status"]["normalising"] is False
    with view(db) as c:
        assert get(c, "/software/google-chrome")["data_status"]["normalising"] is False
    with view(db) as c:
        unknown = names_of(c, security_agent="unknown")
        assert {"r2-odd-edge", "r2-odd-null-name"} <= unknown
        assert not {"r2-odd-edge", "r2-odd-null-name"} & names_of(c, health="missing_security_agent")
        assert not {"r2-odd-edge", "r2-odd-null-name"} & names_of(c, security_agent="missing")
        for sid in (edge, plain):
            assert flyout(c, sid) == {"present": None, "state": "unknown", "reason": "no_software_list", "products": [], "services": []}


@pytest.mark.asyncio
async def test_up_r2_01_older_revision_rows_of_a_server_the_loop_never_selects_do_not_hold_the_probe(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    odd = add_server(db, "r2-odd-stale", os_type="other", os_name=None)
    add_row(db, odd, "Old Pack Tool", source=None, norm_rev=REV - 1, category="other", name_key="old-pack-tool")
    iq.clear_caches()
    async with session(db) as s:
        assert await iq._software_rev_pending(s) is False         # nothing the loop would re-key
        assert iq._rev_clear is not None                          # and the answer is remembered: the slow path is left
        assert (await iq.data_status(s, scope=None))["pending_software_rows"] == 0
    # a row the loop WILL re-key puts the probe and the count back, and the loop takes them away again
    normal = add_server(db, "r2-win-stale", os_type="windows")
    add_row(db, normal, "Old Pack Chrome", source="hklm", norm_rev=REV - 1, category="other", name_key="old-pack-chrome")
    iq.clear_caches()
    async with session(db) as s:
        assert await iq._software_rev_pending(s) is True
        assert (await iq.data_status(s, scope=None))["pending_software_rows"] == 1
    await full_ticks(db)
    iq.clear_caches()
    async with session(db) as s:
        assert await iq._software_rev_pending(s) is False
        assert (await iq.data_status(s, scope=None))["normalising"] is False


# ════════════════════════════════════════════════════════════════════
# X9: no service list is not 'missing'
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_nq_01_nl_03_a_server_with_no_service_rows_is_unknown_with_reason_no_service_list(fleet_db_fresh, pinned_clock):
    """NQ-01 / NL-03 reproduced: baselined, complete and normalised list, no endpoint-protection title, ZERO service rows."""
    db = fleet_db_fresh
    sid = add_server(db, "r2-apm-only", services=0)
    add_row(db, sid, "Google Chrome", source="hklm", norm_rev=REV, category="browser", name_key="google-chrome")
    control = add_server(db, "r2-with-services", services=3)           # the same list WITH a service list (no Defender): missing
    add_row(db, control, "Google Chrome", source="hklm", norm_rev=REV, category="browser", name_key="google-chrome")
    async with session(db) as s:
        before = (await iq.summary(s, scope=None, use_cache=False))["software"]["security_agent_coverage"]
    with view(db) as c:
        assert flyout(c, sid) == {"present": None, "state": "unknown", "reason": "no_service_list", "products": [], "services": []}
        assert flyout(c, control) == {"present": False, "state": "missing", "reason": None, "products": [], "services": []}
        assert "r2-apm-only" in names_of(c, security_agent="unknown")                  # the 'unknown' line of the Overview
        assert "r2-apm-only" not in names_of(c, security_agent="missing")
        assert "r2-apm-only" not in names_of(c, health="missing_security_agent")
        assert "r2-with-services" in names_of(c, health="missing_security_agent")
        cov = get(c, "/summary")["software"]["security_agent_coverage"]
        health = get(c, "/summary")["health"]["missing_security_agent"]
    assert before == cov and cov["unknown"] > 0
    assert cov["covered"] + cov["missing"] + cov["unknown"] == cov["total"]
    assert health == cov["missing"]


@pytest.mark.asyncio
async def test_nq_01_the_daily_snapshot_agrees_with_the_live_overview_for_the_new_states(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    apm = add_server(db, "r2-apm-only", services=0)
    add_row(db, apm, "Google Chrome", source="hklm", norm_rev=REV, category="browser", name_key="google-chrome")
    stuck = add_server(db, "r2-stuck", os_type="other", services=2)
    add_row(db, stuck, "Legacy Tool")
    async with session(db) as s:
        live = await iq.summary(s, scope=None, use_cache=False)
    async with session(db) as s:
        rows = await svc.snapshot_day(s, pinned_clock.date(), zone=pinned_clock.tzinfo)
    assert rows[("health", "missing_security_agent")] == live["health"]["missing_security_agent"]
    states = {n: st for n, (st, _r) in oracle(db).items()}
    assert states["r2-apm-only"] == "unknown" and states["r2-stuck"] == "unknown"
    assert sum(st == "missing" for st in states.values()) == rows[("health", "missing_security_agent")]


@pytest.mark.asyncio
async def test_nq_01_the_state_follows_the_service_list_and_the_software(fleet_db_fresh, pinned_clock):
    db = fleet_db_fresh
    sid = add_server(db, "r2-follows", services=0)
    add_row(db, sid, "Google Chrome", source="hklm", norm_rev=REV, category="browser", name_key="google-chrome")

    def state() -> tuple[str, str | None]:
        iq.clear_caches()
        with view(db) as c:
            f = flyout(c, sid)
            listed = {s for s in ("present", "missing", "unknown") if "r2-follows" in names_of(c, security_agent=s)}
        assert listed == {f["state"]}
        return f["state"], f["reason"]

    assert state() == ("unknown", "no_service_list")
    db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, 'W32Time', 'running')", sid)
    assert state() == ("missing", None)                          # a service list without Defender: now it can be judged
    db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, 'WinDefend', 'Running')", sid)
    assert state() == ("present", None)
    db.execute("DELETE FROM server_service_inventory WHERE server_id = $1", sid)
    db.execute("UPDATE server_software_inventory SET category = 'endpoint-protection' WHERE server_id = $1", sid)
    assert state() == ("present", None)                          # an endpoint-protection title needs no service list
    db.execute("UPDATE server_software_inventory SET category = 'browser' WHERE server_id = $1", sid)
    db.execute("UPDATE servers SET software_truncated = true WHERE id = $1", sid)
    assert state() == ("unknown", "list_truncated")              # the earlier reasons win over a missing service list
    db.execute("UPDATE servers SET software_truncated = false, inventory_baselined_at = NULL WHERE id = $1", sid)
    assert state() == ("unknown", "no_software_list")
    db.execute("UPDATE servers SET inventory_baselined_at = now() WHERE id = $1", sid)
    db.execute("UPDATE server_software_inventory SET norm_rev = NULL WHERE server_id = $1", sid)
    assert state() == ("unknown", "normalising")                 # eligible unkeyed rows (they carry a source) are waiting


@pytest.mark.asyncio
async def test_nq_01_end_to_end_through_the_agent_ingest_an_apm_only_upload_is_unknown(fleet_db_fresh, pinned_clock):
    """NQ-01 through the real ``_upsert_inventory``: the APM-only profile uploads os and software but no services."""
    db = fleet_db_fresh
    sid = ingest_add_server(db, display_name="r2-ingest-apm", os_type="windows", collection_mode="agent")
    await upload(db, sid, {"os": WIN_2022, "software": [sw("Google Chrome", "126.0.1"), sw("7-Zip 23.01 (x64)", "23.01")]})
    with view(db) as c:
        assert flyout(c, sid) == {"present": None, "state": "unknown", "reason": "no_service_list", "products": [], "services": []}
    # the next upload carries the service list (without Defender): now it is a real gap
    await upload(db, sid, {"os": WIN_2022, "software": [sw("Google Chrome", "126.0.1"), sw("7-Zip 23.01 (x64)", "23.01")],
                           "services": [{"service_name": "W32Time", "state": "running", "display_name": "Windows Time"}]})
    iq.clear_caches()
    with view(db) as c:
        assert flyout(c, sid)["state"] == "missing"
    await upload(db, sid, {"os": WIN_2022, "software": [sw("Google Chrome", "126.0.1")],
                           "services": [{"service_name": "WinDefend", "state": "Running"}]})
    iq.clear_caches()
    with view(db) as c:
        assert flyout(c, sid)["state"] == "present"


# ── the fleet: every server has the same answer in the SQL, the Python twin and the flyout ──────────────────

def oracle(db) -> dict[str, tuple[str, str | None]]:
    from test_inventory_fix_endpoint_protection import oracle as fleet_oracle
    return fleet_oracle(db)


@pytest.mark.asyncio
async def test_x9_the_sql_reason_the_python_twin_and_the_flyout_agree_for_every_server(fleet_db, fleet_info, pinned_clock):
    rows = fleet_db.query(
        f"SELECT s.display_name, {iq._SEC_STATE_SQL}, {iq._SEC_REASON_SQL} FROM servers s {iq._security_joins(False)}")
    answers = oracle(fleet_db)
    assert {r[0]: (r[1], r[2]) for r in rows} == answers
    reasons = {r for (_s, r) in answers.values() if r}
    assert reasons == {"no_software_list", "list_truncated", "no_service_list"}, "the fleet shows every reason it can"
    assert [n for n, (_s, r) in answers.items() if r == "no_service_list"] == list(NO_SERVICE_LIST_SERVERS)
    with view(fleet_db) as c:
        for name, (state, reason) in answers.items():
            assert (flyout(c, fleet_info.server_ids[name])["state"], flyout(c, fleet_info.server_ids[name])["reason"]) == (state, reason), name


FACTS = list(itertools.product(
    (True, False),                                            # baselined
    (False, True),                                            # truncated
    (False, True),                                            # an endpoint-protection title
    (False, True),                                            # a running Defender service
    ("none", "other"),                                        # service rows: none, or services without Defender
    ("none", "waiting", "stuck", "stale_waiting"),            # software rows not yet keyed
))


@pytest.mark.asyncio
async def test_x9_x10_every_combination_of_facts_gives_one_answer_in_sql_python_and_the_snapshot(fleet_db_fresh, pinned_clock):
    """The rule is stated three times (live SQL, the Python twin the flyout uses, the daily snapshot CTE): they must be the
    same function of the same facts, over all 128 combinations."""
    db = fleet_db_fresh
    ids = {}
    for n, (baselined, truncated, ep, defender, services, pending) in enumerate(FACTS):
        name = f"r2-combo-{n:03d}"
        os_type, os_name = ("other", None) if pending == "stuck" else ("windows", None)
        sid = add_server(db, name, os_type=os_type, os_name=os_name, baselined=baselined, truncated=truncated,
                         services=1 if services == "other" else 0)
        ids[name] = sid
        add_row(db, sid, "Plain App", source="hklm" if pending != "stuck" else None, norm_rev=REV, category="other", name_key="plain-app")
        if ep:
            add_row(db, sid, "Falcon", source="hklm", norm_rev=REV, category="endpoint-protection", name_key="crowdstrike-falcon-sensor")
        if defender:
            db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, 'WinDefend', 'running')", sid)
        if pending == "waiting":
            add_row(db, sid, "Unkeyed App", source="hklm")
        elif pending == "stuck":
            add_row(db, sid, "Unkeyed Legacy")
        elif pending == "stale_waiting":
            add_row(db, sid, "Old Pack App", source="hklm", norm_rev=REV - 1, category="other", name_key="old-pack-app")
    expected: dict[str, tuple[str, str | None]] = {}
    for name, (baselined, truncated, ep, defender, services, pending) in zip(ids, FACTS):
        service_rows = (1 if services == "other" else 0) + (1 if defender else 0)
        expected[name] = iq.security_assessment(
            endpoint_rows=1 if ep else 0, defender_services=1 if defender else 0, baselined=baselined, truncated=truncated,
            pending_rows=1 if pending in ("waiting", "stale_waiting") else 0, unkeyable_rows=1 if pending == "stuck" else 0,
            service_rows=service_rows)
    live = {r[0]: (r[1], r[2]) for r in db.query(
        f"SELECT s.display_name, {iq._SEC_STATE_SQL}, {iq._SEC_REASON_SQL} FROM servers s {iq._security_joins(True)} "
        "WHERE s.display_name LIKE 'r2-combo-%'")}
    assert live == expected
    # the same through the rows of the Assets list (state) and the flyout (state and reason)
    with view(db) as c:
        listed = {}
        for state in ("present", "missing", "unknown"):
            for n in names_of(c, security_agent=state, q="r2-combo"):
                listed[n] = state
        assert listed == {n: st for n, (st, _r) in expected.items()}
        for name in list(ids)[::7]:
            f = flyout(c, ids[name])
            assert (f["state"], f["reason"]) == expected[name], name
    # and the daily snapshot counts exactly the 'missing' ones
    async with session(db) as s:
        rows = await svc.snapshot_day(s, pinned_clock.date(), zone=pinned_clock.tzinfo)
    every_missing = db.scalar(f"SELECT count(*) FROM servers s {iq._security_joins(True)} WHERE {iq._SEC_STATE_SQL} = 'missing'")
    assert rows[("health", "missing_security_agent")] == every_missing
    assert sum(1 for st, _r in expected.values() if st == "missing") <= every_missing


def test_x9_every_combination_has_a_documented_reason():
    for facts in FACTS:
        baselined, truncated, ep, defender, services, pending = facts
        state, reason = iq.security_assessment(
            endpoint_rows=int(ep), defender_services=int(defender), baselined=baselined, truncated=truncated,
            pending_rows=int(pending in ("waiting", "stale_waiting")), unkeyable_rows=int(pending == "stuck"),
            service_rows=int(services == "other") + int(defender))
        assert (reason is None) == (state != "unknown")
        assert reason is None or reason in iq.SECURITY_UNKNOWN_REASONS
