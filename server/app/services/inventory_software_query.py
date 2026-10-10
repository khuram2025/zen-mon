"""Software catalog read model behind GET /api/v1/inventory/software*.

The catalog groups ``server_software_inventory`` by ``name_key`` (the normaliser's product key). Everything
here follows the rules of ``inventory_query`` (read that module's header first): scope fails closed, unknown is
a value, every number is the size of a list, one ``_tune`` per request, time comes from ``iq.utcnow``.

What a number means (they are the drill-down contract of the Software pages):

* ``hosts``     distinct servers with at least one row of the product. Equals the ``total`` of
                ``/assets?product=<key>`` (a version row: ``&product_version=<version>``).
* ``installs``  rows of the product (a server can hold several: 32 and 64 bit builds, titles that embed their
                version). Equals the ``total`` of ``/software/<key>/installations``.
* ``version_count``  distinct non-blank versions. A missing version is not a version: it is reported as
                ``unversioned`` on the product page and listed in the installations with ``version=__none__`` (``unknown`` is an ordinary version).

Cost model (the catalog budget is 400 ms at 5,000 servers x 400 packages = 2 M rows; measurements in the
contract document):

* The "light table" (installs, category, publisher and first seen of every product) is one parallel scan of the
  software table (about 0.15 s at 2 M rows) and is cached per scope for 60 s. Every filter, sort and page that
  does not need ``hosts`` or the versions is then pure Python over a few thousand rows. The number of versions and
  the newest version of EVERY product (only the sorts by them need these) are one more scan of the
  (product, version) pairs, read once and kept with the table.
* ``hosts`` needs ``count(DISTINCT server_id)``, which costs about one second for ALL products at 2 M rows,
  so it is computed only for the products that matter: the page's own, and for ``sort=hosts`` the candidate
  set that provably contains the page (``hosts <= installs`` and ``hosts <= visible servers`` bound every
  product outside the candidates, the same argument as ``inventory_query._software_block``). Results are kept
  with the light table. Only ``sort=hosts&order=asc`` has no such bound and measures every product once.
* The version distribution of a product is read together with its hosts (one grouping-sets statement).

Versions are counted and listed by their EQUIVALENCE KEY (``versioning.equivalence_key``: ``7.2`` and ``7.2.0``, ``V1`` and
``v1`` are one version), the key the Assets list filters ``product_version`` by, so a version row's ``hosts`` equals the
``total`` of ``/assets?product=<key>&product_version=<version>`` (hosts are counted once across the spellings: a server
holding both is one host), ``version_count`` is the number of keys, and ``/software/<key>/installations?version=`` lists
every spelling of the version. The row is labelled with the longest spelling (then the greatest). Rows with no version
(NULL or blank) are not a version: they are the ``unversioned`` bucket, addressed in ``installations`` as
``version=__none__`` (``NO_VERSION``), so that a version spelled "unknown" is an ordinary version (BN-09).

Who computes (F5, F7, F10): the CPU-bound parts (building a table's rows, ranking the versions of a page or of every
product) run in a small worker pool, never on the event loop. A request that has to wait for a shared build or read gives
its connection back first (``_wait_for_shared``), so waiters cannot use the pool up and starve the work they wait for.
Cached tables are evicted when they expire; a table keeps the version lists of at most ``_DETAIL_CAP`` products; the
export reads its pages through ``retain=False`` and leaves nothing behind. ``catalog_warmer_loop`` keeps the whole-estate
table (and the default views on it) fresh before it expires.

Version order: ``versioning.py`` compares versions of ONE scheme. A software row's scheme follows its source
(``dpkg`` and ``rpm`` packages have their own, everything else is ``semverish``), so a product installed on
Windows and on Ubuntu has two version groups that are ranked separately (``rank`` 1 is the newest of the
group); the group with more installs comes first and its newest version is the product's ``latest_version``. A
version the scheme cannot parse has no rank and no place in the order: it is listed after the ordered ones.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_normalize, versioning
from app.services import inventory_query as iq

UTC = iq.UTC
logger = logging.getLogger("zenplus.inventory_software")

HIDDEN_MODES = ("exclude", "include")
NORM_STATUSES = ("normalized", "publisher_only", "unmatched")
CATALOG_SORTS = ("name", "publisher", "category", "hosts", "version_count", "latest_version", "first_seen")
INSTALL_SORTS = ("asset_name", "package_name", "version", "architecture", "source", "install_date", "first_seen_at", "updated_at")
DEFAULT_CATALOG_SORT, DEFAULT_CATALOG_ORDER = "hosts", "desc"
DISTRIBUTION_TOP = 8                 # versions shown per catalog row
EVIDENCE_LIMIT = 200                 # raw-name pairs shown on the product page
PUBLISHER_LIMIT_DEFAULT, PUBLISHER_LIMIT_MAX = 50, 200
MAX_INSTALL_SORT_ROWS = 50_000       # sort=version fetches the product's rows and orders them by the comparator
NOISE_CATEGORIES = frozenset({"update", "driver", "os-component"})   # categories only noise rules assign
UNKNOWN = iq.UNKNOWN
NO_VERSION = "__none__"              # installations?version=: the rows that report no version (BN-09); 'unknown' is a plain version
MAX_RANKED_PRODUCTS = 40_000         # sort=latest_version orders at most this many products (a CPU cap; narrow the catalog first)

_TABLE_TTL_S = 60.0
_TABLE_STALE_S = 300.0       # the whole-estate table may be served this long while a fresh one is built
_TABLE_MAX = 32
_DETAIL_CAP = 1500           # products whose version lists one table keeps (the pages in use; older ones are read again)
_PUB_SIGNATURES_MAX = 16     # publisher-facet views one table keeps hosts for
# sort=hosts: the first batch of candidate products (by installs) is twice the page's end plus a slack (products outside
# the view and ties make a tight first round fall short, and a round trip costs more than the extra small products do);
# each round that cannot prove the page is final triples the candidates.
_CANDIDATE_MIN = 64
_CANDIDATE_SLACK = 14

# CPU-bound work (row building, version ranking) runs here, not on the event loop. Two threads: the loop and the other
# requests keep the interpreter's attention, and a burst of cold requests cannot start a thread per request.
_CPU = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="inv-catalog")
T = TypeVar("T")


async def _cpu(fn: Callable[..., T], *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(_CPU, fn, *args)


# ── Normalisation status ─────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _pack_rule_keys() -> frozenset[str]:
    return frozenset(r.key for r in getattr(inventory_normalize, "_SOFT_RULES", ()))


@functools.lru_cache(maxsize=1)
def _pack_rule_names() -> Mapping[str, str]:
    return {r.key: r.product for r in getattr(inventory_normalize, "_SOFT_RULES", ())}


def _known_publishers() -> Mapping[str, str]:
    return getattr(inventory_normalize, "_VENDOR_BY_KEY", {})


def norm_status(name_key: str, category: Optional[str], publisher_key: Optional[str]) -> str:
    """How the normaliser arrived at a stored product key, derived from what the rows keep.

    The software rows store the key, the publisher key and the category, not the status. The pipeline
    (``inventory_normalize.normalize_software``) makes the status recoverable: a product rule's key is one of
    the pack's keys (``normalized``); a noise rule assigns update, driver or os-component and nothing else does
    (``normalized``); the generic fallback gives category ``other`` and is ``publisher_only`` when the publisher
    is a known alias and ``unmatched`` otherwise. ``test_inventory_software`` checks this function against
    the real normaliser for every product of the fleet and a corpus of titles, so a pipeline change that
    breaks the derivation fails there instead of showing a wrong pill.
    """
    if name_key in _pack_rule_keys():
        return "normalized"
    if (category or "other") in NOISE_CATEGORIES:
        return "normalized"
    if publisher_key and publisher_key in _known_publishers():
        return "publisher_only"
    return "unmatched"


def _normalise_status(package_name: Optional[str], vendor: Optional[str], source: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(status, rule_id)`` the normaliser gives one raw title (the evidence behind a product)."""
    try:
        key = inventory_normalize.normalize_software(package_name, vendor, None, source=source)
        return key.status, key.rule_id
    except Exception:  # noqa: BLE001 - evidence must never fail a page
        return None, None


def _publisher_label(publisher_key: Optional[str], raw: Optional[Mapping[str, Optional[str]]] = None) -> Optional[str]:
    """The publisher as people write it: the alias table's label, else the raw publisher text found in the data
    (``raw``, see ``_ensure_pub_labels``), else the key itself. None when the product has no publisher at all."""
    if not publisher_key:
        return None
    known = _known_publishers().get(publisher_key)
    if known:
        return known
    vendor = iq._clean((raw or {}).get(publisher_key), 255)
    return vendor or publisher_key


def _slug_tokens(value: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9+#]+", value.lower()) if t]


# ── Filters ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CatalogFilters:
    """Filters of GET /software, /software/publishers and the software export.

    ``q`` matches the product key, the product name of a catalog-rule product and the publisher (every word
    of ``q`` must appear). ``category`` is repeatable. ``hidden='exclude'`` (default) hides the products whose
    category is in ``hide_categories_by_default``, except a category the caller asked for by name.
    ``unmatched`` keeps products the normaliser could not place."""

    q: Optional[str] = None
    category: tuple[str, ...] = ()
    publisher: Optional[str] = None
    hidden: str = "exclude"
    unmatched: bool = False

    def normalized(self) -> "CatalogFilters":
        cats: list[str] = []
        for c in self.category or ():
            c = iq._clean(c, 32)
            if not c:
                continue
            c = c.lower()
            if c not in inventory_normalize.CATEGORIES:
                raise iq.InvalidFilter(f"category must be one of: {', '.join(inventory_normalize.CATEGORIES)}")
            if c not in cats:
                cats.append(c)
        hidden = iq._clean(self.hidden, 16) or "exclude"
        if hidden not in HIDDEN_MODES:
            raise iq.InvalidFilter(f"hidden must be one of: {', '.join(HIDDEN_MODES)}")
        return replace(self, q=iq._clean(self.q), category=tuple(cats), publisher=iq._clean(self.publisher, 96),
                       hidden=hidden, unmatched=bool(self.unmatched))

    def as_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.q:
            out["q"] = self.q
        if self.category:
            out["category"] = list(self.category)
        if self.publisher:
            out["publisher"] = self.publisher
        if self.hidden != "exclude":
            out["hidden"] = self.hidden
        if self.unmatched:
            out["unmatched"] = True
        return out


# ── The light table ──────────────────────────────────────────────────

@dataclass(frozen=True)
class KeyRow:
    key: str
    category: str
    publisher_key: Optional[str]
    installs: int
    first_seen: Optional[datetime]
    status: str
    haystack: str


@dataclass(frozen=True)
class VersionStat:
    version: Optional[str]          # None: the product's rows with no (or a blank) version
    hosts: int
    installs: int
    scheme: str


@dataclass
class KeyTable:
    """Every product of one scope with its cheap figures, plus the exact figures found so far."""

    rows: list[KeyRow]
    by_key: dict[str, KeyRow]
    servers: int                                    # visible servers: an upper bound of any product's hosts
    built_at: datetime
    monotonic_at: float
    vis: dict[str, Any] = field(default_factory=dict)   # the visible server ids the table was built with (empty: unscoped)
    hosts: dict[str, int] = field(default_factory=dict)
    key_installs: dict[str, int] = field(default_factory=dict)             # installs read together with `hosts` (same moment)
    versions: dict[str, list[VersionStat]] = field(default_factory=dict)   # at most _DETAIL_CAP products (oldest dropped first)
    version_counts: dict[str, int] = field(default_factory=dict)           # distinct version keys, from the (product, version) pairs
    latest: dict[str, Optional[str]] = field(default_factory=dict)         # newest version of the products a sort asked for
    counts_ready: bool = False                      # version_counts holds EVERY product
    pub_labels: dict[str, Optional[str]] = field(default_factory=dict)     # raw publisher text of publishers the alias table lacks
    pub_hosts: dict[str, dict[str, int]] = field(default_factory=dict)     # publisher facet hosts, per filter signature (bounded)
    shared: dict[str, "asyncio.Task[Any]"] = field(default_factory=dict)   # reads of this table that concurrent callers share


_tables: dict[tuple, KeyTable] = {}
_inflight: dict[tuple, "asyncio.Task[KeyTable]"] = {}
_last_asked: dict[str, float] = {"all": time.monotonic()}      # when a request last asked for the whole-estate table (the warmer's idle test)


def clear_caches() -> None:
    """Forget cached catalog tables (tests, and anything that needs a fresh read)."""
    _tables.clear()
    _inflight.clear()


def _monotonic() -> float:
    return time.monotonic()


def _scope_key(scope: Optional[Sequence[str]]) -> tuple:
    return ("scoped", tuple(sorted(scope))) if scope is not None else ("all",)


def evict_expired(now: Optional[float] = None) -> int:
    """Drop the cached tables nobody can be served any more: a restricted caller's table after its minute, the whole-estate
    table after the stale window. Runs on every table request and from the warmer, so an idle process lets go of its
    tables too (F10: they were only replaced when their own scope was asked for again). Returns the number dropped."""
    now = _monotonic() if now is None else now
    dead = [k for k, t in _tables.items()
            if now - t.monotonic_at >= (_TABLE_STALE_S if k == ("all",) else _TABLE_TTL_S)]
    for k in dead:
        _tables.pop(k, None)
    return len(dead)


_TABLE_SQL = """
SELECT w.name_key, count(*) AS installs, min(w.first_seen_at) AS first_seen, max(w.category) AS category,
       max(w.publisher_key) AS publisher_key
  FROM server_software_inventory w
 WHERE w.name_key IS NOT NULL {vis}
 GROUP BY w.name_key"""

# Distinct versions per product, at the level of the raw spelling (one row per (product, version): 36,000 at 1.8 M software
# rows). The equivalence key is computed on THESE groups, not on the millions of rows: a regular expression per software
# row costs a second at 2 M rows, per distinct (product, version) a few milliseconds.
_SPELLINGS_SQL = """
SELECT w.name_key, w.version, count(*) AS installs, bool_or(w.source = 'dpkg') AS dpkg, bool_or(w.source = 'rpm') AS rpm
  FROM server_software_inventory w
 WHERE w.name_key IS NOT NULL {vis} {only}
 GROUP BY w.name_key, w.version"""
_ONLY_KEYS = "AND w.name_key = ANY(CAST(:keys AS text[]))"


def _vkey_group(column: str = "g.version") -> str:
    """SQL for the version key of a spelling; NULL for no version (NULL or blank)."""
    return f"(CASE WHEN {versioning.BLANK_VERSION_SQL.format(c=column)} THEN NULL ELSE {iq._vkey(column)} END)"


# One row per (product, version key): installs and scheme flags summed over the spellings, the label the longest spelling
# (then the greatest, byte order, as ``_label_of`` does).
_PAIRS_SQL = f"""
SELECT s.name_key, {_vkey_group('s.version')} AS vk,
       substr(max((lpad(length(s.version)::text, 6, '0') || s.version) COLLATE "C"), 7) AS label,
       sum(s.installs)::bigint AS installs, bool_or(s.dpkg) AS dpkg, bool_or(s.rpm) AS rpm
  FROM ({_SPELLINGS_SQL}) s
 GROUP BY s.name_key, {_vkey_group('s.version')}"""

# version_count of every product: the number of version keys (no ranking, so no Python)
_COUNTS_SQL = f"""
SELECT s.name_key, count(DISTINCT {_vkey_group('s.version')}) AS n
  FROM ({_SPELLINGS_SQL}) s
 GROUP BY s.name_key"""

# the lists behind latest_version: per product the labels and flags of its version keys, one array each
_LATEST_SQL = f"""
SELECT p.name_key,
       array_agg(p.label) FILTER (WHERE p.vk IS NOT NULL) AS labels, array_agg(p.installs) FILTER (WHERE p.vk IS NOT NULL) AS installs,
       array_agg(p.dpkg) FILTER (WHERE p.vk IS NOT NULL) AS dpkg, array_agg(p.rpm) FILTER (WHERE p.vk IS NOT NULL) AS rpm
  FROM ({_PAIRS_SQL}) p
 GROUP BY p.name_key"""
# (the no-version groups are filtered out of the arrays, not with a WHERE: a WHERE on the key would be pushed down into the
# scan of the software table and evaluate the key's regular expression for every row instead of every distinct spelling)


def _build_rows(rows: Sequence[Mapping[str, Any]]) -> list[KeyRow]:
    """The table's rows (CPU work: a product status and a search haystack each; thousands of products)."""
    out: list[KeyRow] = []
    known, names = _known_publishers(), _pack_rule_names()
    for r in rows:
        key = r["name_key"]
        category = r["category"] or "other"
        pub = r["publisher_key"] or None
        label = known.get(pub or "", "")
        hay = " ".join(filter(None, (key, (pub or ""), label, names.get(key, ""))))
        out.append(KeyRow(key=key, category=category, publisher_key=pub, installs=int(r["installs"]),
                          first_seen=r["first_seen"], status=norm_status(key, category, pub), haystack=hay.lower()))
    return out


async def _load_table(db: AsyncSession, scope: Optional[Sequence[str]]) -> KeyTable:
    await iq._tune(db)
    vis_params = await iq._visible_server_ids(db, scope)
    vis = iq._visible_servers_pred(scope)
    # Without this the planner walks idx_ssi_name_key in order (random heap reads, about 0.5 s at 2 M rows)
    # instead of one parallel scan with a partial hash aggregate (about 0.1 s). Transaction-local.
    await db.execute(text("SELECT set_config('enable_indexscan', 'off', true)"))
    rows = await iq._run(db, _TABLE_SQL.format(vis=vis), vis_params)
    await db.execute(text("SELECT set_config('enable_indexscan', 'on', true)"))
    if scope is None:
        servers = int((await iq._run(db, "SELECT count(*) AS n FROM servers", {}))[0]["n"])
    else:
        servers = len(vis_params.get("vis_sids", []))
    out = await _cpu(_build_rows, rows)
    return KeyTable(rows=out, by_key={r.key: r for r in out}, servers=servers, built_at=iq.utcnow(),
                    monotonic_at=_monotonic(), vis=dict(vis_params))


def _start_build(db: AsyncSession, scope: Optional[Sequence[str]], key: tuple) -> "asyncio.Task[KeyTable]":
    """The build of ``scope``'s table that is running, or a new one. A build runs on a session of its own so that a
    caller that goes away mid-way cannot take the shared result down with it, and it is cached when it finishes."""
    loop = asyncio.get_running_loop()
    task = _inflight.get(key)
    if task is not None and not task.done() and task.get_loop() is loop:
        return task
    bind = iq.engine_of(db)

    async def build() -> KeyTable:
        if bind is None:
            return await _load_table(db, scope)
        async with AsyncSession(bind=bind, expire_on_commit=False) as own:
            return await _load_table(own, scope)

    task = loop.create_task(build())
    _inflight[key] = task

    def done(t: "asyncio.Task[KeyTable]") -> None:
        if _inflight.get(key) is t:
            _inflight.pop(key, None)
        if t.cancelled():
            return
        if t.exception() is not None:
            logger.warning("software catalog table for %s could not be built: %r", key[0], t.exception())
            return
        evict_expired()
        if len(_tables) >= _TABLE_MAX:
            _tables.pop(min(_tables, key=lambda k: _tables[k].monotonic_at), None)
        _tables[key] = t.result()

    task.add_done_callback(done)
    return task


async def _wait_for(db: AsyncSession, task: "asyncio.Task[T]") -> T:
    """Wait for a shared task without holding a pooled connection (F5). The task runs on a session of its own and needs a
    connection from the same pool: callers that each kept theirs while they waited could use the pool up and starve the
    task, and with it every other request of the application. Closing the session returns the connection (it can be used
    again afterwards, but its transaction-local planner settings are gone: callers ``iq._tune`` again)."""
    if iq.engine_of(db) is None:
        return await asyncio.shield(task)
    return await iq._wait_for_shared(db, task)           # type: ignore[arg-type]


async def _table(db: AsyncSession, scope: Optional[Sequence[str]]) -> KeyTable:
    """The scope's light table: cached for 60 s, built once for concurrent callers (the Software page asks for the
    catalog and the publishers together).

    The whole-estate table (``scope`` None) is the expensive one (0.15 s of scanning at 2 M software rows, against
    0.05 to 0.1 s for a team's) and nobody's visibility depends on it, so past its minute it is still served for up to
    five minutes while a fresh one is built in the background: a page in use never waits for a rebuild. A restricted
    caller's table is never served stale, because it holds which servers the caller may see.

    A caller that has to wait for a build gives its connection back first (F5) and re-applies the planner settings after."""
    key = _scope_key(scope)
    evict_expired()
    if scope is None:
        _last_asked["all"] = _monotonic()
    hit = _tables.get(key)
    age = _monotonic() - hit.monotonic_at if hit is not None else None
    if hit is not None and age < _TABLE_TTL_S:
        return hit
    task = _start_build(db, scope, key)
    if hit is not None and scope is None and age < _TABLE_STALE_S:
        return hit
    table = await _wait_for(db, task)
    await iq._tune(db)
    return table


async def _shared_read(db: AsyncSession, table: KeyTable, name: str, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` once for every concurrent caller that wants the same read of ``table`` (``name``), on a session of its
    own, and wait for it without a connection of the caller's (F5). The result is stored by ``work`` itself."""
    loop = asyncio.get_running_loop()
    task = table.shared.get(name)
    if task is None or task.done() or task.get_loop() is not loop:
        bind = iq.engine_of(db)

        async def run() -> T:
            if bind is None:
                return await work(db)
            async with AsyncSession(bind=bind, expire_on_commit=False) as own:
                await iq._tune(own)
                return await work(own)

        task = loop.create_task(run())
        table.shared[name] = task

        def done(t: "asyncio.Task[Any]") -> None:
            if table.shared.get(name) is t:
                table.shared.pop(name, None)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("software catalog read %s could not be completed: %r", name, t.exception())

        task.add_done_callback(done)
    try:
        return await _wait_for(db, task)
    finally:
        await iq._tune(db)


# ── Exact figures for chosen products ────────────────────────────────

def scheme_of(dpkg: bool, rpm: bool) -> str:
    return "dpkg" if dpkg else "rpm" if rpm else "semverish"


_DETAIL_SQL = """
SELECT w.name_key, w.version, count(*) AS installs, count(DISTINCT w.server_id) AS hosts,
       grouping(w.version) AS is_key, bool_or(w.source = 'dpkg') AS dpkg, bool_or(w.source = 'rpm') AS rpm
  FROM server_software_inventory w
 WHERE w.name_key = ANY(CAST(:keys AS text[])) {vis}
 GROUP BY GROUPING SETS ((w.name_key, w.version), (w.name_key))"""

# hosts and installs only (no version lists): what ranking products by hosts needs of its candidates
_HOSTS_SQL = """
SELECT w.name_key, count(*) AS installs, count(DISTINCT w.server_id) AS hosts
  FROM server_software_inventory w
 WHERE w.name_key = ANY(CAST(:keys AS text[])) {vis}
 GROUP BY w.name_key"""

# hosts of version keys that have several spellings, counted once across them: (job, product, spelling) triples
_MERGED_HOSTS_SQL = """
SELECT m.job, count(DISTINCT w.server_id) AS hosts
  FROM unnest(CAST(:jobs AS int[]), CAST(:names AS text[]), CAST(:versions AS text[])) AS m(job, name_key, version)
  JOIN server_software_inventory w ON w.name_key = m.name_key AND w.version = m.version
 WHERE true {vis}
 GROUP BY m.job"""

_BLANK_HOSTS_SQL = f"""
SELECT w.name_key, count(DISTINCT w.server_id) AS hosts
  FROM server_software_inventory w
 WHERE w.name_key = ANY(CAST(:keys AS text[])) AND (w.version IS NULL OR {versioning.BLANK_VERSION_SQL.format(c='w.version')}) {{vis}}
 GROUP BY w.name_key"""

_DETAIL_CHUNK = 1500
_MERGED_HOSTS_CHUNK = 20_000          # triples per statement; a job is never split across statements (NB-04)


def _job_chunks(jobs: Sequence[int], limit: int) -> list[tuple[int, int]]:
    """``(start, end)`` slices of the triple lists, about ``limit`` triples each, that never cut through a job. The triples of
    a job are contiguous and the job numbers never decrease, so a slice that would end inside a job is extended to the end of
    it (one job with more spellings than ``limit`` is one slice of its own). A host that holds spellings of one version on both
    sides of a cut would otherwise be counted once per slice."""
    out: list[tuple[int, int]] = []
    start, n = 0, len(jobs)
    while start < n:
        end = min(start + max(1, limit), n)
        while end < n and jobs[end] == jobs[end - 1]:
            end += 1
        out.append((start, end))
        start = end
    return out


def _label_of(spellings: Sequence[str]) -> str:
    """The spelling that labels a version key: the longest (``7.2.0`` over ``7.2``), then the greatest by byte order, so the
    choice never depends on row order. ``_PAIRS_SQL`` makes the same choice in the database."""
    return max(spellings, key=lambda v: (len(v), v))


def merge_spellings(stats: Sequence[VersionStat], keys: Optional[Mapping[Optional[str], Optional[str]]] = None
                    ) -> tuple[list[VersionStat], list[tuple[int, list[Optional[str]]]]]:
    """Merge the spellings of one version (same ``versioning.equivalence_key``) into one entry each. ``keys`` maps a spelling
    to its key as the DATABASE computes it (``_spelling_keys``): the Assets list matches ``product_version`` with the
    database's key, so the merge must too (they differ for non-ASCII letters when the database's locale cannot lower-case
    them); without it the Python function decides.

    Returns ``(merged, multi)``. A merged entry's ``installs`` is the sum, its scheme is dpkg or rpm when any spelling is, and
    its label is ``_label_of`` the spellings (None for the rows that report no version). Its ``hosts`` is only a SUM here:
    a server that holds two spellings would be counted twice, so ``multi`` lists, for every merged entry, its index and
    its spellings, and the caller replaces the sum by the exact distinct count."""
    groups: dict[Optional[str], list[VersionStat]] = {}
    for st in stats:
        groups.setdefault(keys[st.version] if keys is not None and st.version in keys else versioning.equivalence_key(st.version), []).append(st)
    merged: list[VersionStat] = []
    multi: list[tuple[int, list[Optional[str]]]] = []
    for key, members in groups.items():
        if len(members) == 1:
            merged.append(members[0])
            continue
        spellings = [m.version for m in members]
        label = None if key is None else _label_of([v for v in spellings if v is not None])
        scheme = "dpkg" if any(m.scheme == "dpkg" for m in members) else "rpm" if any(m.scheme == "rpm" for m in members) else "semverish"
        merged.append(VersionStat(label, sum(m.hosts for m in members), sum(m.installs for m in members), scheme))
        multi.append((len(merged) - 1, spellings))
    return merged, multi


_SPELLING_KEYS_SQL = f"""
SELECT g.version, {_vkey_group('g.version')} AS vk FROM unnest(CAST(:spellings AS text[])) AS g(version)"""


async def _spelling_keys(db: AsyncSession, spellings: Sequence[Optional[str]]) -> dict[Optional[str], Optional[str]]:
    """The version key of each distinct spelling as the database computes it (one statement; None for no version)."""
    distinct = sorted({sp for sp in spellings if sp is not None})
    out: dict[Optional[str], Optional[str]] = {None: None}
    for i in range(0, len(distinct), 20_000):
        for r in await iq._run(db, _SPELLING_KEYS_SQL, {"spellings": distinct[i:i + 20_000]}):
            out[r["version"]] = r["vk"]
    return out


async def _exact_merged_hosts(db: AsyncSession, scope: Optional[Sequence[str]], vis_params: Mapping[str, Any],
                              work: Sequence[tuple[str, int, list[Optional[str]]]]) -> dict[tuple[str, int], int]:
    """Exact host counts for merged entries: ``work`` is ``(product, index in its merged list, spellings)``."""
    out: dict[tuple[str, int], int] = {}
    vis = iq._visible_servers_pred(scope)
    jobs: list[int] = []
    names: list[str] = []
    versions: list[str] = []
    ids: dict[int, tuple[str, int]] = {}
    blank_keys: list[str] = []
    for product_key, index, spellings in work:
        if all(sp is None or sp.strip() == "" for sp in spellings):        # the unversioned bucket (NULL and '' and '  ')
            blank_keys.append(product_key)
            continue
        job = len(ids)
        ids[job] = (product_key, index)
        for sp in spellings:
            if sp is not None:
                jobs.append(job)
                names.append(product_key)
                versions.append(sp)
    for lo, hi in _job_chunks(jobs, _MERGED_HOSTS_CHUNK):
        for r in await iq._run(db, _MERGED_HOSTS_SQL.format(vis=vis),
                               {"jobs": jobs[lo:hi], "names": names[lo:hi], "versions": versions[lo:hi], **vis_params}):
            out[ids[int(r["job"])]] = int(r["hosts"])
    if blank_keys:
        got = {r["name_key"]: int(r["hosts"]) for r in await iq._run(
            db, _BLANK_HOSTS_SQL.format(vis=vis), {"keys": blank_keys, **vis_params})}
        for product_key, index, spellings in work:
            if product_key in got and all(sp is None or sp.strip() == "" for sp in spellings):
                out[(product_key, index)] = got[product_key]
    return out


async def _detail_rows(db: AsyncSession, scope: Optional[Sequence[str]], keys: Sequence[str],
                       vis_params: Mapping[str, Any], *, versions: bool = True
                       ) -> tuple[dict[str, int], dict[str, list[VersionStat]], dict[str, int]]:
    """``(hosts, version stats, installs)`` per product, one statement per chunk of products. The version stats are one
    entry per version KEY (spellings merged, hosts counted once across them); ``versions=False`` skips them."""
    hosts: dict[str, int] = {}
    stats: dict[str, list[VersionStat]] = {}
    installs: dict[str, int] = {}
    vis = iq._visible_servers_pred(scope)
    for i in range(0, len(keys), _DETAIL_CHUNK):
        chunk = list(keys[i:i + _DETAIL_CHUNK])
        if not versions:
            for r in await iq._run(db, _HOSTS_SQL.format(vis=vis), {"keys": chunk, **vis_params}):
                hosts[r["name_key"]] = int(r["hosts"])
                installs[r["name_key"]] = int(r["installs"])
        else:
            for r in await iq._run(db, _DETAIL_SQL.format(vis=vis), {"keys": chunk, **vis_params}):
                key = r["name_key"]
                if r["is_key"]:
                    hosts[key] = int(r["hosts"])
                    installs[key] = int(r["installs"])
                else:
                    stats.setdefault(key, []).append(VersionStat(
                        version=r["version"], hosts=int(r["hosts"]), installs=int(r["installs"]),
                        scheme=scheme_of(bool(r["dpkg"]), bool(r["rpm"]))))
        for k in chunk:
            hosts.setdefault(k, 0)
            installs.setdefault(k, 0)
            if versions:
                stats.setdefault(k, [])
    if versions:
        work: list[tuple[str, int, list[Optional[str]]]] = []
        spelling_keys = await _spelling_keys(db, [st.version for lst in stats.values() for st in lst])
        for key in list(stats):
            stats[key], multi = merge_spellings(stats[key], spelling_keys)
            work.extend((key, index, spellings) for index, spellings in multi)
        if work:
            exact = await _exact_merged_hosts(db, scope, vis_params, work)
            for (key, index), n in exact.items():
                stats[key][index] = replace(stats[key][index], hosts=n)
    return hosts, stats, installs


# Below this many products the exact read is one statement; above it, two on separate connections (the products are
# dealt out alternately by installs so that both halves have about the same number of rows to read).
_SPLIT_DETAIL_AT = 24


def _remember_versions(table: KeyTable, stats: Mapping[str, list[VersionStat]]) -> None:
    """Keep the version lists of the products just read, most recent last, and let the oldest go past ``_DETAIL_CAP``."""
    for k, v in stats.items():
        table.versions.pop(k, None)
        table.versions[k] = v
    while len(table.versions) > _DETAIL_CAP:
        table.versions.pop(next(iter(table.versions)))


async def _ensure_detail(db: AsyncSession, table: KeyTable, scope: Optional[Sequence[str]], keys: Sequence[str],
                         *, versions: bool = True) -> dict[str, list[VersionStat]]:
    """Fill ``table.hosts`` (and with ``versions`` the version lists) for ``keys`` (those not known yet). The figures are
    read for the servers the table itself was built for, so they always agree with its installs. Returns the version
    lists of ``keys`` (empty without ``versions``): the table keeps only the latest pages' lists, so a caller uses the
    returned ones, which are complete however small the table's cap."""
    wanted = list(dict.fromkeys(keys))
    have = {k: table.versions[k] for k in wanted if k in table.versions} if versions else {}
    missing = [k for k in wanted if k not in table.hosts or (versions and k not in have)]
    if not missing:
        return have
    if len(missing) >= _SPLIT_DETAIL_AT:
        by_size = sorted(missing, key=lambda k: -table.by_key[k].installs if k in table.by_key else 0)
        halves = [by_size[0::2], by_size[1::2]]

        async def half(i: int):
            async def run(s: AsyncSession):
                return await _detail_rows(s, scope, halves[i], table.vis, versions=versions)
            return run

        results = await iq._concurrently(db, await half(0), await half(1))
    else:
        results = [await _detail_rows(db, scope, missing, table.vis, versions=versions)]
    for hosts, stats, installs in results:
        table.hosts.update(hosts)
        table.key_installs.update(installs)
        if versions:
            have.update(stats)
            _remember_versions(table, stats)
    return have


# ── Version order ────────────────────────────────────────────────────

@dataclass(frozen=True)
class RankedVersion:
    version: Optional[str]
    hosts: int
    installs: int
    scheme: str
    rank: Optional[int]             # 1 = newest of its scheme group; None = not orderable (or no version)
    generation: Optional[str]       # 'N', 'N-1', 'older' for rank 1, 2, 3 and up


_SCHEME_ORDER = {"semverish": 0, "dpkg": 1, "rpm": 2}


def _is_blank(version: Optional[str]) -> bool:
    return version is None or version.strip() == ""


def rank_versions(stats: Sequence[VersionStat]) -> tuple[list[RankedVersion], Optional[RankedVersion]]:
    """Order a product's versions and rank them.

    Returns ``(ordered, unversioned)``. ``ordered`` lists the distinct non-blank versions: the scheme group with
    the most installs first (ties: semverish, dpkg, rpm; installs rather than hosts so that the cheap read behind
    ``sort=latest_version`` and the exact one of a page agree), inside a group the comparable versions newest first
    (``rank`` 1, 2, ...; two spellings of one version, ``1.0`` and ``1.0.0``, share a rank and the longer spelling
    comes first), then the versions the scheme cannot parse in natural order, newest first, with no rank.
    ``unversioned`` merges the rows with no or a blank version (None when there are none)."""
    groups: dict[str, list[VersionStat]] = {}
    blank_hosts = blank_installs = 0
    has_blank = False
    for s in stats:
        if _is_blank(s.version):
            has_blank = True
            blank_hosts += s.hosts
            blank_installs += s.installs
        else:
            groups.setdefault(s.scheme, []).append(s)
    out: list[RankedVersion] = []
    for scheme in sorted(groups, key=lambda g: (-sum(x.installs for x in groups[g]), _SCHEME_ORDER.get(g, 9))):
        items = groups[scheme]
        ok = [s for s in items if versioning.parse(scheme, s.version) is not None]   # type: ignore[arg-type]
        bad = [s for s in items if versioning.parse(scheme, s.version) is None]      # type: ignore[arg-type]

        def cmp(a: VersionStat, b: VersionStat, scheme: str = scheme) -> int:
            return versioning.compare(scheme, a.version, b.version) or 0              # type: ignore[arg-type]

        # The natural order is nearly the version order, so sorting by it first leaves the comparator (the slow part)
        # almost nothing to do: Timsort needs about one comparison per version on nearly sorted input.
        ok.sort(key=lambda s: iq._natural_key(s.version), reverse=True)     # (and equal spellings keep longest first)
        ordered = sorted(ok, key=functools.cmp_to_key(cmp), reverse=True)
        level = 0
        prev: Optional[VersionStat] = None
        for s in ordered:
            if prev is None or cmp(prev, s) != 0:
                level += 1
            prev = s
            out.append(RankedVersion(s.version, s.hosts, s.installs, scheme, level,
                                     "N" if level == 1 else "N-1" if level == 2 else "older"))
        for s in sorted(bad, key=lambda x: iq._natural_key(x.version), reverse=True):
            out.append(RankedVersion(s.version, s.hosts, s.installs, scheme, None, None))
    unversioned = RankedVersion(None, blank_hosts, blank_installs, "semverish", None, None) if has_blank else None
    return out, unversioned


def latest_of(ordered: Sequence[RankedVersion]) -> Optional[str]:
    """The newest comparable version of the product's main scheme group (None when nothing can be ordered)."""
    for v in ordered:
        if v.rank == 1:
            return v.version
    return None


# ── Catalog ──────────────────────────────────────────────────────────

def _matches(row: KeyRow, f: CatalogFilters, *, ignore_publisher: bool = False) -> bool:
    if f.category and row.category not in f.category:
        return False
    if f.publisher and not ignore_publisher:
        want = f.publisher.lower()
        have = (row.publisher_key or "").lower()
        if (want == UNKNOWN and row.publisher_key is None) or have == want:
            pass
        else:
            return False
    if f.q:
        # Every word must appear; a query with no word characters at all ('%', '--') is looked for literally, so
        # punctuation never selects everything.
        words = _slug_tokens(f.q) or [f.q.lower()]
        if not all(w in row.haystack for w in words):
            return False
    return True


def _hidden_set(f: CatalogFilters, settings: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(settings["hide_categories_by_default"]) - set(f.category)


def split_rows(table: KeyTable, f: CatalogFilters, settings: Mapping[str, Any],
               *, ignore_publisher: bool = False) -> dict[str, Any]:
    """The products a filter set selects, and the counts of the footer.

    ``hidden_products`` / ``hidden_installs``: the hidden-category products among those the other filters
    select (so ``total(hidden=include) == total(hidden=exclude) + hidden_products``). ``unmatched_titles``: the
    unmatched products among those the other filters select, shown or not by the hidden rule (so
    ``total(unmatched=true) == unmatched_titles``)."""
    hidden = _hidden_set(f, settings)
    base = [r for r in table.rows if _matches(r, f, ignore_publisher=ignore_publisher)]
    in_scope_of_unmatched = [r for r in base if r.status == "unmatched" or not f.unmatched]
    hidden_rows = [r for r in in_scope_of_unmatched if r.category in hidden]
    shown_pool = in_scope_of_unmatched if f.hidden == "include" else [r for r in in_scope_of_unmatched if r.category not in hidden]
    visible_base = base if f.hidden == "include" else [r for r in base if r.category not in hidden]
    return {
        "rows": shown_pool,
        "hidden_products": len(hidden_rows),
        "hidden_installs": sum(r.installs for r in hidden_rows),
        "unmatched_titles": sum(1 for r in visible_base if r.status == "unmatched"),
        "hidden_categories": sorted(hidden),
    }


def _sort_rows(rows: list[KeyRow], sort: str, order: str, value) -> list[KeyRow]:
    """Stable order: key ascending, then the requested key; rows without a value always last."""
    rows = sorted(rows, key=lambda r: r.key)
    have = [r for r in rows if value(r) is not None]
    none = [r for r in rows if value(r) is None]
    have.sort(key=value, reverse=(order == "desc"))      # reverse=True keeps equal rows in their (key) order
    return have + none


_VERSIONS_WITH_CANDIDATES = 100     # a page this big (rows up to its end) reads the candidates' version lists in the same pass


async def _by_hosts(db: AsyncSession, table: KeyTable, scope: Optional[Sequence[str]], rows: list[KeyRow],
                    need: int, order: str) -> list[KeyRow]:
    """``rows`` ordered by hosts (ties: key), exact for at least the first ``need`` of them. The candidates are measured
    for hosts only; for a big page their version lists come with the same read (the page needs most of them anyway)."""
    with_versions = need > _VERSIONS_WITH_CANDIDATES
    if order == "asc":
        await _ensure_detail(db, table, scope, [r.key for r in rows], versions=False)
        return sorted(rows, key=lambda r: (table.hosts[r.key], r.key))
    by_installs = sorted(rows, key=lambda r: (-r.installs, r.key))
    total = len(by_installs)
    size = min(total, max(_CANDIDATE_MIN, 2 * need + _CANDIDATE_SLACK))     # hidden products and ties make the first round short
    while True:
        cands = by_installs[:size]
        await _ensure_detail(db, table, scope, [r.key for r in cands], versions=with_versions)
        ranked = sorted(cands, key=lambda r: (-table.hosts[r.key], r.key))
        if size >= total:
            return ranked
        # Everything outside the candidates has installs <= the last candidate's and hosts <= installs and <=
        # the visible servers, so a page whose last host count is strictly above that bound is final.
        bound = min(cands[-1].installs, table.servers)
        if len(ranked) >= need and table.hosts[ranked[need - 1].key] > bound:
            return ranked
        size = min(total, size * 3)


def _rank_many(lists: Mapping[str, Sequence[VersionStat]]) -> dict[str, tuple[list["RankedVersion"], Optional["RankedVersion"]]]:
    """``rank_versions`` of several products (CPU work: runs in the worker pool)."""
    return {k: rank_versions(v) for k, v in lists.items()}


def _latest_many(rows: Sequence[tuple[str, Sequence[str], Sequence[int], Sequence[bool], Sequence[bool]]]) -> dict[str, Optional[str]]:
    """``latest_of(rank_versions(...))`` of every product, from its version keys as ``(product, labels, installs, dpkg,
    rpm)`` (CPU work: runs in the worker pool; a product with one version needs no comparison)."""
    out: dict[str, Optional[str]] = {}
    for key, labels, installs, dpkg, rpm in rows:
        if not labels:
            out[key] = None                      # only rows with no version
            continue
        if len(labels) == 1:                     # one version: the newest if the scheme can read it (what rank 1 means)
            scheme = scheme_of(bool(dpkg[0]), bool(rpm[0]))
            out[key] = labels[0] if versioning.parse(scheme, labels[0]) is not None else None   # type: ignore[arg-type]
            continue
        stats = [VersionStat(lab, 0, int(n), scheme_of(bool(d), bool(r))) for lab, n, d, r in zip(labels, installs, dpkg, rpm)]
        out[key] = latest_of(rank_versions(stats)[0])
    return out


async def _ensure_pairs(db: AsyncSession, table: KeyTable, scope: Optional[Sequence[str]], *, latest: bool,
                        keys: Optional[Sequence[str]] = None) -> None:
    """``table.version_counts`` for EVERY product (without ``latest``) or ``table.latest`` for ``keys`` (default: every
    product; with ``latest``), from reads of the (product, version key) groups (50,000 at 2 M software rows). Needed only
    to sort by ``version_count`` or ``latest_version``.

    The counts are one grouped read and no ranking (the number of keys IS the version count). The newest version of a
    product needs the comparator: it is ranked in the worker pool, only for the products the sort orders (at most
    ``MAX_RANKED_PRODUCTS`` per request, else the request is refused with a hint to narrow the catalog). Both reads are
    shared by concurrent callers asking for the same thing and are kept with the table."""
    if not latest and not table.counts_ready:
        async def counts(s: AsyncSession) -> None:
            if table.counts_ready:
                return
            await s.execute(text("SELECT set_config('enable_indexscan', 'off', true)"))            # see _load_table
            rows = await iq._run(s, _COUNTS_SQL.format(vis=iq._visible_servers_pred(scope), only=""), table.vis)
            await s.execute(text("SELECT set_config('enable_indexscan', 'on', true)"))
            table.version_counts.update({r["name_key"]: int(r["n"]) for r in rows})
            for r in table.rows:
                table.version_counts.setdefault(r.key, 0)
            table.counts_ready = True

        await _shared_read(db, table, "counts", counts)
    if not latest:
        return
    missing = sorted(k for k in dict.fromkeys(keys if keys is not None else [r.key for r in table.rows]) if k not in table.latest)
    if not missing:
        return
    if len(missing) > MAX_RANKED_PRODUCTS:
        raise iq.InvalidFilter(f"sort=latest_version orders at most {MAX_RANKED_PRODUCTS} products at a time: "
                               f"narrow the catalog with q, category or publisher first")
    every = len(missing) >= len(table.rows)

    async def newest(s: AsyncSession) -> None:
        todo = [k for k in missing if k not in table.latest]
        if not todo:
            return
        params = dict(table.vis) if every else {"keys": todo, **table.vis}
        sql = _LATEST_SQL.format(vis=iq._visible_servers_pred(scope), only="" if every else _ONLY_KEYS)
        await s.execute(text("SELECT set_config('enable_indexscan', 'off', true)"))
        rows = await iq._run(s, sql, params)
        await s.execute(text("SELECT set_config('enable_indexscan', 'on', true)"))
        found = await _cpu(_latest_many, [(r["name_key"], r["labels"], r["installs"], r["dpkg"], r["rpm"]) for r in rows])
        for k in todo:
            table.latest[k] = found.get(k)

    await _shared_read(db, table, "latest:" + hashlib.sha1("\0".join(missing).encode("utf-8")).hexdigest(), newest)


async def _ensure_pub_labels(db: AsyncSession, table: KeyTable, scope: Optional[Sequence[str]], pubs: Sequence[str]) -> None:
    """The raw publisher text (the smallest ``vendor`` string, so it is the same every time) of the publishers in ``pubs``
    that the alias table does not know; these are labelled by it. Known publishers need no read."""
    missing = [p for p in dict.fromkeys(pubs) if p and p != UNKNOWN and p not in _known_publishers() and p not in table.pub_labels]
    if not missing:
        return
    vis = iq._visible_servers_pred(scope)
    got = {r["pub"]: r["vendor"] for r in await iq._run(db, f"""
        SELECT w.publisher_key AS pub, min(w.vendor) AS vendor FROM server_software_inventory w
         WHERE w.publisher_key = ANY(CAST(:pubs AS text[])) AND w.vendor IS NOT NULL AND btrim(w.vendor) <> '' {vis}
         GROUP BY w.publisher_key""", {"pubs": missing, **table.vis})}
    for p in missing:
        table.pub_labels[p] = got.get(p)


_SAMPLE_SQL = """
SELECT k.key AS name_key, w.package_name, w.vendor, w.source
  FROM unnest(CAST(:keys AS text[])) AS k(key)
  CROSS JOIN LATERAL (
      SELECT w.package_name, w.vendor, w.source FROM server_software_inventory w
       WHERE w.name_key = k.key {vis}
       ORDER BY w.version, w.package_name LIMIT 1) w"""


async def _samples(db: AsyncSession, scope: Optional[Sequence[str]], keys: Sequence[str],
                   vis_params: Optional[Mapping[str, Any]] = None) -> dict[str, Mapping[str, Any]]:
    """One raw row per product (the first by version then title, on a visible server): the source of the
    product's display name, and of the publisher text when the alias table does not know the publisher."""
    if not keys:
        return {}
    if vis_params is None:
        vis_params = await iq._visible_server_ids(db, scope)
    rows = await iq._run(db, _SAMPLE_SQL.format(vis=iq._visible_servers_pred(scope)), {"keys": list(keys), **vis_params})
    return {r["name_key"]: r for r in rows}


async def _last_changes(db: AsyncSession, scope: Optional[Sequence[str]], keys: Sequence[str],
                        vis_params: Optional[Mapping[str, Any]] = None) -> dict[str, Optional[datetime]]:
    """The newest software change event of each product (None when the product has none): events of the
    servers the scope can see only. Deleted servers have no tags to prove visibility, so a restricted caller
    never sees their events (the rule of ``inventory_query.changes``)."""
    if not keys:
        return {}
    if vis_params is None:
        vis_params = await iq._visible_server_ids(db, scope)
    scoped = "AND e.asset_id = ANY(CAST(:vis_sids AS uuid[]))" if scope is not None else ""
    rows = await iq._run(db, f"""
        SELECT k.key AS name_key,
               (SELECT e.observed_at FROM inventory_change_events e
                 WHERE e.subject_key = k.key AND e.category = 'software' AND e.asset_kind = 'server' {scoped}
                 ORDER BY e.observed_at DESC LIMIT 1) AS at
          FROM unnest(CAST(:keys AS text[])) AS k(key)""", {"keys": list(keys), **vis_params})
    return {r["name_key"]: r["at"] for r in rows}


def _check_sort(sort: str, order: str, allowed: Sequence[str]) -> None:
    if sort not in allowed:
        raise iq.InvalidFilter(f"sort must be one of: {', '.join(allowed)}")
    if order not in iq.ORDERS:
        raise iq.InvalidFilter("order must be asc or desc")


def _distribution(ranked: Sequence[RankedVersion]) -> dict[str, Any]:
    """The compact version bar of a catalog row: the eight versions with most hosts, newest first, and what is left."""
    # Busiest first; between equally busy versions the newer one (its rank, then the natural order) wins.
    newest_first = sorted(ranked, key=lambda v: iq._natural_key(v.version), reverse=True)
    pick = sorted(newest_first, key=lambda v: (-v.hosts, v.rank if v.rank is not None else 10**9))[:DISTRIBUTION_TOP]
    chosen = {v.version for v in pick}
    top = [{"version": v.version, "hosts": v.hosts, "scheme": v.scheme, "rank": v.rank}
           for v in ranked if v.version in chosen]
    rest = [v for v in ranked if v.version not in chosen]
    return {"top": top, "other": {"versions": len(rest), "hosts": sum(v.hosts for v in rest)}}


async def catalog(db: AsyncSession, filters: CatalogFilters, *, scope: Optional[Sequence[str]],
                  sort: str = DEFAULT_CATALOG_SORT, order: str = DEFAULT_CATALOG_ORDER, page: int = 1,
                  page_size: int = iq.DEFAULT_PAGE_SIZE, settings: Optional[Mapping[str, Any]] = None,
                  retain: bool = True, table: Optional[KeyTable] = None) -> dict[str, Any]:
    """One page of the software catalog (GET /inventory/software).

    ``retain=False`` (the export, which reads every page of the catalog) leaves no version lists behind in the shared
    table. ``table`` is the table to read instead of the cached one (the warmer primes a fresh table with it)."""
    f = filters.normalized()
    page, page_size = iq._page_args(page, page_size)
    _check_sort(sort, order, CATALOG_SORTS)
    await iq._tune(db)
    settings = await iq._settings(db, settings)
    if table is None:
        table = await _table(db, scope)
    split = split_rows(table, f, settings)
    rows: list[KeyRow] = split["rows"]
    offset = (page - 1) * page_size

    if sort == "hosts":
        ordered = await _by_hosts(db, table, scope, rows, offset + page_size, order)
    elif sort in ("latest_version", "version_count"):
        await _ensure_pairs(db, table, scope, latest=sort == "latest_version", keys=[r.key for r in rows])
        if sort == "version_count":
            ordered = _sort_rows(rows, sort, order, lambda r: table.version_counts[r.key])
        else:
            ordered = _sort_rows(rows, sort, order, lambda r: iq._natural_key(table.latest[r.key])
                                 if table.latest.get(r.key) is not None else None)
    else:
        getter = {
            "name": lambda r: r.key,
            "publisher": lambda r: (_known_publishers().get(r.publisher_key or "") or r.publisher_key or "").lower() or None,
            "category": lambda r: r.category,
            "first_seen": lambda r: r.first_seen,
        }[sort]
        ordered = _sort_rows(rows, sort, order, getter)
    page_rows = ordered[offset:offset + page_size]
    keys = [r.key for r in page_rows]

    async def exact(s: AsyncSession):
        await iq._tune(s)
        lists = await _ensure_detail(s, table, scope, keys, versions=True)
        # Taken at once, with no await in between: the table keeps only the version lists of the latest pages, and
        # another request may drop these before this one has used them.
        return {k: (table.hosts[k], table.key_installs[k], lists[k]) for k in keys}

    async def extras(s: AsyncSession):
        await _ensure_pub_labels(s, table, scope, [r.publisher_key for r in page_rows if r.publisher_key])
        return await _samples(s, scope, keys, table.vis), await _last_changes(s, scope, keys, table.vis)

    # The first runs on db; `_concurrently` opens the second on a connection of its own when it may.
    detail, (samples, changes) = await iq._concurrently(db, exact, extras)
    if not retain:
        for k in keys:
            table.versions.pop(k, None)
    ranked_by_key = await _cpu(_rank_many, {k: detail[k][2] for k in keys})
    items = []
    for r in page_rows:
        ranked, _unversioned = ranked_by_key[r.key]
        sample = samples.get(r.key)
        name = (iq.product_display_name(r.key, sample["package_name"], sample["vendor"], sample["source"])
                if sample is not None else r.key)
        hosts, installs, _stats = detail[r.key]
        items.append({
            "product_key": r.key,
            "name": name,
            "publisher": _publisher_label(r.publisher_key, table.pub_labels),
            "publisher_key": r.publisher_key,
            "category": r.category,
            "norm_status": r.status,
            "hosts": hosts,
            "installs": installs,                        # read with `hosts`, so a row is consistent whatever the age of the table
            "version_count": len(ranked),
            "latest_version": latest_of(ranked),
            "distribution": _distribution(ranked),
            "first_seen": iq.iso(r.first_seen),
            "last_change": iq.iso(changes.get(r.key)),
        })
    return {
        "generated_at": iq.iso(table.built_at),
        "scope": iq.scope_block(scope),
        "items": items,
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "order": order,
        "hidden_products": split["hidden_products"],
        "hidden_installs": split["hidden_installs"],
        "unmatched_titles": split["unmatched_titles"],
        "hidden_categories": split["hidden_categories"],
        "data_status": await iq.data_status(db, scope=scope),
    }


# ── Publishers ───────────────────────────────────────────────────────

async def publishers(db: AsyncSession, filters: CatalogFilters, *, scope: Optional[Sequence[str]],
                     limit: int = PUBLISHER_LIMIT_DEFAULT, settings: Optional[Mapping[str, Any]] = None,
                     table: Optional[KeyTable] = None) -> dict[str, Any]:
    """Publisher facet (GET /inventory/software/publishers): products, installs and hosts per publisher over
    the catalog's filters except ``publisher`` itself. ``value`` goes into the catalog's ``publisher`` filter
    (``unknown`` for products without a publisher). Ordered by products, then installs, then label; ``distinct``
    is the uncapped number of publishers."""
    f = filters.normalized()
    if not isinstance(limit, int) or limit < 1 or limit > PUBLISHER_LIMIT_MAX:
        raise iq.InvalidFilter(f"limit must be between 1 and {PUBLISHER_LIMIT_MAX}")
    await iq._tune(db)
    settings = await iq._settings(db, settings)
    if table is None:
        table = await _table(db, scope)
    split = split_rows(table, f, settings, ignore_publisher=True)
    rows: list[KeyRow] = split["rows"]
    groups: dict[str, list[KeyRow]] = {}
    for r in rows:
        groups.setdefault(r.publisher_key or UNKNOWN, []).append(r)

    def label_of(value: str) -> str:
        return "Unknown" if value == UNKNOWN else (_known_publishers().get(value) or value)

    order = sorted(groups, key=lambda v: (-len(groups[v]), -sum(r.installs for r in groups[v]), label_of(v).lower(), v))
    shown = order[:limit]
    # Hosts: servers with at least one row of the publisher's products in this view. One statement for all shown
    # publishers: (publisher, server) pairs are few (publishers x servers), so the distinct is a small hash aggregate
    # over a parallel scan, unlike (product, server) pairs. Kept with the table for the filter signature.
    signature = json.dumps([f.as_params(), sorted(_hidden_set(f, settings)), limit], sort_keys=True)
    hosts = table.pub_hosts.get(signature)
    if hosts is None:
        hosts = {}
        if shown:
            want_unknown = UNKNOWN in shown
            vis = iq._visible_servers_pred(scope)
            keys = [r.key for v in shown for r in groups[v]]
            for r in await iq._run(db, f"""
                    SELECT COALESCE(g.publisher_key, :unknown) AS pub, count(*) AS hosts FROM (
                        SELECT w.publisher_key, w.server_id FROM server_software_inventory w
                         WHERE w.name_key = ANY(CAST(:keys AS text[]))
                           AND (w.publisher_key = ANY(CAST(:pubs AS text[])) OR (:want_unknown AND w.publisher_key IS NULL)) {vis}
                         GROUP BY w.publisher_key, w.server_id) g
                     GROUP BY g.publisher_key""",
                    {"keys": keys, "pubs": [v for v in shown if v != UNKNOWN], "want_unknown": want_unknown, "unknown": UNKNOWN, **table.vis}):
                hosts[r["pub"]] = int(r["hosts"])
        table.pub_hosts[signature] = hosts
        while len(table.pub_hosts) > _PUB_SIGNATURES_MAX:
            table.pub_hosts.pop(next(iter(table.pub_hosts)))
    await _ensure_pub_labels(db, table, scope, shown)
    items = [{
        "value": v,
        "label": "Unknown" if v == UNKNOWN else _publisher_label(v, table.pub_labels),
        "products": len(groups[v]),
        "installs": sum(r.installs for r in groups[v]),
        "hosts": hosts.get(v, 0),
    } for v in shown]
    return {
        "generated_at": iq.iso(table.built_at),
        "scope": iq.scope_block(scope),
        "total": len(rows),
        "distinct": len(groups),
        "items": items,
        "data_status": await iq.data_status(db, scope=scope),
    }


# ── Product page ─────────────────────────────────────────────────────

async def _product_exists(db: AsyncSession, product_key: str, scope: Optional[Sequence[str]]) -> bool:
    vis_params = await iq._visible_server_ids(db, scope)
    vis = iq._visible_servers_pred(scope)
    r = await iq._run(db, f"SELECT EXISTS (SELECT 1 FROM server_software_inventory w WHERE w.name_key = :k {vis}) AS e",
                      {"k": product_key, **vis_params})
    return bool(r[0]["e"])


async def product(db: AsyncSession, product_key: str, *, scope: Optional[Sequence[str]],
                  settings: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The product page (GET /inventory/software/{product_key}); None when the product is unknown or has no
    host in the caller's scope (the route answers both with the same 404)."""
    key = iq._clean(product_key, 160)
    if not key:
        return None
    await iq._tune(db)
    vis_params = await iq._visible_server_ids(db, scope)
    vis = iq._visible_servers_pred(scope)
    hosts, stats, _installs = await _detail_rows(db, scope, [key], vis_params)
    if not stats.get(key):
        return None
    head = (await iq._run(db, f"""
        SELECT count(*) AS installs, min(w.first_seen_at) AS first_seen, max(w.category) AS category,
               max(w.publisher_key) AS publisher_key
          FROM server_software_inventory w WHERE w.name_key = :k {vis}""", {"k": key, **vis_params}))[0]
    evidence_rows = await iq._run(db, f"""
        SELECT g.package_name, g.vendor, g.source, g.installs, g.hosts, count(*) OVER () AS _n FROM (
            SELECT w.package_name, w.vendor, min(w.source) AS source, count(*) AS installs, count(DISTINCT w.server_id) AS hosts
              FROM server_software_inventory w WHERE w.name_key = :k {vis}
             GROUP BY w.package_name, w.vendor) g
         ORDER BY g.hosts DESC, g.installs DESC, g.package_name, g.vendor NULLS LAST LIMIT :lim""",
        {"k": key, "lim": EVIDENCE_LIMIT, **vis_params})
    last = (await _last_changes(db, scope, [key], vis_params)).get(key)
    sample = (await _samples(db, scope, [key], vis_params)).get(key)
    category = head["category"] or "other"
    pub_key = head["publisher_key"] or None
    raw_label: dict[str, Optional[str]] = {}
    if pub_key and pub_key not in _known_publishers():
        got = await iq._run(db, f"""
            SELECT min(w.vendor) AS vendor FROM server_software_inventory w
             WHERE w.publisher_key = :p AND w.vendor IS NOT NULL AND btrim(w.vendor) <> '' {vis}""", {"p": pub_key, **vis_params})
        raw_label[pub_key] = got[0]["vendor"] if got else None
    ranked, unversioned = await _cpu(rank_versions, stats[key])
    evidence = []
    for r in evidence_rows:
        status, rule = _normalise_status(r["package_name"], r["vendor"], r["source"])
        evidence.append({"package_name": r["package_name"], "vendor": r["vendor"], "source": r["source"],
                         "hosts": int(r["hosts"]), "installs": int(r["installs"]), "status": status, "rule_id": rule})
    # The header's name and rule come from the same sample row as the catalog's (first by version, then title).
    name, rule_id = key, None
    if sample is not None:
        name = iq.product_display_name(key, sample["package_name"], sample["vendor"], sample["source"])
        rule_id = _normalise_status(sample["package_name"], sample["vendor"], sample["source"])[1]
    return {
        "generated_at": iq.iso(iq.utcnow()),
        "scope": iq.scope_block(scope),
        "header": {
            "product_key": key,
            "name": name,
            "publisher": _publisher_label(pub_key, raw_label),
            "publisher_key": pub_key,
            "category": category,
            "norm_status": norm_status(key, category, pub_key),
            "rule_id": rule_id,
            "hosts": hosts[key],
            "installs": int(head["installs"]),
            "version_count": len(ranked),
            "latest_version": latest_of(ranked),
            "first_seen": iq.iso(head["first_seen"]),
            "last_change": iq.iso(last),
            "mixed_schemes": len({v.scheme for v in ranked}) > 1,
        },
        "versions": [{"version": v.version, "hosts": v.hosts, "installs": v.installs, "scheme": v.scheme,
                      "rank": v.rank, "generation": v.generation} for v in ranked],
        "unversioned": ({"hosts": unversioned.hosts, "installs": unversioned.installs, "address": NO_VERSION}
                        if unversioned is not None else {"hosts": 0, "installs": 0, "address": NO_VERSION}),
        "evidence": {"items": evidence, "total": int(evidence_rows[0]["_n"]) if evidence_rows else 0,
                     "truncated": bool(evidence_rows and int(evidence_rows[0]["_n"]) > EVIDENCE_LIMIT)},
        "data_status": await iq.data_status(db, scope=scope),
    }


# ── Installations ────────────────────────────────────────────────────

_INSTALL_COLS = ("s.id AS server_id, s.display_name AS asset_name, w.package_name, w.version, w.architecture, w.source, "
                 "w.install_date, w.first_seen_at, w.updated_at")
_INSTALL_ORDER = {
    "asset_name": "lower(s.display_name)", "package_name": "lower(w.package_name)", "version": "w.version",
    "architecture": "w.architecture", "source": "w.source", "install_date": "w.install_date",
    "first_seen_at": "w.first_seen_at", "updated_at": "w.updated_at",
}


def _install_row(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_key": f"server:{r['server_id']}", "asset_id": str(r["server_id"]), "asset_name": r["asset_name"],
        "package_name": r["package_name"], "version": r["version"], "architecture": r["architecture"],
        "source": r["source"], "install_date": iq.iso(r["install_date"]), "first_seen_at": iq.iso(r["first_seen_at"]),
        "updated_at": iq.iso(r["updated_at"]),
    }


def _install_conds(product_key: str, version: Optional[str], q: Optional[str], scope: Optional[Sequence[str]],
                   p: dict[str, Any]) -> list[str]:
    p["k"] = product_key
    c = ["w.name_key = :k", iq._scope_pred("s.tags", scope)]
    if version:
        if version == NO_VERSION:
            c.append(f"(w.version IS NULL OR {versioning.BLANK_VERSION_SQL.format(c='w.version')})")
        else:
            # By equivalence key, as /assets?product_version= matches: 7.2 lists 7.2.0 too, and 'unknown' is a version
            p["ver"] = version
            c.append(f"{iq._vkey('w.version')} = {iq._vkey('CAST(:ver AS text)')}")
    if q:
        p["q"] = iq._like(q)
        c.append("(s.display_name ILIKE :q ESCAPE '\\' OR w.package_name ILIKE :q ESCAPE '\\' OR w.version ILIKE :q ESCAPE '\\')")
    return c


async def installations(db: AsyncSession, product_key: str, *, scope: Optional[Sequence[str]], version: Optional[str] = None,
                        q: Optional[str] = None, sort: str = "asset_name", order: str = "asc", page: int = 1,
                        page_size: int = iq.DEFAULT_PAGE_SIZE) -> Optional[dict[str, Any]]:
    """Installed-on rows of one product (GET /inventory/software/{product_key}/installations); None when the
    product is unknown or has no host in the caller's scope. ``version`` lists every spelling of that version (its
    equivalence key, as the Assets list matches ``product_version``); ``version='__none__'`` (``NO_VERSION``) selects the rows
    with no version, and a version spelled "unknown" is an ordinary version. One row per software row: a server with two
    titles of the product appears twice."""
    key = iq._clean(product_key, 160)
    page, page_size = iq._page_args(page, page_size)
    _check_sort(sort, order, INSTALL_SORTS)
    version = iq._clean(version, iq.MAX_TEXT)
    q = iq._clean(q)
    if not key:
        return None
    await iq._tune(db)
    p: dict[str, Any] = dict(iq._scope_params(scope))
    conds = _install_conds(key, version, q, scope, p)
    where = " AND ".join(conds)
    base = f"FROM server_software_inventory w JOIN servers s ON s.id = w.server_id WHERE {where}"
    direction = "DESC" if order == "desc" else "ASC"
    tie = "lower(s.display_name) ASC, s.id ASC, w.package_name ASC"
    if sort == "version":
        rows = await iq._run(db, f"SELECT {_INSTALL_COLS} {base} ORDER BY lower(s.display_name), s.id, w.package_name LIMIT {MAX_INSTALL_SORT_ROWS + 1}", p)
        if len(rows) > MAX_INSTALL_SORT_ROWS:
            raise iq.InvalidFilter("too many installations to order by version: filter by version or q first")
        total = len(rows)
        stats: dict[str, VersionStat] = {}
        for r in rows:
            v = r["version"]
            if not _is_blank(v) and v not in stats:
                stats[v] = VersionStat(v, 0, 0, "dpkg" if r["source"] == "dpkg" else "rpm" if r["source"] == "rpm" else "semverish")
        ranked, _u = rank_versions(list(stats.values()))
        group = {s: i for i, s in enumerate(dict.fromkeys(v.scheme for v in ranked))}
        rank = {v.version: (group[v.scheme], v.rank if v.rank is not None else 10**9, v.version) for v in ranked}

        def sort_key(r: Mapping[str, Any]):
            k = rank.get(r["version"])
            if k is None:
                return (2, 0, 0, ())                      # no version at all: last in both directions
            if k[1] >= 10**9:
                return (1, k[0], 0, iq._natural_key(k[2]))  # a version the scheme cannot order: after the ordered ones
            # ascending = oldest first inside a group; rank 1 is the newest
            return (0, k[0], -k[1] if order == "asc" else k[1], ())

        ordered = sorted(rows, key=sort_key)
        items = [_install_row(r) for r in ordered[(page - 1) * page_size: page * page_size]]
    else:
        lim = {**p, "limit": page_size, "offset": (page - 1) * page_size}
        rows = await iq._run(db, f"""
            SELECT {_INSTALL_COLS}, count(*) OVER () AS _total {base}
             ORDER BY {_INSTALL_ORDER[sort]} {direction} NULLS LAST, {tie}
             LIMIT :limit OFFSET :offset""", lim)
        if rows:
            total = int(rows[0]["_total"])
        elif page > 1:
            total = int((await iq._run(db, f"SELECT count(*) AS n {base}", p))[0]["n"])
        else:
            total = 0
        items = [_install_row(r) for r in rows]
    if total == 0 and not await _product_exists(db, key, scope):
        return None
    return {
        "generated_at": iq.iso(iq.utcnow()),
        "product_key": key,
        "version": version,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "order": order,
    }


# ── Warm-up ──────────────────────────────────────────────────────────

# The whole-estate table is the expensive one (a cold catalog or publisher request measures 505 and 526 ms at 2 M software
# rows against a 400 ms budget: the table scan plus the exact figures of the default page). The stale window already
# serves it while a new one is built, but the FIRST request after a restart and any request after more than the stale
# window waits for the whole build. This loop builds the table before it can expire, reads the default views (the
# catalog's first page, the publisher facet) on it, and only then publishes it, so a request never waits for any of it.
#
# There is no advisory lock here, unlike the other inventory loops: those do work that must happen once per appliance;
# this fills a cache that lives in the memory of ONE worker process, so every worker has to run its own copy. The cost
# is one table scan per worker per interval (about 0.15 s at 2 M rows).
WARM_ENV = "ZENPLUS_INVENTORY_CATALOG_WARMER"        # "0" switches the loop off
WARM_START_DELAY_S = 20.0
WARM_JITTER_S = 5.0                                  # workers do not all scan in the same second
WARM_INTERVAL_S = 40.0                               # shorter than _TABLE_TTL_S: the next table is in place before the old one expires
WARM_REFRESH_AFTER_S = 30.0                          # a table younger than this is left alone
WARM_IDLE_S = 1800.0                                 # nobody has asked for the catalog for this long: stop refreshing it
                                                     # (the first window starts with the process, so the first visit finds it warm)


def _publish(key: tuple, table: KeyTable) -> None:
    evict_expired()
    if key not in _tables and len(_tables) >= _TABLE_MAX:
        _tables.pop(min(_tables, key=lambda k: _tables[k].monotonic_at), None)
    _tables[key] = table


async def warm_once(session_factory: Callable[[], AsyncSession]) -> bool:
    """Replace the whole-estate table by a fresh one that already holds the default views. False when nothing was done
    (the cached table is young, or nobody has asked for the catalog for ``WARM_IDLE_S``)."""
    key = _scope_key(None)
    evict_expired()
    old = _tables.get(key)
    now = _monotonic()
    if now - _last_asked["all"] > WARM_IDLE_S:
        return False                                  # nobody has opened the catalog for a long time: let the table lapse
    if old is not None and now - old.monotonic_at < WARM_REFRESH_AFTER_S:
        return False
    async with session_factory() as db:
        table = await _load_table(db, None)
        filters = CatalogFilters()
        await catalog(db, filters, scope=None, table=table)
        await publishers(db, filters, scope=None, table=table)
    _publish(key, table)
    return True


async def catalog_warmer_loop(*, start_delay: float = WARM_START_DELAY_S, interval: float = WARM_INTERVAL_S,
                              session_factory: Optional[Callable[[], AsyncSession]] = None) -> None:
    """Background task (started with the other inventory loops): keep the whole-estate catalog table warm. Never dies;
    cancellation propagates. Returns at once when ``ZENPLUS_INVENTORY_CATALOG_WARMER=0``."""
    if os.environ.get(WARM_ENV, "1") == "0":
        return
    if session_factory is None:
        from app.core.database import AsyncSessionLocal as session_factory     # type: ignore[assignment]
    await asyncio.sleep(start_delay + random.uniform(0, WARM_JITTER_S))
    while True:
        try:
            await warm_once(session_factory)       # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed warm-up costs one slow request, never the loop
            from app.services.inventory_service import _reraise_if_cancelling
            _reraise_if_cancelling(exc)
            logger.exception("software catalog warm-up failed")
        await asyncio.sleep(interval)
