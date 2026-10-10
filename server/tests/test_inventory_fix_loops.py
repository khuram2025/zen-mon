"""Phase 1 fix round, task be-c: the background loops (finding UP-07).

The daily snapshot used to be written 60 s after the process started, before the agents had reported and
before the normalise backlog had drained, and was then frozen. These tests pin the replacement: a held first
write, a refresh during the first 24 hours after the capture start, exactly one write per day otherwise, and a
health series that answers the same question as the live Overview. Everything runs against the private cluster
from ``pg_fixture``, never the appliance database.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import inventory_query as iq
from app.services import inventory_service as svc
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures are used by name)

UTC = timezone.utc
DAY7 = date(2026, 10, 7)
_seq = itertools.count(1)


def at(text_):
    return datetime.fromisoformat(text_).replace(tzinfo=UTC)


@pytest.fixture
def inv_db(pg_db):
    pg_db.execute("DELETE FROM servers; DELETE FROM devices")
    return pg_db


@contextlib.asynccontextmanager
async def sessions(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def tick(db):
    async with sessions(db) as factory:
        return await svc.run_normalise_tick(factory, state=svc.TickState())


async def snap_pass(db, now, state=None, zone=UTC):
    async with sessions(db) as factory:
        return await svc.run_snapshot_pass(factory, now=at(now) if isinstance(now, str) else now, zone=zone, state=state)


def add_server(db, name, **cols):
    names = ["display_name", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return db.scalar(f"INSERT INTO servers ({', '.join(names)}) VALUES ({marks}) RETURNING id", name, *cols.values())


def add_device(db, hostname, **cols):
    n = next(_seq)
    cols.setdefault("ip_address", f"10.77.{n // 250}.{n % 250 + 1}")
    names = ["hostname", *cols]
    marks = ", ".join(f"${i + 1}" + ("::inet" if name == "ip_address" else "") for i, name in enumerate(names))
    return db.scalar(f"INSERT INTO devices ({', '.join(names)}) VALUES ({marks}) RETURNING id", hostname, *cols.values())


def add_software(db, server_id, package, vendor=None, version="1.0", source="hklm"):
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source) "
               "VALUES ($1, $2, $3, $4, $5)", server_id, package, version, vendor, source)


def snapshot_rows(db, day):
    return {(m, d): v for m, d, v in db.query(
        "SELECT metric, dimension, value FROM inventory_daily_snapshots WHERE day = $1", day)}


def set_capture_start(db, iso):
    db.execute("INSERT INTO system_settings (key, value) VALUES ('inventory.capture_started_at', to_jsonb($1::text)) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", iso)


class Clock:
    """The loop's monotonic clock, driven by the test."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def new_state(clock, **kw):
    return svc.SnapshotState(started_at=clock.t, clock=clock, **kw)


# ════════════════════════════════════════════════════════════════════
# The settle time
# ════════════════════════════════════════════════════════════════════

def test_UP07_the_first_snapshot_is_not_written_a_minute_after_start():
    state = svc.SnapshotState()
    assert svc.SNAPSHOT_SETTLE_S == 30 * 60 and state.settle_s == svc.SNAPSHOT_SETTLE_S
    assert not state.settled()                       # a fresh loop is not settled, whatever the start delay is
    old = new_state(Clock(10_000.0)); old.started_at = 0.0
    assert old.settled()


def test_UP07_the_loop_gives_its_pass_a_state(monkeypatch):
    seen = []

    async def fake(*args, **kwargs):
        seen.append(kwargs.get("state"))

    monkeypatch.setattr(svc, "run_snapshot_pass", fake)

    async def go():
        task = asyncio.create_task(svc.inventory_snapshot_loop(start_delay=0, interval=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(seen) >= 2 and isinstance(seen[0], svc.SnapshotState) and seen[0] is seen[1]
    assert seen[0].settle_s == svc.SNAPSHOT_SETTLE_S


@pytest.mark.asyncio
async def test_UP07_a_due_snapshot_waits_for_the_process_to_settle(inv_db):
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock = Clock(0.0)
    state = new_state(clock)

    clock.t = 60.0                                   # the old loop wrote here
    early = await snap_pass(inv_db, "2026-10-07T00:20:00", state)
    assert (early.due, early.ran, early.held) == (True, False, "settling")
    assert snapshot_rows(inv_db, DAY7) == {}

    clock.t = 1799.0
    assert (await snap_pass(inv_db, "2026-10-07T00:50:00", state)).held == "settling"
    clock.t = 1800.0
    ran = await snap_pass(inv_db, "2026-10-07T00:55:00", state)
    assert (ran.ran, ran.held, ran.refreshed) == (True, None, False)
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 1


@pytest.mark.asyncio
async def test_UP07_the_snapshot_records_the_settled_estate_not_the_upgrade_minute(inv_db):
    """Upgrade day: the servers have not reported yet at start (Never), they do within the hold."""
    srv = [add_server(inv_db, f"S{i}", os_name="Ubuntu 22.04.4 LTS") for i in range(4)]
    await tick(inv_db)
    clock = Clock(0.0)
    state = new_state(clock)
    clock.t = 60.0
    assert (await snap_pass(inv_db, "2026-10-07T00:20:00", state)).held == "settling"
    for sid in srv:                                   # the agents report during the hold
        inv_db.execute("UPDATE servers SET inventory_updated_at = now() WHERE id = $1", sid)
    clock.t = 2400.0
    assert (await snap_pass(inv_db, "2026-10-07T00:55:00", state)).ran
    rows = snapshot_rows(inv_db, DAY7)
    assert (rows[("coverage", "fresh")], rows[("coverage", "never")]) == (4, 0)


# ════════════════════════════════════════════════════════════════════
# The normalise backlog
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_UP07_normalise_backlog_counts_only_what_the_loop_will_pick_up(inv_db):
    async with sessions(inv_db) as factory:
        async with factory() as db:
            assert (await svc.normalise_backlog(db)).total == 0
    win = add_server(inv_db, "win", os_type="windows", os_name="Microsoft Windows Server 2022 Standard")
    lin = add_server(inv_db, "lin", os_type="linux", os_name="Ubuntu 22.04.4 LTS")
    add_software(inv_db, win, "Google Chrome", "Google LLC")
    add_software(inv_db, win, "Mozilla Firefox", "Mozilla")
    add_software(inv_db, lin, "bash", None, source="dpkg")
    inv_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, source) "
                   "VALUES ($1, 'legacy-row', '1', NULL)", lin)       # pre-157 row on a Linux server: never selected

    async with sessions(inv_db) as factory:
        async with factory() as db:
            backlog = await svc.normalise_backlog(db)
    assert backlog.software_rows == 3                 # chrome, firefox, bash; not the legacy row
    assert backlog.identity_rows == 2 and backlog.total == 5 and backlog.normalising

    await tick(inv_db)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            done = await svc.normalise_backlog(db)
    assert done.total == 0 and not done.normalising
    assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL") == 1   # the legacy row

    # An older revision is backlog too, and the cap bounds the count.
    inv_db.execute("UPDATE server_software_inventory SET norm_rev = norm_rev - 1 WHERE package_name IN ('Google Chrome', 'bash')")
    inv_db.execute("UPDATE inventory_identity SET norm_rev = norm_rev - 1 WHERE asset_kind = 'server' AND asset_id = $1", win)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            old = await svc.normalise_backlog(db)
            capped = await svc.normalise_backlog(db, cap=1)
    assert (old.software_rows, old.identity_rows) == (2, 1)
    assert capped.software_rows == 1 and capped.identity_rows == 1


@pytest.mark.asyncio
async def test_UP07_a_due_snapshot_waits_for_the_normalise_backlog(inv_db):
    srv = add_server(inv_db, "win", os_type="windows", os_name="Microsoft Windows Server 2022 Standard")
    add_software(inv_db, srv, "Google Chrome", "Google LLC")           # dirty identity and an unkeyed row
    clock = Clock(5000.0)
    state = new_state(clock); state.started_at = 0.0                   # settled

    held = await snap_pass(inv_db, "2026-10-07T00:20:00", state)
    assert (held.due, held.ran, held.held) == (True, False, "normalising")
    assert snapshot_rows(inv_db, DAY7) == {}

    await tick(inv_db)
    ran = await snap_pass(inv_db, "2026-10-07T00:25:00", state)
    assert ran.ran and ran.held is None
    assert snapshot_rows(inv_db, DAY7)[("software_products", "installs")] == 1


@pytest.mark.asyncio
async def test_UP07_a_row_that_never_normalises_cannot_stop_the_history(inv_db, caplog):
    add_server(inv_db, "dirty", os_name="Ubuntu 22.04.4 LTS")          # never ticked: dirty for ever
    clock = Clock(13 * 3600.0)                                        # the process has been up for 13 hours
    state = new_state(clock); state.started_at = 0.0
    assert svc.SNAPSHOT_HOLD_MAX_S == 12 * 3600

    assert (await snap_pass(inv_db, "2026-10-07T11:40:00", state)).held == "normalising"       # 11 h 25 after 00:15
    with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
        forced = await snap_pass(inv_db, "2026-10-07T12:20:00", state)                          # 12 h 5 after 00:15
    assert forced.ran and forced.held is None
    assert any("held for 12 h" in r.getMessage() for r in caplog.records)
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 1


@pytest.mark.asyncio
async def test_UP07_a_process_that_restarts_after_the_due_time_still_has_to_settle(inv_db):
    """The loop was down (an upgrade) from the evening until 14:00: the day's snapshot has been due since 00:15,
    but this process has been up for a minute. The 12-hour valve counts from the process start."""
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock = Clock(60.0)
    state = new_state(clock)
    early = await snap_pass(inv_db, "2026-10-07T14:00:00", state)
    assert (early.due, early.ran, early.held) == (True, False, "settling")
    assert snapshot_rows(inv_db, DAY7) == {}

    inv_db.execute("INSERT INTO servers (display_name) VALUES ('dirty-after-restart')")          # a backlog appears
    clock.t = 3600.0
    assert (await snap_pass(inv_db, "2026-10-07T14:59:00", state)).held == "normalising"
    clock.t = 12 * 3600.0 + 120.0                                     # 12 h after the process started
    forced = await snap_pass(inv_db, "2026-10-07T23:59:00", state)
    assert forced.ran and snapshot_rows(inv_db, DAY7)[("assets", "server")] == 2


# ════════════════════════════════════════════════════════════════════
# UP-12: pre-157 software rows of a Windows server filed under another os_type
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_UP12_pre_157_rows_of_a_windows_server_filed_under_another_os_type_are_keyed(inv_db):
    """Rows migration 157 left have no source. Only the Windows agent ever produced them, so a server whose OS name
    says Windows is a Windows machine whatever os_type an operator or a discovery recorded."""
    odd = add_server(inv_db, "odd-type", os_type="other", os_name="Microsoft Windows Server 2016 Standard")
    mislabelled = add_server(inv_db, "mislabelled", os_type="linux", os_name="Microsoft Windows 10 Pro")
    lin = add_server(inv_db, "real-linux", os_type="linux", os_name="Ubuntu 22.04.4 LTS")
    unknown = add_server(inv_db, "unknown-os", os_type="other")
    for srv in (odd, mislabelled):
        add_software(inv_db, srv, "Google Chrome", "Google LLC", source=None)
        add_software(inv_db, srv, "CrowdStrike Windows Sensor", "CrowdStrike, Inc.", source=None)
    add_software(inv_db, lin, "libssl3", "Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>", source=None)
    add_software(inv_db, unknown, "Google Chrome", "Google LLC", source=None)

    result = await tick(inv_db)

    keyed = {r[0]: r[1:] for r in inv_db.query(
        "SELECT sv.display_name || '/' || w.package_name, w.name_key, w.category FROM server_software_inventory w "
        "JOIN servers sv ON sv.id = w.server_id")}
    assert keyed["odd-type/CrowdStrike Windows Sensor"] == ("crowdstrike-falcon-sensor", "endpoint-protection")
    assert keyed["mislabelled/Google Chrome"][0] == "google-chrome"
    assert result.software == 4
    # What cannot be told is still left for the upload that carries a source (the CS-1 rule), and it is no backlog.
    assert keyed["real-linux/libssl3"] == (None, None) and keyed["unknown-os/Google Chrome"] == (None, None)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            assert (await svc.normalise_backlog(db)).software_rows == 0


# ════════════════════════════════════════════════════════════════════
# One write per day, refreshed during the first 24 hours
# ════════════════════════════════════════════════════════════════════

def settled_state():
    state = new_state(Clock(5000.0))
    state.started_at = 0.0
    return state


@pytest.mark.asyncio
async def test_UP07_outside_the_capture_window_a_day_is_written_exactly_once(inv_db):
    set_capture_start(inv_db, "2020-01-01T00:00:00Z")
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    state = settled_state()
    assert (await snap_pass(inv_db, "2026-10-07T00:20:00", state)).ran
    before = snapshot_rows(inv_db, DAY7)
    add_server(inv_db, "S2", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)

    for later in ("2026-10-07T00:25:00", "2026-10-07T09:00:00", "2026-10-07T23:50:00"):
        again = await snap_pass(inv_db, later, state)
        assert (again.due, again.ran) == (False, False), later
    assert snapshot_rows(inv_db, DAY7) == before and before[("assets", "server")] == 1


@pytest.mark.asyncio
async def test_UP07_during_the_first_24_hours_the_current_day_is_refreshed_not_duplicated(inv_db):
    set_capture_start(inv_db, "2026-10-06T03:00:00Z")                  # the window ends 2026-10-07 03:00
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock = Clock(5000.0)
    state = new_state(clock); state.started_at = 0.0

    first = await snap_pass(inv_db, "2026-10-07T00:20:00", state)
    assert (first.ran, first.refreshed) == (True, False)
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 1
    add_server(inv_db, "S2", os_name="Ubuntu 22.04.4 LTS")             # a server arrives (or reports) after the first reading
    await tick(inv_db)

    clock.t += 1800                                                    # not yet: a refresh is hourly at most
    assert (await snap_pass(inv_db, "2026-10-07T00:50:00", state)).due is False
    clock.t += 1900
    refreshed = await snap_pass(inv_db, "2026-10-07T01:30:00", state)
    assert (refreshed.ran, refreshed.refreshed, refreshed.held) == (True, True, None)
    rows = snapshot_rows(inv_db, DAY7)
    assert rows[("assets", "server")] == 2
    assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots WHERE day = $1 AND metric = 'assets'",
                         DAY7) == 2                            # replaced, never doubled

    add_server(inv_db, "S3", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock.t += 3700
    assert (await snap_pass(inv_db, "2026-10-07T02:40:00", state)).refreshed          # inside the window
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 3
    add_server(inv_db, "S4", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock.t += 3700
    assert (await snap_pass(inv_db, "2026-10-07T03:30:00", state)).due is False       # the window is over
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 3


@pytest.mark.asyncio
async def test_UP07_a_closed_day_is_never_rewritten(inv_db):
    set_capture_start(inv_db, "2026-10-07T12:00:00Z")                  # the window runs into the 8th
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock = Clock(5000.0)
    state = new_state(clock); state.started_at = 0.0
    assert (await snap_pass(inv_db, "2026-10-07T12:30:00", state)).ran
    closed = snapshot_rows(inv_db, DAY7)
    add_server(inv_db, "S2", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)

    clock.t += 40_000
    new_day = await snap_pass(inv_db, "2026-10-08T00:20:00", state)       # the 8th's first write
    assert (new_day.ran, new_day.refreshed) == (True, False)
    clock.t += 3700
    again = await snap_pass(inv_db, "2026-10-08T01:30:00", state)         # a refresh of the 8th, inside the window
    assert (again.ran, again.refreshed) == (True, True)
    assert snapshot_rows(inv_db, DAY7) == closed                  # the 7th stays as it closed
    assert snapshot_rows(inv_db, date(2026, 10, 8))[("assets", "server")] == 2


@pytest.mark.asyncio
async def test_UP07_a_refresh_does_not_run_while_the_estate_is_still_normalising(inv_db):
    set_capture_start(inv_db, "2026-10-06T23:00:00Z")
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")
    await tick(inv_db)
    clock = Clock(5000.0)
    state = new_state(clock); state.started_at = 0.0
    assert (await snap_pass(inv_db, "2026-10-07T00:20:00", state)).ran
    add_server(inv_db, "S2", os_name="Ubuntu 22.04.4 LTS")                # dirty identity, not ticked
    clock.t += 3700
    held = await snap_pass(inv_db, "2026-10-07T01:30:00", state)
    assert (held.due, held.ran, held.held) == (True, False, "normalising")
    assert snapshot_rows(inv_db, DAY7)[("assets", "server")] == 1


@pytest.mark.asyncio
async def test_UP07_a_direct_call_without_a_state_keeps_the_plain_behaviour(inv_db):
    set_capture_start(inv_db, "2026-10-06T23:00:00Z")                  # inside the window, but there is no loop state
    add_server(inv_db, "S1", os_name="Ubuntu 22.04.4 LTS")            # dirty: a state would hold on it
    assert (await snap_pass(inv_db, "2026-10-07T00:20:00")).ran
    assert (await snap_pass(inv_db, "2026-10-07T01:30:00")).due is False


@pytest.mark.asyncio
async def test_UP07_the_capture_start_is_read_from_the_setting(inv_db):
    async with sessions(inv_db) as factory:
        async with factory() as db:
            set_capture_start(inv_db, "2026-10-07T08:30:00Z")
            assert await svc.get_capture_started_at(db) == at("2026-10-07T08:30:00")
            await db.rollback()
            inv_db.execute("UPDATE system_settings SET value = to_jsonb('not a date'::text) WHERE key = 'inventory.capture_started_at'")
            assert await svc.get_capture_started_at(db) is None
            await db.rollback()
            inv_db.execute("UPDATE system_settings SET value = '42'::jsonb WHERE key = 'inventory.capture_started_at'")
            assert await svc.get_capture_started_at(db) is None
            await db.rollback()
            inv_db.execute("DELETE FROM system_settings WHERE key = 'inventory.capture_started_at'")
            assert await svc.get_capture_started_at(db) is None


# ════════════════════════════════════════════════════════════════════
# The health series answers the live Overview's question
# ════════════════════════════════════════════════════════════════════

def test_UP07_the_endpoint_protection_constants_match_the_query_module():
    assert svc.ENDPOINT_PROTECTION_CATEGORY == iq.ENDPOINT_PROTECTION_CATEGORY
    assert tuple(svc.ENDPOINT_PROTECTION_SERVICES) == tuple(iq.ENDPOINT_PROTECTION_SERVICES)
    assert svc.CAPTURE_KEY == iq.CAPTURE_KEY
    assert set(svc.SNAPSHOT_HEALTH_KEYS) == set(iq.HEALTH_KEYS)
    assert set(svc.SNAPSHOT_COVERAGE_KEYS) == set(iq.FRESHNESS)


def mixed_estate(db):
    """Servers in every state the predicates tell apart, and devices that can and cannot report."""
    now_ago = lambda h: f"now() - interval '{h} hours'"             # noqa: E731
    s = SimpleNamespace()
    s.protected = add_server(db, "protected", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                             kernel_or_build="10.0.20348.587 Build 20348.587")
    s.unprotected = add_server(db, "unprotected", os_type="windows", os_name="Microsoft Windows Server 2019 Standard",
                               kernel_or_build="10.0.17763.5696 Build 17763.5696")
    s.truncated = add_server(db, "truncated", os_type="windows", os_name="Microsoft Windows Server 2019 Standard",
                             kernel_or_build="10.0.17763.5696 Build 17763.5696", software_truncated=True)
    s.defender = add_server(db, "defender", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                            kernel_or_build="10.0.20348.587 Build 20348.587")
    s.never = add_server(db, "never", os_type="windows", os_name="Microsoft Windows Server 2022 Standard")
    s.agentless = add_server(db, "agentless", os_type="windows", collection_mode="none")
    s.pending = add_server(db, "pending", os_type="windows", os_name="Microsoft Windows Server 2019 Standard",
                           kernel_or_build="10.0.17763.5696 Build 17763.5696")
    s.stale = add_server(db, "stale", os_type="linux", os_name="Ubuntu 22.04.4 LTS")
    # X9: reports software but never a service list (the APM-only agent profile): Defender cannot be judged
    s.apm_only = add_server(db, "apm-only", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                            kernel_or_build="10.0.20348.587 Build 20348.587")
    # X10: unkeyed rows from before migration 157 on a server that is not a Windows machine; the loop never selects them
    s.stuck = add_server(db, "stuck", os_type="other")
    for sid in (s.protected, s.unprotected, s.truncated, s.defender, s.pending, s.apm_only, s.stuck):
        db.execute("UPDATE servers SET inventory_baselined_at = now() WHERE id = $1", sid)
        db.execute(f"UPDATE servers SET inventory_updated_at = {now_ago(1)} WHERE id = $1", sid)
    db.execute(f"UPDATE servers SET inventory_updated_at = {now_ago(24 * 10)} WHERE id = $1", s.stale)
    add_software(db, s.protected, "CrowdStrike Windows Sensor", "CrowdStrike, Inc.")
    add_software(db, s.unprotected, "ZenPlus Agent", "Zentryc LLC")
    add_software(db, s.unprotected, "Google Chrome", "Google LLC")
    add_software(db, s.truncated, "Google Chrome", "Google LLC")
    add_software(db, s.defender, "7-Zip 23.01 (x64)", "Igor Pavlov")
    add_software(db, s.pending, "Google Chrome", "Google LLC")
    add_software(db, s.apm_only, "Google Chrome", "Google LLC")
    add_software(db, s.stuck, "Legacy Tool", "Legacy Ltd", source=None)
    db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, 'WinDefend', 'Running')",
               s.defender)
    for sid in (s.protected, s.unprotected, s.truncated, s.pending, s.stuck):         # a service list without Defender
        db.execute("INSERT INTO server_service_inventory (server_id, service_name, state) VALUES ($1, 'W32Time', 'running')", sid)
    d = SimpleNamespace()
    d.full = add_device(db, "full", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5", serial_number="FGT-1",
                        snmp_enabled=True)
    d.sparse = add_device(db, "sparse", snmp_enabled=True)                       # SNMP on, never polled, nothing known
    d.ping = add_device(db, "ping-only", snmp_enabled=False)                      # cannot report by design
    d.child = add_device(db, "controller-child", snmp_enabled=False, poll_mode="via_controller")
    d.aging = add_device(db, "aging", vendor="Cisco", model="C9300-24T", os_version="17.09.04a", snmp_enabled=True)
    db.execute(f"UPDATE devices SET identity_updated_at = {now_ago(1)} WHERE id = $1", d.full)
    db.execute(f"UPDATE devices SET identity_updated_at = {now_ago(24 * 3)} WHERE id = $1", d.aging)
    return s, d


@pytest.mark.asyncio
async def test_UP07_the_health_and_coverage_series_equal_the_live_overview(inv_db):
    s, d = mixed_estate(inv_db)
    await tick(inv_db)
    # The pending server still has a row the normaliser has not keyed: its protection is unknown, not missing.
    inv_db.execute("UPDATE server_software_inventory SET norm_rev = NULL WHERE server_id = $1", s.pending)

    async with sessions(inv_db) as factory:
        async with factory() as db:
            live = await iq.summary(db, scope=None, use_cache=False, now=datetime.now(UTC))
        async with factory() as db:
            rows = await svc.snapshot_day(db, at("2026-10-07T12:00:00").date(), zone=UTC)

    for key in svc.SNAPSHOT_HEALTH_KEYS:
        assert rows[("health", key)] == live["health"][key], key
    for key in svc.SNAPSHOT_COVERAGE_KEYS:
        assert rows[("coverage", key)] == live["coverage"][key], key
    # Not vacuous: every predicate is exercised on this estate.
    got = {k: v for (m, k), v in rows.items() if m == "health"}
    assert got["missing_security_agent"] == 1                 # only 'unprotected' (ZenPlus Agent and Chrome are no protection)
    # X9 / X10 in the series as in the live panel: no service list (apm-only) and unkeyable rows (stuck) are unknown, not missing
    assert live["software"]["security_agent_coverage"] == {"covered": 2, "missing": 1, "unknown": 7, "total": 10}
    assert got["software_truncated"] == 1
    assert got["never"] == 2 and got["stale"] == 1            # 'never' and the sparse device; the agentless server is by design
    assert got["missing_model"] == 1 and got["missing_serial"] == 2 and got["unknown_vendor"] == 1   # devices only, reporting only
    assert rows[("coverage", "not_applicable")] == 3          # agentless server, ping-only device, controller child
    assert rows[("coverage", "never")] == 2


@pytest.mark.asyncio
async def test_UP07_servers_never_count_as_missing_a_model_serial_or_vendor(inv_db):
    """The old series counted every server (117 / 125 / 108 on the upgrade fixture) against 33 / 41 / 24 live."""
    for i in range(5):
        add_server(inv_db, f"srv{i}", os_type="windows", os_name="Microsoft Windows Server 2022 Standard")
    await tick(inv_db)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            rows = await svc.snapshot_day(db, at("2026-10-07T12:00:00").date(), zone=UTC)
    assert (rows[("health", "missing_model")], rows[("health", "missing_serial")], rows[("health", "unknown_vendor")]) == (0, 0, 0)
    assert rows[("health", "never")] == 5 and rows[("coverage", "never")] == 5


@pytest.mark.asyncio
async def test_UP07_the_entity_counts_use_the_same_blank_rules_as_the_live_panel(inv_db):
    """An identity column holding '' is missing, as it is for the Assets list."""
    dev = add_device(inv_db, "blank", snmp_enabled=True, vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    inv_db.execute("UPDATE devices SET identity_updated_at = now() WHERE id = $1", dev)
    await tick(inv_db)
    inv_db.execute("UPDATE inventory_identity SET hw_model = '', hw_serial = '', os_version = '' "
                   "WHERE asset_kind = 'device' AND asset_id = $1", dev)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            live = await iq.summary(db, scope=None, use_cache=False, now=datetime.now(UTC))
            rows = await svc.snapshot_day(db, at("2026-10-07T12:00:00").date(), zone=UTC)
    for key in ("missing_os_version", "missing_model", "missing_serial"):
        assert rows[("health", key)] == live["health"][key] == 1, key
