"""CSV export of the Inventory datasets (GET /api/v1/inventory/export).

One rule decides what a file may contain: NOTHING the matching list endpoint would not return to the same user.
Every dataset is therefore produced by calling that endpoint's own service function (``list_assets``, ``catalog``,
``installations``, ``os_tree``, ``hardware``, the change feed) with the caller's tag scope, page after page; the
export adds no query of its own except the change feed's keyset paging (same conditions, no statistics).

Format: UTF-8 with a byte-order mark (spreadsheets open it correctly), CRLF line ends, one header row. A cell
that a spreadsheet would run as a formula (a string starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage
return) gets a leading apostrophe; the rule is ``esc()`` of ``dashboard/src/components/availability/exportCsv.ts``
plus the carriage return. Numbers and booleans are written as they are.

Limits: a hard cap of ``EXPORT_CAP`` rows (the route says so with ``X-Export-Truncated: true``). The whole file is
read in one ``REPEATABLE READ`` read-only transaction on a session of its own, so the pages of one file come from
one snapshot and the stream does not depend on the request's session staying open. The session is pinned
(``inventory_query.engine_of``): nothing the datasets read through opens another connection for it or closes it.

What "one snapshot" means for each dataset (NB-03). assets, installations, os, hardware and changes read the database
inside the transaction and nothing else. software is the catalog, which is built from an in-process read model: the
light table (every product's key, category, publisher, installs and so the order and the total) is fixed once when the
file starts (cached for up to 60 s and shared with other requests of the same scope, so it may be that much older than the
file), and the per-product figures the catalog has already cached on that table (hosts, version lists, newest versions) are
reused as they are. What is not cached yet (hosts, versions, names, last change of a page's products, the cached figures of
products not read before) is read inside the transaction. A software file is therefore consistent in its rows and order,
and each figure is a value the catalog itself has shown at some moment within the minutes around the file, not one instant.

Who does the database work (F3). The response generator never touches the database. A producer task (``ExportRun``)
owns the session, reads one batch at a time and hands the encoded chunks to the generator through a bounded queue.
A client that goes away cancels the generator, never a query: the generator's ``finally`` only raises a stop flag, and
the producer, which nobody cancels, notices it at the next batch boundary, rolls back, closes the connection and gives
its slot back. (A query cancelled by the server framework in the middle of a round trip leaves asyncpg's socket open
and the backend "idle in transaction" with its snapshot until the worker restarts; ``idle_in_transaction_session_timeout``
is only the backstop for that.) A client that stays connected but stops reading fills the queue; after
``CONSUMER_STALL_S`` the producer gives up the same way and the stream ends with an error instead of holding a
connection and a snapshot for as long as the client sleeps.

How many (F8). An export holds one pooled connection and a snapshot for as long as it runs, so ``limiter`` admits at
most ``MAX_EXPORTS`` at a time per API worker process and ``MAX_EXPORTS_PER_USER`` per user; the route answers 429 with
a ``Retry-After`` for the rest. The connection is NOT released between batches: the pages of one file come from one
snapshot (OFFSET paging would shift under a writer), and the concurrency cap is what protects the pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_query as iq
from app.services import inventory_software_query as isq

logger = logging.getLogger("zenplus.inventory_export")

EXPORT_CAP = 200_000
BATCH = 500                      # rows fetched per page (the lists' own maximum page size)
DATASETS = ("assets", "software", "installations", "os", "hardware", "changes")
SOFTWARE_EXPORT_SORTS = isq.CATALOG_SORTS
ACTION = "inventory.export"
BOM = "﻿"

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NEEDS_QUOTES = ('"', ",", "\n", "\r", "'")


class NotFound(LookupError):
    """The export names a product that does not exist or that the caller cannot see (the route answers 404)."""


# ── CSV ──────────────────────────────────────────────────────────────

def csv_cell(value: Any) -> str:
    """One CSV cell. ``None`` is empty, booleans and numbers are written as they are, lists as ``a;b``, mappings as
    JSON; text that starts like a spreadsheet formula gets a leading apostrophe, then is quoted when it contains a
    quote, a comma, a line break or an apostrophe (so the neutralised cell is always quoted)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        value = ";".join("" if v is None else str(v) for v in value)
    elif isinstance(value, Mapping):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    s = str(value)
    if s.startswith(_FORMULA_START):
        s = "'" + s
    if any(c in s for c in _NEEDS_QUOTES):
        s = '"' + s.replace('"', '""') + '"'
    return s


def csv_line(cells: Sequence[Any]) -> str:
    return ",".join(csv_cell(c) for c in cells) + "\r\n"


# ── Plans ────────────────────────────────────────────────────────────

Batches = Callable[[AsyncSession], AsyncIterator[list[list[Any]]]]


@dataclass
class ExportParams:
    """Everything the route parsed: the filter objects of the lists, the dataset's own extras, sort and paging."""

    dataset: str
    assets: iq.AssetFilters = field(default_factory=iq.AssetFilters)
    software: isq.CatalogFilters = field(default_factory=isq.CatalogFilters)
    changes: iq.ChangeFilters = field(default_factory=iq.ChangeFilters)
    version: Optional[str] = None
    sort: Optional[str] = None
    order: Optional[str] = None


@dataclass
class ExportPlan:
    dataset: str
    columns: list[str]
    filters: dict[str, Any]
    total: int                    # rows the filters select
    rows: int                     # rows the file holds: min(total, EXPORT_CAP)
    truncated: bool
    batches: Batches
    filename: str = ""


def _plan(dataset: str, columns: list[str], filters: dict[str, Any], total: int, batches: Batches, now: datetime) -> ExportPlan:
    rows = min(total, EXPORT_CAP)
    return ExportPlan(dataset=dataset, columns=columns, filters=filters, total=total, rows=rows,
                      truncated=total > EXPORT_CAP, batches=batches,
                      filename=f"zenplus-inventory-{dataset}-{now.strftime('%Y%m%d')}.csv")


def _sort_args(p: ExportParams, default_sort: str, default_order: str) -> tuple[str, str]:
    return (iq._clean(p.sort, 32) or default_sort), (iq._clean(p.order, 8) or default_order)


def _ts(value: Optional[datetime]) -> Optional[str]:
    return iq.iso(value)


async def _pages(fetch: Callable[[int], Awaitable[list[list[Any]]]], rows: int) -> AsyncIterator[list[list[Any]]]:
    """Page 1, 2, ... of ``BATCH`` rows until ``rows`` have been produced or a page comes back short."""
    sent, page = 0, 1
    while sent < rows:
        got = await fetch(page)
        if not got:
            return
        take = got[: rows - sent]
        yield take
        sent += len(take)
        if len(got) < BATCH:
            return
        page += 1


# assets ----------------------------------------------------------------

ASSET_COLUMNS = ["key", "kind", "name", "ip", "asset_type", "status", "vendor", "model", "serial", "os_family", "os_product",
                 "os_release", "os_version", "os_build", "os_edition", "os_source", "hw_source", "site", "tags", "sources",
                 "last_seen", "inventory_at", "inventory_age_s", "freshness", "software_truncated"]


async def _assets_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    f = p.assets.normalized()
    sort, order = _sort_args(p, "name", "asc")
    first = await iq.list_assets(db, f, scope=scope, sort=sort, order=order, page=1, page_size=1, settings=settings, now=now)

    def batches(s: AsyncSession):
        async def fetch(page: int):
            res = await iq.list_assets(s, f, scope=scope, sort=sort, order=order, page=page, page_size=BATCH,
                                       settings=settings, now=now)
            return [[it[c] for c in ASSET_COLUMNS] for it in res["items"]]
        return _pages(fetch, min(first["total"], EXPORT_CAP))

    filters = {**f.as_params(), "sort": sort, "order": order}
    return _plan("assets", ASSET_COLUMNS, filters, first["total"], batches, now)


# software --------------------------------------------------------------

SOFTWARE_COLUMNS = ["product_key", "name", "publisher", "category", "norm_status", "hosts", "installs", "version_count",
                    "latest_version", "top_versions", "first_seen", "last_change"]


def _software_cells(it: Mapping[str, Any]) -> list[Any]:
    top = ";".join(f"{v['version']} ({v['hosts']})" for v in it["distribution"]["top"])
    other = it["distribution"]["other"]
    if other["versions"]:
        top += f";other {other['versions']} versions ({other['hosts']})"
    return [it["product_key"], it["name"], it["publisher"], it["category"], it["norm_status"], it["hosts"], it["installs"],
            it["version_count"], it["latest_version"], top, it["first_seen"], it["last_change"]]


async def _software_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    f = p.software.normalized()
    sort, order = _sort_args(p, "name", "asc")
    # The catalog's light table (the product list, its installs and its order) is fixed ONCE for the whole file: every page
    # is cut from this object, so a table that expires or is rebuilt while the file streams cannot shift or repeat a row.
    table = await isq._table(db, scope)
    first = await isq.catalog(db, f, scope=scope, sort=sort, order=order, page=1, page_size=1, settings=settings, table=table)

    def batches(s: AsyncSession):
        async def fetch(page: int):
            res = await isq.catalog(s, f, scope=scope, sort=sort, order=order, page=page, page_size=BATCH, settings=settings,
                                    retain=False, table=table)       # (F10) a file reads every page: leave no version lists in the shared table
            return [_software_cells(it) for it in res["items"]]
        return _pages(fetch, min(first["total"], EXPORT_CAP))

    return _plan("software", SOFTWARE_COLUMNS, {**f.as_params(), "sort": sort, "order": order}, first["total"], batches, now)


# installations ---------------------------------------------------------

INSTALL_COLUMNS = ["product_key", "asset_key", "asset_name", "package_name", "version", "architecture", "source",
                   "install_date", "first_seen_at", "updated_at"]


async def _installations_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    key = iq._clean(p.assets.product, 160)
    if not key:
        raise iq.InvalidFilter("product is required for dataset=installations")
    version = iq._clean(p.version, iq.MAX_TEXT)
    q = iq._clean(p.assets.q)
    sort, order = _sort_args(p, "asset_name", "asc")
    first = await isq.installations(db, key, scope=scope, version=version, q=q, sort=sort, order=order, page=1, page_size=1)
    if first is None:
        raise NotFound(key)

    def batches(s: AsyncSession):
        async def fetch(page: int):
            res = await isq.installations(s, key, scope=scope, version=version, q=q, sort=sort, order=order,
                                          page=page, page_size=BATCH)
            return [[key, r["asset_key"], r["asset_name"], r["package_name"], r["version"], r["architecture"], r["source"],
                     r["install_date"], r["first_seen_at"], r["updated_at"]] for r in (res or {"items": []})["items"]]
        return _pages(fetch, min(first["total"], EXPORT_CAP))

    filters = {k: v for k, v in {"product": key, "version": version, "q": q, "sort": sort, "order": order}.items() if v}
    return _plan("installations", INSTALL_COLUMNS, filters, first["total"], batches, now)


# os --------------------------------------------------------------------

OS_COLUMNS = ["family", "family_label", "product", "release", "version", "assets", "release_assets", "release_latest_version",
              "build_currency", "raw_example"]


def _os_rows(tree: Mapping[str, Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for fam in tree["families"]:
        for prod in fam["products"]:
            scheme = iq.scheme_for(fam["family"], prod["product"])      # the scheme the page ordered this product's versions by
            for rel in prod["releases"]:
                cur = rel["currency"]
                for ver in rel["versions"]:
                    bucket = iq.version_bucket(scheme, ver["version"], cur)
                    out.append([fam["family"], fam["label"], prod["product"], rel["release"], ver["version"], ver["count"],
                                rel["count"], rel["latest_version"], bucket, ver["raw_example"]])
    return out


async def _os_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    kind = iq._enum(iq._clean(p.assets.kind), iq.KINDS, "kind")
    tree = await iq.os_tree(db, scope=scope, kind=kind, settings=settings, now=now)
    rows = _os_rows(tree)

    def batches(_s: AsyncSession):
        async def gen():
            for i in range(0, min(len(rows), EXPORT_CAP), BATCH):
                yield rows[i:i + BATCH][: EXPORT_CAP - i]
        return gen()

    return _plan("os", OS_COLUMNS, {"kind": kind} if kind else {}, len(rows), batches, now)


# hardware --------------------------------------------------------------

HW_COLUMNS = ["section", "vendor", "model", "class", "value", "label", "count", "devices", "firmware_versions",
              "serial_missing", "serial_coverage_pct", "modules"]


def _hardware_rows(hw: Mapping[str, Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for v in hw["network"]["vendors"]:
        out.append(["network_vendor", v["vendor"], None, None, None, None, v["count"], v["count"], v["firmware_versions"],
                    v["serial_missing"], v["serial_coverage_pct"], v["modules"]])
    for m in hw["network"]["models"]:
        out.append(["network_model", m["vendor"], m["model"], None, None, None, m["count"], m["count"], m["firmware_versions"],
                    m["serial_missing"], m["serial_coverage_pct"], m["modules"]])
    for g in hw["modules"]:
        out.append(["module", None, g["model"], g["class"], None, g["class_label"], g["count"], g["devices"], None, None, None, None])
    for name in ("cpu_models", "memory", "disks", "virtualization"):
        for d in hw["servers"][name] or []:
            out.append([f"server_{name}", None, None, None, d["value"], d["label"], d["count"], None, None, None, None, None])
    return out


async def _hardware_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    vendor = iq._clean(p.assets.vendor)
    hw = await iq.hardware(db, scope=scope, vendor=vendor, settings=settings, now=now)
    rows = _hardware_rows(hw)

    def batches(_s: AsyncSession):
        async def gen():
            for i in range(0, min(len(rows), EXPORT_CAP), BATCH):
                yield rows[i:i + BATCH][: EXPORT_CAP - i]
        return gen()

    return _plan("hardware", HW_COLUMNS, {"vendor": vendor} if vendor else {}, len(rows), batches, now)


# changes ---------------------------------------------------------------

# Whether each asset still exists: the list's statement asks for the server behind every linked device with a per-device
# subquery on servers.device_id (no index), which costs 60 ms for a page of 500 rows at 5,000 servers. The export reads
# hundreds of pages, so it aggregates servers once per page instead. Same answer, as the tests compare row by row.
_PRESENCE_SQL = """
SELECT 'server'::text AS kind, s.id AS id FROM servers s WHERE s.id = ANY(CAST(:sids AS uuid[]))
UNION ALL
SELECT 'device'::text, d.id FROM devices d WHERE d.id = ANY(CAST(:dids AS uuid[]))"""


async def _decorate(s: AsyncSession, items: list[dict[str, Any]]) -> None:
    """``asset_present`` and the upgrade / downgrade ``pair`` of each row, as ``inventory_query._decorate_events``
    computes them for the list (same pairing statement and rule), without the ``server_id`` link the file does not carry."""
    try:
        if not items:
            return
        sids = sorted({x["asset_id"] for x in items if x["asset_kind"] == "server"})
        dids = sorted({x["asset_id"] for x in items if x["asset_kind"] == "device"})
        present = {(r["kind"], str(r["id"])) for r in await iq._run(s, _PRESENCE_SQL, {"sids": sids, "dids": dids})}
        for x in items:
            x["asset_present"] = (x["asset_kind"], x["asset_id"]) in present
        halves = [x for x in items if x["category"] == "software" and x["action"] in ("added", "removed")]
        if not halves:
            return
        keys = sorted({(x["asset_kind"], x["asset_id"], x["_at"].isoformat()) for x in halves})
        rows = await iq._run(s, iq._PAIR_SQL, {"keys": json.dumps([{"kind": k, "id": i, "at": a} for k, i, a in keys])})
        groups: dict[tuple, list[Mapping[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r["asset_kind"], str(r["asset_id"]), r["pkey"], r["observed_at"]), []).append(r)
        for x in halves:
            g = groups.get((x["asset_kind"], x["asset_id"], x["subject_key"] or x["subject"], x["_at"]), [])
            added = [r for r in g if r["action"] == "added"]
            removed = [r for r in g if r["action"] == "removed"]
            if len(added) == 1 and len(removed) == 1:
                other = removed[0] if x["action"] == "added" else added[0]
                x["pair"] = {"event_id": int(other["id"]), "label": iq.version_label(removed[0]["old_value"], added[0]["new_value"])}
    finally:
        for x in items:
            x.pop("_at", None)


CHANGE_COLUMNS = ["id", "observed_at", "asset_key", "asset_name", "asset_present", "category", "action", "subject",
                  "subject_key", "old_value", "new_value", "label", "paired_event_id", "paired_label", "marker",
                  "snapshot_truncated"]


def _change_cells(r: Mapping[str, Any]) -> list[Any]:
    pair = r["pair"] or {}
    return [r["id"], r["observed_at"], r["asset_key"], r["asset_name"], r["asset_present"], r["category"], r["action"],
            r["subject"], r["subject_key"], r["old_value"], r["new_value"], r["label"], pair.get("event_id"),
            pair.get("label"), r["marker"], r["snapshot_truncated"]]


async def _changes_plan(db: AsyncSession, p: ExportParams, scope, settings, now) -> ExportPlan:
    cf = p.changes.normalized()
    since = cf.since or (now - timedelta(days=7))
    if cf.until is not None and cf.until <= since:
        raise iq.InvalidFilter("until must be after since")
    # The list's own first page gives the exact total (and validates the filters the same way).
    first = await iq.changes(db, scope=scope, params=cf, page=1, page_size=1, now=now)

    linked = None
    if cf.asset and iq.parse_asset_key(cf.asset)[0] == "server":
        row = await iq._run(db, "SELECT device_id FROM servers WHERE id = :i", {"i": iq.parse_asset_key(cf.asset)[1]})
        linked = row[0]["device_id"] if row else None
    pairs = iq._event_asset_pairs(cf, linked)

    def batches(s: AsyncSession):
        async def gen():
            await iq._tune(s)
            visible = await iq._visible_assets(s, scope, cf, pairs)
            label_pairs = None
            if cf.label:
                stats = await iq._window_stats(s, visible, cf, pairs, since, None, with_pairs=False)
                label_pairs = stats["_label_pairs"].get(cf.label)
            sent = 0
            rows = min(first["total"], EXPORT_CAP)
            last: Optional[tuple[datetime, int]] = None
            while sent < rows:
                params: dict[str, Any] = {}
                conds = iq._event_base_conds(cf, since, params) + iq._event_class_conds(cf, params, label_pairs)
                if visible is None:
                    conds += iq._kind_asset_conds(cf, pairs, params, "e.asset_kind", "e.asset_id")
                if last is not None:
                    params["k_at"], params["k_id"] = last
                    conds.append("(e.observed_at, e.id) < (:k_at, :k_id)")
                params["limit"] = BATCH
                found = await iq._run(s, iq._events_sql(
                    visible, outer=iq._EV_COLS, conds=conds, p=params,
                    inner_limit="ORDER BY e.observed_at DESC, e.id DESC LIMIT :limit",
                    tail="ORDER BY e.observed_at DESC, e.id DESC LIMIT :limit"), params)
                if not found:
                    return
                items = [iq._event_row(r) for r in found]
                await _decorate(s, items)
                last = (found[-1]["observed_at"], int(found[-1]["id"]))
                take = items[: rows - sent]
                yield [_change_cells(r) for r in take]
                sent += len(take)
                if len(found) < BATCH:
                    return
        return gen()

    filters = {k: v for k, v in {
        "category": cf.category, "action": cf.action, "kind": cf.kind, "asset": cf.asset, "subject_key": cf.subject_key,
        "q": cf.q, "label": cf.label, "since": iq.iso(since), "until": iq.iso(cf.until)}.items() if v}
    return _plan("changes", CHANGE_COLUMNS, filters, first["total"], batches, now)


# ── Entry points ─────────────────────────────────────────────────────

_PLANNERS = {
    "assets": _assets_plan, "software": _software_plan, "installations": _installations_plan,
    "os": _os_plan, "hardware": _hardware_plan, "changes": _changes_plan,
}


async def plan_export(db: AsyncSession, params: ExportParams, *, scope: Optional[Sequence[str]]) -> ExportPlan:
    """Validate the request and count what it selects. Raises ``iq.InvalidFilter`` (422) or ``NotFound`` (404)."""
    if params.dataset not in _PLANNERS:
        raise iq.InvalidFilter(f"dataset must be one of: {', '.join(DATASETS)}")
    now = iq.utcnow()
    settings = await iq._settings(db, None)
    return await _PLANNERS[params.dataset](db, params, scope, settings, now)


# ── Concurrency (F8) ─────────────────────────────────────────────────

MAX_EXPORTS = 2                  # exports running at once in one API worker process
MAX_EXPORTS_PER_USER = 1
RETRY_AFTER_S = 10


class ExportBusy(RuntimeError):
    """Too many exports are running (the route answers 429). ``per_user`` says which limit was hit."""

    def __init__(self, per_user: bool):
        super().__init__("you already have an export running; wait for it to finish" if per_user
                         else "too many exports are running; try again shortly")
        self.per_user = per_user
        self.retry_after = RETRY_AFTER_S


class ExportSlot:
    """One admitted export. ``release`` is idempotent: the route releases it when the export never starts, the producer
    when it ends."""

    def __init__(self, limiter: "ExportLimiter", user_id: Any):
        self._limiter, self._user_id, self._held = limiter, user_id, True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._limiter._release(self._user_id)


class ExportLimiter:
    """Process-wide admission control for exports. A plain counter under a lock (not an ``asyncio.Semaphore``, which is
    bound to one event loop and would wait; this refuses at once)."""

    def __init__(self, total: int = MAX_EXPORTS, per_user: int = MAX_EXPORTS_PER_USER):
        self.total, self.per_user = total, per_user
        self._lock = threading.Lock()
        self._running = 0
        self._by_user: dict[Any, int] = {}

    def acquire(self, user_id: Any) -> ExportSlot:
        with self._lock:
            if self._by_user.get(user_id, 0) >= self.per_user:
                raise ExportBusy(per_user=True)
            if self._running >= self.total:
                raise ExportBusy(per_user=False)
            self._running += 1
            self._by_user[user_id] = self._by_user.get(user_id, 0) + 1
        return ExportSlot(self, user_id)

    def _release(self, user_id: Any) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            left = self._by_user.get(user_id, 0) - 1
            if left > 0:
                self._by_user[user_id] = left
            else:
                self._by_user.pop(user_id, None)

    @property
    def running(self) -> int:
        with self._lock:
            return self._running


limiter = ExportLimiter()


# ── Streaming (F3) ───────────────────────────────────────────────────

QUEUE_DEPTH = 4                          # encoded batches waiting for the client (about 2,000 rows)
CONSUMER_STALL_S = 120.0                 # the producer gives up on a client that has not read for this long
IDLE_IN_TRANSACTION_S = 300              # backstop: PostgreSQL ends a backend left idle in its export transaction this long


class ExportStalled(RuntimeError):
    """The client stopped reading; the export was abandoned and its connection returned."""


class _End:
    """The queue's normal terminator."""


class _Failed:
    """The queue's error terminator: the generator raises ``exc`` so the response ends in an error, never as a short file."""

    def __init__(self, exc: BaseException):
        self.exc = exc


def encode_batch(batch: Sequence[Sequence[Any]]) -> bytes:
    return "".join(csv_line(row) for row in batch).encode("utf-8")


class ExportRun:
    """One export in flight: a producer task that reads the dataset in a snapshot and the generator that serves it.

    Create it inside the running event loop (the producer starts at once, so the slot and the connection are
    accounted for even if the response is never iterated: it then stalls out after ``CONSUMER_STALL_S``)."""

    def __init__(self, bind: Any, plan: ExportPlan, *, release: Optional[Callable[[], None]] = None):
        self._plan, self._bind, self._release = plan, bind, release
        self.queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._stop = asyncio.Event()
        self.batches_read = 0
        self.task = asyncio.get_running_loop().create_task(self._produce())

    # -- consumer side: the only part a disconnect can cancel; it never awaits the database --
    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            yield (BOM + csv_line(self._plan.columns)).encode("utf-8")
            while True:
                item = await self.queue.get()
                if isinstance(item, _End):
                    return
                if isinstance(item, _Failed):
                    raise item.exc
                yield item
        finally:
            self._stop.set()

    # -- producer side: nobody cancels this task; it ends at a batch boundary --
    async def _put(self, item: Any) -> bool:
        """Queue ``item``; False when the client is gone or has not read for ``CONSUMER_STALL_S``."""
        if self._stop.is_set():
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            pass
        putter = asyncio.ensure_future(self.queue.put(item))        # (cancelling a queue put is harmless: no I/O)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            done, _pending = await asyncio.wait({putter, stopper}, timeout=CONSUMER_STALL_S,
                                                return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (putter, stopper):
                if not fut.done():
                    fut.cancel()
        return putter in done and not putter.cancelled()

    def _fail_now(self, exc: BaseException) -> None:
        """Replace whatever is queued by the failure (a client that comes back must see an error, not a short file)."""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queue.put_nowait(_Failed(exc))

    async def _produce(self) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        stalled = False
        outcome: Any = _End()
        try:
            async with AsyncSession(bind=self._bind, expire_on_commit=False) as session:
                # Pinned (NB-03): every helper the dataset reads through stays on THIS session, one statement after another.
                # Without it the catalog fanned half of a page out to other connections (another snapshot) and closed this
                # session to wait for a shared build, which ended the transaction and its read-only and idle-timeout settings.
                session.info[iq.PINNED_KEY] = True
                try:
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    await session.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = {int(IDLE_IN_TRANSACTION_S * 1000)}"))
                    async with contextlib.aclosing(self._plan.batches(session)) as batches:
                        async for batch in batches:
                            if self._stop.is_set():
                                break
                            self.batches_read += 1
                            if not await self._put(encode_batch(batch)):
                                stalled = not self._stop.is_set()
                                break
                finally:
                    await session.rollback()
            if stalled:
                outcome = _Failed(ExportStalled(f"export abandoned: the client did not read for {CONSUMER_STALL_S:g} s"))
        except Exception as exc:  # noqa: BLE001 - the stream ends in an error; the cause is in the log
            logger.exception("inventory export of %s failed", self._plan.dataset)
            outcome = _Failed(exc)
        finally:
            if self._release is not None:
                self._release()                  # before the terminator: a client that asks again at once is admitted
        if isinstance(outcome, _Failed):
            if not self._stop.is_set():
                if stalled:
                    logger.warning("%s", outcome.exc)
                self._fail_now(outcome.exc)
        elif not await self._put(outcome) and not self._stop.is_set():
            self._fail_now(ExportStalled(f"export abandoned: the client did not read for {CONSUMER_STALL_S:g} s"))


async def stream_csv(bind: Any, plan: ExportPlan, *, release: Optional[Callable[[], None]] = None) -> AsyncIterator[bytes]:
    """The file, as chunks of bytes: the header row first, then ``plan.rows`` rows read in pages from a snapshot by a
    producer task (see the module header). Call from inside the event loop that serves the response."""
    async with contextlib.aclosing(ExportRun(bind, plan, release=release).chunks()) as chunks:
        async for chunk in chunks:
            yield chunk
