"""Tests for app/services/inventory_service.py (Inventory phase 0, task 0.5).

Pure unit tests (settings, snapshot time arithmetic, failed-row back-off, the
loop wrappers, the shutdown list) need nothing. Everything that touches SQL runs
the real service functions against a database on the private cluster from
``pg_fixture`` (never the appliance database) and skips where PostgreSQL
binaries are absent.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import itertools
import logging
import random
import re
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import inventory_normalize
from app.services import inventory_service as svc
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures are used by name)

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
UTC = timezone.utc


# ════════════════════════════════════════════════════════════════════
# Settings
# ════════════════════════════════════════════════════════════════════

def test_settings_defaults_match_the_design():
    assert svc.merge_inventory_settings(None) == {
        "fresh_hours": {"server": 24, "device": 2},
        "stale_days": 7,
        "change_retention_days": 400,
        "snapshot_hour": 0,
        "hide_categories_by_default": ["update", "driver", "os-component"],
        "trends_enabled": True,
    }


@pytest.mark.parametrize("raw", [[1, 2], "not json", 42, True, "[1]", b"\xff\xfe", {"unrelated": 1}])
def test_settings_malformed_blob_falls_back_to_defaults(raw):
    assert svc.merge_inventory_settings(raw) == svc.DEFAULT_SETTINGS


def test_settings_json_text_is_parsed():
    assert svc.merge_inventory_settings('{"stale_days": 14}')["stale_days"] == 14


def test_settings_partial_blob_merges_over_defaults():
    out = svc.merge_inventory_settings({"fresh_hours": {"server": 48}, "snapshot_hour": 3})
    assert out["fresh_hours"] == {"server": 48, "device": 2}
    assert out["snapshot_hour"] == 3
    assert out["stale_days"] == 7 and out["change_retention_days"] == 400


@pytest.mark.parametrize("field,value,expected", [
    ("stale_days", 0, 1), ("stale_days", -4, 1), ("stale_days", 9999, 365), ("stale_days", 3.6, 4),
    ("change_retention_days", 1, 30), ("change_retention_days", 10**9, 3650),
    ("snapshot_hour", 24, 23), ("snapshot_hour", -1, 0), ("snapshot_hour", 5.4, 5),
])
def test_settings_scalars_are_clamped(field, value, expected):
    assert svc.merge_inventory_settings({field: value})[field] == expected


@pytest.mark.parametrize("value", [True, False, None, "7", "abc", float("nan"), float("inf"), [], {}])
def test_settings_scalar_of_the_wrong_type_keeps_the_default(value):
    out = svc.merge_inventory_settings({"stale_days": value, "change_retention_days": value,
                                        "snapshot_hour": value, "fresh_hours": {"server": value, "device": value}})
    assert out == svc.DEFAULT_SETTINGS


def test_settings_one_bad_field_does_not_discard_the_others():
    out = svc.merge_inventory_settings({"stale_days": "x", "snapshot_hour": 6, "change_retention_days": 90})
    assert (out["stale_days"], out["snapshot_hour"], out["change_retention_days"]) == (7, 6, 90)


def test_settings_fresh_hours_clamped_and_capped_by_stale_days():
    assert svc.merge_inventory_settings({"fresh_hours": {"server": 0, "device": 10**6}})["fresh_hours"] == {
        "server": 1, "device": 168}  # 720 h, then capped at the default 7 days
    out = svc.merge_inventory_settings({"stale_days": 2, "fresh_hours": {"server": 500, "device": 30}})
    assert out["fresh_hours"] == {"server": 48, "device": 30}  # buckets stay ordered
    assert svc.merge_inventory_settings({"fresh_hours": "nope"})["fresh_hours"] == {"server": 24, "device": 2}


def test_settings_hidden_categories_are_validated():
    merge = lambda v: svc.merge_inventory_settings({"hide_categories_by_default": v})["hide_categories_by_default"]  # noqa: E731
    assert merge(["driver", "bogus", "driver", 7, "update"]) == ["driver", "update"]
    assert merge([]) == []                                  # hide nothing is a valid choice
    assert merge(["bogus", "also-bogus"]) == ["update", "driver", "os-component"]   # a mistake, not a choice
    assert merge("update") == ["update", "driver", "os-component"]
    assert merge(None) == ["update", "driver", "os-component"]


def test_settings_trends_enabled_must_be_a_bool():
    merge = lambda v: svc.merge_inventory_settings({"trends_enabled": v})["trends_enabled"]  # noqa: E731
    assert merge(False) is False
    assert merge("false") is True and merge(0) is True and merge(None) is True


def test_settings_result_is_independent_of_the_defaults():
    out = svc.merge_inventory_settings(None)
    out["fresh_hours"]["server"] = 1
    out["hide_categories_by_default"].append("x")
    assert svc.DEFAULT_SETTINGS["fresh_hours"]["server"] == 24
    assert svc.DEFAULT_SETTINGS["hide_categories_by_default"] == ["update", "driver", "os-component"]
    assert "x" not in svc.merge_inventory_settings(None)["hide_categories_by_default"]


def test_get_inventory_settings_reads_the_inventory_key():
    seen = {}

    class Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class Db:
        def __init__(self, row):
            self.row = row

        async def execute(self, statement, params):
            seen["sql"], seen["params"] = str(statement), params
            return Result(self.row)

    got = asyncio.run(svc.get_inventory_settings(Db(({"stale_days": 3},))))
    assert got["stale_days"] == 3 and got["snapshot_hour"] == 0
    assert seen["params"] == {"k": "inventory"} and "system_settings" in seen["sql"]
    assert asyncio.run(svc.get_inventory_settings(Db(None))) == svc.DEFAULT_SETTINGS


# ════════════════════════════════════════════════════════════════════
# Snapshot time arithmetic
# ════════════════════════════════════════════════════════════════════

def at(text_utc: str) -> datetime:
    return datetime.fromisoformat(text_utc).replace(tzinfo=UTC)


@pytest.mark.parametrize("now,hour,have_today,due", [
    ("2026-10-07T00:14:59", 0, False, False),     # one second before 00:15
    ("2026-10-07T00:15:00", 0, False, True),
    ("2026-10-07T12:00:00", 0, False, True),      # any later time of day while the row is missing
    ("2026-10-07T23:59:59", 0, False, True),
    ("2026-10-07T00:20:00", 0, True, False),      # already done today
    ("2026-10-07T02:59:00", 3, False, False),     # configured hour
    ("2026-10-07T03:14:00", 3, False, False),
    ("2026-10-07T03:15:00", 3, False, True),
    ("2026-10-07T03:16:00", 3, True, False),
    ("2026-10-07T23:14:00", 23, False, False),
    ("2026-10-07T23:15:00", 23, False, True),
])
def test_snapshot_due_in_utc(now, hour, have_today, due):
    assert svc.snapshot_due(at(now), UTC, hour, have_today=have_today) is due


def test_snapshot_due_catches_up_after_downtime():
    # The appliance was off through 00:15 and comes back mid-afternoon with no row yet.
    assert svc.snapshot_due(at("2026-10-07T15:40:00"), UTC, 0, have_today=False)
    # Yesterday's row does not count: have_today is about the local calendar day only.
    assert not svc.snapshot_due(at("2026-10-07T00:05:00"), UTC, 0, have_today=False)


def test_snapshot_due_uses_the_local_clock_and_date():
    karachi = ZoneInfo("Asia/Karachi")           # UTC+5, no DST
    # 19:00Z on the 7th is already midnight on the 8th in Karachi: not yet 00:15.
    assert not svc.snapshot_due(at("2026-10-07T19:00:00"), karachi, 0, have_today=False)
    assert svc.snapshot_local_day(at("2026-10-07T19:00:00"), karachi) == date(2026, 10, 8)
    assert svc.snapshot_due(at("2026-10-07T19:15:00"), karachi, 0, have_today=False)
    # 06:30Z on the 8th is still the evening of the 7th in Los Angeles (UTC-7).
    la = ZoneInfo("America/Los_Angeles")
    assert svc.snapshot_local_day(at("2026-10-08T06:30:00"), la) == date(2026, 10, 7)
    assert svc.snapshot_local_day(at("2026-10-08T07:00:00"), la) == date(2026, 10, 8)
    # The same instant is due or not depending on the zone's wall clock: 23:30 in UTC,
    # but 12:30 on the 8th in Auckland (UTC+13 in October).
    assert svc.snapshot_due(at("2026-10-07T23:30:00"), UTC, 23, have_today=False)
    assert not svc.snapshot_due(at("2026-10-07T23:30:00"), ZoneInfo("Pacific/Auckland"), 23, have_today=False)


def test_snapshot_due_across_dst_changes():
    ny = ZoneInfo("America/New_York")
    # Spring forward 2026-03-08: 02:15 does not exist. The first wake after the gap runs it.
    assert not svc.snapshot_due(at("2026-03-08T06:59:00"), ny, 2, have_today=False)   # 01:59 EST
    assert svc.snapshot_due(at("2026-03-08T07:00:00"), ny, 2, have_today=False)       # 03:00 EDT
    # Fall back 2026-11-01: 01:30 happens twice; both are due and the row stops the second.
    assert svc.snapshot_due(at("2026-11-01T05:30:00"), ny, 1, have_today=False)       # 01:30 EDT
    assert svc.snapshot_due(at("2026-11-01T06:30:00"), ny, 1, have_today=False)       # 01:30 EST
    assert not svc.snapshot_due(at("2026-11-01T06:30:00"), ny, 1, have_today=True)


def test_day_bounds_follow_the_local_calendar_day():
    assert svc.day_bounds(date(2026, 10, 7), UTC) == (at("2026-10-07T00:00:00"), at("2026-10-08T00:00:00"))
    karachi = ZoneInfo("Asia/Karachi")
    assert svc.day_bounds(date(2026, 10, 7), karachi) == (at("2026-10-06T19:00:00"), at("2026-10-07T19:00:00"))
    ny = ZoneInfo("America/New_York")
    start, end = svc.day_bounds(date(2026, 3, 8), ny)       # 23-hour day
    assert (start, end) == (at("2026-03-08T05:00:00"), at("2026-03-09T04:00:00"))
    start, end = svc.day_bounds(date(2026, 11, 1), ny)      # 25-hour day
    assert (start, end) == (at("2026-11-01T04:00:00"), at("2026-11-02T05:00:00"))


def _fake_settings_db(value):
    """Just enough of a session for get_configured_timezone: one row, or none."""
    class Result:
        def first(self):
            return None if value is None else (value,)

    class Db:
        async def execute(self, statement, params=None):
            return Result()

    return Db()


def _offsets(zone):
    """UTC offsets of a zone in summer and winter, which tells UTC, Karachi and New York apart."""
    return tuple(at(t).astimezone(zone).utcoffset() for t in ("2026-07-01T12:00:00", "2026-01-01T12:00:00"))


@pytest.mark.parametrize("company,expected", [
    ({"timezone": "Asia/Karachi"}, ZoneInfo("Asia/Karachi")),
    ({"timezone": "America/New_York", "base_url": "https://x"}, ZoneInfo("America/New_York")),
    ({"timezone": ""}, UTC),                      # the setting exists but is blank
    ({"timezone": "Not/AZone"}, UTC),             # a name this host does not know
    ({"timezone": "../etc/passwd"}, UTC),
    ({}, UTC),
    (None, UTC),                                  # no company row at all: the product default
])
def test_appliance_zone_is_the_configured_timezone_with_a_utc_fallback(company, expected):
    got = asyncio.run(svc.get_appliance_zone(_fake_settings_db(company)))
    assert _offsets(got) == _offsets(expected)


def test_appliance_zone_never_shells_out_to_timedatectl(monkeypatch):
    from app.api.v1 import system_time

    def boom(timeout=2.0):
        raise AssertionError("the snapshot zone must come from the company setting")

    monkeypatch.setattr(system_time, "read_timedatectl", boom)
    assert asyncio.run(svc.get_appliance_zone(_fake_settings_db({"timezone": "Asia/Karachi"}))) == ZoneInfo("Asia/Karachi")


# ════════════════════════════════════════════════════════════════════
# Failed-row back-off
# ════════════════════════════════════════════════════════════════════

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_failure_log_backs_off_for_an_hour_then_retries():
    clock = FakeClock()
    log = svc.FailureLog(backoff_s=3600, clock=clock)
    assert log.record("a") is True
    assert log.record("a") is False            # same episode: do not log again
    assert log.active() == ["a"] and "a" in log and len(log) == 1
    clock.now += 3599
    assert log.active() == ["a"]
    clock.now += 2
    assert log.active() == [] and "a" not in log
    assert log.record("a") is True             # a new episode after the back-off


def test_failure_log_clear_and_bound():
    clock = FakeClock()
    log = svc.FailureLog(backoff_s=3600, max_entries=3, clock=clock)
    for key in "abcd":
        clock.now += 1
        log.record(key)
    assert sorted(log.active()) == ["b", "c", "d"]    # oldest evicted
    log.clear("c")
    log.clear("missing")
    assert sorted(log.active()) == ["b", "d"]


# ════════════════════════════════════════════════════════════════════
# Loop wrappers (one iteration, injected delays) and the shutdown list
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("loop_name,message", [
    ("inventory_normalize_loop", "inventory normalise tick failed"),
    ("inventory_snapshot_loop", "inventory snapshot pass failed"),
])
@pytest.mark.asyncio
async def test_loops_survive_failures_and_keep_ticking(loop_name, message, caplog):
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    task = asyncio.create_task(getattr(svc, loop_name)(start_delay=0, interval=0.01, tick=tick))
    with caplog.at_level(logging.ERROR, logger="zenplus.inventory_service"):
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 3, "the loop stopped after the first failure"
        assert not task.done()
    assert any(message in r.getMessage() and r.exc_info for r in caplog.records)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


@pytest.mark.parametrize("loop_name", ["inventory_normalize_loop", "inventory_snapshot_loop"])
@pytest.mark.asyncio
async def test_loops_reraise_cancellation(loop_name):
    started = asyncio.Event()

    async def slow_tick():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(getattr(svc, loop_name)(start_delay=0, interval=60, tick=slow_tick))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    async def cancelled_tick():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(getattr(svc, loop_name)(start_delay=0, interval=0, tick=cancelled_tick), 5)

    # Cancelled during the start delay, before the first tick.
    task = asyncio.create_task(getattr(svc, loop_name)(start_delay=60, interval=60, tick=slow_tick))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


def test_loop_defaults_match_the_design():
    assert svc.NORMALIZE_START_DELAY_S == 45 and svc.NORMALIZE_INTERVAL_S == 120
    assert svc.NORMALIZE_BATCH == 500 and svc.SOFTWARE_BATCH == 2000 and svc.ORPHAN_BATCH == 1000
    assert svc.SNAPSHOT_POLL_S == 300 and svc.SNAPSHOT_MINUTE == 15 and svc.PURGE_BATCH == 10_000
    assert (svc.NORMALIZE_LOCK_KEY, svc.SNAPSHOT_LOCK_KEY) == (1515074397, 1515074398)


# Tasks that main.py starts but has never listed for cancellation. Both predate the
# inventory loops. The test fails when one of them is fixed, so the entry gets removed.
KNOWN_NOT_STOPPED_AT_SHUTDOWN = {"alert_escalation_sweeper", "agent_package_sync"}


def _main_tasks() -> tuple[dict[str, int], set[str]]:
    tree = ast.parse((SERVER_ROOT / "app" / "main.py").read_text())
    started: dict[str, int] = {}
    stopped: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, call = node.targets[0], node.value
            on_app_state = (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute)
                            and target.value.attr == "state" and isinstance(target.value.value, ast.Name)
                            and target.value.value.id == "app")
            if on_app_state and isinstance(call, ast.Call):
                callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                if callee == "create_task":
                    started[target.attr] = node.lineno
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "attr"
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            stopped |= {e.value for e in node.iter.elts if isinstance(e, ast.Constant)}
    return started, stopped


def test_inventory_loops_are_started_and_stopped_in_main():
    started, stopped = _main_tasks()
    for attr in ("inventory_normalize", "inventory_snapshot"):
        assert attr in started, f"main.py does not start app.state.{attr}"
        assert attr in stopped, f"app.state.{attr} is not in the shutdown tuple"


def test_every_started_background_task_is_in_the_shutdown_tuple():
    started, stopped = _main_tasks()
    assert len(started) > 15, "the parser stopped recognising app.state.<name> = asyncio.create_task(...)"
    leaked = sorted(set(started) - stopped - KNOWN_NOT_STOPPED_AT_SHUTDOWN)
    assert not leaked, f"started in startup but never cancelled in _stop_background_tasks: {leaked}"
    fixed = sorted(KNOWN_NOT_STOPPED_AT_SHUTDOWN & stopped)
    assert not fixed, f"now stopped at shutdown, drop them from KNOWN_NOT_STOPPED_AT_SHUTDOWN: {fixed}"
    typos = sorted(stopped - set(started))
    assert not typos, f"cancelled at shutdown but never started: {typos}"


def test_advisory_lock_keys_are_used_by_nothing_else():
    keys = (str(svc.NORMALIZE_LOCK_KEY), str(svc.SNAPSHOT_LOCK_KEY))
    own = {Path(svc.__file__).resolve(), Path(__file__).resolve()}
    hits = []
    # Production code only: a test or a doc may mention a key without using it.
    for root, patterns in ((SERVER_ROOT / "app", ("*.py",)), (REPO_ROOT / "scripts", ("*.sql", "*.py", "*.sh")),
                           (REPO_ROOT / "poller", ("*.go",))):
        for pattern in patterns:
            for path in root.rglob(pattern):
                if path.resolve() in own or "__pycache__" in path.parts:
                    continue
                body = path.read_text(errors="ignore")
                hits += [f"{path.relative_to(REPO_ROOT)}: {k}" for k in keys if k in body]
    assert not hits, hits


# ════════════════════════════════════════════════════════════════════
# SQL fixtures and helpers
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def inv_db(pg_db):
    """A fresh database without the demo devices that the migration set seeds: their
    identity rows would otherwise be counted by every assertion below."""
    pg_db.execute("DELETE FROM servers; DELETE FROM devices")
    return pg_db


_seq = itertools.count(1)


def add_server(db, name=None, **cols):
    names = ["display_name", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return db.scalar(f"INSERT INTO servers ({', '.join(names)}) VALUES ({marks}) RETURNING id",
                     name or f"srv-{next(_seq)}", *cols.values())


def add_device(db, hostname=None, **cols):
    n = next(_seq)
    cols.setdefault("ip_address", f"10.88.{n // 250}.{n % 250 + 1}")
    names = ["hostname", *cols]
    marks = ", ".join(f"${i + 1}" + ("::inet" if name == "ip_address" else "") for i, name in enumerate(names))
    return db.scalar(f"INSERT INTO devices ({', '.join(names)}) VALUES ({marks}) RETURNING id",
                     hostname or f"dev-{n}", *cols.values())


def add_software(db, server_id, package, version="1.0", vendor=None, source="hklm"):
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source) "
               "VALUES ($1, $2, $3, $4, $5)", server_id, package, version, vendor, source)


def baseline(db, server_id):
    db.execute("UPDATE servers SET inventory_baselined_at = now() WHERE id = $1", server_id)


def set_template(db, device_id, key, value, age_hours=0, group="system"):
    db.execute(
        "INSERT INTO device_template_values (device_id, group_key, metric_key, series_key, value_text, updated_at) "
        "VALUES ($1, $2, $3, $3, $4, now() - make_interval(hours => $5)) "
        "ON CONFLICT (device_id, group_key, metric_key, instance) DO UPDATE "
        "SET value_text = EXCLUDED.value_text, updated_at = EXCLUDED.updated_at",
        device_id, group, key, value, age_hours)


def add_chassis(db, device_id, ent_index, *, parent_index=0, cls="chassis", serial=None, model=None, firmware=None):
    db.execute("INSERT INTO device_entities (device_id, ent_index, parent_index, class, name, serial_number, "
               "model_name, fw_revision) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
               device_id, ent_index, parent_index, cls, f"ent-{ent_index}", serial, model, firmware)


IDENT_COLS = ("vendor_key", "os_family", "os_product", "os_release", "os_version", "os_build", "os_source",
              "hw_vendor", "hw_model", "hw_serial", "hw_source", "lifecycle_slug", "norm_rev")


def ident(db, kind, asset_id):
    rows = db.query(f"SELECT {', '.join(IDENT_COLS)} FROM inventory_identity WHERE asset_kind = $1 AND asset_id = $2",
                    kind, asset_id)
    assert rows, f"no identity row for {kind} {asset_id}"
    return dict(zip(IDENT_COLS, rows[0]))


def is_dirty(db, kind, asset_id):
    return db.scalar("SELECT norm_rev IS NULL FROM inventory_identity WHERE asset_kind = $1 AND asset_id = $2",
                     kind, asset_id)


def values_rev(db, kind, asset_id):
    return db.scalar("SELECT values_rev FROM inventory_identity WHERE asset_kind = $1 AND asset_id = $2", kind, asset_id)


def age_template(db, device_id, hours):
    """Make a device's template values ``hours`` old without changing value_text, so the row is not
    dirtied (the dirty-marking trigger watches value_text; the poller's refresh does the same)."""
    db.execute("UPDATE device_template_values SET updated_at = now() - make_interval(hours => $2) "
               "WHERE device_id = $1", device_id, hours)


def events(db):
    cols = ("category", "action", "subject", "old_value", "new_value", "asset_kind", "asset_name")
    return [dict(zip(cols, r)) for r in db.query(
        f"SELECT {', '.join(cols)} FROM inventory_change_events ORDER BY id")]


def event_count(db):
    return db.scalar("SELECT count(*) FROM inventory_change_events")


def snapshot_rows(db, day):
    return {(m, d): v for m, d, v in db.query(
        "SELECT metric, dimension, value FROM inventory_daily_snapshots WHERE day = $1", day)}


@contextlib.asynccontextmanager
async def sessions(db, user=None):
    engine = create_async_engine(db.url if user is None else db.url_for(user), poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def tick(db, state=None, user=None, **kw):
    async with sessions(db, user) as factory:
        return await svc.run_normalise_tick(factory, state=state or svc.TickState(), **kw)


@contextlib.asynccontextmanager
async def holding_lock(db, key):
    """Another worker mid-batch: an open transaction that holds the advisory lock."""
    import asyncpg

    conn = await asyncpg.connect(db.url.replace("postgresql+asyncpg://", "postgresql://"))
    txn = conn.transaction()
    await txn.start()
    try:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", key)
        yield
    finally:
        await txn.rollback()
        await conn.close()


@contextlib.asynccontextmanager
async def holding_rows(db, sql, *args, commit=False):
    """Another transaction mid-write (an agent upload, the poller): it runs ``sql`` and keeps
    the row locks that took until the block ends, then rolls back (or commits)."""
    import asyncpg

    conn = await asyncpg.connect(db.url.replace("postgresql+asyncpg://", "postgresql://"))
    txn = conn.transaction()
    await txn.start()
    try:
        await conn.execute(sql, *args)
        yield conn
    finally:
        await (txn.commit() if commit else txn.rollback())
        await conn.close()


def fleet(db):
    """A small mixed fleet, identity rows left dirty (as the triggers leave them)."""
    s_win = add_server(db, "win-1", os_name="Microsoft Windows Server 2022 Standard Evaluation",
                       os_version="21H2", kernel_or_build="10.0.20348.587 Build 20348.587", architecture="x86_64")
    s_lin = add_server(db, "lin-1", os_name="Ubuntu 22.04.4 LTS", os_version="22.04")
    d_fgt = add_device(db, "fgt-1", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    d_cis = add_device(db, "cis-1", vendor="Cisco", model="C9300-24T", os_version="17.09.04a")
    d_bare = add_device(db, "bare-1")
    return SimpleNamespace(s_win=s_win, s_lin=s_lin, d_fgt=d_fgt, d_cis=d_cis, d_bare=d_bare)


# ════════════════════════════════════════════════════════════════════
# Identity normalisation (database)
# ════════════════════════════════════════════════════════════════════

def test_identity_columns_written_are_exactly_the_tables_data_columns(inv_db):
    cols = {r[0] for r in inv_db.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'inventory_identity'")}
    technical = {"asset_kind", "asset_id", "norm_rev", "values_rev", "source_changed_at", "normalized_at"}
    # lifecycle_slug is the one data column the loop must not write: the lifecycle phase owns it.
    assert cols - technical - {"lifecycle_slug"} == set(svc._IDENTITY_COLUMNS)
    assert "lifecycle_slug" in cols and "lifecycle_slug" not in svc._IDENTITY_COLUMNS
    assert set(svc._OS_BLOCK) <= set(svc._IDENTITY_COLUMNS)
    assert "lifecycle_slug" not in svc._WRITE_IDENTITY


@pytest.mark.asyncio
async def test_assets_without_an_identity_row_get_one_and_are_normalised(inv_db):
    f = fleet(inv_db)
    inv_db.execute("DELETE FROM inventory_identity")          # as if the seed and triggers never ran
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity") == 0

    result = await tick(inv_db)

    assert result.created == 5 and result.dirty == 5 and result.locked
    rev = inventory_normalize.RULEPACK_REV
    win = ident(inv_db, "server", f.s_win)
    assert (win["os_family"], win["os_product"], win["os_release"], win["os_version"], win["os_source"]) == (
        "windows", "Windows Server", "2022", "10.0.20348.587", "agent")
    assert win["norm_rev"] == rev
    fgt = ident(inv_db, "device", f.d_fgt)
    assert (fgt["vendor_key"], fgt["os_family"], fgt["os_version"], fgt["hw_model"], fgt["hw_source"]) == (
        "fortinet", "fortios", "7.2.5", "FortiGate-60F", "device")
    assert ident(inv_db, "device", f.d_bare)["os_family"] == "unknown"
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS DISTINCT FROM $1", rev) == 0
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE normalized_at IS NULL") == 0
    assert event_count(inv_db) == 0                           # NULL -> value is discovery


@pytest.mark.asyncio
async def test_normalisation_runs_in_a_worker_thread(inv_db, monkeypatch):
    fleet(inv_db)
    seen = set()
    real = inventory_normalize.normalize_server

    def spy(*a, **k):
        seen.add(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(inventory_normalize, "normalize_server", spy)
    await tick(inv_db)
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_a_batch_writes_source_changed_at_untouched_and_stamps_the_revision(inv_db):
    f = fleet(inv_db)
    before = inv_db.scalar("SELECT source_changed_at FROM inventory_identity WHERE asset_id = $1", f.d_fgt)
    await tick(inv_db)
    row = inv_db.query("SELECT source_changed_at, normalized_at, norm_rev FROM inventory_identity "
                      "WHERE asset_id = $1", f.d_fgt)[0]
    assert row[0] == before and row[1] > before and row[2] == inventory_normalize.RULEPACK_REV


@pytest.mark.asyncio
async def test_T9_template_value_marks_dirty_and_wins_and_emits_one_firmware_event(inv_db):
    # The profile name is an input too: attach one so the join is exercised.
    profile = inv_db.scalar("INSERT INTO device_profiles (name, vendor) VALUES ('Fortinet FortiGate', 'Fortinet') RETURNING id")
    dev = add_device(inv_db, "fgt-t9", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5", profile_id=profile)
    await tick(inv_db)
    assert (ident(inv_db, "device", dev)["os_version"], ident(inv_db, "device", dev)["os_source"]) == ("7.2.5", "profile")
    assert event_count(inv_db) == 0

    # A template value arrives with the same version: marks the device dirty, becomes the source, no event.
    set_template(inv_db, dev, "fgt_fw_version", "v7.2.5,build1517,230606 (GA.F)")
    assert is_dirty(inv_db, "device", dev)
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_build"], row["os_source"]) == ("7.2.5", "1517", "template")
    assert event_count(inv_db) == 0

    # Template changes 7.2.5 -> 7.2.8 while devices.os_version still says 7.2.5: the template wins.
    set_template(inv_db, dev, "fgt_fw_version", "v7.2.8,build1639,230606 (GA.F)")
    assert is_dirty(inv_db, "device", dev)
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_build"], row["os_source"], row["os_release"]) == ("7.2.8", "1639", "template", "7.2")
    assert inv_db.scalar("SELECT os_version FROM devices WHERE id = $1", dev) == "7.2.5"
    assert events(inv_db) == [{"category": "firmware", "action": "changed", "subject": "FortiOS",
                              "old_value": "7.2.5", "new_value": "7.2.8", "asset_kind": "device",
                              "asset_name": "fgt-t9"}]


@pytest.mark.asyncio
async def test_T9_stale_template_value_does_not_win(inv_db):
    stale = add_device(inv_db, "fgt-stale", vendor="Fortinet", model="FortiGate-60F", os_version="6.4.9")
    edge_fresh = add_device(inv_db, "fgt-23h", vendor="Fortinet", model="FortiGate-60F", os_version="6.4.9")
    edge_stale = add_device(inv_db, "fgt-25h", vendor="Fortinet", model="FortiGate-60F", os_version="6.4.9")
    set_template(inv_db, stale, "fgt_fw_version", "v7.0.1,build157", age_hours=30)
    set_template(inv_db, edge_fresh, "fgt_fw_version", "v7.0.1,build157", age_hours=23)
    set_template(inv_db, edge_stale, "fgt_fw_version", "v7.0.1,build157", age_hours=25)
    await tick(inv_db)
    assert (ident(inv_db, "device", stale)["os_version"], ident(inv_db, "device", stale)["os_source"]) == ("6.4.9", "profile")
    assert ident(inv_db, "device", edge_fresh)["os_source"] == "template"
    assert ident(inv_db, "device", edge_fresh)["os_version"] == "7.0.1"
    assert ident(inv_db, "device", edge_stale)["os_version"] == "6.4.9"


@pytest.mark.asyncio
async def test_template_metrics_other_than_identity_ones_and_the_newest_row_count(inv_db):
    f5 = add_device(inv_db, "f5-1", vendor="F5 Networks", model="BIG-IP i5800")
    set_template(inv_db, f5, "f5_version", "17.1.1.3", group="sys")
    set_template(inv_db, f5, "f5_serial", "f5-serial-9", group="sys")
    set_template(inv_db, f5, "cpu_util", "99", group="sys")                    # not an identity metric
    fgt = add_device(inv_db, "fgt-2", vendor="Fortinet", model="FortiGate-60F")
    set_template(inv_db, fgt, "fgt_fw_version", "v7.0.1,build157", age_hours=5, group="a")
    set_template(inv_db, fgt, "fgt_fw_version", "v7.2.8,build1639", age_hours=1, group="b")   # newer row wins
    await tick(inv_db)
    row = ident(inv_db, "device", f5)
    assert (row["os_family"], row["os_version"], row["os_source"], row["hw_serial"]) == (
        "tmos", "17.1.1.3", "template", "f5-serial-9")
    assert ident(inv_db, "device", fgt)["os_version"] == "7.2.8"


@pytest.mark.asyncio
async def test_T8_rule_pack_revision_bump_re_normalises_without_events(inv_db, monkeypatch):
    f = fleet(inv_db)
    await tick(inv_db)
    targets = [("server", f.s_win), ("server", f.s_lin), ("device", f.d_fgt), ("device", f.d_cis), ("device", f.d_bare)]
    before = [ident(inv_db, kind, i) for kind, i in targets]
    assert event_count(inv_db) == 0

    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    result = await tick(inv_db)

    assert result.stale == 5 and result.dirty == 0 and result.locked
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev = $1",
                        inventory_normalize.RULEPACK_REV) == 5
    for (kind, i), old in zip(targets, before):
        new = ident(inv_db, kind, i)
        assert {k: v for k, v in new.items() if k != "norm_rev"} == {k: v for k, v in old.items() if k != "norm_rev"}
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_T8_stale_rows_that_really_differ_are_rewritten_silently(inv_db):
    """The same tampered content is an event when the row is dirty and silence when it is only old."""
    f = fleet(inv_db)
    inv_db.execute("UPDATE devices SET serial_number = 'SN-REAL' WHERE id = $1", f.d_fgt)
    await tick(inv_db)
    assert ident(inv_db, "device", f.d_fgt)["hw_serial"] == "SN-REAL"
    # An older revision stored different values for every identity-bearing column.
    inv_db.psql("BEGIN;\nSELECT set_config('zenplus.inventory_suppress', 'on', true);\n"
               "UPDATE inventory_identity SET os_version = '0.0.0', hw_model = 'old-model', hw_serial = 'old-serial', "
               "norm_rev = 0;\nCOMMIT;")
    assert event_count(inv_db) == 0
    # Control: one device also had a real source change, so it is dirty rather than stale.
    inv_db.execute("UPDATE devices SET sys_descr = 'changed' WHERE id = $1", f.d_cis)

    result = await tick(inv_db)

    assert (result.dirty, result.stale) == (1, 4)
    row = ident(inv_db, "device", f.d_fgt)
    assert (row["os_version"], row["hw_model"], row["hw_serial"]) == ("7.2.5", "FortiGate-60F", "SN-REAL")
    assert row["norm_rev"] == inventory_normalize.RULEPACK_REV
    got = events(inv_db)
    assert {e["asset_name"] for e in got} == {"cis-1"}                    # only the dirty row wrote history
    assert sorted((e["category"], e["subject"]) for e in got) == [("firmware", "IOS XE"), ("hardware", "hw_model")]


@pytest.mark.asyncio
async def test_T8_a_real_change_writes_exactly_one_event_per_asset(inv_db):
    f = fleet(inv_db)
    await tick(inv_db)
    inv_db.execute("UPDATE devices SET os_version = '7.2.8' WHERE id = $1", f.d_fgt)
    assert is_dirty(inv_db, "device", f.d_fgt)
    result = await tick(inv_db)
    assert result.dirty == 1 and result.stale == 0
    assert events(inv_db) == [{"category": "firmware", "action": "changed", "subject": "FortiOS",
                              "old_value": "7.2.5", "new_value": "7.2.8", "asset_kind": "device",
                              "asset_name": "fgt-1"}]

    inv_db.execute("UPDATE servers SET kernel_or_build = '10.0.20348.2700 Build 20348.2700' WHERE id = $1", f.s_win)
    await tick(inv_db)
    assert [e for e in events(inv_db) if e["category"] == "os"] == [
        {"category": "os", "action": "changed", "subject": "Windows Server", "old_value": "10.0.20348.587",
         "new_value": "10.0.20348.2700", "asset_kind": "server", "asset_name": "win-1"}]
    assert len(events(inv_db)) == 2
    await tick(inv_db)                                       # nothing left to do, nothing new written
    assert len(events(inv_db)) == 2


@pytest.mark.asyncio
async def test_T8_a_real_change_in_the_same_tick_as_a_revision_bump_is_absorbed_and_the_next_one_recorded(
        inv_db, monkeypatch):
    """Contract C1 (migration 157): the capture trigger fires only under an unchanged values_rev.
    A genuine change that lands in the very write that moves the revision cannot be told from the
    new pack re-rendering the old data, and a false event is worse than a missing one, so it is
    absorbed (this test used to expect the event). The row is current, and history resumes."""
    f = fleet(inv_db)
    await tick(inv_db)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    inv_db.execute("UPDATE devices SET os_version = '7.2.8' WHERE id = $1", f.d_fgt)     # dirty; the other four are stale

    result = await tick(inv_db)

    assert (result.dirty, result.stale) == (1, 4)
    assert event_count(inv_db) == 0
    assert ident(inv_db, "device", f.d_fgt)["os_version"] == "7.2.8"                   # stored, not lost
    rev = inventory_normalize.RULEPACK_REV
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev = $1 AND values_rev = $1", rev) == 5

    inv_db.execute("UPDATE devices SET os_version = '7.2.9' WHERE id = $1", f.d_fgt)
    await tick(inv_db)
    assert [(e["category"], e["old_value"], e["new_value"]) for e in events(inv_db)] == [("firmware", "7.2.8", "7.2.9")]


# ---- rule-pack revisions never read as changes (contract C1)

@pytest.mark.asyncio
async def test_C1_every_write_stamps_values_rev_together_with_norm_rev(inv_db, monkeypatch):
    f = fleet(inv_db)
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE values_rev IS NOT NULL") == 0
    await tick(inv_db)
    rev = inventory_normalize.RULEPACK_REV
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev = $1 AND values_rev = $1", rev) == 5

    # Dirty-marking resets norm_rev and must leave values_rev alone: it is the memory of which pack
    # rendered the stored values.
    inv_db.execute("UPDATE devices SET sys_descr = 'changed' WHERE id = $1", f.d_fgt)
    assert is_dirty(inv_db, "device", f.d_fgt) and values_rev(inv_db, "device", f.d_fgt) == rev

    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", rev + 1)
    await tick(inv_db)
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev = $1 AND values_rev = $1", rev + 1) == 5


def _pack_that_drops_the_last_version_component(monkeypatch):
    """A rule-pack release (revision 2 and later) that renders '10.0.20348.587' as '10.0.20348'."""
    real = inventory_normalize.normalize_server

    def short(*a, **k):
        identity = real(*a, **k)
        if inventory_normalize.RULEPACK_REV >= 2 and identity.os_version:
            return dataclasses.replace(identity, os_version=identity.os_version.rsplit(".", 1)[0])
        return identity

    monkeypatch.setattr(inventory_normalize, "normalize_server", short)


@pytest.mark.asyncio
async def test_C1_a_dirty_row_that_a_new_rule_pack_renders_differently_writes_no_history(inv_db, monkeypatch):
    """After a revision bump a row that is merely dirty (an unrelated column changed) is written with
    the capture triggers on. Only its values_rev tells the trigger the new value comes from a new pack."""
    f = fleet(inv_db)
    await tick(inv_db)
    assert ident(inv_db, "server", f.s_win)["os_version"] == "10.0.20348.587"
    _pack_that_drops_the_last_version_component(monkeypatch)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", 2)
    inv_db.execute("UPDATE servers SET cpu_model = 'Xeon Gold 6338' WHERE id = $1", f.s_win)    # not an OS column
    assert is_dirty(inv_db, "server", f.s_win)

    result = await tick(inv_db)

    assert (result.dirty, result.stale) == (1, 4)
    assert ident(inv_db, "server", f.s_win)["os_version"] == "10.0.20348"
    assert events(inv_db) == []

    # And the other way: a rollback to the old pack, again through a dirty row.
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", 1)
    inv_db.execute("UPDATE servers SET cpu_model = 'Xeon Gold 6348' WHERE id = $1", f.s_win)
    await tick(inv_db)
    assert ident(inv_db, "server", f.s_win)["os_version"] == "10.0.20348.587"
    assert events(inv_db) == []


@pytest.mark.asyncio
async def test_N15_the_loop_never_overwrites_lifecycle_slug(inv_db, monkeypatch):
    f = fleet(inv_db)
    await tick(inv_db)
    inv_db.execute("UPDATE inventory_identity SET lifecycle_slug = 'windows-server' WHERE asset_id = $1", f.s_win)
    inv_db.execute("UPDATE servers SET cpu_model = 'Xeon' WHERE id = $1", f.s_win)         # a dirty re-key
    assert (await tick(inv_db)).dirty == 1
    assert ident(inv_db, "server", f.s_win)["lifecycle_slug"] == "windows-server"
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    assert (await tick(inv_db)).stale == 5                                                   # a stale re-key
    assert ident(inv_db, "server", f.s_win)["lifecycle_slug"] == "windows-server"


# ---- a missing source is a gap, not a change (N-01)

@pytest.mark.asyncio
async def test_N01_template_only_firmware_that_ages_past_the_window_survives_a_revision_bump(inv_db, monkeypatch):
    dev = add_device(inv_db, "fgt-dark", vendor="Fortinet", model="FortiGate-60F")      # the template is its only version
    set_template(inv_db, dev, "fgt_fw_version", "v7.2.5,build1517")
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_source"]) == ("7.2.5", "template")

    age_template(inv_db, dev, 30)                     # the device was unreachable for more than a day...
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    result = await tick(inv_db)                       # ...when the appliance was upgraded

    assert result.stale == 1
    row = ident(inv_db, "device", dev)
    assert (row["os_family"], row["os_version"], row["os_build"], row["os_source"]) == (
        "fortios", "7.2.5", "1517", "template_stale")
    assert event_count(inv_db) == 0

    # It must still be able to record the upgrade that happens after it comes back (NULL -> value is not an event).
    set_template(inv_db, dev, "fgt_fw_version", "v7.4.3,build2573")
    await tick(inv_db)
    assert [(e["category"], e["old_value"], e["new_value"]) for e in events(inv_db)] == [("firmware", "7.2.5", "7.4.3")]


@pytest.mark.asyncio
async def test_N01_an_aged_template_does_not_roll_a_newer_firmware_back_to_a_sticky_profile_value(inv_db):
    dev = add_device(inv_db, "fgt-sticky", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    set_template(inv_db, dev, "fgt_fw_version", "v7.4.3,build2573")        # newer than the sticky devices.os_version
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_source"]) == ("7.4.3", "template")

    age_template(inv_db, dev, 30)
    inv_db.execute("UPDATE devices SET sys_descr = 'edited' WHERE id = $1", dev)   # a watched column: dirty, capture on
    assert is_dirty(inv_db, "device", dev)
    await tick(inv_db)

    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_source"]) == ("7.4.3", "template")
    assert event_count(inv_db) == 0                                               # no downgrade that never happened


@pytest.mark.asyncio
async def test_N01_a_leftover_template_row_does_not_freeze_a_device_that_is_still_polled(inv_db):
    """The guard only applies while the device is silent: a device moved to another profile keeps its old
    template rows for ever, and the poller refreshes identity_updated_at every cycle."""
    dev = add_device(inv_db, "fgt-moved", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    set_template(inv_db, dev, "fgt_fw_version", "v7.4.3,build2573")
    await tick(inv_db)
    assert ident(inv_db, "device", dev)["os_source"] == "template"

    age_template(inv_db, dev, 30)
    inv_db.execute("UPDATE devices SET identity_updated_at = now(), sys_descr = 'polled again' WHERE id = $1", dev)
    await tick(inv_db)

    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["os_source"]) == ("7.2.5", "profile")


@pytest.mark.asyncio
async def test_N01_f5_version_and_serial_from_aged_templates_survive_a_revision_bump(inv_db, monkeypatch):
    dev = add_device(inv_db, "f5-dark", vendor="F5 Networks", model="BIG-IP i5800")
    set_template(inv_db, dev, "f5_version", "17.1.1.3", group="sys")
    set_template(inv_db, dev, "f5_serial", "f5-serial-9", group="sys")
    await tick(inv_db)
    age_template(inv_db, dev, 30)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["os_version"], row["hw_serial"]) == ("17.1.1.3", "f5-serial-9")
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_N01_an_aged_template_ranks_below_a_profile_or_entity_version(inv_db):
    by_profile = add_device(inv_db, "fgt-profile", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.0")
    by_entity = add_device(inv_db, "fgt-entity", vendor="Fortinet", model="FortiGate-60F")
    add_chassis(inv_db, by_entity, 1, firmware="7.0.12,build523")
    for dev in (by_profile, by_entity):
        set_template(inv_db, dev, "fgt_fw_version", "v7.4.3,build2573", age_hours=30)
    await tick(inv_db)
    assert [ident(inv_db, "device", d)["os_source"] for d in (by_profile, by_entity)] == ["profile", "entity"]
    assert [ident(inv_db, "device", d)["os_version"] for d in (by_profile, by_entity)] == ["7.2.0", "7.0.12"]


@pytest.mark.asyncio
async def test_N01_a_recomputed_identity_without_an_os_version_keeps_the_stored_os_block(inv_db):
    f = fleet(inv_db)
    await tick(inv_db)
    before = ident(inv_db, "server", f.s_win)
    assert before["os_version"] == "10.0.20348.587"

    # The agent momentarily reports no build: a gap, not a change.
    inv_db.execute("UPDATE servers SET kernel_or_build = NULL, os_version = NULL WHERE id = $1", f.s_win)
    result = await tick(inv_db)
    assert result.dirty == 1
    assert ident(inv_db, "server", f.s_win) == before
    assert event_count(inv_db) == 0

    # Nothing known at all is a gap too.
    inv_db.execute("UPDATE servers SET os_name = NULL WHERE id = $1", f.s_win)
    await tick(inv_db)
    assert ident(inv_db, "server", f.s_win) == before

    # A different known operating system is not: the stored block would describe the wrong OS.
    inv_db.execute("UPDATE servers SET os_name = 'FreeBSD' WHERE id = $1", f.s_win)
    await tick(inv_db)
    after = ident(inv_db, "server", f.s_win)
    assert (after["os_family"], after["os_product"], after["os_version"]) == ("bsd", "FreeBSD", None)
    assert event_count(inv_db) == 0                                    # a version appearing or vanishing is no event


# ---- the normaliser runs strict (N-02)

@pytest.mark.asyncio
async def test_N02_a_normaliser_failure_leaves_the_row_dirty_and_the_stored_identity_alone(inv_db, monkeypatch, caplog):
    good = add_device(inv_db, "fgt-ok", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    bad = add_device(inv_db, "fgt-bad", vendor="Poison", model="FortiGate-60F", os_version="7.2.5")
    srv = add_server(inv_db, "srv-bad", os_name="Poison OS 1.0", os_version="1.0")
    await tick(inv_db)
    stored = {d: ident(inv_db, k, d) for k, d in (("device", good), ("device", bad), ("server", srv))}
    assert all(r["norm_rev"] == inventory_normalize.RULEPACK_REV for r in stored.values())

    # An engine bug: the internal functions (not the public wrappers, which would hide it unless strict).
    real_device, real_server = inventory_normalize._normalize_device, inventory_normalize._normalize_server

    def broken_device(vendor, *a, **k):
        if vendor == "Poison":
            raise ValueError("engine bug")
        return real_device(vendor, *a, **k)

    def broken_server(os_name, *a, **k):
        if os_name == "Poison OS 1.0":
            raise ValueError("engine bug")
        return real_server(os_name, *a, **k)

    monkeypatch.setattr(inventory_normalize, "_normalize_device", broken_device)
    monkeypatch.setattr(inventory_normalize, "_normalize_server", broken_server)
    inv_db.execute("UPDATE devices SET sys_descr = 'changed' WHERE id = ANY($1)", [good, bad])
    inv_db.execute("UPDATE servers SET cpu_model = 'Xeon' WHERE id = $1", srv)
    clock = FakeClock()
    state = svc.TickState(failures=svc.FailureLog(clock=clock))

    with caplog.at_level(logging.ERROR, logger="zenplus.inventory_service"):
        first = await tick(inv_db, state)
        assert (first.dirty, first.failed) == (1, 2)
        for kind, asset in (("device", bad), ("server", srv)):
            assert is_dirty(inv_db, kind, asset)                                 # not stamped
            row = ident(inv_db, kind, asset)
            assert {k: v for k, v in row.items() if k != "norm_rev"} == {
                k: v for k, v in stored[asset].items() if k != "norm_rev"}       # not overwritten with 'unknown'
        assert not is_dirty(inv_db, "device", good)                              # the rest of the batch went through
        for asset in (bad, srv):
            logged = [r for r in caplog.records if str(asset) in r.getMessage()]
            assert len(logged) == 1 and repr(str(asset)) in logged[0].getMessage()

        second = await tick(inv_db, state)                                       # backed off: not retried, not logged
        assert (second.failed, second.dirty) == (0, 0)
        assert len([r for r in caplog.records if str(bad) in r.getMessage()]) == 1

        clock.now += svc.FAILURE_BACKOFF_S + 1
        monkeypatch.setattr(inventory_normalize, "_normalize_device", real_device)
        monkeypatch.setattr(inventory_normalize, "_normalize_server", real_server)
        third = await tick(inv_db, state)                                        # retried after the back-off
    assert third.dirty == 2 and third.failed == 0
    assert not is_dirty(inv_db, "device", bad) and not is_dirty(inv_db, "server", srv)


def test_N02_the_loop_asks_the_normaliser_to_raise(monkeypatch):
    seen = {}

    def spy(name):
        real = getattr(inventory_normalize, name)

        def wrapper(*a, **k):
            seen[name] = k.get("strict")
            return real(*a, **k)
        monkeypatch.setattr(inventory_normalize, name, wrapper)

    for name in ("normalize_server", "normalize_device", "normalize_software"):
        spy(name)
    svc._compute_identities(
        [svc._Candidate("server", "s", datetime.now(UTC)), svc._Candidate("device", "d", datetime.now(UTC))],
        {"s": dict(os_name="Ubuntu 22.04", os_version="22.04", kernel_or_build=None, architecture=None, cpu_model=None,
                   hw_manufacturer=None, hw_model=None, hw_serial=None)},
        {"d": dict(vendor="Fortinet", model="FG", os_version=None, firmware_version=None, serial_number=None,
                   sys_object_id=None, sys_descr=None, profile_name=None, unpolled=True)},
        {}, {})
    svc._compute_software_keys([{"server_id": "s", "package_name": "x", "vendor": None, "source": None}])
    assert seen == {"normalize_server": True, "normalize_device": True, "normalize_software": True}


# ---- optimistic concurrency (6b)

def _change_source_during_compute(monkeypatch, change):
    """Run ``change`` after the batch has read its rows and before it writes them."""
    real = svc._compute_identities
    fired = []

    def hooked(*args, **kwargs):
        if not fired:
            fired.append(1)
            change()
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "_compute_identities", hooked)
    return fired


@pytest.mark.parametrize("how", ["device_row", "template_value"])
@pytest.mark.asyncio
async def test_6b_source_change_between_read_and_write_leaves_the_row_dirty(inv_db, monkeypatch, how):
    dev = add_device(inv_db, "race-1", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    if how == "device_row":
        change = lambda: inv_db.execute("UPDATE devices SET os_version = '7.2.9' WHERE id = $1", dev)  # noqa: E731
    else:
        change = lambda: set_template(inv_db, dev, "fgt_fw_version", "v7.2.9,build1700")  # noqa: E731
    fired = _change_source_during_compute(monkeypatch, change)

    async with sessions(inv_db) as factory:
        async with factory() as db:
            result = await svc.normalize_pending_batch(db, 10, failures=svc.FailureLog())

    assert fired and result.skipped == 1 and result.dirty == 0 and result.failed == 0
    stored = ident(inv_db, "device", dev)
    assert stored["norm_rev"] is None and stored["os_version"] is None       # not stamped clean with the old read
    assert inv_db.scalar("SELECT normalized_at IS NULL FROM inventory_identity WHERE asset_id = $1", dev)

    monkeypatch.undo()
    await tick(inv_db)
    assert ident(inv_db, "device", dev)["os_version"] == "7.2.9"
    assert not is_dirty(inv_db, "device", dev)


@pytest.mark.asyncio
async def test_6b_a_stale_row_changed_meanwhile_is_not_silenced(inv_db, monkeypatch):
    f = fleet(inv_db)
    await tick(inv_db)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    _change_source_during_compute(
        monkeypatch, lambda: inv_db.execute("UPDATE devices SET os_version = '7.2.8' WHERE id = $1", f.d_fgt))

    result = await tick(inv_db)

    # The row went dirty under the suppressed write, which therefore matched nothing for it.
    assert result.skipped == 1 and result.stale == 4
    assert is_dirty(inv_db, "device", f.d_fgt) and event_count(inv_db) == 0

    monkeypatch.undo()
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    await tick(inv_db)
    # The change is not lost (the value is stored and the row is clean). It is not an event either:
    # the row's values_rev moves in this write (C1), so the trigger cannot tell it from a re-render.
    assert ident(inv_db, "device", f.d_fgt)["os_version"] == "7.2.8" and not is_dirty(inv_db, "device", f.d_fgt)
    assert event_count(inv_db) == 0
    inv_db.execute("UPDATE devices SET os_version = '7.2.9' WHERE id = $1", f.d_fgt)
    await tick(inv_db)
    assert [(e["category"], e["old_value"], e["new_value"]) for e in events(inv_db)] == [("firmware", "7.2.8", "7.2.9")]


# ---- device inputs (6c)

@pytest.mark.asyncio
async def test_6c_chassis_row_fills_what_the_device_row_lacks(inv_db):
    dev = add_device(inv_db, "chassis-1", vendor="Fortinet")
    add_chassis(inv_db, dev, 11, parent_index=10, serial="MEMBER-2", model="FG-member", firmware="9.9.9,build1")
    add_chassis(inv_db, dev, 20, parent_index=0, serial="FGT100F123", model="FG-100F", firmware="7.2.5,build1517")
    add_chassis(inv_db, dev, 1, parent_index=0, cls="module", serial="MODULE-1", model="FG-module")
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["hw_model"], row["hw_serial"], row["hw_source"]) == ("FG-100F", "FGT100F123", "entity")
    assert (row["os_family"], row["os_version"], row["os_source"]) == ("fortios", "7.2.5", "entity")


@pytest.mark.asyncio
async def test_6c_chassis_picks_the_root_first_then_the_lowest_index(inv_db):
    dev = add_device(inv_db, "chassis-2", vendor="Fortinet")
    add_chassis(inv_db, dev, 5, parent_index=2, serial="CHILD-5", model="child-5")
    add_chassis(inv_db, dev, 3, parent_index=2, serial="CHILD-3", model="child-3")
    await tick(inv_db)
    assert ident(inv_db, "device", dev)["hw_serial"] == "CHILD-3"       # no root: lowest index wins
    add_chassis(inv_db, dev, 9, parent_index=0, serial="ROOT-9", model="root-9")
    inv_db.execute("UPDATE devices SET sys_descr = 'changed' WHERE id = $1", dev)    # a real source change: dirty
    await tick(inv_db)
    assert ident(inv_db, "device", dev)["hw_serial"] == "ROOT-9"         # a root chassis beats a lower child index


@pytest.mark.asyncio
async def test_6c_device_row_values_win_and_keep_the_device_source(inv_db):
    dev = add_device(inv_db, "chassis-3", vendor="Fortinet", model="FG-60F", serial_number="OWN-SERIAL",
                     firmware_version="7.0.1,build157")
    add_chassis(inv_db, dev, 1, serial="CHASSIS-SERIAL", model="FG-chassis", firmware="7.2.5,build1517")
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["hw_model"], row["hw_serial"], row["hw_source"], row["os_version"]) == (
        "FG-60F", "OWN-SERIAL", "device", "7.0.1")


@pytest.mark.asyncio
async def test_6c_blank_device_values_count_as_empty(inv_db):
    dev = add_device(inv_db, "chassis-4", vendor="Fortinet", model="  ", serial_number="", firmware_version=" ")
    add_chassis(inv_db, dev, 1, serial="FROM-CHASSIS", model="FG-100F", firmware="7.2.5,build1517")
    await tick(inv_db)
    row = ident(inv_db, "device", dev)
    assert (row["hw_model"], row["hw_serial"], row["hw_source"]) == ("FG-100F", "FROM-CHASSIS", "entity")


# ---- orphans (6d)

@pytest.mark.asyncio
async def test_6d_orphan_identity_rows_are_skipped_then_removed_and_events_stay(inv_db):
    f = fleet(inv_db)
    ghost_server, ghost_device = uuid.uuid4(), uuid.uuid4()
    for kind, i in (("server", ghost_server), ("device", ghost_device)):
        inv_db.execute("INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev) VALUES ($1, $2, NULL)", kind, i)
        inv_db.execute("INSERT INTO inventory_change_events (asset_kind, asset_id, category, action, subject) "
                      "VALUES ($1, $2, 'firmware', 'changed', 'history')", kind, i)

    async with sessions(inv_db) as factory:
        async with factory() as db:
            result = await svc.normalize_pending_batch(db, 100, failures=svc.FailureLog())
    assert result.dirty == 5 and result.selected == 5             # the ghosts were not even picked up
    assert is_dirty(inv_db, "server", ghost_server) and is_dirty(inv_db, "device", ghost_device)

    state = svc.TickState()
    done = await tick(inv_db, state)
    assert done.orphans_removed == 2
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE asset_id = ANY($1)",
                        [ghost_server, ghost_device]) == 0
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity") == 5
    assert inv_db.scalar("SELECT count(*) FROM inventory_change_events") == 2       # history is never deleted


@pytest.mark.asyncio
async def test_6d_a_tick_removes_at_most_a_thousand_orphans(inv_db):
    inv_db.execute("INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev) "
                  "SELECT 'device', gen_random_uuid(), NULL FROM generate_series(1, 1200)")
    first = await tick(inv_db)
    assert first.orphans_removed == 1000
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity") == 200
    second = await tick(inv_db)
    assert second.orphans_removed == 200 and inv_db.scalar("SELECT count(*) FROM inventory_identity") == 0


@pytest.mark.asyncio
async def test_6d_truncate_leaves_orphans_that_the_tick_cleans_up(inv_db):
    fleet(inv_db)
    await tick(inv_db)
    inv_db.execute("TRUNCATE devices, servers CASCADE")         # does not fire the delete triggers
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity") == 5
    result = await tick(inv_db)
    assert result.orphans_removed == 5 and inv_db.scalar("SELECT count(*) FROM inventory_identity") == 0


# ---- failed rows (3)

@pytest.mark.asyncio
async def test_a_failing_row_is_skipped_logged_once_and_does_not_starve_the_rest(inv_db, monkeypatch, caplog):
    devices = [add_device(inv_db, f"d{n}", vendor="Poison" if n == 1 else "Fortinet", model="FG") for n in range(1, 5)]
    inv_db.execute("UPDATE inventory_identity SET source_changed_at = now() - (random() * interval '1 minute')")
    inv_db.execute("UPDATE inventory_identity SET source_changed_at = now() - interval '1 day' WHERE asset_id = $1",
                  devices[0])                                      # the poison row is the oldest
    real = inventory_normalize.normalize_device

    def maybe_raise(**kw):
        if kw.get("vendor") == "Poison":
            raise ValueError("cannot normalise this one")
        return real(**kw)

    monkeypatch.setattr(inventory_normalize, "normalize_device", maybe_raise)
    clock = FakeClock()
    failures = svc.FailureLog(clock=clock)
    async with sessions(inv_db) as factory:
        with caplog.at_level(logging.ERROR, logger="zenplus.inventory_service"):
            async with factory() as db:
                one = await svc.normalize_pending_batch(db, 2, failures=failures)
            assert (one.failed, one.dirty, one.selected) == (1, 1, 2)       # poison + the next oldest
            async with factory() as db:
                two = await svc.normalize_pending_batch(db, 2, failures=failures)
            assert (two.failed, two.dirty, two.selected) == (0, 2, 2)       # skipped, so the others are reached
            async with factory() as db:
                three = await svc.normalize_pending_batch(db, 2, failures=failures)
            assert (three.failed, three.dirty, three.selected) == (0, 0, 0)

            logged = [r for r in caplog.records if str(devices[0]) in r.getMessage()]
            assert len(logged) == 1                                          # once, with the asset id

            clock.now += svc.FAILURE_BACKOFF_S + 1                           # back-off over: retried, fails, logged again
            async with factory() as db:
                four = await svc.normalize_pending_batch(db, 2, failures=failures)
            assert (four.failed, four.selected) == (1, 1)
            assert len([r for r in caplog.records if str(devices[0]) in r.getMessage()]) == 2

    assert is_dirty(inv_db, "device", devices[0])
    assert all(not is_dirty(inv_db, "device", d) for d in devices[1:])


@pytest.mark.asyncio
async def test_a_row_the_database_rejects_is_isolated_by_the_row_by_row_retry(inv_db, monkeypatch, caplog):
    servers = [add_server(inv_db, f"s{n}", os_name="Ubuntu 22.04.4 LTS", os_version="22.04" if n else "bad")
               for n in range(3)]
    real = inventory_normalize.normalize_server

    def too_wide(os_name=None, os_version=None, *a, **k):
        ident_ = real(os_name, os_version, *a, **k)
        return dataclasses.replace(ident_, os_source="x" * 40) if os_version == "bad" else ident_   # varchar(16)

    monkeypatch.setattr(inventory_normalize, "normalize_server", too_wide)
    failures = svc.FailureLog()
    async with sessions(inv_db) as factory:
        with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
            async with factory() as db:
                result = await svc.normalize_pending_batch(db, 10, failures=failures)
    assert (result.dirty, result.failed) == (2, 1)
    assert sum(1 for s in servers if not is_dirty(inv_db, "server", s)) == 2
    assert failures.active() == [str(servers[0])]
    assert sum(1 for r in caplog.records if str(servers[0]) in r.getMessage()) == 1


@pytest.mark.asyncio
async def test_a_tick_stops_after_max_batches_and_the_next_tick_continues(inv_db):
    for n in range(10):
        add_device(inv_db, f"bulk-{n}", vendor="Fortinet", model="FG", os_version="7.2.5")
    first = await tick(inv_db, batch_size=2, max_batches=2)
    assert first.dirty == 4 and first.identity_batches == 2
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NULL") == 6
    second = await tick(inv_db, batch_size=2)
    assert second.dirty == 6 and inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NULL") == 0


# ---- rule-pack health (6a)

@pytest.mark.asyncio
async def test_6a_unhealthy_packs_stamp_nothing_and_warn_once_per_tick(inv_db, monkeypatch, caplog):
    f = fleet(inv_db)
    add_software(inv_db, f.s_win, "Google Chrome", "126.0", "Google LLC")
    inv_db.execute("DELETE FROM inventory_identity WHERE asset_id = $1", f.d_bare)   # a missing row is still created
    monkeypatch.setattr(inventory_normalize, "packs_healthy", lambda: False)
    with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
        result = await tick(inv_db)
    assert result.packs_unhealthy and result.dirty == 0 and result.stale == 0 and result.software == 0
    assert result.created == 1
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NOT NULL") == 0
    assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NOT NULL") == 0
    assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE name_key IS NOT NULL") == 0
    assert len([r for r in caplog.records if "rule packs are not healthy" in r.getMessage()]) == 1

    monkeypatch.undo()                                   # packs load again: everything is picked up
    result = await tick(inv_db)
    assert result.dirty == 5 and result.software == 1 and not result.packs_unhealthy


@pytest.mark.asyncio
async def test_6a_unhealthy_packs_also_stop_stale_revision_rewrites(inv_db, monkeypatch):
    fleet(inv_db)
    await tick(inv_db)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    monkeypatch.setattr(inventory_normalize, "packs_healthy", lambda: False)
    result = await tick(inv_db)
    assert result.stale == 0 and result.packs_unhealthy
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev = $1",
                        inventory_normalize.RULEPACK_REV - 1) == 5


# ════════════════════════════════════════════════════════════════════
# Software key backfill (database)
# ════════════════════════════════════════════════════════════════════

def software_state(db, server_id):
    return {r[0]: r[1:] for r in db.query(
        "SELECT package_name, name_key, publisher_key, category, norm_rev, version, updated_at, vendor, source "
        "FROM server_software_inventory WHERE server_id = $1", server_id)}


def seeded_software(db):
    srv = add_server(db, "soft-1", os_name="Microsoft Windows Server 2022 Standard")
    add_software(db, srv, "Google Chrome", "126.0.6478.127", "Google LLC")
    add_software(db, srv, "Mozilla Firefox (x64 en-US)", "127.0", "Mozilla")
    add_software(db, srv, "KB4011281", None, None)
    add_software(db, srv, "openssl", "3.0.2-0ubuntu1.15", None, source="dpkg")
    db.execute("UPDATE server_software_inventory SET updated_at = now() - interval '3 days' WHERE server_id = $1", srv)
    baseline(db, srv)                      # baselined: any version change would now become an event
    return srv


@pytest.mark.asyncio
async def test_software_backfill_fills_keys_without_touching_version_updated_at_or_events(inv_db):
    srv = seeded_software(inv_db)
    before = software_state(inv_db, srv)
    assert all(v[0] is None and v[3] is None for v in before.values())

    async with sessions(inv_db) as factory:
        async with factory() as db:
            result = await svc.backfill_software_keys(db, 2000, failures=svc.FailureLog())
    assert (result.locked, result.selected, result.written) == (True, 4, 4)

    after = software_state(inv_db, srv)
    rev = inventory_normalize.RULEPACK_REV
    assert after["Google Chrome"][:4] == ("google-chrome", "google", "browser", rev)
    assert after["Mozilla Firefox (x64 en-US)"][0] == "mozilla-firefox"
    assert after["KB4011281"][0] == "kb4011281" and after["KB4011281"][2] == "update"
    assert after["openssl"][0] == "openssl"
    for name, row in after.items():
        assert row[4] == before[name][4]          # version
        assert row[5] == before[name][5]          # updated_at: the ingest prune depends on it
        assert row[6:] == before[name][6:]        # vendor, source
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_software_backfill_respects_the_limit_and_leaves_current_rows_alone(inv_db):
    srv = seeded_software(inv_db)
    rev = inventory_normalize.RULEPACK_REV
    inv_db.execute("UPDATE server_software_inventory SET name_key = 'manual', publisher_key = NULL, category = 'other', "
                  "norm_rev = $2 WHERE server_id = $1 AND package_name = 'openssl'", srv, rev)
    xmin_before = inv_db.scalar("SELECT xmin::text FROM server_software_inventory WHERE package_name = 'openssl'")
    async with sessions(inv_db) as factory:
        seen = []
        for _ in range(4):
            async with factory() as db:
                r = await svc.backfill_software_keys(db, 2, failures=svc.FailureLog())
            seen.append((r.selected, r.written))
    assert seen == [(2, 2), (1, 1), (0, 0), (0, 0)]
    assert software_state(inv_db, srv)["openssl"][0] == "manual"
    assert inv_db.scalar("SELECT xmin::text FROM server_software_inventory WHERE package_name = 'openssl'") == xmin_before


@pytest.mark.asyncio
async def test_software_backfill_walks_the_primary_key_with_a_cursor(inv_db):
    srv = seeded_software(inv_db)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            first = await svc.backfill_software_keys(db, 2, failures=svc.FailureLog())
        async with factory() as db:
            second = await svc.backfill_software_keys(db, 2, after=first.cursor, failures=svc.FailureLog())
        async with factory() as db:
            third = await svc.backfill_software_keys(db, 2, after=second.cursor, failures=svc.FailureLog())
    # Primary key order is (server_id, package_name); the cluster uses the C collation.
    assert first.cursor == (str(srv), "KB4011281") and (first.selected, first.written) == (2, 2)
    assert second.cursor == (str(srv), "openssl") and (second.selected, second.written) == (2, 2)
    assert (third.selected, third.cursor) == (0, None)
    # Rows before the cursor are not looked at again, whatever their state.
    inv_db.execute("UPDATE server_software_inventory SET norm_rev = NULL WHERE package_name = 'Google Chrome'")
    async with sessions(inv_db) as factory:
        async with factory() as db:
            after_chrome = await svc.backfill_software_keys(db, 10, after=(str(srv), "Google Chrome"),
                                                            failures=svc.FailureLog())
        async with factory() as db:
            from_top = await svc.backfill_software_keys(db, 10, failures=svc.FailureLog())
    assert after_chrome.selected == 0 and from_top.selected == 1


@pytest.mark.asyncio
async def test_software_rows_that_cannot_be_stored_do_not_starve_the_rest_of_the_walk(inv_db, monkeypatch, caplog):
    srv = seeded_software(inv_db)
    real = svc._compute_software_keys
    bad = {"Google Chrome", "KB4011281"}

    def too_wide(rows):
        out = real(rows)
        for item in out:
            if item["package_name"] in bad:
                item["category"] = "x" * 40            # varchar(32): the database rejects the row
        return out

    monkeypatch.setattr(svc, "_compute_software_keys", too_wide)
    with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
        result = await tick(inv_db, software_batch_size=2)
    state = software_state(inv_db, srv)
    assert (result.software, result.failed) == (2, 2)
    assert state["Mozilla Firefox (x64 en-US)"][0] == "mozilla-firefox" and state["openssl"][0] == "openssl"
    assert state["Google Chrome"][0] is None and state["KB4011281"][0] is None
    assert result.software_batches == 3        # two full batches, then the empty one that ends the walk
    assert sum(1 for r in caplog.records if "Google Chrome" in r.getMessage()) == 1


@pytest.mark.asyncio
async def test_a_software_row_that_cannot_be_stored_is_backed_off_and_never_poisons_the_identity_batch(
        inv_db, monkeypatch, caplog):
    """Software failures are keyed 'server_id/package_name'; the identity skip list is a uuid[]
    parameter. Sharing one log between them made every identity batch fail for an hour."""
    srv = seeded_software(inv_db)
    real = svc._compute_software_keys

    def too_wide(rows):
        out = real(rows)
        for item in out:
            if item["package_name"] == "Google Chrome":
                item["category"] = "x" * 40            # varchar(32): the database rejects the row
        return out

    monkeypatch.setattr(svc, "_compute_software_keys", too_wide)
    clock = FakeClock()
    state = svc.TickState(failures=svc.FailureLog(clock=clock), software_failures=svc.FailureLog(clock=clock))
    with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
        first = await tick(inv_db, state)
        assert (first.software, first.failed) == (3, 1)
        assert state.software_failures.active() == [f"{srv}/Google Chrome"] and state.failures.active() == []

        add_device(inv_db, "after-failure", vendor="Fortinet", model="FG", os_version="7.2.5")   # a new dirty row
        state.software_idle_until = 0.0
        second = await tick(inv_db, state)
        assert (second.dirty, second.failed, second.software) == (1, 0, 0)   # identity fine; the bad row is skipped

        clock.now += svc.FAILURE_BACKOFF_S + 1             # back-off over: tried again, fails again
        state.software_idle_until = 0.0
        third = await tick(inv_db, state)
        assert (third.failed, third.software) == (1, 0)
    assert sum(1 for r in caplog.records if "Google Chrome" in r.getMessage()) == 2   # once per episode
    assert software_state(inv_db, srv)["Google Chrome"][0] is None
    assert software_state(inv_db, srv)["openssl"][0] == "openssl"


@pytest.mark.asyncio
async def test_software_backfill_re_keys_old_revisions_silently(inv_db, monkeypatch):
    srv = seeded_software(inv_db)
    await tick(inv_db)
    before = software_state(inv_db, srv)
    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", inventory_normalize.RULEPACK_REV + 1)
    result = await tick(inv_db)
    assert result.software == 4
    after = software_state(inv_db, srv)
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in before.items()}
    assert all(v[3] == inventory_normalize.RULEPACK_REV for v in after.values())
    assert all(after[k][5] == before[k][5] and after[k][4] == before[k][4] for k in after)
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_software_row_whose_inputs_changed_meanwhile_is_left_for_the_next_pass(inv_db, monkeypatch):
    srv = seeded_software(inv_db)
    real = svc._compute_software_keys
    fired = []

    def hooked(rows):
        if not fired:
            fired.append(1)
            inv_db.execute("UPDATE server_software_inventory SET vendor = 'Google Inc.' "
                          "WHERE server_id = $1 AND package_name = 'Google Chrome'", srv)
        return real(rows)

    monkeypatch.setattr(svc, "_compute_software_keys", hooked)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            result = await svc.backfill_software_keys(db, 2000, failures=svc.FailureLog())
    assert (result.written, result.skipped) == (3, 1)
    assert software_state(inv_db, srv)["Google Chrome"][0] is None
    monkeypatch.undo()
    await tick(inv_db)
    assert software_state(inv_db, srv)["Google Chrome"][0] == "google-chrome"


@pytest.mark.asyncio
async def test_software_backfill_idle_gate_skips_the_scan_until_the_recheck(inv_db):
    srv = seeded_software(inv_db)
    state = svc.TickState()
    first = await tick(inv_db, state)
    assert first.software == 4 and state.software_idle_until == 0.0      # a busy scan does not arm the gate
    idle = await tick(inv_db, state)
    assert idle.software == 0 and state.software_idle_until > 0          # empty scan arms it
    add_software(inv_db, srv, "7-Zip 23.01 (x64)", "23.01", "Igor Pavlov")
    gated = await tick(inv_db, state)
    assert gated.software == 0 and gated.software_batches == 0
    state.software_idle_until = 0.0
    assert (await tick(inv_db, state)).software == 1
    assert (await tick(inv_db, svc.TickState())).software == 0


# ---- the walk: an index for unkeyed rows, the old-revision scan once, a cursor across ticks (CS-3)

def _plan(db, sql, mode):
    """The plan of a service statement as a prepared statement planned in ``mode``
    (force_custom_plan or force_generic_plan), the way the driver's prepared statements are."""
    types = {"n": "int", "skip": "text[]", "rev": "int", "after_server": "uuid", "after_package": "varchar"}
    values = {"n": "2000", "skip": "'{}'", "rev": "1",
              "after_server": "'00000000-0000-0000-0000-000000000000'", "after_package": "''"}
    names = list(dict.fromkeys(re.findall(r":([a-z_]+)", sql)))
    numbered = re.sub(r":([a-z_]+)", lambda m: f"${names.index(m.group(1)) + 1}", sql)
    script = (f"PREPARE q({', '.join(types[n] for n in names)}) AS {numbered};\n"
              f"SET plan_cache_mode = {mode};\n"
              f"EXPLAIN (COSTS OFF) EXECUTE q({', '.join(values[n] for n in names)});")
    return db.psql(script)


@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
@pytest.mark.parametrize("after", [False, True])
def test_CS3_the_unkeyed_software_scan_uses_the_partial_index_whatever_the_plan(inv_db, mode, after):
    """The steady-state idle check ran as a scan of the whole table (an ordered primary key scan once
    the driver's prepared statement flipped to a generic plan). It is now answered by the tiny partial
    index idx_ssi_unkeyed with either plan; 20,000 current rows make a table scan the wrong choice."""
    srv = add_server(inv_db, "plan-1", os_type="windows")
    inv_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, source, norm_rev) "
                  "SELECT $1, 'pkg-' || g, '1.0', 'hklm', 1 FROM generate_series(1, 20000) g", srv)
    inv_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, source) "
                  "VALUES ($1, 'unkeyed-1', '1.0', 'hklm'), ($1, 'unkeyed-2', '1.0', 'hklm')", srv)
    inv_db.execute("ANALYZE server_software_inventory")
    plan = _plan(inv_db, svc._select_software_sql(include_stale=False, after=after), mode)
    assert "idx_ssi_unkeyed" in plan, plan
    assert "Seq Scan on server_software_inventory" not in plan, plan
    # Control: the old-revision variant has no index to use.
    assert "idx_ssi_unkeyed" not in _plan(inv_db, svc._select_software_sql(include_stale=True, after=after), mode)


@pytest.mark.asyncio
async def test_CS3_the_old_revision_scan_runs_once_per_process_and_unkeyed_rows_are_always_found(inv_db):
    srv = seeded_software(inv_db)
    state = svc.TickState()
    rev = inventory_normalize.RULEPACK_REV
    first = await tick(inv_db, state)
    assert first.software == 4 and state.software_stale_rev == rev and state.software_cursor is None

    # A row of an older revision is only found by the once-per-start scan.
    inv_db.execute("UPDATE server_software_inventory SET norm_rev = 0 WHERE package_name = 'openssl'")
    state.software_idle_until = 0.0
    again = await tick(inv_db, state)
    assert again.software == 0 and software_state(inv_db, srv)["openssl"][3] == 0
    assert state.software_idle_until > 0                           # an empty walk from the top arms the gate

    # An unkeyed row is found at once (the index), without another full scan.
    add_software(inv_db, srv, "7-Zip 23.01 (x64)", "23.01", "Igor Pavlov")
    state.software_idle_until = 0.0
    assert (await tick(inv_db, state)).software == 1

    # A restart scans everything again.
    restarted = await tick(inv_db, svc.TickState())
    assert restarted.software == 1 and software_state(inv_db, srv)["openssl"][3] == rev


@pytest.mark.asyncio
async def test_CS3_a_software_walk_that_runs_out_of_batches_resumes_where_it_stopped(inv_db):
    srv = seeded_software(inv_db)          # primary key order (C collation): Google Chrome, KB4011281, Mozilla..., openssl
    state = svc.TickState()
    kw = dict(software_batch_size=2, max_software_batches=1)

    first = await tick(inv_db, state, **kw)
    assert (first.software, first.software_batches) == (2, 1)
    assert state.software_cursor == (str(srv), "KB4011281") and state.software_stale_rev is None

    second = await tick(inv_db, state, **kw)
    assert second.software == 2 and state.software_cursor == (str(srv), "openssl")

    third = await tick(inv_db, state, **kw)                         # resumes after the last row: the walk is over
    assert third.software == 0 and state.software_cursor is None
    assert state.software_stale_rev == inventory_normalize.RULEPACK_REV and state.software_idle_until == 0.0

    fourth = await tick(inv_db, state, **kw)                        # the next walk starts at the top and finds nothing
    assert fourth.software == 0 and state.software_idle_until > 0


# ---- software rows without a source on other operating systems (CS-1)

@pytest.mark.asyncio
async def test_CS1_unsourced_software_on_a_non_windows_server_is_left_unkeyed_for_the_next_upload(inv_db):
    """Rows migration 157 leaves have no source. Without one a dpkg/rpm package name is keyed as a
    generic title and stamped clean, and nothing would ever correct it."""
    win = add_server(inv_db, "legacy-win", os_type="windows")
    lin = add_server(inv_db, "legacy-lin", os_type="linux")
    add_software(inv_db, win, "Google Chrome", "126.0", "Google LLC", source=None)
    add_software(inv_db, lin, "libssl3", "3.0.13", "Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>", source=None)
    state = svc.TickState()

    result = await tick(inv_db, state)

    assert (result.software, result.failed) == (1, 0)
    assert software_state(inv_db, win)["Google Chrome"][0] == "google-chrome"
    assert software_state(inv_db, lin)["libssl3"][0] is None and software_state(inv_db, lin)["libssl3"][3] is None

    # Walks keep ignoring it (and the idle gate is not kept open by it)...
    state.software_idle_until = 0.0
    assert (await tick(inv_db, state)).software == 0 and state.software_idle_until > 0
    assert software_state(inv_db, lin)["libssl3"][0] is None
    # ...until the upload that carries the source arrives.
    inv_db.execute("UPDATE server_software_inventory SET source = 'dpkg' WHERE package_name = 'libssl3'")
    state.software_idle_until = 0.0
    assert (await tick(inv_db, state)).software == 1
    assert software_state(inv_db, lin)["libssl3"][0] == "libssl3"


# ---- a title the normaliser cannot key (N-02) and log hygiene (SEC-10)

@pytest.mark.asyncio
async def test_N02_a_title_the_normaliser_cannot_key_is_left_unstamped_and_backed_off(inv_db, monkeypatch, caplog):
    srv = seeded_software(inv_db)
    real = inventory_normalize._software_cached

    def broken(name, publisher, source):
        if name == "Google Chrome":
            raise RuntimeError("rule bug")
        return real(name, publisher, source)

    monkeypatch.setattr(inventory_normalize, "_software_cached", broken)
    clock = FakeClock()
    state = svc.TickState(software_failures=svc.FailureLog(clock=clock))
    with caplog.at_level(logging.ERROR, logger="zenplus.inventory_service"):
        first = await tick(inv_db, state)
        assert (first.software, first.failed) == (3, 1)                  # the batch carried on
        chrome = software_state(inv_db, srv)["Google Chrome"]
        assert chrome[0] is None and chrome[3] is None                     # no generic 'unnamed-software' key, no stamp
        assert software_state(inv_db, srv)["openssl"][0] == "openssl"
        logged = [r for r in caplog.records if "Google Chrome" in r.getMessage()]
        assert len(logged) == 1 and repr(f"{srv}/Google Chrome") in logged[0].getMessage()

        state.software_idle_until = 0.0
        assert (await tick(inv_db, state)).failed == 0                       # backed off, not logged again
        assert len([r for r in caplog.records if "Google Chrome" in r.getMessage()]) == 1

        clock.now += svc.FAILURE_BACKOFF_S + 1
        monkeypatch.setattr(inventory_normalize, "_software_cached", real)
        state.software_idle_until = 0.0
        assert (await tick(inv_db, state)).software == 1                     # retried after the back-off
    assert software_state(inv_db, srv)["Google Chrome"][0] == "google-chrome"


@pytest.mark.asyncio
async def test_SEC10_agent_supplied_names_are_logged_with_repr_and_a_failing_batch_stays_short(inv_db, monkeypatch, caplog):
    srv = add_server(inv_db, "log-1", os_type="windows")
    evil = "Evil\nWARNING zenplus.auth: forged entry"
    add_software(inv_db, srv, evil, "1.0", "V")
    for n in range(12):
        add_software(inv_db, srv, f"bad-{n:02d}", "1.0", "V")
    real = svc._compute_software_keys

    def too_wide(rows):
        out = real(rows)
        for item in out:
            item["category"] = "x" * 40                    # varchar(32): the database rejects every row
        return out

    monkeypatch.setattr(svc, "_compute_software_keys", too_wide)
    with caplog.at_level(logging.WARNING, logger="zenplus.inventory_service"):
        result = await tick(inv_db)

    assert result.failed == 13 and result.software == 0
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r.getMessage() for r in errors if "\n" in r.getMessage()]        # nothing could forge a log line
    assert any(repr(f"{srv}/{evil}") in r.getMessage() for r in errors)
    traced = [r for r in errors if r.exc_info and "write failed" in r.getMessage()]
    assert len(traced) == svc.MAX_FAILURE_LOGS_PER_BATCH                        # a few tracebacks...
    assert [r.getMessage() for r in errors if "more rows failed" in r.getMessage()] == [
        f"inventory software: {13 - svc.MAX_FAILURE_LOGS_PER_BATCH} more rows failed in this batch (not listed)"]  # ...and a count


# ════════════════════════════════════════════════════════════════════
# Locks and concurrency (database)
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_second_normalise_pass_skips_cleanly_while_another_holds_the_lock(inv_db):
    f = fleet(inv_db)
    add_software(inv_db, f.s_win, "Google Chrome", "126.0", "Google LLC")
    async with holding_lock(inv_db, svc.NORMALIZE_LOCK_KEY):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                batch = await svc.normalize_pending_batch(db, 10, failures=svc.FailureLog())
            async with factory() as db:
                sw = await svc.backfill_software_keys(db, 10, failures=svc.FailureLog())
            async with factory() as db:
                orphans = await svc.delete_orphan_identities(db)
            result = await svc.run_normalise_tick(factory, state=svc.TickState())
        assert not batch.locked and batch.written == 0 and batch.created == 0
        assert not sw.locked and sw.written == 0 and orphans == 0
        assert not result.locked and result.dirty == 0 and result.software == 0
        assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NOT NULL") == 0
        assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NOT NULL") == 0
    after = await tick(inv_db)                                # lock released: the work gets done
    assert after.locked and after.dirty == 5 and after.software == 1


@pytest.mark.asyncio
async def test_concurrent_ticks_normalise_every_row_exactly_once(inv_db):
    for n in range(30):
        add_device(inv_db, f"par-{n}", vendor="Fortinet", model="FG", os_version="7.2.5")
    async with sessions(inv_db) as factory:
        results = await asyncio.gather(
            svc.run_normalise_tick(factory, state=svc.TickState(), batch_size=7),
            svc.run_normalise_tick(factory, state=svc.TickState(), batch_size=7),
            svc.run_normalise_tick(factory, state=svc.TickState(), batch_size=7))
    assert sum(r.dirty for r in results) == 30
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NULL") == 0
    assert event_count(inv_db) == 0


# ---- row locks: the loop never waits for an upload or the poller

@pytest.mark.asyncio
async def test_no_row_is_locked_while_the_batch_computes(inv_db, monkeypatch):
    """Raw data is read without locks, and locks are taken only for the write: an upload or a
    poller write that lands while the normaliser works is never made to wait for it."""
    dev = add_device(inv_db, "unlocked-1", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    probed = []
    real = svc._compute_identities

    def hooked(*args, **kwargs):
        # NOWAIT raises at once if our transaction holds this row.
        probed.append(inv_db.scalar("SELECT 1 FROM inventory_identity WHERE asset_id = $1 FOR UPDATE NOWAIT", dev))
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "_compute_identities", hooked)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            result = await svc.normalize_pending_batch(db, 10, failures=svc.FailureLog())
    assert probed == [1] and result.dirty == 1


@pytest.mark.asyncio
async def test_an_identity_row_another_transaction_holds_is_skipped_not_waited_for(inv_db):
    f = fleet(inv_db)
    async with holding_rows(inv_db, "SELECT 1 FROM inventory_identity WHERE asset_id = $1 FOR UPDATE", f.d_fgt):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                batch = await asyncio.wait_for(svc.normalize_pending_batch(db, 100, failures=svc.FailureLog()), 20)
    assert (batch.locked, batch.contended) == (True, False)
    assert (batch.selected, batch.dirty, batch.skipped, batch.failed) == (5, 4, 1, 0)
    assert is_dirty(inv_db, "device", f.d_fgt)
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NULL") == 1

    again = await tick(inv_db)                                  # the holder is gone: the row is picked up
    assert again.dirty == 1 and not is_dirty(inv_db, "device", f.d_fgt)


@pytest.mark.asyncio
async def test_a_software_row_another_transaction_holds_is_skipped_not_waited_for(inv_db):
    srv = seeded_software(inv_db)
    async with holding_rows(inv_db, "SELECT 1 FROM server_software_inventory WHERE server_id = $1 "
                                    "AND package_name = 'Google Chrome' FOR UPDATE", srv):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                sw = await asyncio.wait_for(svc.backfill_software_keys(db, 100, failures=svc.FailureLog()), 20)
    assert (sw.locked, sw.contended) == (True, False)
    assert (sw.selected, sw.written, sw.skipped, sw.failed) == (4, 3, 1, 0)
    state = software_state(inv_db, srv)
    assert state["Google Chrome"][0] is None and state["openssl"][0] == "openssl"

    async with sessions(inv_db) as factory:
        async with factory() as db:
            later = await svc.backfill_software_keys(db, 100, failures=svc.FailureLog())
    assert (later.selected, later.written) == (1, 1)
    assert software_state(inv_db, srv)["Google Chrome"][0] == "google-chrome"
    assert event_count(inv_db) == 0


@pytest.mark.asyncio
async def test_an_orphan_row_another_transaction_holds_is_skipped_by_the_clean_up(inv_db):
    ghosts = [uuid.uuid4(), uuid.uuid4()]
    for g in ghosts:
        inv_db.execute("INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev) VALUES ('device', $1, NULL)", g)
    async with holding_rows(inv_db, "SELECT 1 FROM inventory_identity WHERE asset_id = $1 FOR UPDATE", ghosts[0]):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                removed = await asyncio.wait_for(svc.delete_orphan_identities(db), 20)
    assert removed == 1
    assert inv_db.scalar("SELECT asset_id FROM inventory_identity") == ghosts[0]
    async with sessions(inv_db) as factory:
        async with factory() as db:
            assert await svc.delete_orphan_identities(db) == 1


@pytest.mark.asyncio
async def test_an_upload_holding_the_identity_row_is_skipped_and_its_change_is_not_lost(inv_db):
    """An upload's first OS-column update takes the identity row through the dirty-marking trigger.
    The tick skips it, and once the upload commits the next tick records exactly its change."""
    f = fleet(inv_db)
    await tick(inv_db)
    inv_db.execute("UPDATE servers SET kernel_or_build = '10.0.20348.1000 Build 20348.1000' WHERE id = $1", f.s_win)
    assert is_dirty(inv_db, "server", f.s_win)
    async with holding_rows(inv_db, "UPDATE servers SET kernel_or_build = '10.0.20348.2700 Build 20348.2700' "
                                    "WHERE id = $1", f.s_win, commit=True):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                batch = await asyncio.wait_for(svc.normalize_pending_batch(db, 100, failures=svc.FailureLog()), 20)
        assert (batch.dirty, batch.skipped, batch.contended) == (0, 1, False)
        assert is_dirty(inv_db, "server", f.s_win) and event_count(inv_db) == 0
    await tick(inv_db)
    assert [(e["category"], e["old_value"], e["new_value"]) for e in events(inv_db)] == [
        ("os", "10.0.20348.587", "10.0.20348.2700")]       # the 1000 build was never stored, so never an event


@pytest.mark.asyncio
async def test_a_lock_wait_that_times_out_skips_the_batch_instead_of_raising(inv_db, monkeypatch):
    """The one statement that can still wait is the insert of a missing identity row behind an
    in-doubt insert of the same key. After the lock timeout the batch is rolled back and reported."""
    monkeypatch.setattr(svc, "BATCH_LOCK_TIMEOUT", "300ms")
    srv = add_server(inv_db, "in-doubt", os_name="Ubuntu 22.04.4 LTS", os_version="22.04")
    inv_db.execute("DELETE FROM inventory_identity")
    async with holding_rows(inv_db, "INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev) "
                                    "VALUES ('server', $1, NULL)", srv):
        async with sessions(inv_db) as factory:
            async with factory() as db:
                batch = await svc.normalize_pending_batch(db, 10, failures=svc.FailureLog())
            result = await svc.run_normalise_tick(factory, state=svc.TickState())
    assert (batch.locked, batch.contended, batch.written, batch.created) == (True, True, 0, 0)
    assert result.contended and result.identity_batches == 0 and result.software_batches == 0
    assert inv_db.scalar("SELECT count(*) FROM inventory_identity") == 0           # rolled back

    done = await tick(inv_db)                                                        # the holder is gone
    assert done.created == 1 and done.dirty == 1 and not done.contended


@pytest.mark.asyncio
async def test_a_deadlock_is_not_swallowed_by_the_contention_handling():
    for state_code, swallowed in (("55P03", True), ("40P01", False), ("23505", False)):
        class Orig(Exception):
            sqlstate = state_code

        exc = svc.DBAPIError("stmt", {}, Orig("x"))
        assert svc._is_contention(exc) is swallowed
    assert svc._is_contention(RuntimeError("x")) is False
    assert svc._sqlstate(RuntimeError("x")) is None


def _ingest_identity_upsert(identity) -> tuple[str, list]:
    """The inline identity write of host_metric_service._refresh_server_identity, for asyncpg:
    values_rev goes in together with norm_rev (contract C1), lifecycle_slug is left alone."""
    cols = list(svc._IDENTITY_COLUMNS)
    data = dataclasses.asdict(identity)
    marks = ", ".join(f"${i + 3}" for i in range(len(cols)))
    sql = (f"INSERT INTO inventory_identity (asset_kind, asset_id, {', '.join(cols)}, norm_rev, values_rev, "
           f"normalized_at) VALUES ('server', $1, {marks}, $2, $2, now()) "
           "ON CONFLICT (asset_kind, asset_id) DO UPDATE SET "
           + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
           + ", norm_rev = EXCLUDED.norm_rev, values_rev = EXCLUDED.values_rev, normalized_at = now()")
    return sql, [inventory_normalize.RULEPACK_REV, *[data[c] for c in cols]]


@pytest.mark.asyncio
async def test_ingest_style_transactions_never_deadlock_with_the_normaliser_and_the_backfill(inv_db):
    """Agent ingest locks the servers row, the software rows (in the agent's order) and the
    identity row (first through the dirty-marking trigger, then explicitly). The loop's
    transactions must never wait for any of those locks, or a cycle aborts an upload."""
    import asyncpg

    srv = add_server(inv_db, "ingest-1", os_name="Microsoft Windows Server 2022 Standard", os_version="21H2",
                     kernel_or_build="10.0.20348.587 Build 20348.587")
    packages = [f"Package {n:03d}" for n in range(40)]
    for name in packages:
        add_software(inv_db, srv, name, "1.0", "Vendor")
    baseline(inv_db, srv)
    await tick(inv_db)
    assert event_count(inv_db) == 0

    dev = add_device(inv_db, "poller-1", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5")
    await tick(inv_db)
    identity = inventory_normalize.normalize_server(
        "Microsoft Windows Server 2022 Standard", "21H2", "10.0.20348.587 Build 20348.587", None)
    upsert_sql, upsert_args = _ingest_identity_upsert(identity)
    rounds = 25
    versions = [f"v7.2.{6 + i % 4},build{1600 + i}" for i in range(rounds)]
    done = {"ingest": 0, "poller": 0, "batches": 0, "keyed": 0}
    stop = asyncio.Event()

    async def ingest():
        conn = await asyncpg.connect(inv_db.url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            for i in range(rounds):
                order = random.Random(i).sample(packages, len(packages))
                async with conn.transaction():
                    # OS columns: the dirty-marking trigger locks the identity row here.
                    await conn.execute("UPDATE servers SET os_version = $2 WHERE id = $1", srv, "22H2" if i % 2 else "21H2")
                    for name in order:
                        # Left unkeyed on purpose (an inline key failure), so the backfill always has work.
                        await conn.execute(
                            "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source, updated_at) "
                            "VALUES ($1, $2, '1.0', 'Vendor', 'hklm', now()) ON CONFLICT (server_id, package_name) DO UPDATE "
                            "SET version = EXCLUDED.version, vendor = EXCLUDED.vendor, source = EXCLUDED.source, "
                            "name_key = NULL, publisher_key = NULL, category = NULL, norm_rev = NULL, updated_at = now()",
                            srv, name)
                        await asyncio.sleep(0)
                    await conn.execute("UPDATE servers SET inventory_updated_at = now() WHERE id = $1", srv)
                    await conn.execute(upsert_sql, srv, *upsert_args)
                done["ingest"] += 1
        finally:
            await conn.close()
            stop.set()

    async def poller():
        # The Go poller: one transaction per device, a devices update and a template value, each
        # of which marks the identity row dirty through a trigger.
        conn = await asyncpg.connect(inv_db.url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            for i, version in enumerate(versions):
                async with conn.transaction():
                    await conn.execute("UPDATE devices SET sys_descr = $2, identity_updated_at = now() WHERE id = $1",
                                       dev, f"FortiGate poll {i}")
                    await conn.execute(
                        "INSERT INTO device_template_values (device_id, group_key, metric_key, series_key, value_text, "
                        "updated_at) VALUES ($1, 'system', 'fgt_fw_version', 'fgt_fw_version', $2, now()) "
                        "ON CONFLICT (device_id, group_key, metric_key, instance) DO UPDATE "
                        "SET value_text = EXCLUDED.value_text, updated_at = EXCLUDED.updated_at", dev, version)
                done["poller"] += 1
                await asyncio.sleep(0)
        finally:
            await conn.close()

    async def loop_side():
        async with sessions(inv_db) as factory:
            failures, software_failures = svc.FailureLog(), svc.FailureLog()
            while not stop.is_set():
                async with factory() as db:
                    await svc.normalize_pending_batch(db, 50, failures=failures)
                async with factory() as db:
                    sw = await svc.backfill_software_keys(db, 7, failures=software_failures)
                    done["keyed"] += sw.written
                async with factory() as db:
                    await svc.delete_orphan_identities(db)
                done["batches"] += 1
                await asyncio.sleep(0)

    async def run_writers():
        try:
            await asyncio.gather(ingest(), poller())
        finally:
            stop.set()

    await asyncio.wait_for(asyncio.gather(run_writers(), loop_side(), loop_side()), timeout=120)

    assert (done["ingest"], done["poller"]) == (rounds, rounds)
    assert done["batches"] > 5 and done["keyed"] > 0                 # the writers and the loop really overlapped
    final = await tick(inv_db)
    assert final.locked
    assert not is_dirty(inv_db, "server", srv) and not is_dirty(inv_db, "device", dev)
    assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL") == 0
    # Re-keying is silent, and an unchanged version is not a change.
    assert inv_db.scalar("SELECT count(*) FROM inventory_change_events WHERE category = 'software'") == 0
    assert inv_db.scalar("SELECT count(*) FROM inventory_change_events WHERE category = 'os'") == 0
    # The poller's template values did change firmware. Whichever of them the loop saw, the events form
    # one unbroken chain from the version the device started with to the one it ends on.
    chain = events(inv_db)
    assert all(e["category"] == "firmware" for e in chain)
    previous = "7.2.5"
    for e in chain:
        assert e["old_value"] == previous and e["new_value"] != previous
        previous = e["new_value"]
    assert ident(inv_db, "device", dev)["os_version"] == versions[-1].split(",")[0][1:]


def test_default_session_factory_is_the_applications():
    from app.core.database import AsyncSessionLocal

    assert svc._default_session_factory() is AsyncSessionLocal


def _idle_after_each_tick(tick):
    """Wrap a loop's tick so that ``ticked`` is set when it ends: from then on the loop is asleep
    until its next wake-up, which is the only moment a test may cancel it (a cancel inside a database
    call is its own case, tested below). Returns (wrapped tick, coroutine function that waits for the
    next tick to end)."""
    ticked = asyncio.Event()

    async def wrapped():
        try:
            return await tick()
        finally:
            ticked.set()

    async def next_tick():
        ticked.clear()
        await asyncio.wait_for(ticked.wait(), 30)

    return wrapped, next_tick


async def _cancel_and_wait(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)           # bounded: a loop that swallows the cancel fails, it does not hang


@pytest.mark.asyncio
async def test_normalise_loop_runs_real_ticks_until_cancelled(inv_db):
    f = fleet(inv_db)
    add_software(inv_db, f.s_win, "Google Chrome", "126.0", "Google LLC")
    async with sessions(inv_db) as factory:
        state = svc.TickState()
        tick_, next_tick = _idle_after_each_tick(lambda: svc.run_normalise_tick(factory, state=state))
        task = asyncio.create_task(svc.inventory_normalize_loop(start_delay=0, interval=0.05, tick=tick_))
        for _ in range(100):
            await next_tick()
            if (inv_db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NULL") == 0
                    and inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE norm_rev IS NULL") == 0):
                break
        assert not task.done()
        inv_db.execute("UPDATE devices SET os_version = '7.2.8' WHERE id = $1", f.d_fgt)    # picked up on a later tick
        for _ in range(100):
            await next_tick()
            if event_count(inv_db):
                break
        await _cancel_and_wait(task)
    assert [(e["category"], e["new_value"]) for e in events(inv_db)] == [("firmware", "7.2.8")]
    assert software_state(inv_db, f.s_win)["Google Chrome"][0] == "google-chrome"


@pytest.mark.asyncio
async def test_snapshot_loop_runs_a_real_pass_until_cancelled(inv_db):
    snapshot_fleet(inv_db)
    async with sessions(inv_db) as factory:
        tick_, next_tick = _idle_after_each_tick(
            lambda: svc.run_snapshot_pass(factory, now=at("2026-10-07T12:00:00"), zone=UTC))
        task = asyncio.create_task(svc.inventory_snapshot_loop(start_delay=0, interval=0.05, tick=tick_))
        for _ in range(100):
            await next_tick()
            if snapshot_rows(inv_db, DAY):
                break
        await next_tick()                        # one more wake-up: the day's rows are written once
        await _cancel_and_wait(task)
    assert snapshot_rows(inv_db, DAY)[("assets", "server")] == 4


# ---- cancellation always ends a loop (F1)

@pytest.fixture
def invalidating_rollback_hook():
    """The application installs engine-wide commit/rollback listeners that read ``Connection.info``
    (api/v1/netflow/access.py). On a connection that a cancellation left invalid that raises
    PendingRollbackError from inside Session.close(), and the new exception replaces CancelledError.
    pytest imports app.main, which installs the real listeners; a stand-in keeps these tests
    independent of that import."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def hook(conn):
        _ = conn.info

    event.listen(Engine, "rollback", hook)
    try:
        yield
    finally:
        event.remove(Engine, "rollback", hook)


@pytest.mark.parametrize("loop_name", ["inventory_normalize_loop", "inventory_snapshot_loop"])
@pytest.mark.asyncio
async def test_F1_a_loop_whose_tick_raises_something_else_while_being_cancelled_still_ends(loop_name):
    """A cleanup error can replace CancelledError on its way out of the tick. The loop used to log it
    and carry on, leaving a cancelled task (cancelling() == 1) running for ever."""
    started = asyncio.Event()

    async def tick():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("PendingRollbackError stand-in") from None

    task = asyncio.create_task(getattr(svc, loop_name)(start_delay=0, interval=60, tick=tick))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("loop_name", ["inventory_normalize_loop", "inventory_snapshot_loop"])
@pytest.mark.asyncio
async def test_F1_a_loop_cancelled_inside_a_database_call_ends(inv_db, invalidating_rollback_hook, loop_name):
    started = asyncio.Event()
    async with sessions(inv_db) as factory:
        async def tick():
            async with factory() as db:                    # a plain session: close() raises after the cancel
                started.set()
                await db.execute(text("SELECT pg_sleep(30)"))

        task = asyncio.create_task(getattr(svc, loop_name)(start_delay=0, interval=60, tick=tick))
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.sleep(0.2)                           # inside the statement
        await _cancel_and_wait(task)


@pytest.mark.asyncio
async def test_F1_a_tick_cancelled_inside_a_database_call_is_cancelled_not_failed(inv_db, monkeypatch,
                                                                                    invalidating_rollback_hook):
    """The tick's own sessions close tolerantly, so the cancellation itself comes out."""
    started = asyncio.Event()

    async def stuck(db):
        started.set()
        await db.execute(text("SELECT pg_sleep(30)"))
        return 0

    monkeypatch.setattr(svc, "delete_orphan_identities", stuck)
    async with sessions(inv_db) as factory:
        task = asyncio.create_task(svc.run_normalise_tick(factory, state=svc.TickState()))
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.sleep(0.2)
        await _cancel_and_wait(task)


@pytest.mark.asyncio
async def test_F1_a_snapshot_pass_cancelled_inside_a_database_call_is_cancelled_not_failed(
        inv_db, monkeypatch, invalidating_rollback_hook):
    started = asyncio.Event()

    async def stuck(db, day):
        started.set()
        await db.execute(text("SELECT pg_sleep(30)"))
        return False

    monkeypatch.setattr(svc, "_day_has_snapshot", stuck)
    async with sessions(inv_db) as factory:
        task = asyncio.create_task(svc.run_snapshot_pass(factory, now=at("2026-10-07T12:00:00"), zone=UTC))
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.sleep(0.2)
        await _cancel_and_wait(task)


@pytest.mark.asyncio
async def test_F1_closing_a_session_never_raises_and_a_failed_rollback_does_not_mask_the_error(inv_db):
    class BrokenClose(AsyncSession):
        async def close(self):
            await super().close()
            raise RuntimeError("close failed")

    fleet(inv_db)
    engine = create_async_engine(inv_db.url, poolclass=NullPool)
    try:
        result = await svc.run_normalise_tick(async_sessionmaker(engine, class_=BrokenClose, expire_on_commit=False),
                                              state=svc.TickState())
        assert result.dirty == 5                            # the work was done; the failed close changes nothing

        class BrokenRollback:
            async def rollback(self):
                raise RuntimeError("the connection is unusable")

        await svc._rollback_quietly(BrokenRollback())      # must not raise: it runs in an error handler
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_CS9_normalisation_does_not_wait_for_the_default_executor(inv_db):
    """Every agent upload runs its ClickHouse insert through the loop's default executor. When that is
    slow all its threads are taken; the normalise transaction (advisory lock held, connection pinned)
    must not sit idle behind them."""
    import concurrent.futures

    fleet(inv_db)
    loop = asyncio.get_running_loop()
    default = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(default)
    release = threading.Event()
    busy = loop.run_in_executor(None, release.wait, 60)         # the only default-executor thread is taken
    try:
        result = await asyncio.wait_for(tick(inv_db), 20)
        assert result.dirty == 5
    finally:
        release.set()
        await busy
        default.shutdown()


@pytest.mark.asyncio
async def test_tick_runs_as_the_application_role(inv_db):
    """install.sh grants the application role ALL on tables and sequences; the service
    and the capture triggers it fires must need nothing more."""
    inv_db.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO zenplus; "
                  "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO zenplus")
    f = fleet(inv_db)
    srv = f.s_win
    add_software(inv_db, srv, "Google Chrome", "126.0", "Google LLC")
    state = svc.TickState()
    first = await tick(inv_db, state, user="zenplus")
    assert first.dirty == 5 and first.software == 1
    inv_db.execute("UPDATE devices SET os_version = '7.2.8' WHERE id = $1", f.d_fgt)
    second = await tick(inv_db, state, user="zenplus")
    assert second.dirty == 1
    assert [(e["category"], e["new_value"]) for e in events(inv_db)] == [("firmware", "7.2.8")]
    async with sessions(inv_db, "zenplus") as factory:
        passed = await svc.run_snapshot_pass(factory, now=at("2026-10-07T12:00:00"), zone=UTC)
    assert passed.ran


# ════════════════════════════════════════════════════════════════════
# Settings (database)
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("blob,expected_stale_days", [
    (None, 7), ("[1, 2, 3]", 7), ('"text"', 7), ("42", 7), ("null", 7), ('{"stale_days": "x"}', 7),
    ('{"stale_days": 30}', 30), ('{"stale_days": 100000}', 365),
])
@pytest.mark.asyncio
async def test_get_inventory_settings_reads_the_real_table(inv_db, blob, expected_stale_days):
    if blob is not None:
        # A literal, not a parameter: the helper's jsonb codec would JSON-encode a str argument again.
        inv_db.execute(f"INSERT INTO system_settings (key, value) VALUES ('inventory', '{blob}'::jsonb)")
    async with sessions(inv_db) as factory:
        async with factory() as db:
            got = await svc.get_inventory_settings(db)
    assert got["stale_days"] == expected_stale_days
    assert got["fresh_hours"] == {"server": 24, "device": 2} and got["snapshot_hour"] == 0


# ════════════════════════════════════════════════════════════════════
# Daily snapshot (database)
# ════════════════════════════════════════════════════════════════════

DAY = date(2026, 10, 7)


def snapshot_fleet(db):
    """Seven assets in the union (one device is the SNMP side of a server and does not count)."""
    d_link = add_device(db, "linked-1", vendor="Juniper", os_version="21.4R3-S5.4")
    s1 = add_server(db, "S1", os_name="Microsoft Windows Server 2022 Standard Evaluation", os_version="21H2",
                    kernel_or_build="10.0.20348.587 Build 20348.587", device_id=d_link, software_truncated=True)
    s2 = add_server(db, "S2", os_name="Ubuntu 22.04.4 LTS", os_version="22.04")
    s3 = add_server(db, "S3", os_name="Microsoft Windows Server 2019 Datacenter", os_version="1809",
                    kernel_or_build="10.0.17763.6532")
    s4 = add_server(db, "S4", os_name="Microsoft Windows Server 2019 Datacenter", os_version="1809",
                    kernel_or_build="10.0.17763.6532")
    d1 = add_device(db, "D1", vendor="Fortinet", model="FortiGate-60F", os_version="7.2.5", serial_number="FGT-1")
    d2 = add_device(db, "D2", vendor="Cisco", model="C9300-24T", os_version="17.09.04a")
    d4 = add_device(db, "D4")
    for sid, age in ((s1, "1 hour"), (s2, "3 days"), (s4, "10 days")):
        db.execute(f"UPDATE servers SET inventory_updated_at = now() - interval '{age}' WHERE id = $1", sid)
    for did, age in ((d_link, "5 minutes"), (d1, "30 minutes"), (d2, "5 hours")):
        db.execute(f"UPDATE devices SET identity_updated_at = now() - interval '{age}' WHERE id = $1", did)
    add_software(db, s1, "Google Chrome", "126.0", "Google LLC")
    add_software(db, s1, "Mozilla Firefox (x64 en-US)", "127.0", "Mozilla")
    add_software(db, s1, "KB4011281", None, None)
    add_software(db, s1, "Intel(R) HD Graphics Driver", "31.0", "Intel Corporation")
    add_software(db, s2, "Google Chrome", "126.0", "Google LLC")
    add_software(db, s2, "openssl", "3.0.2", None, source="dpkg")
    add_software(db, s3, "7-Zip 23.01 (x64)", "23.01", "Igor Pavlov")
    return SimpleNamespace(d_link=d_link, s1=s1, s2=s2, s3=s3, s4=s4, d1=d1, d2=d2, d4=d4)


def add_event(db, observed_at, category="software", action="added", subject="x"):
    db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) "
               "VALUES ($1, 'server', gen_random_uuid(), $2, $3, $4)", observed_at, category, action, subject)


async def snapshot(db, day=DAY, zone=UTC, settings=None):
    async with sessions(db) as factory:
        async with factory() as s:
            rows = await svc.snapshot_day(s, day, zone=zone, settings=settings)
    return rows


@pytest.mark.asyncio
async def test_snapshot_day_computes_every_family_from_a_seeded_fleet(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    for ts, cat, act in (("2026-10-07T00:00:00", "software", "added"), ("2026-10-07T10:00:00", "software", "added"),
                         ("2026-10-07T23:59:59", "firmware", "changed"), ("2026-10-08T00:00:00", "software", "added"),
                         ("2026-10-06T23:59:59", "software", "removed")):
        add_event(inv_db, at(ts), cat, act)
    rows = await snapshot(inv_db)

    assert {m for m, _ in rows} == set(svc.SNAPSHOT_METRICS)
    assert snapshot_rows(inv_db, DAY) == rows
    assert {d: v for (m, d), v in rows.items() if m == "assets"} == {"server": 4, "device": 3}
    assert {d: v for (m, d), v in rows.items() if m == "coverage"} == {"fresh": 2, "aging": 2, "stale": 1, "never": 2}
    assert {d: v for (m, d), v in rows.items() if m == "os_release"} == {
        "windows/Windows Server/2022": 1, "windows/Windows Server/2019": 2, "linux/Ubuntu/22.04": 1,
        "fortios/FortiOS/7.2": 1, "ios-xe/IOS XE/17.9": 1, "unknown//": 1}
    assert {d: v for (m, d), v in rows.items() if m == "software_products"} == {"products": 4, "installs": 5}
    assert {d: v for (m, d), v in rows.items() if m == "health"} == {
        "missing_os_version": 1, "missing_model": 5, "missing_serial": 6, "unknown_vendor": 5,
        "software_truncated": 1, "stale": 1, "never": 2}
    assert {d: v for (m, d), v in rows.items() if m == "changes"} == {
        "software/added": 2, "software/removed": 0, "software/changed": 0,
        "os/changed": 0, "firmware/changed": 1, "hardware/changed": 0}
    # The linked device is not an asset of its own: no junos row, and the totals add up.
    assert not any("junos" in d for (m, d) in rows if m == "os_release")
    assert sum(v for (m, d), v in rows.items() if m == "os_release") == 7
    assert sum(v for (m, d), v in rows.items() if m == "coverage") == 7


@pytest.mark.asyncio
async def test_snapshot_day_hidden_categories_follow_the_settings(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    rows = await snapshot(inv_db, settings={"hide_categories_by_default": []})
    assert (rows[("software_products", "products")], rows[("software_products", "installs")]) == (6, 7)
    rows = await snapshot(inv_db, settings={"hide_categories_by_default": ["browser", "update", "driver"]})
    assert (rows[("software_products", "products")], rows[("software_products", "installs")]) == (2, 2)


@pytest.mark.asyncio
async def test_snapshot_day_coverage_follows_the_settings_thresholds(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    rows = await snapshot(inv_db, settings={"fresh_hours": {"server": 2, "device": 6}, "stale_days": 2})
    # S1 (1 h) and D1 (30 min) and D2 (5 h) are fresh; S2 (3 days) and S4 (10 days) are past two days.
    assert {d: v for (m, d), v in rows.items() if m == "coverage"} == {"fresh": 3, "aging": 0, "stale": 2, "never": 2}
    assert rows[("health", "stale")] == 2


@pytest.mark.asyncio
async def test_snapshot_day_changes_use_the_local_calendar_day(inv_db):
    add_event(inv_db, at("2026-10-07T18:59:00"))       # 23:59 on the 7th in Karachi
    add_event(inv_db, at("2026-10-07T19:00:00"))       # 00:00 on the 8th in Karachi
    karachi = ZoneInfo("Asia/Karachi")
    assert (await snapshot(inv_db, DAY, karachi))[("changes", "software/added")] == 1
    assert (await snapshot(inv_db, date(2026, 10, 8), karachi))[("changes", "software/added")] == 1
    assert (await snapshot(inv_db, DAY, UTC))[("changes", "software/added")] == 2


@pytest.mark.asyncio
async def test_snapshot_day_is_idempotent_and_replaces_rows_that_no_longer_apply(inv_db):
    f = snapshot_fleet(inv_db)
    await tick(inv_db)
    first = await snapshot(inv_db)
    stored_first = snapshot_rows(inv_db, DAY)
    second = await snapshot(inv_db)
    assert second == first and snapshot_rows(inv_db, DAY) == stored_first
    assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots WHERE day = $1", DAY) == len(first)

    inv_db.execute("DELETE FROM servers WHERE id = $1", f.s2)                       # the only Ubuntu server
    third = await snapshot(inv_db)
    assert ("os_release", "linux/Ubuntu/22.04") not in third
    assert ("os_release", "linux/Ubuntu/22.04") not in snapshot_rows(inv_db, DAY)
    assert third[("assets", "server")] == 3
    assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots WHERE day = $1", DAY) == len(third)


@pytest.mark.asyncio
async def test_snapshot_day_commits_unless_the_caller_keeps_the_transaction(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            held = await svc.snapshot_day(db, DAY, zone=UTC, commit=False)
            assert snapshot_rows(inv_db, DAY) == {}                  # nothing visible to others yet
            await db.commit()
        assert snapshot_rows(inv_db, DAY) == held
        async with factory() as db:
            standalone = await svc.snapshot_day(db, date(2026, 10, 8), zone=UTC)   # the default commits
    assert snapshot_rows(inv_db, date(2026, 10, 8)) == standalone


@pytest.mark.asyncio
async def test_snapshot_day_leaves_other_days_alone(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    await snapshot(inv_db, date(2026, 10, 6))
    before = snapshot_rows(inv_db, date(2026, 10, 6))
    await snapshot(inv_db, DAY)
    assert snapshot_rows(inv_db, date(2026, 10, 6)) == before


@pytest.mark.asyncio
async def test_snapshot_day_on_an_empty_database_writes_zeroes_not_nothing(inv_db):
    rows = await snapshot(inv_db)
    assert rows[("assets", "server")] == 0 and rows[("assets", "device")] == 0
    assert rows[("software_products", "products")] == 0
    assert {d for (m, d) in rows if m == "coverage"} == {"fresh", "aging", "stale", "never"}
    assert {d for (m, d) in rows if m == "health"} == {
        "missing_os_version", "missing_model", "missing_serial", "unknown_vendor", "software_truncated", "stale", "never"}
    assert not [k for k in rows if k[0] == "os_release"]


@pytest.mark.asyncio
async def test_snapshot_day_counts_assets_that_have_no_identity_row_as_unknown(inv_db):
    add_server(inv_db, "bare-server")
    inv_db.execute("DELETE FROM inventory_identity")
    rows = await snapshot(inv_db)
    assert rows[("os_release", "unknown//")] == 1 and rows[("health", "missing_os_version")] == 1
    assert rows[("coverage", "never")] == 1


# ---- snapshot pass

@pytest.mark.asyncio
async def test_snapshot_pass_waits_for_the_hour_runs_once_and_catches_up(inv_db):
    snapshot_fleet(inv_db)
    await tick(inv_db)
    async with sessions(inv_db) as factory:
        early = await svc.run_snapshot_pass(factory, now=at("2026-10-07T00:10:00"), zone=UTC)
        assert (early.due, early.ran, early.day) == (False, False, DAY)
        assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots") == 0

        ran = await svc.run_snapshot_pass(factory, now=at("2026-10-07T00:20:00"), zone=UTC)
        assert (ran.due, ran.ran, ran.locked) == (True, True, True) and ran.rows > 20
        stored = snapshot_rows(inv_db, DAY)

        again = await svc.run_snapshot_pass(factory, now=at("2026-10-07T00:25:00"), zone=UTC)
        assert (again.due, again.ran) == (False, False)
        assert snapshot_rows(inv_db, DAY) == stored

        # The next day the appliance was down at 00:15 and wakes mid-afternoon: it catches up for that day only.
        late = await svc.run_snapshot_pass(factory, now=at("2026-10-09T15:00:00"), zone=UTC)
        assert late.ran and late.day == date(2026, 10, 9)
        assert inv_db.scalar("SELECT count(DISTINCT day) FROM inventory_daily_snapshots") == 2   # no row for the 8th


@pytest.mark.asyncio
async def test_snapshot_pass_uses_the_configured_hour_and_zone(inv_db):
    inv_db.execute("INSERT INTO system_settings (key, value) VALUES ('inventory', '{\"snapshot_hour\": 3}'::jsonb)")
    karachi = ZoneInfo("Asia/Karachi")
    async with sessions(inv_db) as factory:
        # 21:30Z is 02:30 on the 8th in Karachi: before 03:15.
        assert not (await svc.run_snapshot_pass(factory, now=at("2026-10-07T21:30:00"), zone=karachi)).due
        ran = await svc.run_snapshot_pass(factory, now=at("2026-10-07T22:20:00"), zone=karachi)   # 03:20 local
        assert ran.ran and ran.day == date(2026, 10, 8)
    assert snapshot_rows(inv_db, date(2026, 10, 8)) and not snapshot_rows(inv_db, DAY)


@pytest.mark.asyncio
async def test_snapshot_pass_takes_its_day_from_the_configured_timezone(inv_db):
    async with sessions(inv_db) as factory:
        # No company setting: UTC, like every other scheduled feature. 19:20Z is the 7th.
        utc_run = await svc.run_snapshot_pass(factory, now=at("2026-10-07T19:20:00"))
        assert (utc_run.ran, utc_run.day) == (True, DAY)

        inv_db.execute("INSERT INTO system_settings (key, value) VALUES ('company', '{\"timezone\": \"Asia/Karachi\"}'::jsonb)")
        # The same instant is 00:20 on the 8th in Karachi: a new day, and past 00:15.
        karachi_run = await svc.run_snapshot_pass(factory, now=at("2026-10-07T19:20:00"))
        assert (karachi_run.ran, karachi_run.day) == (True, date(2026, 10, 8))
        # 19:10Z is 00:10 there: not due yet, however late it is in UTC.
        early = await svc.run_snapshot_pass(factory, now=at("2026-10-08T19:10:00"))
        assert (early.due, early.day) == (False, date(2026, 10, 9))
    assert snapshot_rows(inv_db, DAY) and snapshot_rows(inv_db, date(2026, 10, 8))
    assert not snapshot_rows(inv_db, date(2026, 10, 9))


@pytest.mark.asyncio
async def test_snapshot_pass_skips_cleanly_while_another_worker_holds_the_lock(inv_db):
    snapshot_fleet(inv_db)
    async with holding_lock(inv_db, svc.SNAPSHOT_LOCK_KEY):
        async with sessions(inv_db) as factory:
            result = await svc.run_snapshot_pass(factory, now=at("2026-10-07T12:00:00"), zone=UTC)
            async with factory() as db:
                purge = await svc.purge_expired(db, retention_days=30, today=DAY)
        assert (result.due, result.ran, result.locked) == (True, False, False)
        assert purge["locked"] is False
    assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots") == 0


@pytest.mark.asyncio
async def test_snapshot_pass_refreshes_yesterdays_change_counts_once_the_day_is_complete(inv_db):
    yesterday = date(2026, 10, 6)
    inv_db.execute("INSERT INTO inventory_daily_snapshots (day, metric, dimension, value) VALUES "
                  "($1, 'changes', 'software/added', 0), ($1, 'assets', 'server', 9)", yesterday)
    add_event(inv_db, at("2026-10-06T08:00:00"))
    add_event(inv_db, at("2026-10-06T20:00:00"), "software", "removed")
    add_event(inv_db, at("2026-10-05T12:00:00"))                       # two days ago: no snapshot, not created
    async with sessions(inv_db) as factory:
        await svc.run_snapshot_pass(factory, now=at("2026-10-07T00:20:00"), zone=UTC)
    rows = snapshot_rows(inv_db, yesterday)
    assert rows[("changes", "software/added")] == 1 and rows[("changes", "software/removed")] == 1
    assert rows[("assets", "server")] == 9                           # only the changes family is refreshed
    assert inv_db.scalar("SELECT count(*) FROM inventory_daily_snapshots WHERE day = $1", date(2026, 10, 5)) == 0


# ---- retention

@pytest.mark.asyncio
async def test_retention_deletes_old_events_and_snapshots_in_batches_and_keeps_recent_ones(inv_db, monkeypatch):
    inv_db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) "
                  "SELECT now() - interval '500 days' - make_interval(mins => g), 'server', gen_random_uuid(), "
                  "'software', 'added', 'old' FROM generate_series(1, 25) g")
    inv_db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) "
                  "VALUES (now() - interval '399 days', 'server', gen_random_uuid(), 'software', 'added', 'keep-399'), "
                  "(now() - interval '1 day', 'server', gen_random_uuid(), 'software', 'added', 'keep-1')")
    inv_db.execute("INSERT INTO inventory_daily_snapshots (day, metric, dimension, value) VALUES "
                  "('2023-10-05', 'assets', 'server', 1), ('2023-10-06', 'assets', 'server', 1), "
                  "('2023-10-07', 'assets', 'server', 1), ('2026-10-06', 'assets', 'server', 1)")
    statements = []
    real = svc._try_lock

    async def counting(db, key):
        statements.append(key)
        return await real(db, key)

    monkeypatch.setattr(svc, "_try_lock", counting)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            out = await svc.purge_expired(db, retention_days=400, today=DAY, batch=10)

    assert out == {"events": 25, "snapshots": 2, "locked": True}
    assert len(statements) == 3 + 1                    # 10, 10, 5 for events; one short pass for snapshots
    assert sorted(r[0] for r in inv_db.query("SELECT subject FROM inventory_change_events")) == ["keep-1", "keep-399"]
    assert [str(r[0]) for r in inv_db.query("SELECT day FROM inventory_daily_snapshots ORDER BY day")] == [
        "2023-10-07", "2026-10-06"]                    # exactly three years old is kept


@pytest.mark.asyncio
async def test_retention_default_batch_is_ten_thousand_rows_per_statement(inv_db, monkeypatch):
    inv_db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) "
                  "SELECT now() - interval '800 days', 'server', gen_random_uuid(), 'software', 'added', 'old' "
                  "FROM generate_series(1, 10005)")
    statements = []
    real = svc._try_lock

    async def counting(db, key):
        statements.append(key)
        return await real(db, key)

    monkeypatch.setattr(svc, "_try_lock", counting)
    async with sessions(inv_db) as factory:
        async with factory() as db:
            out = await svc.purge_expired(db, retention_days=400, today=DAY)
    assert out["events"] == 10005 and len(statements) == 2 + 1
    assert event_count(inv_db) == 0

    # An exact multiple needs one more (empty) statement to prove there is nothing left.
    inv_db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) "
                  "SELECT now() - interval '800 days', 'server', gen_random_uuid(), 'software', 'added', 'old' "
                  "FROM generate_series(1, 10000)")
    statements.clear()
    async with sessions(inv_db) as factory:
        async with factory() as db:
            out = await svc.purge_expired(db, retention_days=400, today=DAY)
    assert out["events"] == 10000 and len(statements) == 2 + 1


@pytest.mark.asyncio
async def test_snapshot_pass_purges_with_the_configured_retention(inv_db):
    inv_db.execute("INSERT INTO system_settings (key, value) VALUES ('inventory', '{\"change_retention_days\": 30}'::jsonb)")
    inv_db.execute("INSERT INTO inventory_change_events (observed_at, asset_kind, asset_id, category, action, subject) VALUES "
                  "(now() - interval '31 days', 'server', gen_random_uuid(), 'software', 'added', 'old'), "
                  "(now() - interval '29 days', 'server', gen_random_uuid(), 'software', 'added', 'recent')")
    async with sessions(inv_db) as factory:
        result = await svc.run_snapshot_pass(factory, now=datetime.now(UTC).replace(hour=12), zone=UTC)
    assert result.ran and result.purged_events == 1
    assert [r[0] for r in inv_db.query("SELECT subject FROM inventory_change_events")] == ["recent"]
