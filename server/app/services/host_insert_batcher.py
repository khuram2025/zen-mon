"""Group commit for host telemetry inserts.

Agents upload small envelopes (often one sample per metric kind), and every
upload used to become one synchronous INSERT — one new ClickHouse part — per
kind. A fleet produced tens of thousands of single-row parts per hour; the
resulting merges rewrote many gigabytes and the per-insert DESCRIBE and query
logging inflated the diagnostic logs.

Concurrent uploads now join a per-table batch that is written as one INSERT
when it reaches ``max_rows`` or ``max_delay_s`` after its first row. Each
upload is acknowledged only after the batch containing its rows was written
(durable acknowledgement: no fire-and-forget), so a failure still returns 503
and the agent replays from its spool.

Retry identity: a batch of one upload keeps that upload's own deduplication
token, exactly as before. A combined batch gets a token derived from its
members and is retried, for up to RETRY_WINDOW_S, as the identical block with
that token, so ClickHouse deduplicates a retry of a block that did land. If
the outcome is still unknown after that, raw-row verification is allowed only
for an unreplicated table with no dependent views. Replicated writes and view
completion require a successful insert response; the rest replay from spools.
Without the opt-in PostgreSQL journal, a combined block that landed but could neither be confirmed
nor verified (ClickHouse unreachable) can be stored twice when its uploads
replay under their own tokens, as can an upload replayed to another worker
while its first write is still in flight.

The commissioned journal path retains the original grouping across processes
and restarts. See host_ingest_journal for its bounded replay and capacity gates.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("zenplus.host_insert_batcher")

MAX_ROWS = int(os.environ.get("HOST_INSERT_BATCH_MAX_ROWS", "20000"))
MAX_DELAY_S = float(os.environ.get("HOST_INSERT_BATCH_DELAY_S", "1.0"))
# Retry the identical block for this long before failing its uploads; the
# host-results endpoint gives up after 20 s (HOST_RESULTS_INGEST_TIMEOUT_S).
RETRY_WINDOW_S = float(os.environ.get("HOST_INSERT_RETRY_WINDOW_S", "12"))
# How long a written upload token is remembered for replay detection.
RECENT_TOKEN_S = 600.0


# ─── Column types: one DESCRIBE per table per process, not per insert ────────

_types_lock = threading.Lock()
_column_types: dict[tuple[str, tuple[str, ...]], list[str]] = {}


def column_type_names(client, table: str, columns: list[str]) -> list[str]:
    key = (table, tuple(columns))
    with _types_lock:
        cached = _column_types.get(key)
    if cached is not None:
        return cached
    described = {row[0]: row[1] for row in client.query(f"DESCRIBE TABLE {table}").result_rows}
    types = [described[c] for c in columns]
    with _types_lock:
        _column_types[key] = types
    return types


def forget_column_types(table: str) -> None:
    with _types_lock:
        for key in [k for k in _column_types if k[0] == table]:
            del _column_types[key]


def insert_rows(client, table: str, columns: list[str], rows: list[list], token: str) -> None:
    """One INSERT with cached column types and a deduplication token."""
    settings = {"insert_deduplication_token": token, "insert_deduplicate": 1}
    try:
        types = column_type_names(client, table, columns)
        client.insert(table, rows, column_names=columns, column_type_names=types,
                      settings=settings)
    except Exception as exc:
        # A migration may have added a column or changed a type: describe
        # again once (a column missing from the cached schema is a KeyError).
        if isinstance(exc, KeyError) or "type" in str(exc).lower() or "column" in str(exc).lower():
            forget_column_types(table)
            client.insert(table, rows, column_names=columns,
                          column_type_names=column_type_names(client, table, columns),
                          settings=settings)
        else:
            raise


def _ts_literal(value: Any) -> str:
    """DateTime64(3) literal in UTC with milliseconds (the driver drops them)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return str(value)


def rows_present(client, table: str, columns: list[str], rows: list[list]) -> bool:
    """Whether ALL of one upload's rows are stored (after an unknown outcome).

    Counts stored rows matching the upload's (agent_id, timestamp) keys; a
    partially stored upload (e.g. an insert spanning partitions that failed
    half-way) is not acknowledged and replays.
    """
    if "agent_id" not in columns or "timestamp" not in columns or not rows:
        return False
    ai, ti = columns.index("agent_id"), columns.index("timestamp")
    agents = {r[ai] for r in rows}
    if len(agents) != 1:
        return False
    stamps = sorted({_ts_literal(r[ti]) for r in rows})
    result = client.query(
        f"SELECT count() FROM {table} WHERE agent_id = %(a)s "
        "AND timestamp IN (SELECT toDateTime64(arrayJoin(%(ts)s), 3, 'UTC'))",
        parameters={"a": next(iter(agents)), "ts": stamps},
    ).result_rows
    return bool(result) and int(result[0][0]) >= len(rows)


def presence_can_confirm(client, table: str) -> bool:
    """Raw presence cannot prove replication quorum or dependent-view completion.

    A timed-out quorum insert can later leave a raw row without its materialized
    view contribution. Returning success in that state discards the producer's
    retry opportunity. Metadata errors or unknown engines conservatively refuse
    presence-based acknowledgement.
    """
    if not re.fullmatch(r'(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?[a-zA-Z_][a-zA-Z0-9_]*',table):
        return False
    database,_,name=table.rpartition('.')
    result=client.query(
        "SELECT engine,length(dependencies_table) FROM system.tables "
        "WHERE database=if(%(database)s='',currentDatabase(),%(database)s) "
        "AND name=%(name)s",
        parameters={'database':database,'name':name or table},
    ).result_rows
    return (len(result)==1 and result[0][0] in {'MergeTree','ReplacingMergeTree'}
            and int(result[0][1])==0)


# ─── Batcher ─────────────────────────────────────────────────────────────────

@dataclass
class _Member:
    rows: list[list]
    token: str
    future: asyncio.Future


@dataclass
class _Batch:
    table: str
    columns: list[str]
    members: list[_Member] = field(default_factory=list)
    rows: int = 0
    timer: Optional[asyncio.TimerHandle] = None


class HostInsertBatcher:
    def __init__(self, client_factory: Callable[[], Any], *, max_rows: int = MAX_ROWS,
                 max_delay_s: float = MAX_DELAY_S, journal=None):
        self.client_factory = client_factory
        self.max_rows = max_rows
        self.max_delay_s = max_delay_s
        self.journal = journal
        self._open: dict[tuple[str, tuple[str, ...]], _Batch] = {}
        # token -> (outcome future, monotonic time): a replay of an upload that
        # is still being written, or was stored recently, reuses that outcome
        # instead of writing its rows a second time under a different block.
        self._recent: "OrderedDict[tuple[str, str], tuple[asyncio.Future, float]]" = OrderedDict()
        self.stats = {"inserts": 0, "rows": 0, "members": 0, "failures": 0, "verified": 0,
                      "replays_joined": 0}

    def _prune_recent(self, now: float) -> None:
        while self._recent:
            token, (fut, at) = next(iter(self._recent.items()))
            if now - at < RECENT_TOKEN_S:
                break
            del self._recent[token]

    async def insert(self, table: str, columns: list[str], rows: list[list], token: str) -> None:
        """Return once ``rows`` are stored; raise if they could not be.

        The write is shielded: cancelling the caller (an HTTP deadline) does
        not cancel or orphan the write, and a replay with the same token joins
        its outcome.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._prune_recent(now)
        identity = (table, token)
        prior = self._recent.get(identity)
        if prior is not None:
            fut = prior[0]
            if not fut.done() or (not fut.cancelled() and fut.exception() is None):
                self.stats["replays_joined"] += 1
                await asyncio.shield(fut)
                return
        key = (table, tuple(columns))
        batch = self._open.get(key)
        if batch is None:
            batch = self._open[key] = _Batch(table, list(columns))
            batch.timer = loop.call_later(self.max_delay_s, self._flush_soon, key, batch)
        member = _Member(rows, token, loop.create_future())
        # The caller may be gone (deadline); mark the outcome as observed.
        member.future.add_done_callback(lambda f: f.cancelled() or f.exception())
        batch.members.append(member)
        batch.rows += len(rows)
        self._recent[identity] = (member.future, now)
        self._recent.move_to_end(identity)
        if batch.rows >= self.max_rows:
            self._flush_soon(key, batch)
        await asyncio.shield(member.future)

    def _flush_soon(self, key, batch: _Batch) -> None:
        if self._open.get(key) is not batch:
            return  # already flushed by size
        del self._open[key]
        if batch.timer:
            batch.timer.cancel()
        asyncio.get_running_loop().create_task(self._flush(batch))

    async def _flush(self, batch: _Batch) -> None:
        rows = [r for m in batch.members for r in m.rows]
        if len(batch.members) == 1:
            token = batch.members[0].token
        else:
            token = hashlib.sha256(
                ("zenplus-host-batch-v1\0" + "\0".join(sorted(m.token for m in batch.members)))
                .encode()).hexdigest()
        error: Optional[BaseException] = None
        try:
            if self.journal is not None:
                await self.journal.submit(batch.table, batch.columns,
                                          [(m.token, m.rows) for m in batch.members])
            else:
                await asyncio.to_thread(self._write, batch.table, batch.columns, rows, token)
        except BaseException as exc:  # noqa: BLE001 — delivered to the members
            error = exc
        stored: set[int] = set()
        if error is not None:
            # Outcome unknown (e.g. the response was lost after the write).
            # Only plain stores without dependent views can use raw presence
            # as confirmation. Quorum and materialized-view completion need a
            # successful retry; leave other uploads available for replay.
            if self.journal is None:
                try:
                    stored = await asyncio.to_thread(self._stored_members, batch)
                except Exception:
                    stored = set()
            self.stats["verified"] += len(stored)
            self.stats["failures"] += 1
            logger.warning("host telemetry batch insert into %s failed (%d uploads, %d verified "
                           "stored): %s", batch.table, len(batch.members), len(stored), error)
        else:
            self.stats["inserts"] += 1
            self.stats["rows"] += len(rows)
        self.stats["members"] += len(batch.members)
        for i, m in enumerate(batch.members):
            if m.future.done():
                continue
            if error is None or i in stored:
                m.future.set_result(None)
            else:
                m.future.set_exception(error)

    def _write(self, table: str, columns: list[str], rows: list[list], token: str) -> None:
        """Insert, retrying the identical block with the same token.

        Retrying the same block is what keeps a combined batch idempotent: if an
        earlier attempt landed, ClickHouse deduplicates the retry by token.
        Members only fail (and replay under their own tokens) after the retry
        window, which stays inside the HTTP ingest deadline.
        """
        last: Optional[Exception] = None
        deadline = time.monotonic() + RETRY_WINDOW_S
        delay = 0.25
        while True:
            try:
                insert_rows(self.client_factory(), table, columns, rows, token)
                return
            except Exception as exc:
                last = exc
            if time.monotonic() + delay > deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise last  # type: ignore[misc]

    def _stored_members(self, batch: _Batch) -> set[int]:
        client = self.client_factory()
        if not presence_can_confirm(client,batch.table):
            return set()
        return {i for i, m in enumerate(batch.members)
                if rows_present(client, batch.table, batch.columns, m.rows)}


_batchers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, HostInsertBatcher]" = \
    weakref.WeakKeyDictionary()


def batcher_for_loop(client_factory: Callable[[], Any]) -> HostInsertBatcher:
    """One batcher per event loop (each Uvicorn worker has its own)."""
    loop = asyncio.get_running_loop()
    b = _batchers.get(loop)
    if b is None:
        b = _batchers[loop] = HostInsertBatcher(client_factory)
        from app.services.host_ingest_journal import enabled, HostIngestJournal
        if enabled():
            from app.core.database import engine
            b.journal = HostIngestJournal(engine, b._write)
    return b
