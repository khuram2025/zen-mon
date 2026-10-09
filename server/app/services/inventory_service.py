"""Inventory background service (design doc 02, sections 3.2, 5.3, 5.4, 6 and 9).

Two loops, started from ``main.py`` in every API worker:

``inventory_normalize_loop``
    Every 120 s (45 s after start): fills ``inventory_identity`` for assets that
    have no row, re-normalises rows the capture triggers marked dirty
    (``norm_rev IS NULL``), re-normalises rows produced by an older rule-pack
    revision, and keys ``server_software_inventory`` rows whose ``norm_rev`` is
    missing (every tick, through the partial index ``idx_ssi_unkeyed``) or old
    (one walk of the whole table per process start; the walk resumes across ticks
    from ``TickState.software_cursor``). Batches of 500 identity rows, one
    transaction per batch.

``inventory_snapshot_loop``
    Wakes every 5 minutes. From ``snapshot_hour``:15 appliance time it writes
    the day's ``inventory_daily_snapshots`` rows once (also catching up after
    downtime), then purges expired change events and old snapshots. The loop
    holds the write while the process is younger than SNAPSHOT_SETTLE_S or the
    normalise backlog is not empty (UP-07), so the first point of every series
    is not an artefact of an upgrade that has not settled; during the first 24
    hours after the capture start the current day's row is refreshed (see
    ``run_snapshot_pass``).

Concurrency
-----------
Both loops run in every worker. Each transaction starts with
``pg_try_advisory_xact_lock`` (the baseline_repair_loop / udt_sweeper pattern: a
transaction-scoped lock is released by the commit or rollback that ends the
transaction, so a pooled connection can never leak it) and does nothing when
another worker holds it. The lock is re-taken for every batch, so workers
interleave batches instead of one blocking the others for a whole backlog.

Row locks. Agent ingest locks the ``servers`` row, then the software rows (in the
agent's order), then the identity row (the servers trigger already takes it at the
first OS-column update). This module must never be the second half of a lock
cycle with it, so:

* the identity batch, the software-key backfill and the orphan clean-up are three
  separate short transactions: no transaction here ever holds identity-row locks
  and then asks for software rows of the same server;
* raw data is read without locks. Rows are locked only in the write step, with
  ``FOR UPDATE ... SKIP LOCKED``: a row an upload is writing is skipped and stays
  dirty for the next tick instead of being waited on, and the locks are held for
  one UPDATE, not across the normalisation;
* every transaction sets a short ``lock_timeout`` (BATCH_LOCK_TIMEOUT) for the few
  statements that can still wait (``INSERT .. ON CONFLICT`` behind an in-doubt
  insert). A timeout rolls the transaction back and counts as contention, not as
  a failure. A deadlock is never swallowed: it would mean this design was broken.

Change history
--------------
The normaliser writes ``inventory_identity``; the migration-157 trigger on that
table turns a real OS/firmware/serial/model change into an event. A false event
is worse than a missing one (history cannot be corrected afterwards), so:

* Every write sets ``values_rev`` (the rule-pack revision that rendered the stored
  values) together with ``norm_rev``. The capture trigger fires only when the old
  and the new ``values_rev`` are equal, so a rule-pack revision change never
  writes history, whether the row was dirty or merely old. The price is that a
  genuine change landing in the very write that moves the revision is absorbed.
* Dirty rows followed a real source change, so their write runs with the capture
  triggers enabled. Stale-revision rows (rule-pack upgrade, nothing changed at
  the source) are additionally written with ``zenplus.inventory_suppress = 'on'``.
* Every write is conditional on ``source_changed_at`` still being the value read
  with the row. The dirty-marking triggers advance it on every real source
  change (even when the row is already dirty), so a change that lands between
  the read and the write makes the row match nothing and it stays dirty for the
  next tick instead of being stamped clean with outdated values.
* A source going missing is a gap, not a change. A recomputed identity without an
  OS version never replaces a stored OS block that has one, and template values
  older than ``TEMPLATE_FRESH_HOURS`` are passed on as ``stale_template_values``
  (the lowest-ranked evidence) instead of being dropped. A device that has not
  been polled for that long and whose stored version came from a template keeps it
  against a sticky, older profile value, so an outage never reads as a downgrade.
  A write that moves ``values_rev`` (a rule-pack re-render) is the exception: the new
  pack's answer replaces the stored block, so a value the pack no longer accepts (a
  MikroTik board name stored as the RouterOS version) is cleared, silently (NL-02).
* The loop never writes ``lifecycle_slug`` (the lifecycle phase owns it).

Failures
--------
The normaliser is called with ``strict=True``: a bug in the engine or a pack
raises instead of returning a generic identity that would be stamped clean and
never revisited. The row stays dirty (unstamped), is backed off in a
``FailureLog`` for an hour, and the rest of the batch carries on.

Cancellation
------------
Cancelling a loop while a tick is inside a database call can leave the connection
invalid; closing the session then raises a different exception (the netflow
engine hooks touch ``Connection.info``) that replaces ``CancelledError``. The
session helper here swallows errors on close, and the loops re-raise whenever
their task is being cancelled, so a cancelled loop always ends.

Appliance time
--------------
"Appliance time" is the appliance's configured timezone: the ``company``
system-setting's ``timezone`` (default UTC), read with
``alert_schedule.get_configured_timezone``. It is what report schedules ("Time of
day, appliance timezone") and alert quiet hours already use, so the snapshot day
is the same calendar day the rest of the product schedules by. The operating
system zone that ``system_time.read_timedatectl`` reports is a clock diagnostic
for the UI and is deliberately not used: it would shell out to ``timedatectl``
every five minutes and could disagree with the zone the operator chose. The
snapshot day is the calendar date in that zone, and the "changes" counts cover
that local day.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import functools
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_normalize

logger = logging.getLogger("zenplus.inventory_service")

__all__ = [
    "NORMALIZE_LOCK_KEY", "SNAPSHOT_LOCK_KEY", "SETTINGS_KEY", "DEFAULT_SETTINGS",
    "merge_inventory_settings", "get_inventory_settings",
    "FailureLog", "TickState", "BatchResult", "SoftwareResult", "TickResult", "SnapshotPassResult",
    "normalize_pending_batch", "backfill_software_keys", "delete_orphan_identities",
    "run_normalise_tick", "inventory_normalize_loop",
    "get_appliance_zone", "snapshot_due", "snapshot_local_day", "day_bounds",
    "snapshot_day", "purge_expired", "run_snapshot_pass", "inventory_snapshot_loop",
    "NormaliseBacklog", "normalise_backlog", "SnapshotState", "get_capture_started_at",
]

# Advisory lock keys, pinned in Documentation/Inventory/00-INDEX.md. Nothing else
# in the repository uses either.
NORMALIZE_LOCK_KEY = 1515074397
SNAPSHOT_LOCK_KEY = 1515074398

NORMALIZE_START_DELAY_S = 45
NORMALIZE_INTERVAL_S = 120
NORMALIZE_BATCH = 500
SOFTWARE_BATCH = 2000
ORPHAN_BATCH = 1000
# A tick stops after this many batches so a first-run backlog (15,000 assets,
# millions of software rows) is worked off over several ticks instead of
# monopolising a connection; the next tick continues where this one stopped.
MAX_IDENTITY_BATCHES_PER_TICK = 40
MAX_SOFTWARE_BATCHES_PER_TICK = 50
# Backstop for the few statements that can still wait on another transaction (the
# row locks themselves are taken with SKIP LOCKED): a timeout skips the batch.
BATCH_LOCK_TIMEOUT = "3s"
# Template values older than this do not count as evidence (doc 02 section 3.2).
TEMPLATE_FRESH_HOURS = 24
TEMPLATE_METRIC_KEYS = ("fgt_fw_version", "pan_sw_version", "f5_version", "f5_serial")
# A row whose normalisation failed is not retried for this long.
FAILURE_BACKOFF_S = 3600
MAX_FAILURE_ENTRIES = 5000
# Unkeyed software rows (norm_rev IS NULL) are found through the partial index
# idx_ssi_unkeyed, which is tiny while nothing is pending. Rows keyed by an older
# rule-pack revision have no index; that scan walks the whole table, so it runs
# once per process start (TickState.software_stale_rev), not on every tick: a
# rule-pack bump only ships with a restart, so an upgrade is never delayed. After
# a scan from the top that found nothing, wait this long before looking again:
# agent ingest keys its own rows inline, so only a failed inline path or a
# legacy writer ever leaves rows behind.
SOFTWARE_IDLE_RECHECK_S = 600
# At most this many tracebacks per batch for rows that fail one by one; the rest
# of the batch is summarised in one line (a database outage fails every row).
MAX_FAILURE_LOGS_PER_BATCH = 5

SNAPSHOT_START_DELAY_S = 60
SNAPSHOT_POLL_S = 300
SNAPSHOT_MINUTE = 15
# UP-07. The first daily row after an upgrade used to be written 60 s after the process started, before the
# agents had reported (a server reports every 6 h) and before the normalise backlog had drained, and was then
# frozen for three years. A snapshot is therefore held until the process has been up for SNAPSHOT_SETTLE_S and
# no row waits for the normaliser; it is not held for ever (a row that fails to normalise would otherwise stop
# the history): SNAPSHOT_HOLD_MAX_S after the day's snapshot became due (or the process started, if later) it is
# written anyway.
SNAPSHOT_SETTLE_S = 30 * 60
SNAPSHOT_HOLD_MAX_S = 12 * 3600
# During the first 24 hours after the capture start (system_settings['inventory.capture_started_at'], written by
# migration 158) today's row is refreshed, at most once per SNAPSHOT_REFRESH_S per process, while the estate
# settles. A day that has closed is never rewritten, and outside that window a day is written exactly once.
SNAPSHOT_REFRESH_WINDOW_S = 24 * 3600
SNAPSHOT_REFRESH_S = 3600
CAPTURE_KEY = "inventory.capture_started_at"
# Mirrors inventory_query.ENDPOINT_PROTECTION_CATEGORY / _SERVICES (a test keeps the two equal; this module
# cannot import that one, which imports this).
ENDPOINT_PROTECTION_CATEGORY = "endpoint-protection"
ENDPOINT_PROTECTION_SERVICES = ("WinDefend", "Sense")
# The most backlog rows a count looks at: the answer is "none" or "at least this many".
BACKLOG_COUNT_CAP = 100_000
PURGE_BATCH = 10_000
SNAPSHOT_RETENTION_YEARS = 3

SETTINGS_KEY = "inventory"
DEFAULT_SETTINGS: dict[str, Any] = {
    "fresh_hours": {"server": 24, "device": 2},
    "stale_days": 7,
    "change_retention_days": 400,
    "snapshot_hour": 0,
    # Runtimes join the noise categories (UI-03 / DT-08): the Visual C++ redistributables, .NET and Java
    # runtimes top every catalog by installs and say nothing about what a server is for. An install that
    # already saved this list keeps exactly what it saved (merge_inventory_settings); only an install that
    # never stored the key gets the new default.
    "hide_categories_by_default": ["update", "driver", "os-component", "runtime"],
    "trends_enabled": True,
}
_FRESH_HOURS_RANGE = (1, 720)
_STALE_DAYS_RANGE = (1, 365)
# Evidence, not a cache: never let a typo purge a month of history.
_RETENTION_DAYS_RANGE = (30, 3650)
_SNAPSHOT_HOUR_RANGE = (0, 23)


# ── Settings ─────────────────────────────────────────────────────────

def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """``value`` as an int inside [lo, hi]; anything that is not a finite number gives ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return max(lo, min(hi, int(round(value))))


def merge_inventory_settings(raw: Any) -> dict[str, Any]:
    """Built-in defaults overlaid with a stored ``inventory`` settings blob.

    Never raises. Unknown keys are dropped, every value is validated and clamped
    on its own (one bad field does not discard the others), and a blob that is
    not an object gives the defaults. ``fresh_hours`` is capped at ``stale_days``
    so the fresh / aging / stale buckets stay in order.

    ``trends_enabled`` is a display switch for the API and UI. The snapshot loop
    records regardless: history cannot be backfilled, so turning trends off must
    not leave a permanent gap for when they are turned back on.
    """
    out: dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return out
    if not isinstance(raw, Mapping):
        return out

    out["stale_days"] = _clamp_int(raw.get("stale_days"), out["stale_days"], *_STALE_DAYS_RANGE)
    fresh = raw.get("fresh_hours")
    if isinstance(fresh, Mapping):
        for kind in ("server", "device"):
            out["fresh_hours"][kind] = _clamp_int(fresh.get(kind), out["fresh_hours"][kind], *_FRESH_HOURS_RANGE)
    for kind in ("server", "device"):
        out["fresh_hours"][kind] = min(out["fresh_hours"][kind], out["stale_days"] * 24)
    out["change_retention_days"] = _clamp_int(
        raw.get("change_retention_days"), out["change_retention_days"], *_RETENTION_DAYS_RANGE)
    out["snapshot_hour"] = _clamp_int(raw.get("snapshot_hour"), out["snapshot_hour"], *_SNAPSHOT_HOUR_RANGE)

    cats = raw.get("hide_categories_by_default")
    if isinstance(cats, (list, tuple)):
        known = set(inventory_normalize.CATEGORIES)
        clean: list[str] = []
        for c in cats:
            if isinstance(c, str) and c in known and c not in clean:
                clean.append(c)
        # [] is a valid choice (hide nothing); a non-empty list of nothing but
        # unknown names is a mistake, not a choice.
        if clean or not cats:
            out["hide_categories_by_default"] = clean
    if isinstance(raw.get("trends_enabled"), bool):
        out["trends_enabled"] = raw["trends_enabled"]
    return out


async def get_inventory_settings(db: AsyncSession) -> dict[str, Any]:
    """``system_settings['inventory']`` merged over the defaults (see merge_inventory_settings)."""
    row = (await db.execute(
        text("SELECT value FROM system_settings WHERE key = :k"), {"k": SETTINGS_KEY},
    )).first()
    return merge_inventory_settings(row[0] if row else None)


# ── Failed-row back-off ──────────────────────────────────────────────

class FailureLog:
    """In-memory record of rows that failed, so one bad row cannot starve the rest.

    ``active()`` is what the selection queries skip. A row is retried once its
    entry has aged out; ``record`` returns True only for a new failure episode, so
    callers log each one once.
    """

    def __init__(self, backoff_s: float = FAILURE_BACKOFF_S, max_entries: int = MAX_FAILURE_ENTRIES,
                 clock: Callable[[], float] = time.monotonic):
        self.backoff_s = backoff_s
        self.max_entries = max_entries
        self._clock = clock
        self._failed: dict[str, float] = {}

    def _prune(self) -> None:
        cutoff = self._clock() - self.backoff_s
        for key in [k for k, at in self._failed.items() if at <= cutoff]:
            del self._failed[key]

    def record(self, key: str) -> bool:
        self._prune()
        new = key not in self._failed
        self._failed[key] = self._clock()
        if len(self._failed) > self.max_entries:
            oldest = min(self._failed, key=self._failed.__getitem__)
            del self._failed[oldest]
        return new

    def clear(self, key: str) -> None:
        self._failed.pop(key, None)

    def active(self) -> list[str]:
        self._prune()
        return list(self._failed)

    def __contains__(self, key: object) -> bool:
        self._prune()
        return key in self._failed

    def __len__(self) -> int:
        self._prune()
        return len(self._failed)


@dataclass
class TickState:
    """What one worker remembers between normalise ticks.

    ``failures`` holds identity asset ids (uuid text, used as a uuid[] parameter);
    ``software_failures`` holds ``server_id/package_name`` keys. They are separate
    logs because the two key shapes must never be mixed in one SQL array.
    """

    failures: FailureLog = field(default_factory=FailureLog)
    software_failures: FailureLog = field(default_factory=FailureLog)
    software_idle_until: float = 0.0
    # Where the software walk stopped when a tick ran out of batches: the next tick
    # continues after it. None when the walk has to start at the top.
    software_cursor: Optional[tuple[str, str]] = None
    # The rule-pack revision for which the full-table scan of old-revision rows has
    # completed in this process; None until it has. Until then the walk also selects
    # rows with an older norm_rev, afterwards only unkeyed ones (partial index).
    software_stale_rev: Optional[int] = None


_STATE = TickState()


# ── Shared SQL helpers ───────────────────────────────────────────────

LOCK_NOT_AVAILABLE = "55P03"   # lock_timeout expired
DEADLOCK_DETECTED = "40P01"


async def _try_lock(db: AsyncSession, key: int) -> bool:
    got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})).scalar()
    return bool(got)


async def _set_lock_timeout(db: AsyncSession) -> None:
    """Transaction-local, so it ends with the commit or rollback and never leaks to the pool."""
    await db.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": BATCH_LOCK_TIMEOUT})


def _sqlstate(exc: BaseException) -> Optional[str]:
    """SQLSTATE of a database error however the driver wrapped it."""
    seen: set[int] = set()
    todo = [exc]
    while todo:
        err = todo.pop()
        if err is None or id(err) in seen:
            continue
        seen.add(id(err))
        code = getattr(err, "sqlstate", None) or getattr(err, "pgcode", None)
        if code:
            return str(code)
        todo += [getattr(err, "orig", None), err.__cause__, err.__context__]
    return None


def _is_contention(exc: BaseException) -> bool:
    """A lock wait that timed out: the batch is skipped, not failed."""
    return isinstance(exc, DBAPIError) and _sqlstate(exc) == LOCK_NOT_AVAILABLE


def _json(items: list) -> str:
    return json.dumps(items, separators=(",", ":"), default=str)


async def _rollback_quietly(db: AsyncSession) -> None:
    """Roll back in an error handler without letting a failing rollback replace the
    error being handled (the connection may already be unusable)."""
    try:
        await db.rollback()
    except Exception:
        logger.debug("inventory: rollback failed on an unusable connection", exc_info=True)


@contextlib.asynccontextmanager
async def _session(factory):
    """``async with factory() as db`` whose close cannot raise.

    Closing a session rolls back; when a cancellation interrupted a statement the
    connection is invalid and the rollback raises another exception that would
    replace CancelledError (module docstring, "Cancellation"). A failed close is
    harmless: the work was committed or rolled back already and the pool discards
    the connection.
    """
    db = factory()
    try:
        yield db
    finally:
        try:
            await asyncio.shield(db.close())
        except Exception:
            logger.debug("inventory: closing the session failed", exc_info=True)


def _reraise_if_cancelling(exc: BaseException) -> None:
    """Called from a loop's ``except Exception``: when the task is being cancelled, an
    unrelated exception (a cleanup error on an invalid connection) has replaced
    CancelledError. Swallowing it would leave a cancelled task running for ever, so
    it is turned back into a cancellation. ``Task.cancelling`` needs Python 3.11."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    if cancelling is not None and cancelling() > 0:
        raise asyncio.CancelledError() from exc


# The CPU-bound normalisation runs on a pool of its own. asyncio.to_thread uses the
# loop's default executor, which every agent upload shares for its ClickHouse insert;
# when ClickHouse is slow all of its threads are busy, and the loop transaction
# (advisory lock held, connection pinned) would sit idle until one frees up.
_WORKERS: Optional[concurrent.futures.ThreadPoolExecutor] = None


async def _in_worker_thread(fn: Callable[..., Any], *args: Any) -> Any:
    global _WORKERS
    if _WORKERS is None:
        _WORKERS = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="inventory-norm")
    return await asyncio.get_running_loop().run_in_executor(_WORKERS, fn, *args)


class _FailureNotes:
    """Caps the log of one batch of failing rows at a few tracebacks and one summary."""

    def __init__(self, label: str):
        self.label = label
        self.logged = 0
        self.suppressed = 0

    def error(self, message: str, *args: Any, exc_info: Any = None) -> None:
        if self.logged < MAX_FAILURE_LOGS_PER_BATCH:
            self.logged += 1
            logger.error(message, *args, exc_info=exc_info)
        else:
            self.suppressed += 1

    def summary(self) -> None:
        if self.suppressed:
            logger.error("%s: %d more rows failed in this batch (not listed)", self.label, self.suppressed)


def _ids(values) -> list[str]:
    return [str(v) for v in values]


# An identity row belongs to an asset that still exists. TRUNCATE does not fire
# the delete triggers, so a row can outlive its asset; those are skipped here and
# removed by delete_orphan_identities.
_ASSET_EXISTS = """(
    (i.asset_kind = 'server' AND EXISTS (SELECT 1 FROM servers s WHERE s.id = i.asset_id))
 OR (i.asset_kind = 'device' AND EXISTS (SELECT 1 FROM devices d WHERE d.id = i.asset_id)))"""

_SELECT_DIRTY = f"""
SELECT i.asset_kind, i.asset_id, i.source_changed_at
  FROM inventory_identity i
 WHERE i.norm_rev IS NULL
   AND {_ASSET_EXISTS}
   AND NOT (i.asset_id = ANY(CAST(:skip AS uuid[])))
 ORDER BY i.source_changed_at, i.asset_id
 LIMIT :n"""

_SELECT_STALE = f"""
SELECT i.asset_kind, i.asset_id, i.source_changed_at
  FROM inventory_identity i
 WHERE i.norm_rev < :rev
   AND {_ASSET_EXISTS}
   AND NOT (i.asset_id = ANY(CAST(:skip AS uuid[])))
 ORDER BY i.norm_rev, i.source_changed_at, i.asset_id
 LIMIT :n"""


def _insert_missing_sql(kind: str, table: str) -> str:
    # Both values come from this module, never from input.
    return f"""
INSERT INTO inventory_identity (asset_kind, asset_id, norm_rev, source_changed_at)
SELECT '{kind}', a.id, NULL, now() FROM {table} a
 WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i
                    WHERE i.asset_kind = '{kind}' AND i.asset_id = a.id)
 LIMIT :n
ON CONFLICT (asset_kind, asset_id) DO NOTHING"""


_SELECT_SERVERS = """
SELECT id, os_name, os_version, kernel_or_build, architecture, cpu_model,
       hw_manufacturer, hw_model, hw_serial
  FROM servers WHERE id = ANY(CAST(:ids AS uuid[]))"""

# ``unpolled``: the poller has not written system info for the device within the
# freshness window (or never has), so a template value older than that is not a
# leftover of a profile change but the last thing the device told us.
_SELECT_DEVICES = """
SELECT d.id, d.vendor, d.model, d.os_version, d.firmware_version, d.serial_number,
       d.sys_object_id, d.sys_descr, p.name AS profile_name,
       (d.identity_updated_at IS NULL
        OR d.identity_updated_at < now() - make_interval(hours => CAST(:fresh AS integer))) AS unpolled
  FROM devices d
  LEFT JOIN device_profiles p ON p.id = d.profile_id
 WHERE d.id = ANY(CAST(:ids AS uuid[]))"""

# Newest value per (device, metric): the template table keeps one row per group
# and instance, and a metric can appear in more than one. ``fresh`` says whether
# that newest value is inside the window; an older one is still evidence (the
# normaliser ranks it last) and must not be dropped, or a device that was
# unreachable for a day would lose its firmware version at the next re-key.
_SELECT_TEMPLATE_VALUES = """
SELECT DISTINCT ON (device_id, metric_key) device_id, metric_key, value_text,
       (updated_at > now() - make_interval(hours => CAST(:fresh AS integer))) AS fresh
  FROM device_template_values
 WHERE device_id = ANY(CAST(:ids AS uuid[]))
   AND metric_key = ANY(CAST(:keys AS text[]))
   AND btrim(value_text) <> ''
 ORDER BY device_id, metric_key, updated_at DESC"""

# The poller's chassisEntitySQL: a root chassis first, then the lowest index (a
# stacked switch's member chassis rows hang off the stack entity).
_SELECT_CHASSIS = """
SELECT DISTINCT ON (device_id) device_id,
       NULLIF(btrim(serial_number), '') AS serial,
       NULLIF(btrim(model_name), '')    AS model,
       NULLIF(btrim(fw_revision), '')   AS firmware
  FROM device_entities
 WHERE device_id = ANY(CAST(:ids AS uuid[])) AND class = 'chassis'
 ORDER BY device_id, (COALESCE(parent_index, 0) <> 0), ent_index"""

# The columns the loop writes: every Identity field except lifecycle_slug, which the
# lifecycle phase (P4) owns and the normaliser never sets (agent ingest leaves it
# alone too). Writing it would reset a stored slug to NULL on every re-key.
_IDENTITY_COLUMNS = tuple(f.name for f in dataclasses.fields(inventory_normalize.Identity)
                          if f.name != "lifecycle_slug")
# The OS columns that travel together: a stored block is kept or replaced as a whole.
_OS_BLOCK = ("os_family", "os_product", "os_release", "os_version", "os_build", "os_edition", "os_source")

# A batch is stored in two statements of one transaction (see _lock_then_write).
#
# 1. Lock: of the rows the batch computed, take the ones that still carry the
#    source_changed_at they were read with (the optimistic-concurrency check of the
#    module docstring) and that no other transaction holds. SKIP LOCKED means a row
#    an agent upload or the poller is writing is left alone, never waited for.
_LOCK_IDENTITY = """
SELECT i.asset_id
  FROM inventory_identity i
  JOIN jsonb_to_recordset(CAST(:rows AS jsonb)) AS v(asset_kind text, asset_id uuid, seen timestamptz)
    ON i.asset_kind = v.asset_kind AND i.asset_id = v.asset_id AND i.source_changed_at = v.seen
 ORDER BY i.asset_kind, i.asset_id
   FOR UPDATE OF i SKIP LOCKED"""

# 2. Write the rows that were locked. source_changed_at is never written. Capture
#    triggers fire for rows whose os_version, hw_serial or hw_model really changed
#    under an unchanged values_rev, unless the caller turned suppression on.
#
#    The stored OS block (_OS_BLOCK) is kept, as a whole, when the write does not move
#    values_rev (it re-keys the same rule-pack revision) and
#    * the recomputed identity has no OS version (a source that went missing is a
#      gap, not a change), unless it names a different known OS family; or
#    * the version was stored from a template, the device has not been polled for a
#      day (``tpl_aged``), and the recomputed version only comes from a source that
#      the template outranks in reality (an older, sticky profile or firmware value).
#    A write that moves values_rev is a rule-pack re-render and is authoritative (NL-02):
#    the new pack decided that the stored value is no longer a value (a MikroTik board
#    name stored as the RouterOS version by revision 1), so keeping it would make the
#    correction apply to new devices only. It is silent, as every re-render is: the capture
#    trigger ignores a write that moves values_rev, and a version that becomes NULL emits
#    no event anyway.
_KEEP_STORED_OS = """(i.values_rev IS NOT DISTINCT FROM :rev AND i.os_version IS NOT NULL AND (
          (v.os_version IS NULL AND (v.os_family = 'unknown' OR v.os_family IS NOT DISTINCT FROM i.os_family))
       OR (v.tpl_aged AND i.os_source IN ('template', 'template_stale')
           AND v.os_source IS DISTINCT FROM 'template' AND v.os_source IS DISTINCT FROM 'template_stale')))"""


def _write_identity_assignment(column: str) -> str:
    if column in _OS_BLOCK:
        return f"{column} = CASE WHEN {_KEEP_STORED_OS} THEN i.{column} ELSE v.{column} END"
    return f"{column} = v.{column}"


_WRITE_IDENTITY = f"""
UPDATE inventory_identity AS i
   SET {", ".join(_write_identity_assignment(c) for c in _IDENTITY_COLUMNS)},
       norm_rev = :rev,
       values_rev = :rev,
       normalized_at = now()
  FROM jsonb_to_recordset(CAST(:rows AS jsonb))
       AS v(asset_kind text, asset_id uuid, seen timestamptz, tpl_aged boolean,
            {", ".join(f"{c} text" for c in _IDENTITY_COLUMNS)})
 WHERE i.asset_kind = v.asset_kind
   AND i.asset_id = v.asset_id
   AND i.source_changed_at = v.seen
RETURNING i.asset_id"""

# SKIP LOCKED so a row an upload holds does not stall the clean-up of the others.
_DELETE_ORPHANS = """
DELETE FROM inventory_identity
 WHERE (asset_kind, asset_id) IN (
        SELECT i.asset_kind, i.asset_id
          FROM inventory_identity i
         WHERE (i.asset_kind = 'server' AND NOT EXISTS (SELECT 1 FROM servers s WHERE s.id = i.asset_id))
            OR (i.asset_kind = 'device' AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.id = i.asset_id))
         LIMIT :n
           FOR UPDATE OF i SKIP LOCKED)"""

# Keyset pagination on the primary key. Without it every batch of a large backfill
# would rescan the rows already done to find the next pending ones (quadratic over
# millions of rows), and a row that can never be stored would be picked first
# forever. A walk moves forward through the table, across ticks (TickState.software_cursor).
# Rows whose last write failed are skipped for the back-off period.
#
# ``norm_rev IS NULL`` is written literally, never as a parameter, so the planner can
# prove that the partial index idx_ssi_unkeyed covers the statement whatever plan
# (custom or generic) the driver's prepared statement ends up with. The old-revision
# variant cannot use an index and is only issued once per process (see
# SOFTWARE_IDLE_RECHECK_S).
#
# A row without a source on a server that is not a Windows machine is never selected.
# Rows from before migration 157 have no source, and only the Windows agent has ever
# produced software rows, so in practice those are Windows rows. On any other OS a NULL
# source would be keyed as a generic package title (dpkg/rpm names need their source to
# be read as identifiers) and stamped clean; leaving it unkeyed lets the next upload,
# which carries the source, key it correctly.
#
# "A Windows machine" is a server whose os_type says so or whose recorded OS name does
# (UP-12): an operator edit or a discovery that filed a Windows server under 'other' or
# 'linux' must not leave its pre-157 rows unkeyed for as long as it stays silent.
#
# normalise_backlog counts exactly these rows, so a row the loop never selects cannot hold
# a daily snapshot for ever. data_status, the endpoint-protection state of the Assets queries,
# the flyout and the daily snapshot ask the same question through ``software_eligible_sql``
# (UP-R2-01, X10): one rule, two renderings, so the banner, the backlog and the 'unknown' state
# can never disagree about which rows are waiting.
_WINDOWS_SERVER_SQL = r"COALESCE({sv}.os_type = 'windows' OR {sv}.os_name ~* '\mwindows\M', false)"


def software_eligible_sql(row: str = "s", server: Optional[str] = None) -> str:
    """SQL predicate: the normalise loop will pick up (or has picked up) the software row aliased ``row``.

    A row with a source is always eligible; a row without one (written before migration 157) only when its server is a
    Windows machine (see the comment above). ``server`` is the alias of an already joined ``servers`` row, which the
    predicate then reads directly; without it the predicate asks with a correlated EXISTS of its own (alias ``sv``,
    so a caller that has its own ``sv`` must pass ``server``). Both renderings are the same rule, and neither is ever
    NULL (a server without an os_name is simply not a Windows machine), so ``NOT software_eligible_sql(...)`` is safe."""
    if server is not None:
        return f"({row}.source IS NOT NULL OR {_WINDOWS_SERVER_SQL.format(sv=server)})"
    return (f"({row}.source IS NOT NULL OR EXISTS (SELECT 1 FROM servers sv WHERE sv.id = {row}.server_id AND "
            f"{_WINDOWS_SERVER_SQL.format(sv='sv')}))")


_SOFTWARE_ELIGIBLE = software_eligible_sql("s")


def _select_software_sql(*, include_stale: bool, after: bool) -> str:
    pending = "(s.norm_rev IS NULL OR s.norm_rev < :rev)" if include_stale else "s.norm_rev IS NULL"
    cursor = ("\n   AND (s.server_id, s.package_name) > (CAST(:after_server AS uuid), CAST(:after_package AS varchar))"
              if after else "")
    return f"""
SELECT s.server_id, s.package_name, s.vendor, s.source
  FROM server_software_inventory s
 WHERE {pending}{cursor}
   AND {_SOFTWARE_ELIGIBLE}
   AND NOT (CAST(s.server_id AS text) || '/' || s.package_name = ANY(CAST(:skip AS text[])))
 ORDER BY s.server_id, s.package_name
 LIMIT :n"""


# Software rows are matched on the PK plus the two inputs the key was computed from,
# so a row whose vendor or source changed after it was read is left for the next
# pass. The lock statement skips rows an upload holds; the write statement then
# touches only the locked ones. Neither writes version (no capture trigger fires) or
# updated_at (the ingest prune depends on it).
_LOCK_SOFTWARE = """
SELECT s.server_id, s.package_name
  FROM server_software_inventory s
  JOIN jsonb_to_recordset(CAST(:rows AS jsonb))
       AS v(server_id uuid, package_name text, vendor text, source text,
            name_key text, publisher_key text, category text)
    ON s.server_id = v.server_id AND s.package_name = v.package_name
   AND s.vendor IS NOT DISTINCT FROM v.vendor
   AND s.source IS NOT DISTINCT FROM v.source
 WHERE (s.norm_rev IS NULL OR s.norm_rev < :rev)
 ORDER BY s.server_id, s.package_name
   FOR UPDATE OF s SKIP LOCKED"""

_WRITE_SOFTWARE = """
UPDATE server_software_inventory AS s
   SET name_key = v.name_key,
       publisher_key = v.publisher_key,
       category = v.category,
       norm_rev = :rev
  FROM jsonb_to_recordset(CAST(:rows AS jsonb))
       AS v(server_id uuid, package_name text, vendor text, source text,
            name_key text, publisher_key text, category text)
 WHERE s.server_id = v.server_id
   AND s.package_name = v.package_name
   AND s.vendor IS NOT DISTINCT FROM v.vendor
   AND s.source IS NOT DISTINCT FROM v.source
   AND (s.norm_rev IS NULL OR s.norm_rev < :rev)
RETURNING s.server_id, s.package_name"""


# ── Identity normalisation ───────────────────────────────────────────

@dataclass
class BatchResult:
    """Counts from one normalize_pending_batch call (one transaction)."""

    locked: bool = False             # False: another worker held the lock, nothing was done
    contended: bool = False          # a lock wait timed out: the batch was rolled back and skipped
    created: int = 0                 # identity rows inserted for assets that had none
    dirty: int = 0                   # dirty rows stored (capture triggers enabled)
    stale: int = 0                   # old-revision rows stored (capture suppressed)
    selected: int = 0                # rows picked up, stored or not
    skipped: int = 0                 # source changed since the read, or row held by another
                                     # transaction; left dirty for the next tick
    failed: int = 0                  # normalisation or write failed; backed off
    packs_unhealthy: bool = False    # rule packs did not load: nothing was stamped

    @property
    def written(self) -> int:
        return self.dirty + self.stale


@dataclass
class _Candidate:
    kind: str
    id: str
    seen: datetime


def _has(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _identity_for_server(raw: Mapping[str, Any]) -> inventory_normalize.Identity:
    return inventory_normalize.normalize_server(
        raw["os_name"], raw["os_version"], raw["kernel_or_build"], raw["architecture"],
        cpu_model=raw["cpu_model"], hw_manufacturer=raw["hw_manufacturer"],
        hw_model=raw["hw_model"], hw_serial=raw["hw_serial"], strict=True,
    )


def _identity_for_device(raw: Mapping[str, Any], templates: Mapping[str, str],
                         chassis: Optional[Mapping[str, Any]],
                         stale_templates: Optional[Mapping[str, str]] = None) -> inventory_normalize.Identity:
    """Device identity; the chassis ENTITY-MIB row fills model, serial and firmware
    the device row lacks (the poller copies them across, but leaves a serial that
    another device already holds). ``templates`` are the fresh template values,
    ``stale_templates`` the older ones, which the normaliser ranks last."""
    chassis = chassis or {}
    model, serial, firmware = raw["model"], raw["serial_number"], raw["firmware_version"]
    model_from_chassis = not _has(model) and _has(chassis.get("model"))
    serial_from_chassis = not _has(serial) and _has(chassis.get("serial"))
    firmware_from_chassis = not _has(firmware) and _has(chassis.get("firmware"))
    if model_from_chassis:
        model = chassis["model"]
    if serial_from_chassis:
        serial = chassis["serial"]
    if firmware_from_chassis:
        firmware = chassis["firmware"]

    identity = inventory_normalize.normalize_device(
        vendor=raw["vendor"], model=model, os_version=raw["os_version"], firmware_version=firmware,
        serial_number=serial, sys_object_id=raw["sys_object_id"], sys_descr=raw["sys_descr"],
        profile_name=raw["profile_name"], template_values=templates,
        stale_template_values=stale_templates or {}, strict=True,
    )
    # os_source needs no relabelling: normalize_device already reports a winning
    # firmware_version argument as 'entity'. The hardware source is 'device'
    # unless the chassis row supplied something that ended up in the identity.
    if ((model_from_chassis and identity.hw_model) or (serial_from_chassis and identity.hw_serial)):
        identity = dataclasses.replace(identity, hw_source="entity")
    return identity


def _compute_identities(candidates: list[_Candidate], servers: dict, devices: dict,
                        templates: dict, chassis: dict,
                        stale_templates: Optional[dict] = None) -> list[tuple[_Candidate, Optional[Any], Optional[Exception]]]:
    """Pure CPU work for the worker thread. A row whose raw data vanished (asset
    deleted since the selection) is dropped; a row that raises is reported with its
    error so it can be backed off without poisoning the rest of the batch. The
    normaliser runs strict, so an internal failure is such an error instead of a
    generic identity."""
    stale_templates = stale_templates or {}
    out: list[tuple[_Candidate, Optional[Any], Optional[Exception]]] = []
    for c in candidates:
        try:
            if c.kind == "server":
                raw = servers.get(c.id)
                if raw is None:
                    continue
                identity = _identity_for_server(raw)
            else:
                raw = devices.get(c.id)
                if raw is None:
                    continue
                identity = _identity_for_device(raw, templates.get(c.id, {}), chassis.get(c.id),
                                                stale_templates.get(c.id, {}))
            out.append((c, identity, None))
        except Exception as exc:
            out.append((c, None, exc))
    return out


async def _load_raw(db: AsyncSession, candidates: list[_Candidate]):
    server_ids = _ids(c.id for c in candidates if c.kind == "server")
    device_ids = _ids(c.id for c in candidates if c.kind == "device")
    servers: dict[str, dict] = {}
    devices: dict[str, dict] = {}
    templates: dict[str, dict[str, str]] = {}
    stale_templates: dict[str, dict[str, str]] = {}
    chassis: dict[str, dict] = {}
    if server_ids:
        for r in (await db.execute(text(_SELECT_SERVERS), {"ids": server_ids})).mappings().all():
            servers[str(r["id"])] = dict(r)
    if device_ids:
        for r in (await db.execute(text(_SELECT_DEVICES), {
                "ids": device_ids, "fresh": TEMPLATE_FRESH_HOURS})).mappings().all():
            devices[str(r["id"])] = dict(r)
        for r in (await db.execute(text(_SELECT_TEMPLATE_VALUES), {
                "ids": device_ids, "keys": list(TEMPLATE_METRIC_KEYS), "fresh": TEMPLATE_FRESH_HOURS,
        })).mappings().all():
            target = templates if r["fresh"] else stale_templates
            target.setdefault(str(r["device_id"]), {})[r["metric_key"]] = r["value_text"]
        # The chassis row only matters where the device row has a gap.
        gaps = [i for i, d in devices.items()
                if not (_has(d["model"]) and _has(d["serial_number"]) and _has(d["firmware_version"]))]
        if gaps:
            for r in (await db.execute(text(_SELECT_CHASSIS), {"ids": gaps})).mappings().all():
                chassis[str(r["device_id"])] = dict(r)
    return servers, devices, templates, chassis, stale_templates


async def _lock_then_write(db: AsyncSession, lock_sql: str, write_sql: str, items: list[dict], rev: int,
                           key_of: Callable[[dict], str], returned_key: Callable[[Any], str],
                           failures: Optional[FailureLog], label: str) -> tuple[set[str], list[str]]:
    """Store a batch without waiting for another transaction's row lock.

    Statement 1 locks (FOR UPDATE SKIP LOCKED) the rows that still match what was read;
    statement 2 writes the locked rows only. Rows that are neither updated nor failed
    were skipped (changed since the read, or held by another transaction) and are left
    for the next tick. If a statement fails (one row that the database rejects fails
    them all), retry row by row, each in a savepoint, so only the offending rows are
    lost. A lock timeout or deadlock is not a bad row: it is re-raised at once instead of
    being retried row by row. Returns (keys updated, keys that failed).

    Keys can contain agent-supplied text (package names), so they are logged with
    ``%r``, and a batch that fails row by row logs a few tracebacks and one summary.
    """
    async def run(chunk: list[dict]) -> set[str]:
        async with db.begin_nested():
            locked = {returned_key(r) for r in (await db.execute(
                text(lock_sql), {"rows": _json(chunk), "rev": rev})).all()}
            held = [c for c in chunk if key_of(c) in locked]
            if not held:
                return set()
            rows = (await db.execute(text(write_sql), {"rows": _json(held), "rev": rev})).all()
        return {returned_key(r) for r in rows}

    try:
        return await run(items), []
    except Exception as exc:
        if isinstance(exc, DBAPIError) and _sqlstate(exc) in (LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED):
            raise
        logger.warning("%s: batch write failed, retrying row by row", label, exc_info=True)
    updated: set[str] = set()
    failed: list[str] = []
    notes = _FailureNotes(label)
    for item in items:
        key = key_of(item)
        try:
            updated |= await run([item])
        except Exception as exc:
            if isinstance(exc, DBAPIError) and _sqlstate(exc) in (LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED):
                raise
            failed.append(key)
            if failures is None or failures.record(key):
                notes.error("%s: write failed for %r; retry in %ss", label, key,
                            int(failures.backoff_s) if failures is not None else 0, exc_info=True)
    notes.summary()
    return updated, failed


async def _normalise_rows(db: AsyncSession, candidates: list[_Candidate], rev: int,
                          failures: FailureLog, result: BatchResult) -> int:
    """Normalise, then conditionally store, one group of rows. Returns the number stored."""
    if not candidates:
        return 0
    servers, devices, templates, chassis, stale_templates = await _load_raw(db, candidates)
    computed = await _in_worker_thread(
        _compute_identities, candidates, servers, devices, templates, chassis, stale_templates)

    payload: list[dict] = []
    notes = _FailureNotes("inventory normalise")
    for c, identity, error in computed:
        if error is not None or identity is None:
            result.failed += 1
            if failures.record(c.id):
                notes.error("inventory normalise failed for %s %r: %r; retry in %ss",
                            c.kind, c.id, error, int(failures.backoff_s), exc_info=error)
            continue
        data = dataclasses.asdict(identity)
        # An old template value only pins the stored version while the device is silent:
        # a device that is still polled but no longer reports the template (profile
        # change) must not be frozen by a leftover row.
        tpl_aged = bool(c.kind == "device" and devices.get(c.id, {}).get("unpolled")
                        and stale_templates.get(c.id))
        payload.append({"asset_kind": c.kind, "asset_id": c.id, "seen": c.seen.isoformat(),
                        "tpl_aged": tpl_aged, **{col: data[col] for col in _IDENTITY_COLUMNS}})
    notes.summary()
    if not payload:
        return 0
    # A stable order, for the row-by-row fallback; the lock statement orders its own locks.
    payload.sort(key=lambda p: (p["asset_kind"], p["asset_id"]))
    updated, failed = await _lock_then_write(
        db, _LOCK_IDENTITY, _WRITE_IDENTITY, payload, rev, lambda p: p["asset_id"], lambda r: str(r[0]),
        failures, "inventory identity")
    result.failed += len(failed)
    for p in payload:
        if p["asset_id"] in updated:
            failures.clear(p["asset_id"])
    result.skipped += len(payload) - len(updated) - len(failed)
    return len(updated)


async def normalize_pending_batch(db: AsyncSession, limit: int = NORMALIZE_BATCH, *,
                                  failures: Optional[FailureLog] = None) -> BatchResult:
    """One normalise transaction: lock, create missing identity rows, normalise up to
    ``limit`` dirty rows, then (with room left) up to ``limit`` minus that of
    stale-revision rows. Commits on success, rolls back and re-raises on error.

    Dirty rows are written with the capture triggers enabled (a real source change
    may be an OS or firmware change). Stale-revision rows are written with
    ``zenplus.inventory_suppress`` on, so a rule-pack upgrade writes no events. That
    phase is last, so the setting needs no reset before the commit ends it.

    Raw data is read without row locks; the write step locks the identity rows with
    SKIP LOCKED (module docstring), so a row an upload is writing is skipped and
    stays dirty. A lock wait that times out returns ``contended`` instead of raising.
    """
    failures = failures if failures is not None else _STATE.failures
    result = BatchResult()
    try:
        if not await _try_lock(db, NORMALIZE_LOCK_KEY):
            await db.rollback()
            return result
        result.locked = True
        await _set_lock_timeout(db)
        for kind, table in (("server", "servers"), ("device", "devices")):
            result.created += (await db.execute(text(_insert_missing_sql(kind, table)), {"n": limit})).rowcount or 0

        if not inventory_normalize.packs_healthy():
            # A broken pack degrades every result to the generic fallback; stamping
            # norm_rev now would freeze those keys under the current revision.
            result.packs_unhealthy = True
            await db.commit()
            return result

        rev = inventory_normalize.RULEPACK_REV
        skip = failures.active()
        dirty = [_Candidate(r[0], str(r[1]), r[2]) for r in (await db.execute(
            text(_SELECT_DIRTY), {"skip": skip, "n": limit})).all()]
        result.selected += len(dirty)
        result.dirty = await _normalise_rows(db, dirty, rev, failures, result)

        room = limit - len(dirty)
        if room > 0:
            stale = [_Candidate(r[0], str(r[1]), r[2]) for r in (await db.execute(
                text(_SELECT_STALE), {"rev": rev, "skip": skip, "n": room})).all()]
            result.selected += len(stale)
            if stale:
                await db.execute(text("SELECT set_config('zenplus.inventory_suppress', 'on', true)"))
                result.stale = await _normalise_rows(db, stale, rev, failures, result)
        await db.commit()
        return result
    except Exception as exc:
        await _rollback_quietly(db)
        if _is_contention(exc):
            logger.warning("inventory normalise: waited too long for a lock (%s); batch skipped", BATCH_LOCK_TIMEOUT)
            return BatchResult(locked=True, contended=True)
        raise


async def delete_orphan_identities(db: AsyncSession, limit: int = ORPHAN_BATCH) -> int:
    """Delete up to ``limit`` identity rows whose server or device no longer exists
    (TRUNCATE does not fire the delete triggers). Change events are never touched.
    Rows another transaction holds are skipped; a lock timeout removes nothing."""
    try:
        if not await _try_lock(db, NORMALIZE_LOCK_KEY):
            await db.rollback()
            return 0
        await _set_lock_timeout(db)
        removed = (await db.execute(text(_DELETE_ORPHANS), {"n": limit})).rowcount or 0
        await db.commit()
        return removed
    except Exception as exc:
        await _rollback_quietly(db)
        if _is_contention(exc):
            logger.warning("inventory orphan clean-up: waited too long for a lock; skipped")
            return 0
        raise


# ── Software key backfill ────────────────────────────────────────────

@dataclass
class SoftwareResult:
    locked: bool = False
    contended: bool = False          # a lock wait timed out: the batch was rolled back and skipped
    selected: int = 0
    written: int = 0
    skipped: int = 0                 # vendor/source changed since the read, or row held by an upload
    failed: int = 0
    packs_unhealthy: bool = False
    # (server_id, package_name) of the last row selected: pass it back as ``after`` to
    # continue the walk where this batch stopped.
    cursor: Optional[tuple[str, str]] = None


def _compute_software_keys(rows: list[dict]) -> list[dict]:
    """One item per row, for the worker thread. The normaliser runs strict: a row it
    cannot key comes back as ``{"server_id", "package_name", "error"}`` instead of the
    generic 'unnamed-software' key that would be stamped clean, and one such row never
    fails the others."""
    out = []
    for r in rows:
        try:
            key = inventory_normalize.normalize_software(
                r["package_name"], r["vendor"], source=r["source"], strict=True)
        except Exception as exc:
            out.append({"server_id": str(r["server_id"]), "package_name": r["package_name"], "error": exc})
            continue
        out.append({"server_id": str(r["server_id"]), "package_name": r["package_name"],
                    "vendor": r["vendor"], "source": r["source"], "name_key": key.name_key,
                    "publisher_key": key.publisher_key, "category": key.category})
    return out


async def backfill_software_keys(db: AsyncSession, limit: int = SOFTWARE_BATCH, *,
                                 after: Optional[tuple[str, str]] = None,
                                 failures: Optional[FailureLog] = None,
                                 include_stale: bool = True) -> SoftwareResult:
    """Key up to ``limit`` software rows whose ``norm_rev`` is missing (and, with
    ``include_stale``, old), in primary key order, starting after the
    ``(server_id, package_name)`` given as ``after``.

    ``include_stale=False`` selects unkeyed rows only, through the partial index
    ``idx_ssi_unkeyed``; the default also selects rows keyed by an older rule-pack
    revision, which needs a scan of the whole table (the tick does that once per
    process start). A row without a source on a server that is not a Windows machine
    is never selected (see ``_select_software_sql``).

    Writes only name_key, publisher_key, category and norm_rev: never ``version``
    (no capture trigger fires) and never ``updated_at`` (the ingest prune depends
    on it). Rows are read without locks and locked with SKIP LOCKED for the write
    only, so an agent upload that holds software rows is never waited for (module
    docstring). Commits on success, rolls back and re-raises on error; a lock wait
    that times out returns ``contended``.

    ``failures`` holds ``server_id/package_name`` keys (``TickState.software_failures``):
    rows in it are not selected until their back-off ends. A row the normaliser
    cannot key is left unstamped and goes in there.
    """
    failures = failures if failures is not None else _STATE.software_failures
    result = SoftwareResult()
    try:
        if not await _try_lock(db, NORMALIZE_LOCK_KEY):
            await db.rollback()
            return result
        result.locked = True
        if not inventory_normalize.packs_healthy():
            result.packs_unhealthy = True
            await db.commit()
            return result
        await _set_lock_timeout(db)
        rev = inventory_normalize.RULEPACK_REV
        params: dict[str, Any] = {"n": limit, "skip": failures.active()}
        if include_stale:
            params["rev"] = rev
        if after is not None:
            params.update(after_server=str(after[0]), after_package=after[1])
        fetched = await db.execute(
            text(_select_software_sql(include_stale=include_stale, after=after is not None)), params)
        rows = [dict(r) for r in fetched.mappings().all()]
        result.selected = len(rows)
        if rows:
            result.cursor = (str(rows[-1]["server_id"]), rows[-1]["package_name"])
            computed = await _in_worker_thread(_compute_software_keys, rows)
            notes = _FailureNotes("inventory software keys")
            items = []
            for item in computed:
                if "error" not in item:
                    items.append(item)
                    continue
                key = f"{item['server_id']}/{item['package_name']}"
                result.failed += 1
                if failures.record(key):
                    notes.error("inventory software key failed for %r: %r; retry in %ss",
                                key, item["error"], int(failures.backoff_s), exc_info=item["error"])
            notes.summary()
            items.sort(key=lambda p: (p["server_id"], p["package_name"]))
            updated, failed = await _lock_then_write(
                db, _LOCK_SOFTWARE, _WRITE_SOFTWARE, items, rev, lambda p: f"{p['server_id']}/{p['package_name']}",
                lambda r: f"{r[0]}/{r[1]}", failures, "inventory software")
            result.written = len(updated)
            result.failed += len(failed)
            result.skipped = len(items) - len(updated) - len(failed)
        await db.commit()
        return result
    except Exception as exc:
        await _rollback_quietly(db)
        if _is_contention(exc):
            logger.warning("inventory software keys: waited too long for a lock (%s); batch skipped",
                           BATCH_LOCK_TIMEOUT)
            return SoftwareResult(locked=True, contended=True)
        raise


# ── Normalise tick and loop ──────────────────────────────────────────

@dataclass
class TickResult:
    locked: bool = True              # False when another worker held the lock at the first batch
    contended: bool = False          # a batch gave up waiting for a row lock; the tick stopped early
    packs_unhealthy: bool = False
    orphans_removed: int = 0
    created: int = 0
    dirty: int = 0
    stale: int = 0
    skipped: int = 0                 # changed since the read or held by an upload; left for the next tick
    failed: int = 0
    software: int = 0
    identity_batches: int = 0
    software_batches: int = 0


def _default_session_factory():
    from app.core.database import AsyncSessionLocal
    return AsyncSessionLocal


async def run_normalise_tick(session_factory=None, *, state: Optional[TickState] = None,
                             batch_size: int = NORMALIZE_BATCH, software_batch_size: int = SOFTWARE_BATCH,
                             max_batches: int = MAX_IDENTITY_BATCHES_PER_TICK,
                             max_software_batches: int = MAX_SOFTWARE_BATCHES_PER_TICK) -> TickResult:
    """One pass of the normalise loop: orphan clean-up, identity batches until the
    backlog is empty (or ``max_batches``), then software-key batches. Every part is
    its own short transaction (module docstring, "Row locks")."""
    factory = session_factory or _default_session_factory()
    state = state or _STATE
    result = TickResult()

    if not inventory_normalize.packs_healthy():
        result.packs_unhealthy = True
        logger.warning("inventory rule packs are not healthy (%s); leaving rows dirty",
                       "; ".join(inventory_normalize.PACK_ERRORS[:3]) or "unknown error")

    async with _session(factory) as db:
        result.orphans_removed = await delete_orphan_identities(db)

    for _ in range(max_batches):
        async with _session(factory) as db:
            batch = await normalize_pending_batch(db, batch_size, failures=state.failures)
        if not batch.locked:
            result.locked = False
            break
        if batch.contended:
            result.contended = True
            break
        result.identity_batches += 1
        result.created += batch.created
        result.dirty += batch.dirty
        result.stale += batch.stale
        result.skipped += batch.skipped
        result.failed += batch.failed
        if batch.packs_unhealthy:
            result.packs_unhealthy = True
            break
        # A short batch means the backlog is empty; a batch that stored nothing
        # (all skipped, all failed) would only repeat itself.
        if batch.selected < batch_size or batch.written == 0:
            break

    if (result.locked and not result.contended and not result.packs_unhealthy
            and time.monotonic() >= state.software_idle_until):
        rev = inventory_normalize.RULEPACK_REV
        # Until the whole table has been checked once for rows of an older revision
        # (once per process start), the walk selects those too; afterwards it only
        # asks the partial index for unkeyed rows.
        include_stale = state.software_stale_rev != rev
        cursor = state.software_cursor
        walk_done = False
        for _ in range(max_software_batches):
            async with _session(factory) as db:
                sw = await backfill_software_keys(db, software_batch_size, after=cursor,
                                                  failures=state.software_failures, include_stale=include_stale)
            if not sw.locked:
                break
            if sw.contended:
                result.contended = True
                break
            if sw.packs_unhealthy:
                result.packs_unhealthy = True
                break
            result.software_batches += 1
            result.software += sw.written
            result.skipped += sw.skipped
            result.failed += sw.failed
            if sw.selected == 0 and cursor is None:
                state.software_idle_until = time.monotonic() + SOFTWARE_IDLE_RECHECK_S
                walk_done = True
                break
            # The walk moves forward whether or not a row was stored, so rows that
            # were skipped or cannot be written wait for the next walk instead of
            # being selected again and again.
            if sw.selected < software_batch_size:
                walk_done = True
                break
            cursor = sw.cursor
        # A walk that ran out of batches (or was interrupted) resumes on the next tick
        # where it stopped; one that reached the end starts from the top again.
        state.software_cursor = None if walk_done else cursor
        if walk_done and include_stale:
            state.software_stale_rev = rev

    if (result.orphans_removed or result.created or result.dirty or result.stale
            or result.software or result.failed or result.contended):
        logger.info(
            "inventory normalise: %d created, %d dirty, %d re-keyed, %d software keyed, "
            "%d orphans removed, %d skipped, %d failed%s (%d identity and %d software batches)",
            result.created, result.dirty, result.stale, result.software, result.orphans_removed,
            result.skipped, result.failed, ", stopped early on a lock wait" if result.contended else "",
            result.identity_batches, result.software_batches)
    return result


async def inventory_normalize_loop(*, start_delay: float = NORMALIZE_START_DELAY_S,
                                   interval: float = NORMALIZE_INTERVAL_S, tick=None) -> None:
    """Background task: normalise on a fixed interval. Never dies; cancellation propagates,
    including when the cancel surfaces as another exception from a cleanup (the loop then
    turns it back into a cancellation instead of logging it and carrying on).

    ``tick`` is a coroutine function taking no arguments (default
    ``run_normalise_tick``); tests inject their own and tiny delays.
    """
    tick = tick or run_normalise_tick
    await asyncio.sleep(start_delay)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _reraise_if_cancelling(exc)
            logger.exception("inventory normalise tick failed")
        await asyncio.sleep(interval)


# ── Normalise backlog ────────────────────────────────────────────────

@dataclass(frozen=True)
class NormaliseBacklog:
    """What the normalise loop still has to do, install-wide. ``software_rows`` are software rows with no key or a
    key from an older rule-pack revision that the loop will pick up (a row the loop never selects, see
    _select_software_sql, is not backlog: it would hold a snapshot for ever); ``identity_rows`` are identity rows
    that are dirty or old plus assets that have no identity row yet. Each count stops at ``cap``."""

    software_rows: int = 0
    identity_rows: int = 0

    @property
    def total(self) -> int:
        return self.software_rows + self.identity_rows

    @property
    def normalising(self) -> bool:
        return self.total > 0


async def normalise_backlog(db: AsyncSession, *, cap: int = BACKLOG_COUNT_CAP) -> NormaliseBacklog:
    """Rows still waiting for the normaliser, install-wide. Cheap: the unkeyed software rows are found through the
    partial index idx_ssi_unkeyed, the identity table is small, and the old-revision software scan (no index)
    stops at ``cap`` rows (it reads the whole table only when nothing is old, once per call)."""
    rev = inventory_normalize.RULEPACK_REV
    unkeyed = (await db.execute(text(
        f"SELECT count(*) FROM (SELECT 1 FROM server_software_inventory s WHERE s.norm_rev IS NULL "
        f"AND {_SOFTWARE_ELIGIBLE} LIMIT :cap) q"), {"cap": cap})).scalar() or 0
    old = 0
    if unkeyed < cap:
        old = (await db.execute(text(
            f"SELECT count(*) FROM (SELECT 1 FROM server_software_inventory s WHERE s.norm_rev < :rev "
            f"AND {_SOFTWARE_ELIGIBLE} LIMIT :cap) q"), {"rev": rev, "cap": cap - int(unkeyed)})).scalar() or 0
    dirty = (await db.execute(text(
        f"SELECT count(*) FROM (SELECT 1 FROM inventory_identity i WHERE (i.norm_rev IS NULL OR i.norm_rev < :rev) "
        f"AND {_ASSET_EXISTS} LIMIT :cap) q"), {"rev": rev, "cap": cap})).scalar() or 0
    missing = (await db.execute(text(
        "SELECT (SELECT count(*) FROM (SELECT 1 FROM servers a WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i "
        "WHERE i.asset_kind = 'server' AND i.asset_id = a.id) LIMIT :cap) x) "
        "+ (SELECT count(*) FROM (SELECT 1 FROM devices a WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i "
        "WHERE i.asset_kind = 'device' AND i.asset_id = a.id) LIMIT :cap) y)"), {"cap": cap})).scalar() or 0
    return NormaliseBacklog(software_rows=int(unkeyed) + int(old), identity_rows=int(dirty) + int(missing))


async def get_capture_started_at(db: AsyncSession) -> Optional[datetime]:
    """When change capture began (``system_settings['inventory.capture_started_at']``, an ISO-8601 UTC string
    written by migration 158), or None when it is missing or unreadable."""
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": CAPTURE_KEY})).first()
    raw = row[0] if row else None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str) and raw.startswith('"'):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# ── Snapshot time arithmetic ─────────────────────────────────────────

async def get_appliance_zone(db: AsyncSession) -> tzinfo:
    """The appliance's configured timezone (module docstring, "Appliance time"): the
    ``company`` setting's IANA name that report schedules and alert quiet hours use,
    UTC when it is unset or not a zone this host knows."""
    from app.services.alert_schedule import get_configured_timezone

    name = await get_configured_timezone(db)
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("unknown appliance timezone %r; using UTC for the inventory snapshot day", name)
        return timezone.utc


def snapshot_local_day(now: datetime, zone: tzinfo) -> date:
    """Calendar date of ``now`` (timezone-aware) in ``zone``."""
    return now.astimezone(zone).date()


def snapshot_due(now: datetime, zone: tzinfo, snapshot_hour: int, *, have_today: bool) -> bool:
    """True from ``snapshot_hour``:15 local time until the day's rows exist.

    Compares wall-clock time of day, so a DST gap or repeated hour cannot skip or
    repeat the run, and a loop that was down at 00:15 runs at the first wake-up after
    it (catch-up). ``now`` must be timezone-aware.
    """
    if have_today:
        return False
    local = now.astimezone(zone)
    return (local.hour, local.minute) >= (snapshot_hour, SNAPSHOT_MINUTE)


def day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """[start, end) of the local calendar ``day`` as UTC instants."""
    start = datetime.combine(day, dtime.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ── Daily snapshot ───────────────────────────────────────────────────

SNAPSHOT_METRICS = ("assets", "os_release", "software_products", "coverage", "health", "changes")

# The health dimensions, named like the predicates of the live Overview and the Assets ?health= filter.
SNAPSHOT_HEALTH_KEYS = ("missing_os_version", "missing_model", "missing_serial", "unknown_vendor", "stale", "never",
                        "software_truncated", "missing_security_agent")
# Fresh, aging, stale and never describe assets that can report; not_applicable counts the ones that cannot, by
# design, so fresh / (fresh + aging + stale + never) is the share of the assets that could have reported.
SNAPSHOT_COVERAGE_KEYS = ("fresh", "aging", "stale", "never", "not_applicable")

# Every change category/action the capture triggers can emit; always written, so a
# quiet day is a recorded zero rather than a gap.
_CHANGE_DIMENSIONS = ("software/added", "software/removed", "software/changed",
                      "os/changed", "firmware/changed", "hardware/changed")

# The asset union of doc 02 section 3.1: every server, plus every device that is not
# the SNMP side of a server (which counts once).
_ASSETS_CTE = """
WITH assets AS (
    SELECT 'server'::text AS kind, s.id AS id, s.inventory_updated_at AS seen_at,
           s.software_truncated AS truncated
      FROM servers s
    UNION ALL
    SELECT 'device'::text, d.id, d.identity_updated_at, false
      FROM devices d
     WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id)
)"""

# The same union with the evidence the health predicates need, evaluated per asset. The definitions here are the
# ones inventory_query.py uses for its live Overview (HEALTH_SQL, _SERVER_FRESHNESS_SQL, _DEVICE_FRESHNESS_SQL,
# _SEC_STATE_SQL): the snapshot and the live panel must answer the same question, or a trend line starts at a
# different number than the tile it sits next to (UP-07). test_inventory_fix_loops.py compares the two on one fleet.
#   by_design  the asset cannot report an inventory and never did (freshness 'not_applicable', X3): a server whose
#              collection mode is not the agent, a device that is ping-only, controller-managed or via a controller
#   baselined / has_ep / has_defender / pending / stuck / has_services  the inputs of sec_state (X1, X9, X10): a
#              complete, normalised software list with no endpoint-protection row and no running Defender service, and a
#              service list to look in, is the only 'missing'. ``pending`` are the rows the normaliser will still key;
#              ``stuck`` are unkeyed rows it never selects (no source on a server that is not a Windows machine): the
#              list cannot be read, so the server is unknown, not missing. ``has_services`` is false for a server that
#              never uploaded services (the APM-only agent profile): built-in Defender cannot be judged.
_EP_SERVICE_LIST = ", ".join("'" + n + "'" for n in ENDPOINT_PROTECTION_SERVICES)    # constants, never input
_SERVER_ELIGIBLE = software_eligible_sql("w", server="s")
_ASSETS_HEALTH_CTE = f"""
WITH assets AS (
    SELECT 'server'::text AS kind, s.id AS id, s.inventory_updated_at AS seen_at,
           (s.inventory_updated_at IS NULL AND s.collection_mode <> 'agent') AS by_design,
           s.software_truncated AS truncated,
           (s.inventory_baselined_at IS NOT NULL) AS baselined,
           EXISTS (SELECT 1 FROM server_software_inventory w
                    WHERE w.server_id = s.id AND w.category = '{ENDPOINT_PROTECTION_CATEGORY}') AS has_ep,
           EXISTS (SELECT 1 FROM server_service_inventory v
                    WHERE v.server_id = s.id AND v.service_name IN ({_EP_SERVICE_LIST})
                      AND lower(v.state) = 'running') AS has_defender,
           EXISTS (SELECT 1 FROM server_software_inventory w
                    WHERE w.server_id = s.id AND (w.norm_rev IS NULL OR w.norm_rev < :rev)
                      AND {_SERVER_ELIGIBLE}) AS pending,
           EXISTS (SELECT 1 FROM server_software_inventory w
                    WHERE w.server_id = s.id AND w.norm_rev IS NULL AND NOT {_SERVER_ELIGIBLE}) AS stuck,
           EXISTS (SELECT 1 FROM server_service_inventory v WHERE v.server_id = s.id) AS has_services
      FROM servers s
    UNION ALL
    SELECT 'device'::text, d.id, d.identity_updated_at,
           (d.identity_updated_at IS NULL AND (NOT COALESCE(d.snmp_enabled, false)
             OR d.managed_by_device_id IS NOT NULL OR d.poll_mode = 'via_controller')),
           false, false, false, false, false, false, false
      FROM devices d
     WHERE NOT EXISTS (SELECT 1 FROM servers s WHERE s.device_id = d.id)
)"""

_SNAPSHOT_BY_KIND = _ASSETS_HEALTH_CTE + """
, cuts AS (
    SELECT now() - make_interval(hours => CAST(:fresh_server AS integer)) AS fresh_server,
           now() - make_interval(hours => CAST(:fresh_device AS integer)) AS fresh_device,
           now() - make_interval(days  => CAST(:stale_days   AS integer)) AS stale_cut
), tagged AS (
    SELECT a.*, CASE a.kind WHEN 'server' THEN c.fresh_server ELSE c.fresh_device END AS fresh_cut, c.stale_cut
      FROM assets a CROSS JOIN cuts c
)
SELECT t.kind,
       count(*)                                                                      AS total,
       count(*) FILTER (WHERE t.by_design)                                           AS not_applicable,
       count(*) FILTER (WHERE t.seen_at IS NULL AND NOT t.by_design)                 AS never,
       count(*) FILTER (WHERE t.seen_at >= t.fresh_cut)                              AS fresh,
       count(*) FILTER (WHERE t.seen_at < t.fresh_cut AND t.seen_at >= t.stale_cut)  AS aging,
       count(*) FILTER (WHERE t.seen_at < t.stale_cut)                               AS stale,
       count(*) FILTER (WHERE NOT t.by_design AND NULLIF(i.os_version, '') IS NULL)  AS missing_os_version,
       count(*) FILTER (WHERE NOT t.by_design AND t.kind = 'device'
                          AND NULLIF(i.hw_model, '') IS NULL)                        AS missing_model,
       count(*) FILTER (WHERE NOT t.by_design AND t.kind = 'device'
                          AND NULLIF(i.hw_serial, '') IS NULL)                       AS missing_serial,
       count(*) FILTER (WHERE NOT t.by_design AND t.kind = 'device'
                          AND COALESCE(NULLIF(i.vendor_label, ''), NULLIF(i.hw_vendor, '')) IS NULL) AS unknown_vendor,
       count(*) FILTER (WHERE t.kind = 'server' AND t.truncated)                     AS software_truncated,
       count(*) FILTER (WHERE t.kind = 'server' AND t.baselined AND NOT t.truncated AND NOT t.has_ep
                          AND NOT t.has_defender AND NOT t.pending AND NOT t.stuck
                          AND t.has_services)                                        AS missing_security_agent
  FROM tagged t
  LEFT JOIN inventory_identity i ON i.asset_kind = t.kind AND i.asset_id = t.id
 GROUP BY t.kind"""

_SNAPSHOT_OS = _ASSETS_CTE + """
SELECT COALESCE(i.os_family, 'unknown'), COALESCE(i.os_product, ''), COALESCE(i.os_release, ''), count(*)
  FROM assets a
  LEFT JOIN inventory_identity i ON i.asset_kind = a.kind AND i.asset_id = a.id
 GROUP BY 1, 2, 3"""

_SNAPSHOT_SOFTWARE = """
SELECT count(DISTINCT name_key), count(*)
  FROM server_software_inventory
 WHERE name_key IS NOT NULL
   AND COALESCE(category, 'other') <> ALL(CAST(:hidden AS text[]))"""

_SNAPSHOT_CHANGES = """
SELECT category, action, count(*)
  FROM inventory_change_events
 WHERE observed_at >= CAST(:start AS timestamptz) AND observed_at < CAST(:end AS timestamptz)
 GROUP BY 1, 2"""

_UPSERT_SNAPSHOT = """
INSERT INTO inventory_daily_snapshots (day, metric, dimension, value)
SELECT CAST(:day AS date), v.metric, v.dimension, v.value
  FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS v(metric text, dimension text, value bigint)
ON CONFLICT (day, metric, dimension) DO UPDATE SET value = EXCLUDED.value"""


def _dimension_part(value: str) -> str:
    return value.replace("/", "-")


async def _changes_rows(db: AsyncSession, day: date, zone: tzinfo) -> dict[tuple[str, str], int]:
    start, end = day_bounds(day, zone)
    counts = {("changes", d): 0 for d in _CHANGE_DIMENSIONS}
    for category, action, n in (await db.execute(text(_SNAPSHOT_CHANGES), {"start": start, "end": end})).all():
        counts[("changes", f"{category}/{action}")] = int(n)
    return counts


async def _write_snapshot_rows(db: AsyncSession, day: date, rows: dict[tuple[str, str], int]) -> None:
    items = [{"metric": m, "dimension": d[:255], "value": v} for (m, d), v in sorted(rows.items())]
    await db.execute(text(_UPSERT_SNAPSHOT), {"day": day, "rows": _json(items)})


async def snapshot_day(db: AsyncSession, day: date, *, zone: Optional[tzinfo] = None,
                       settings: Optional[Mapping[str, Any]] = None,
                       commit: bool = True) -> dict[tuple[str, str], int]:
    """Compute the metric families of doc 02 section 6 from the live tables and upsert
    them as ``day``'s rows. Returns ``{(metric, dimension): value}``.

    Commits when it is done, so a standalone call is complete; ``run_snapshot_pass``
    passes ``commit=False`` to keep its advisory lock until its own commit. Idempotent:
    the day's rows for these metrics are replaced as a whole inside one transaction, so a
    repeat run leaves the same rows and a changed dataset leaves no row that no longer
    applies.

    * assets             dimension = kind ('server', 'device')
    * os_release         'family/product/release' from inventory_identity ('unknown//' when
                         nothing is known; a '/' inside a name is written as '-')
    * software_products  'products' (distinct name keys) and 'installs', keyed rows only,
                         without the categories in ``hide_categories_by_default``
    * coverage           'fresh', 'aging', 'stale', 'never': servers by inventory_updated_at,
                         devices by identity_updated_at, against the settings thresholds; and
                         'not_applicable', the assets that cannot report by design (they are in none
                         of the other four)
    * health             the predicates of the live Overview, by the same names and definitions
                         (SNAPSHOT_HEALTH_KEYS): missing_os_version for every reporting asset,
                         missing_model / missing_serial / unknown_vendor for network devices only,
                         stale, never, software_truncated and missing_security_agent (servers). Assets
                         that cannot report by design are skipped by the identity predicates
    * changes            'category/action' events observed on the local calendar day. A day's
                         row is only complete once the day is over, so the daily pass also
                         refreshes the previous day (see run_snapshot_pass).

    Counts are of the asset union (a server and its linked SNMP device count once); an
    asset with no identity yet counts as unknown. Never backfills a missed day.
    """
    settings = merge_inventory_settings(settings) if settings is not None else await get_inventory_settings(db)
    if zone is None:
        zone = await get_appliance_zone(db)
    fresh = settings["fresh_hours"]
    rows: dict[tuple[str, str], int] = {}

    kinds = {k: dict.fromkeys(("total", "not_applicable", "never", "fresh", "aging", "stale", "missing_os_version",
                               "missing_model", "missing_serial", "unknown_vendor", "software_truncated",
                               "missing_security_agent"), 0)
             for k in ("server", "device")}
    for r in (await db.execute(text(_SNAPSHOT_BY_KIND), {
            "fresh_server": fresh["server"], "fresh_device": fresh["device"],
            "stale_days": settings["stale_days"], "rev": inventory_normalize.RULEPACK_REV})).mappings().all():
        kinds[r["kind"]] = {k: int(v) for k, v in r.items() if k != "kind"}
    total = {k: sum(v[k] for v in kinds.values()) for k in kinds["server"]}
    for kind, v in kinds.items():
        rows[("assets", kind)] = v["total"]
    for bucket in SNAPSHOT_COVERAGE_KEYS:
        rows[("coverage", bucket)] = total[bucket]
    for key in SNAPSHOT_HEALTH_KEYS:
        rows[("health", key)] = total[key]

    for family, product, release, n in (await db.execute(text(_SNAPSHOT_OS))).all():
        dim = "/".join(_dimension_part(p) for p in (family, product, release))
        rows[("os_release", dim)] = rows.get(("os_release", dim), 0) + int(n)

    products, installs = (await db.execute(text(_SNAPSHOT_SOFTWARE), {
        "hidden": list(settings["hide_categories_by_default"])})).one()
    rows[("software_products", "products")] = int(products)
    rows[("software_products", "installs")] = int(installs)

    rows.update(await _changes_rows(db, day, zone))

    await db.execute(text(
        "DELETE FROM inventory_daily_snapshots WHERE day = :day AND metric = ANY(CAST(:metrics AS text[]))"),
        {"day": day, "metrics": list(SNAPSHOT_METRICS)})
    await _write_snapshot_rows(db, day, rows)
    if commit:
        await db.commit()
    return rows


async def _refresh_previous_changes(db: AsyncSession, today: date, zone: tzinfo) -> int:
    """Recompute yesterday's 'changes' rows now that the day is complete. The snapshot
    for a day is written a quarter of an hour into it, when almost none of that day's
    events exist yet. Only touches a day that already has a 'changes' family, so it never
    creates a lone fragment of a day that was not snapshotted."""
    yesterday = today - timedelta(days=1)
    exists = (await db.execute(text(
        "SELECT EXISTS (SELECT 1 FROM inventory_daily_snapshots WHERE day = :d AND metric = 'changes')"),
        {"d": yesterday})).scalar()
    if not exists:
        return 0
    rows = await _changes_rows(db, yesterday, zone)
    await _write_snapshot_rows(db, yesterday, rows)
    return len(rows)


# ── Retention ────────────────────────────────────────────────────────

_PURGE_EVENTS = """
DELETE FROM inventory_change_events
 WHERE id IN (SELECT id FROM inventory_change_events
               WHERE observed_at < now() - make_interval(days => CAST(:days AS integer))
               ORDER BY observed_at LIMIT :n)"""

_PURGE_SNAPSHOTS = """
DELETE FROM inventory_daily_snapshots
 WHERE (day, metric, dimension) IN (
        SELECT day, metric, dimension FROM inventory_daily_snapshots
         WHERE day < (CAST(:today AS date) - make_interval(years => :years))
         LIMIT :n)"""


async def purge_expired(db: AsyncSession, *, retention_days: int, today: date,
                        batch: int = PURGE_BATCH) -> dict[str, Any]:
    """Delete change events older than ``retention_days`` and snapshots older than three
    years, ``batch`` rows per statement, until a statement deletes fewer than ``batch``.

    Each statement is its own transaction that takes the snapshot advisory lock first, so
    a large backlog never holds one long transaction; ``locked`` is False if another
    worker took over mid-way (it finishes the job).
    """
    out: dict[str, Any] = {"events": 0, "snapshots": 0, "locked": True}
    plan = (
        ("events", _PURGE_EVENTS, {"days": int(retention_days)}),
        ("snapshots", _PURGE_SNAPSHOTS, {"today": today, "years": SNAPSHOT_RETENTION_YEARS}),
    )
    for name, sql, params in plan:
        while True:
            try:
                if not await _try_lock(db, SNAPSHOT_LOCK_KEY):
                    await db.rollback()
                    out["locked"] = False
                    return out
                await _set_lock_timeout(db)
                deleted = (await db.execute(text(sql), {**params, "n": batch})).rowcount or 0
                await db.commit()
            except Exception:
                await _rollback_quietly(db)
                raise
            out[name] += deleted
            if deleted < batch:
                break
    return out


# ── Snapshot pass and loop ───────────────────────────────────────────

@dataclass
class SnapshotPassResult:
    due: bool = False
    ran: bool = False
    locked: bool = True
    day: Optional[date] = None
    rows: int = 0
    purged_events: int = 0
    purged_snapshots: int = 0
    refreshed: bool = False          # the pass re-wrote a row that already existed (first 24 h after the capture start)
    held: Optional[str] = None       # why a due snapshot was not written: 'settling' or 'normalising'


@dataclass
class SnapshotState:
    """What one snapshot loop remembers: when its process came up and when it last wrote a day.

    Passed to ``run_snapshot_pass`` it switches on the rules that only make sense for a running loop: the settle
    time after a restart, the normalise-backlog hold and the refresh during the first 24 h after the capture
    start. A direct call without a state writes the day's snapshot as soon as it is due (tests, manual repair).
    """

    started_at: float = field(default_factory=time.monotonic)
    settle_s: float = SNAPSHOT_SETTLE_S
    refresh_every_s: float = SNAPSHOT_REFRESH_S
    clock: Callable[[], float] = time.monotonic
    last_write: Optional[float] = None

    def settled(self) -> bool:
        return self.clock() - self.started_at >= self.settle_s

    def refresh_due(self) -> bool:
        return self.last_write is None or self.clock() - self.last_write >= self.refresh_every_s


async def _day_has_snapshot(db: AsyncSession, day: date) -> bool:
    return bool((await db.execute(text(
        "SELECT EXISTS (SELECT 1 FROM inventory_daily_snapshots WHERE day = :d)"), {"d": day})).scalar())


def _held_for_s(now: datetime, zone: tzinfo, snapshot_hour: int, state: SnapshotState) -> float:
    """How long a due snapshot has been waiting: since the local day's snapshot became due (snapshot_hour:15) or
    since this process started, whichever is later. A process that comes up in the afternoon of a day whose
    snapshot has been due since the morning has been holding for minutes, not for the whole day: it must still
    settle (UP-07)."""
    local = now.astimezone(zone)
    due_at = datetime.combine(local.date(), dtime(snapshot_hour, SNAPSHOT_MINUTE), tzinfo=zone)
    up_for = max(0.0, state.clock() - state.started_at)
    started = (now - timedelta(seconds=up_for)).astimezone(zone)
    return max(0.0, (local - max(due_at, started)).total_seconds())


async def run_snapshot_pass(session_factory=None, *, now: Optional[datetime] = None,
                            zone: Optional[tzinfo] = None,
                            state: Optional[SnapshotState] = None) -> SnapshotPassResult:
    """One wake-up of the snapshot loop: if today's snapshot is due and missing, write it
    (and refresh yesterday's change counts), then run retention.

    With a ``state`` (the loop always passes one) three rules apply that keep the first point of every series
    honest (UP-07):

    * Hold. A due snapshot is not written while the process is younger than ``state.settle_s`` or any row waits
      for the normaliser (``normalise_backlog``): the numbers of an upgrade that has not settled are an artefact,
      and a day is written once. The hold ends ``SNAPSHOT_HOLD_MAX_S`` after the day's snapshot became due or the
      process started (the later), so a row that can never be normalised cannot stop the history; the result says
      ``held``.
    * Refresh. During the first 24 hours after the capture start (migration 158) today's row, once written, is
      recomputed at most every ``state.refresh_every_s``: servers report every few hours, so the first reading
      is partial. A day that has closed is never rewritten, and outside that window a day is written exactly once.
    * The same health predicates as the live Overview (see ``snapshot_day``).
    """
    factory = session_factory or _default_session_factory()
    now = now or datetime.now(timezone.utc)

    async with _session(factory) as db:
        settings = await get_inventory_settings(db)
        if zone is None:
            zone = await get_appliance_zone(db)
        today = snapshot_local_day(now, zone)
        have_today = await _day_has_snapshot(db, today)
        capture = await get_capture_started_at(db) if state is not None else None
        await db.rollback()
    result = SnapshotPassResult(day=today)
    first_due = snapshot_due(now, zone, settings["snapshot_hour"], have_today=have_today)
    refresh = bool(
        state is not None and have_today and capture is not None
        and capture <= now < capture + timedelta(seconds=SNAPSHOT_REFRESH_WINDOW_S)
        and state.refresh_due())
    result.due = first_due or refresh
    if not result.due:
        return result

    if state is not None:
        hold = None
        if not state.settled():
            hold = "settling"
        else:
            async with _session(factory) as db:
                backlog = await normalise_backlog(db, cap=1)
                await db.rollback()
            if backlog.normalising:
                hold = "normalising"
        if hold and refresh and not first_due:
            result.held = hold              # a refresh simply waits for the next wake-up
            return result
        if hold and _held_for_s(now, zone, settings["snapshot_hour"], state) < SNAPSHOT_HOLD_MAX_S:
            result.held = hold
            logger.info("inventory snapshot for %s held (%s)", today, hold)
            return result
        if hold:
            logger.warning("inventory snapshot for %s written while %s: it was held for %d h",
                           today, hold, SNAPSHOT_HOLD_MAX_S // 3600)

    async with _session(factory) as db:
        try:
            if not await _try_lock(db, SNAPSHOT_LOCK_KEY):
                await db.rollback()
                result.locked = False
                return result
            await _set_lock_timeout(db)
            # Another worker may have finished between the check above and the lock.
            exists = await _day_has_snapshot(db, today)
            if exists and not refresh:
                await db.rollback()
                return result
            rows = await snapshot_day(db, today, zone=zone, settings=settings, commit=False)
            if not exists:
                await _refresh_previous_changes(db, today, zone)
            await db.commit()
        except Exception:
            await _rollback_quietly(db)
            raise
    result.ran = True
    result.refreshed = bool(exists)
    result.rows = len(rows)
    if state is not None:
        state.last_write = state.clock()
    if exists:
        logger.info("inventory snapshot for %s refreshed: %d rows", today, result.rows)
        return result

    async with _session(factory) as db:
        purged = await purge_expired(db, retention_days=settings["change_retention_days"], today=today)
    result.purged_events = purged["events"]
    result.purged_snapshots = purged["snapshots"]
    logger.info("inventory snapshot for %s: %d rows; purged %d change events, %d snapshot rows",
                today, result.rows, result.purged_events, result.purged_snapshots)
    return result


async def inventory_snapshot_loop(*, start_delay: float = SNAPSHOT_START_DELAY_S,
                                  interval: float = SNAPSHOT_POLL_S, tick=None) -> None:
    """Background task: wake every 5 minutes and write the day's snapshot when it is due.
    Never dies; cancellation propagates. ``tick`` as in inventory_normalize_loop
    (default ``run_snapshot_pass`` with this loop's ``SnapshotState``)."""
    if tick is None:
        state = SnapshotState()
        tick = functools.partial(run_snapshot_pass, state=state)
    await asyncio.sleep(start_delay)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _reraise_if_cancelling(exc)
            logger.exception("inventory snapshot pass failed")
        await asyncio.sleep(interval)
