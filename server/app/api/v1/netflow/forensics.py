"""NetFlow forensics: complete raw-flow search, paging and export (NF-10, FE-04, CR-17).

Endpoints (all under the router-level ``netflow.view`` gate, every query scoped
to the caller's exporter visibility):

    GET    /forensics                         legacy list (bare array or CSV, up to 2 000 rows)
    GET    /forensics/search                  one page of records + keyset cursor + total
    GET    /forensics/export                  the complete result as streamed CSV (netflow.export,
                                              up to NETFLOW_EXPORT_MAX_ROWS)
    POST   /forensics/export-jobs             queue a background export beyond that cap
    GET    /forensics/export-jobs             the caller's jobs (progress, expiry)
    GET    /forensics/export-jobs/{id}
    GET    /forensics/export-jobs/{id}/download
    DELETE /forensics/export-jobs/{id}        cancel a running job / remove a finished one

Why a keyset cursor. The old page stopped at 500 rows, and ``ORDER BY bytes``
without a tie-breaker returns a different row set on every run, so even
OFFSET paging produced duplicates and omissions. Every search orders by the
requested column, then the flow time, then ``row_key`` (a hash of the record's
identity columns); the cursor carries the last emitted key plus how many rows
with exactly that key were already emitted (identical duplicate records hash
alike), so the next page resumes exactly after it. The cursor also pins the
window (a relative "last hour" would otherwise slide between pages) and a
``received_at`` snapshot, so rows arriving while the user pages are neither
skipped nor duplicated.

Totals. The first page also counts the complete match set under a separate
budget; when that budget runs out ClickHouse stops early
(``timeout_overflow_mode='break'``) and the total is reported as a lower bound
(``total_exact: false``), never as a guess.

Forensics shows every stored record: the volume guard (§3.3) is not applied,
so firewall create/deny events and implausible records are visible and can be
filtered with ``fw_event``. Fields the exporter did not send (``fields_present``)
are returned as null rather than as fabricated zeros.

Retention (FE-11). Raw records are kept for a limited number of days. A window
reaching further back is answered for the part that still exists, and the
response says so (``meta.window.effective_from`` + a ``raw_retention``
warning); a window entirely older than raw retention returns
``unavailable: {reason: 'raw_retention', …}`` instead of an empty result that
would read as "no traffic".
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.device_interface import DeviceInterface
from app.models.netflow_governance import NetflowExportJob
from app.models.user import User
from app.services import netflow_classify
from app.services.audit_service import write_audit_log

from .access import (
    EXPORT_PERMISSION,
    FlowVisibility,
    flow_visibility,
    require_view,
    resolve_exporter_owners,
    visibility_predicate,
)
from .common import (
    BUDGET_EXPORT,
    BUDGET_FORENSICS,
    EXPORTER_MINUTES,
    MAX_SPAN_RAW,
    RAW,
    NetflowError,
    Window,
    _interface_display_name,
    _interface_payload,
    _resolve_interface_names,
    _to_iso_utc,
    current_meta,
    get_retention,
    invalid_filter,
    resolve_window,
    run_query,
    table_ref,
    window_params,
)
from .filters import FW_EVENTS, FlowFilter, compile_filter, flow_filter, require_filter_exporters_visible
from .sql import DST, EXPORTER, NEXT_HOP, SRC, build_plan, display

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Limits ────────────────────────────────────────────────────────────────────

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return max(lo, min(hi, value))


PAGE_SIZE_DEFAULT = 500
PAGE_SIZE_MAX = 1000
LEGACY_LIMIT_MAX = 2000
# The total is counted under its own, shorter budget (CR-17): a count that
# cannot finish in time is reported as a lower bound instead of failing the page.
COUNT_BUDGET_S = 10.0
# Direct (streamed) CSV export; larger result sets go through an export job.
EXPORT_STREAM_MAX_ROWS = _env_int("NETFLOW_EXPORT_MAX_ROWS", 100_000, 1_000, 5_000_000)
EXPORT_JOB_MAX_ROWS = _env_int("NETFLOW_EXPORT_JOB_MAX_ROWS", 2_000_000, 10_000, 50_000_000)
EXPORT_JOB_RETENTION_HOURS = _env_int("NETFLOW_EXPORT_RETENTION_HOURS", 24, 1, 24 * 14)
EXPORT_JOBS_ACTIVE_PER_USER = 3
# Rows fetched per keyset chunk while exporting.
EXPORT_CHUNK_ROWS = 20_000
DEFAULT_EXPORT_DIR = "/opt/zenplus/data/netflow-exports"


_EXPORT_DIRS: dict[str, Path] = {}


def _usable_dir(path: Path, *, private: bool) -> bool:
    """Create ``path`` if needed and check the service can write it. A private
    (temp) directory must also belong to this account and not be writable by
    others, so flow data never lands in a directory another local user controls."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o750)
        st = path.stat()
    except OSError:
        return False
    if private and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        return False
    return os.access(path, os.W_OK | os.X_OK)


def export_dir() -> Path:
    """Where export jobs write: ``NETFLOW_EXPORT_DIR``, else the appliance data
    directory. When neither can be created or written by the service account
    (the data directory belongs to another account on some installs), a private
    directory under the system temp dir is used instead (files expire within a
    day anyway) and the fallback is logged once. If nothing is usable the
    configured path is returned and the job fails with a logged reason."""
    configured = os.environ.get("NETFLOW_EXPORT_DIR") or DEFAULT_EXPORT_DIR
    cached = _EXPORT_DIRS.get(configured)
    if cached is not None:
        return cached
    primary = Path(configured)
    if _usable_dir(primary, private=False):
        _EXPORT_DIRS[configured] = primary
        return primary
    fallback = Path(tempfile.gettempdir()) / f"zenplus-netflow-exports-{os.getuid()}"
    if _usable_dir(fallback, private=True):
        logger.warning("netflow export: %s is not writable by this service; export files go to %s",
                       primary, fallback)
        _EXPORT_DIRS[configured] = fallback
        return fallback
    return primary


# ── SQL building blocks ───────────────────────────────────────────────────────

# NF-18 duration validity (contract §3.4): both switched times present, in
# order, on the same clock (both absolute epoch ms or both uptime-relative) and
# at most a day apart. Invalid durations are -1 in SQL (sorts first ascending)
# and null in responses.
DURATION_VALID = (
    "(first_switched_ms > 0 AND last_switched_ms >= first_switched_ms"
    " AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)"
    " AND last_switched_ms - first_switched_ms <= 86400000)"
)
DURATION = f"if({DURATION_VALID}, toInt64(last_switched_ms) - toInt64(first_switched_ms), toInt64(-1))"

# Identity of a stored record for the stable tie-break. Two rows only share a
# row_key when every one of these columns is equal (true duplicates), and the
# cursor's duplicate counter handles those.
ROW_KEY = (
    "cityHash64(timestamp, received_at, collector_id, exporter_ip, exporter_ip6, observation_domain,"
    " flow_sequence, src_addr, src_addr6, dst_addr, dst_addr6, src_port, dst_port, protocol,"
    " bytes, packets, first_switched_ms, last_switched_ms, input_if, output_if, tcp_flags, tos,"
    " fw_event, xlate_src_port, xlate_dst_port, flow_end_reason)"
)

# Sortable columns -> SQL expression (timestamp is handled as the time key).
SORTS: dict[str, str] = {
    "timestamp": "timestamp",
    "bytes": "bytes",
    "packets": "packets",
    "src_port": "src_port",
    "dst_port": "dst_port",
    "duration": DURATION,
}
ORDERS = ("asc", "desc")


def _ms_expr(param: str) -> str:
    return f"fromUnixTimestamp64Milli(toInt64(%({param})s), 'UTC')"


# The columns of a forensics row, in SELECT order.
_SELECT: tuple[tuple[str, str], ...] = (
    ("ts_ms", "toUnixTimestamp64Milli(timestamp)"),
    ("rx_ms", "toUnixTimestamp64Milli(received_at)"),
    ("rk", ROW_KEY),
    ("sv", "0"),                                   # replaced by the sort expression
    ("exporter", display(EXPORTER)),
    ("observation_domain", "observation_domain"),
    ("export_protocol", "export_protocol"),
    ("flow_version", "flow_version"),
    ("collector_id", "collector_id"),
    ("src", display(SRC)),
    ("dst", display(DST)),
    ("src_port", "src_port"),
    ("dst_port", "dst_port"),
    ("protocol", "protocol"),
    ("ip_version", "ip_version"),
    ("tcp_flags", "tcp_flags"),
    ("tos", "tos"),
    ("input_if", "input_if"),
    ("output_if", "output_if"),
    ("packets", "packets"),
    ("bytes", "bytes"),
    ("sampling_interval", "sampling_interval"),
    ("duration_ms", DURATION),
    ("src_as", "src_as"),
    ("dst_as", "dst_as"),
    ("src_mask", "src_mask"),
    ("dst_mask", "dst_mask"),
    ("next_hop", display(NEXT_HOP)),
    ("flow_direction", "flow_direction"),
    ("app_id", "app_id"),
    ("fw_event", "fw_event"),
    ("xlate_src", "replaceRegexpOne(toString(xlate_src_addr6), '^::ffff:', '')"),
    ("xlate_dst", "replaceRegexpOne(toString(xlate_dst_addr6), '^::ffff:', '')"),
    ("xlate_src_port", "xlate_src_port"),
    ("xlate_dst_port", "xlate_dst_port"),
    ("flow_end_reason", "flow_end_reason"),
    ("fields_present", "fields_present"),
)
_COLS = tuple(name for name, _ in _SELECT)
_IDX = {name: i for i, name in enumerate(_COLS)}

# fields_present bits (contract §4.2).
BIT_TCP_FLAGS, BIT_TOS, BIT_L4_PORTS, BIT_INPUT_IF, BIT_OUTPUT_IF, BIT_FLOW_TIMES = 0, 1, 2, 3, 4, 5
BIT_AS, BIT_MASKS, BIT_NEXT_HOP, BIT_DIRECTION, BIT_APP_ID = 6, 7, 8, 9, 10
BIT_NAT, BIT_FW_EVENT, BIT_SAMPLING_INLINE, BIT_CLOCK_CLAMPED, BIT_SAMPLED_SFLOW, BIT_REVERSE = 12, 13, 14, 28, 29, 30

FW_EVENT_NAMES = {v: k for k, v in FW_EVENTS.items()}
ICMP_PROTOCOLS = frozenset({1, 58})
FLOW_DIRECTION_NAMES = {0: "ingress", 1: "egress"}
_ZERO_ADDRS = frozenset({"0.0.0.0", "::", ""})


@dataclass(frozen=True)
class Cursor:
    """Where the next page starts (opaque to clients: base64url JSON)."""

    sort: str
    order: str
    start: datetime
    end: datetime
    snapshot_ms: int
    sort_value: int | None      # None for sort=timestamp (the time key is the sort key)
    ts_ms: int
    row_key: int
    dup: int                    # rows with exactly this key already emitted
    offset: int                 # rows emitted before the next page
    fingerprint: str
    mode: str = "relative"


def filter_fingerprint(f: FlowFilter) -> str:
    """A short hash of the applied filter: a cursor only continues the search it came from."""
    payload = json.dumps({**f.canonical(), "tz": f.tz}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def encode_cursor(c: Cursor) -> str:
    body = {
        "v": 1, "s": c.sort, "o": c.order, "f": _to_iso_utc(c.start), "t": _to_iso_utc(c.end),
        "m": c.mode, "n": c.snapshot_ms, "k": [c.sort_value, c.ts_ms, c.row_key], "d": c.dup,
        "x": c.offset, "q": c.fingerprint,
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor:
    """Parse a cursor; anything malformed is a 400 (never a 500)."""
    bad = invalid_filter("The page cursor is not valid; run the search again", {"cursor": "Invalid cursor"})
    if not token or len(token) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise bad
    try:
        body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if body.get("v") != 1:
            raise ValueError("version")
        sort, order = body["s"], body["o"]
        if sort not in SORTS or order not in ORDERS:
            raise ValueError("sort")
        sv, ts, rk = body["k"]
        values = [ts, rk, body["n"], body["d"], body["x"]] + ([] if sort == "timestamp" else [sv])
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            raise ValueError("key")
        if sort == "timestamp" and sv is not None:
            raise ValueError("key")
        # The duplicate counter counts rows already emitted, so it never
        # exceeds the offset; bounds keep every value a valid UInt64/Int64
        # parameter. (Its size no longer sets how many rows a page fetches:
        # see build_page_sql.)
        if not (0 <= rk < 2 ** 64 and 0 <= body["x"] < 2 ** 53 and 1 <= body["d"] <= body["x"]
                and abs(ts) < 2 ** 53 and (sv is None or abs(sv) < 2 ** 63)):
            raise ValueError("range")
        start = datetime.fromisoformat(body["f"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(body["t"].replace("Z", "+00:00"))
        mode = body.get("m") if body.get("m") in ("relative", "fixed") else "fixed"
        return Cursor(sort=sort, order=order, start=start, end=end, snapshot_ms=int(body["n"]),
                      sort_value=sv, ts_ms=int(ts), row_key=int(rk), dup=int(body["d"]),
                      offset=int(body["x"]), fingerprint=str(body["q"])[:32], mode=mode)
    except NetflowError:
        raise
    except Exception:
        raise bad from None


@dataclass(frozen=True)
class SearchSpec:
    """One forensics query: window, snapshot, filter, order and position."""

    start: datetime
    end: datetime
    snapshot_ms: int
    filter: FlowFilter
    vis: FlowVisibility
    sort: str = "timestamp"
    order: str = "desc"
    after: Cursor | None = None
    limit: int = PAGE_SIZE_DEFAULT


KEYSET_PARTS = ("after", "equal")


def build_where(spec: SearchSpec, *, keyset: bool = True, part: str = "after") -> tuple[str, dict]:
    """``(predicate, params)``: half-open window, received_at snapshot, the
    caller's visibility and every filter key (no volume guard), plus the keyset
    position when continuing: ``part='after'`` selects the rows strictly after
    the cursor's key, ``part='equal'`` the rows with exactly that key (the
    identical duplicates the cursor's counter partly emitted)."""
    if part not in KEYSET_PARTS:
        raise ValueError(f"unknown keyset part {part!r}")
    fsql, params = compile_filter(spec.filter, RAW, spec.vis, volume_guard=False)
    params = dict(params)
    params.update({"fx_start": spec.start, "fx_end": spec.end, "fx_snap": spec.snapshot_ms})
    parts = [
        "timestamp >= %(fx_start)s AND timestamp < %(fx_end)s",
        f"received_at <= {_ms_expr('fx_snap')}",
        fsql,
    ]
    c = spec.after if keyset else None
    if c is not None:
        cmp_strict = "<" if spec.order == "desc" else ">"
        cmp_equal = "<=" if spec.order == "desc" else ">="
        params["fx_kt"] = c.ts_ms
        params["fx_kr"] = c.row_key
        ts = _ms_expr("fx_kt")
        if spec.sort != "timestamp":
            params["fx_ks"] = c.sort_value
        expr = SORTS[spec.sort]
        if part == "equal":
            parts.append(f"timestamp = {ts}")
            parts.append(f"{ROW_KEY} = %(fx_kr)s")
            if spec.sort != "timestamp":
                parts.append(f"({expr}) = %(fx_ks)s")
        else:
            time_part = (f"(timestamp {cmp_strict} {ts} OR "
                         f"(timestamp = {ts} AND {ROW_KEY} {cmp_strict} %(fx_kr)s))")
            if spec.sort == "timestamp":
                # Sargable bound first so the primary key prunes granules.
                parts.append(f"timestamp {cmp_equal} {ts}")
                parts.append(time_part)
            else:
                parts.append(f"(({expr}) {cmp_strict} %(fx_ks)s OR (({expr}) = %(fx_ks)s AND {time_part}))")
    return " AND ".join(p for p in parts if p and p != "1"), params


def build_page_sql(spec: SearchSpec, limit: int) -> tuple[str, dict]:
    """The page query: at most ``limit`` rows in order after the cursor.

    Continuing a page needs the rows after the cursor's key plus those
    identical duplicates of the key itself that were not emitted yet. Both
    are selected in ClickHouse -- the equal-key rows with ``OFFSET <dup>``,
    over the (tiny) set of records that really share the key -- so the API
    never receives the rows it skips. A crafted cursor with a huge duplicate
    counter therefore costs nothing: the old ``LIMIT page_size + dup + 1``
    let a client make one request load hundreds of thousands of rows into
    the API's memory."""
    direction = spec.order.upper()
    select = []
    for name, expr in _SELECT:
        if name == "sv":
            expr = "toInt64(0)" if spec.sort == "timestamp" else f"toInt64({SORTS[spec.sort]})"
        select.append(f"{expr} AS {name}")
    order_by = (f"timestamp {direction}, rk {direction}" if spec.sort == "timestamp"
                else f"sv {direction}, timestamp {direction}, rk {direction}")
    cols = ", ".join(select)
    where, params = build_where(spec, part="after")
    params["fx_limit"] = int(limit)
    after_sql = f"SELECT {cols} FROM {table_ref(RAW)} WHERE {where} ORDER BY {order_by} LIMIT %(fx_limit)s"
    c = spec.after
    if c is None or c.dup <= 0:
        return after_sql, params
    eq_where, eq_params = build_where(spec, part="equal")
    params.update(eq_params)
    params["fx_dup"] = int(c.dup)
    equal_sql = (f"SELECT {cols} FROM {table_ref(RAW)} WHERE {eq_where} ORDER BY {order_by} "
                 f"LIMIT %(fx_limit)s OFFSET %(fx_dup)s")
    names = ", ".join(_COLS)
    # Outside the subqueries the flow time is ts_ms (same order: millisecond column).
    outer_order = (f"ts_ms {direction}, rk {direction}" if spec.sort == "timestamp"
                   else f"sv {direction}, ts_ms {direction}, rk {direction}")
    sql = (f"SELECT {names} FROM (({equal_sql}) UNION ALL ({after_sql})) "
           f"ORDER BY {outer_order} LIMIT %(fx_limit)s")
    return sql, params


def build_count_sql(spec: SearchSpec, budget_s: float = COUNT_BUDGET_S) -> tuple[str, dict]:
    where, params = build_where(spec, keyset=False)
    sql = (
        "SELECT count() AS n, sum(bytes) AS b, sum(packets) AS p, "
        "countIf(sampling_interval > 1) AS sampled, max(sampling_interval) AS max_si, "
        "countIf(fw_event = 1) AS fw_created, countIf(fw_event = 3) AS fw_denied "
        f"FROM {table_ref(RAW)} WHERE {where} "
        f"SETTINGS max_execution_time = {max(1, int(budget_s))}, timeout_overflow_mode = 'break'"
    )
    return sql, params


def _row_key_of(spec: SearchSpec, row: tuple) -> tuple[int, int, int]:
    return (int(row[_IDX["sv"]]), int(row[_IDX["ts_ms"]]), int(row[_IDX["rk"]]))


def apply_page(spec: SearchSpec, rows: list[tuple], page_size: int) -> tuple[list[tuple], Cursor | None]:
    """Cut the page and build the next cursor (None when this was the last
    page). ``rows`` are the ordered rows after the cursor -- the duplicates
    it already emitted are skipped by the query (build_page_sql) -- up to
    ``page_size + 1`` of them."""
    c = spec.after
    remaining = rows
    page = remaining[:page_size]
    has_more = len(remaining) > page_size
    if not page or not has_more:
        return page, None
    last = _row_key_of(spec, page[-1])
    tail = 0
    for r in reversed(page):
        if _row_key_of(spec, r) != last:
            break
        tail += 1
    dup = tail
    if c is not None and tail == len(page):
        prev = (0 if c.sort_value is None else c.sort_value, c.ts_ms, c.row_key)
        if prev == last:
            dup += c.dup
    offset = (c.offset if c else 0) + len(page)
    nxt = Cursor(
        sort=spec.sort, order=spec.order, start=spec.start, end=spec.end, snapshot_ms=spec.snapshot_ms,
        sort_value=None if spec.sort == "timestamp" else last[0], ts_ms=last[1], row_key=last[2],
        dup=dup, offset=offset, fingerprint=filter_fingerprint(spec.filter),
        mode=c.mode if c else "relative",
    )
    return page, nxt


async def fetch_page(spec: SearchSpec, page_size: int, *, budget_s: float, label: str) -> tuple[list[tuple], Cursor | None]:
    sql, params = build_page_sql(spec, page_size + 1)
    res = await run_query(sql, params, budget_s=budget_s, label=label)
    return apply_page(spec, list(res.result_rows), page_size)


@dataclass
class CountResult:
    total: int
    exact: bool
    bytes: int
    packets: int
    sampled: int
    max_sampling: int | None
    # NSEL connection-created / denied events among the matches: records
    # without traffic volume that every volume total excludes (§3.3).
    fw_created: int = 0
    fw_denied: int = 0

    @property
    def fw_events(self) -> int:
        return self.fw_created + self.fw_denied


def count_is_partial(summary: dict | None, elapsed_s: float, budget_s: float) -> bool:
    """Whether a ``timeout_overflow_mode='break'`` count stopped early: fewer
    rows read than planned, or it ran to the end of its budget."""
    s = summary or {}
    try:
        read = int(s.get("read_rows") or 0)
        planned = int(s.get("total_rows_to_read") or 0)
    except (TypeError, ValueError):
        read = planned = 0
    if planned and read < planned:
        return True
    return elapsed_s >= budget_s * 0.95


async def count_matches(spec: SearchSpec, *, budget_s: float = COUNT_BUDGET_S) -> CountResult:
    sql, params = build_count_sql(spec, budget_s)
    started = asyncio.get_running_loop().time()
    res = await run_query(sql, params, budget_s=budget_s + 5, label="forensics-count")
    elapsed = asyncio.get_running_loop().time() - started
    row = tuple(res.result_rows[0]) if res.result_rows else ()
    n, b, p, sampled, max_si, created, denied = (row + (0,) * 7)[:7]
    exact = not count_is_partial(getattr(res, "summary", None), elapsed, budget_s)
    return CountResult(total=int(n or 0), exact=exact, bytes=int(b or 0), packets=int(p or 0),
                       sampled=int(sampled or 0), max_sampling=int(max_si) if n else None,
                       fw_created=int(created or 0), fw_denied=int(denied or 0))


def firewall_events_warning(counted: CountResult, f: FlowFilter) -> str | None:
    """Disclosure for a search that includes NSEL create/deny events (no
    volume): traffic totals and volume cards exclude them, so a drill-down
    lists more records than the card counted unless ``fw_event=flows``."""
    if counted.fw_events <= 0 or f.fw_event is not None:
        return None
    parts = []
    if counted.fw_created:
        parts.append(f"{counted.fw_created:,} connection-created")
    if counted.fw_denied:
        parts.append(f"{counted.fw_denied:,} denied")
    lead = "At least " if not counted.exact else ""
    return (f"{lead}{' and '.join(parts)} firewall event records (NSEL) match: they carry no traffic volume, "
            "so traffic totals and volume cards leave them out. Use Firewall event = flow records only "
            "(fw_event=flows) to list just the records those totals count")


# ── Rows ──────────────────────────────────────────────────────────────────────

def _bit(fp: int, bit: int) -> bool:
    return bool((fp >> bit) & 1)


def _present(fp: int, bit: int) -> bool:
    """A field is shown when the record says it was exported, or when the
    record predates field tracking (fields_present = 0: presence unknown)."""
    return fp == 0 or _bit(fp, bit)


def _iso_ms(ms: int | None) -> str | None:
    if ms is None or ms <= 0:
        return None
    # Integer arithmetic: ms / 1000 as a float can land a millisecond early.
    return _to_iso_utc(datetime.fromtimestamp(ms // 1000, tz=timezone.utc) + timedelta(milliseconds=ms % 1000))


def _addr_or_none(v: Any) -> str | None:
    s = str(v or "")
    return None if s in _ZERO_ADDRS else s


def row_ids(rows: list[tuple], after: Cursor | None = None) -> list[str]:
    """Stable ids for result rows (React keys, expanded-row state across
    refreshes): time + row key, plus an ordinal among identical duplicates
    that continues across a page boundary."""
    seen: dict[tuple[int, int], int] = {}
    if after is not None:
        seen[(after.ts_ms, after.row_key)] = after.dup
    out = []
    for r in rows:
        key = (int(r[_IDX["ts_ms"]]), int(r[_IDX["rk"]]))
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append(f"{key[0]}-{key[1]:x}-{n}")
    return out


def format_row(row: tuple, iface_meta: dict | None = None, rid: str | None = None) -> dict:
    """One raw record as JSON. Absent fields are null, never zero (principle 6)."""
    g = lambda name: row[_IDX[name]]  # noqa: E731
    fp = int(g("fields_present") or 0)
    tracked = fp != 0
    exporter = str(g("exporter"))
    proto = int(g("protocol"))
    src, dst = str(g("src")), str(g("dst"))
    sport, dport = int(g("src_port")), int(g("dst_port"))
    app_id = int(g("app_id") or 0)
    try:
        cls = netflow_classify.classify(proto, sport, dport, app_id if _present(fp, BIT_APP_ID) else 0,
                                        src_ip=src, dst_ip=dst)
    except Exception:   # classifier trouble must not lose the record
        cls = None
    flags_present = _present(fp, BIT_TCP_FLAGS)
    in_if = int(g("input_if") or 0)
    out_if = int(g("output_if") or 0)
    iface_meta = iface_meta or {}

    def iface(idx: int, bit: int) -> dict | None:
        if idx == 0 or not _present(fp, bit):
            return None
        return _interface_payload(exporter, idx, iface_meta.get((exporter, idx)))

    duration = int(g("duration_ms"))
    fw = int(g("fw_event") or 0)
    xsrc, xdst = _addr_or_none(g("xlate_src")), _addr_or_none(g("xlate_dst"))
    nat = None
    if (xsrc or xdst) and _present(fp, BIT_NAT):
        nat = {"src": xsrc, "src_port": int(g("xlate_src_port")) or None,
               "dst": xdst, "dst_port": int(g("xlate_dst_port")) or None}
    tos = int(g("tos"))
    direction = int(g("flow_direction"))
    # ICMP/ICMPv6 records carry type * 256 + code in dst_port (contract §3.1):
    # not ports. The row keeps the stored values and names them.
    icmp = proto in ICMP_PROTOCOLS
    return {
        "id": rid or f"{int(g('ts_ms'))}-{int(g('rk')):x}",
        "timestamp": _iso_ms(int(g("ts_ms"))),
        "received_at": _iso_ms(int(g("rx_ms"))),
        "exporter": exporter,
        "exporter_ip": exporter,                 # legacy name
        "observation_domain": int(g("observation_domain")),
        "export_protocol": str(g("export_protocol") or "") or None,
        "flow_version": int(g("flow_version")),
        "collector_id": str(g("collector_id") or "") or None,
        "ip_version": int(g("ip_version") or 4),
        "src": src,
        "dst": dst,
        "src_port": sport if _present(fp, BIT_L4_PORTS) or proto in (1, 58) else None,
        "dst_port": dport if _present(fp, BIT_L4_PORTS) or proto in (1, 58) else None,
        "protocol": proto,
        "protocol_name": netflow_classify.protocol_name(proto),
        "icmp_type": (dport >> 8) & 0xFF if icmp else None,
        "icmp_code": dport & 0xFF if icmp else None,
        "service": cls.service if cls else None,
        "service_port": cls.service_port if cls else None,
        "application": cls.application if cls else None,
        "class_direction": cls.direction if cls else None,
        "class_basis": cls.basis if cls else None,
        "tcp_flags": int(g("tcp_flags")) if flags_present and proto == 6 else None,
        "flags_state": ("legacy" if not tracked else ("exported" if _bit(fp, BIT_TCP_FLAGS) else "absent"))
        if proto == 6 else "n/a",
        "tos": tos if _present(fp, BIT_TOS) else None,
        "dscp": (tos >> 2) if _present(fp, BIT_TOS) else None,
        "input_if": in_if if _present(fp, BIT_INPUT_IF) else None,
        "output_if": out_if if _present(fp, BIT_OUTPUT_IF) else None,
        "input_snmp": in_if,                      # legacy names (full width now)
        "output_snmp": out_if,
        "input_interface": iface(in_if, BIT_INPUT_IF),
        "output_interface": iface(out_if, BIT_OUTPUT_IF),
        "packets": int(g("packets")),
        "bytes": int(g("bytes")),
        "sampling_interval": max(1, int(g("sampling_interval") or 1)),
        "duration_ms": duration if duration >= 0 and _present(fp, BIT_FLOW_TIMES) else None,
        "src_as": int(g("src_as")) if tracked and _bit(fp, BIT_AS) or (not tracked and int(g("src_as"))) else None,
        "dst_as": int(g("dst_as")) if tracked and _bit(fp, BIT_AS) or (not tracked and int(g("dst_as"))) else None,
        "src_mask": int(g("src_mask")) if _present(fp, BIT_MASKS) and (tracked or int(g("src_mask"))) else None,
        "dst_mask": int(g("dst_mask")) if _present(fp, BIT_MASKS) and (tracked or int(g("dst_mask"))) else None,
        "next_hop": _addr_or_none(g("next_hop")) if _present(fp, BIT_NEXT_HOP) else None,
        "flow_direction": FLOW_DIRECTION_NAMES.get(direction),
        "app_id": app_id if app_id and _present(fp, BIT_APP_ID) else None,
        "fw_event": FW_EVENT_NAMES.get(fw) if fw else None,
        "nat": nat,
        "flow_end_reason": int(g("flow_end_reason")) or None,
        "fields_present": fp,
        "tracked": tracked,
        "clock_clamped": _bit(fp, BIT_CLOCK_CLAMPED),
        "reverse": _bit(fp, BIT_REVERSE),
    }


async def interface_names_for(rows: Iterable[tuple], db: AsyncSession) -> dict:
    keys = []
    for r in rows:
        exporter = str(r[_IDX["exporter"]])
        for col in ("input_if", "output_if"):
            idx = int(r[_IDX[col]] or 0)
            if idx:
                keys.append((exporter, idx))
    return await _resolve_interface_names(keys, db) if keys else {}


async def all_interface_names(db: AsyncSession) -> dict[tuple[str, int], str]:
    """{(exporter, ifindex): display name} for every owned exporter (exports:
    the rows are not known in advance)."""
    owners = await resolve_exporter_owners(db)
    by_device: dict[str, list[str]] = {}
    for ip, o in owners.items():
        by_device.setdefault(o.device_id, []).append(ip)
    if not by_device:
        return {}
    rows = (await db.execute(
        select(DeviceInterface.device_id, DeviceInterface.if_index, DeviceInterface.if_name,
               DeviceInterface.if_descr, DeviceInterface.if_alias)
        .where(DeviceInterface.device_id.in_([uuid.UUID(d) for d in by_device]))
    )).all()
    out: dict[tuple[str, int], str] = {}
    for r in rows:
        name = _interface_display_name({"if_name": r.if_name, "if_descr": r.if_descr, "if_alias": r.if_alias},
                                       int(r.if_index))
        for ip in by_device.get(str(r.device_id), ()):
            out[(ip, int(r.if_index))] = name
    return out


# ── CSV ───────────────────────────────────────────────────────────────────────

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection in free text (the platform's
    alerts export convention, plus tab/CR prefixes)."""
    v = "" if value is None else str(value)
    return "'" + v if v[:1] in _FORMULA_PREFIXES else v


def tcp_flag_letters(flags: int | None) -> str:
    if flags is None:
        return ""
    return "".join(ch for bit, ch in ((32, "U"), (16, "A"), (8, "P"), (4, "R"), (2, "S"), (1, "F")) if flags & bit) or "·"


# Column name -> value from a formatted row (text columns go through csv_safe).
EXPORT_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("timestamp_utc", lambda r, n: r["timestamp"]),
    ("received_at_utc", lambda r, n: r["received_at"]),
    ("exporter", lambda r, n: r["exporter"]),
    ("observation_domain", lambda r, n: r["observation_domain"]),
    ("export_protocol", lambda r, n: r["export_protocol"]),
    ("ip_version", lambda r, n: r["ip_version"]),
    ("src", lambda r, n: r["src"]),
    # ICMP type/code are their own columns, never shown as ports.
    ("src_port", lambda r, n: None if r["icmp_type"] is not None else r["src_port"]),
    ("dst", lambda r, n: r["dst"]),
    ("dst_port", lambda r, n: None if r["icmp_type"] is not None else r["dst_port"]),
    ("protocol", lambda r, n: r["protocol"]),
    ("protocol_name", lambda r, n: csv_safe(r["protocol_name"])),
    ("icmp_type", lambda r, n: r["icmp_type"]),
    ("icmp_code", lambda r, n: r["icmp_code"]),
    ("service", lambda r, n: csv_safe(r["service"])),
    ("application", lambda r, n: csv_safe(r["application"])),
    ("tcp_flags", lambda r, n: tcp_flag_letters(r["tcp_flags"])),
    ("tcp_flags_value", lambda r, n: r["tcp_flags"]),
    ("dscp", lambda r, n: r["dscp"]),
    ("input_if", lambda r, n: r["input_if"]),
    ("input_interface", lambda r, n: csv_safe(n.get((r["exporter"], r["input_if"])) if r["input_if"] else "")),
    ("output_if", lambda r, n: r["output_if"]),
    ("output_interface", lambda r, n: csv_safe(n.get((r["exporter"], r["output_if"])) if r["output_if"] else "")),
    ("flow_direction", lambda r, n: r["flow_direction"]),
    ("bytes", lambda r, n: r["bytes"]),
    ("packets", lambda r, n: r["packets"]),
    ("sampling_interval", lambda r, n: r["sampling_interval"]),
    ("duration_ms", lambda r, n: r["duration_ms"]),
    ("src_as", lambda r, n: r["src_as"]),
    ("dst_as", lambda r, n: r["dst_as"]),
    ("src_mask", lambda r, n: r["src_mask"]),
    ("dst_mask", lambda r, n: r["dst_mask"]),
    ("next_hop", lambda r, n: r["next_hop"]),
    ("app_id", lambda r, n: r["app_id"]),
    ("fw_event", lambda r, n: r["fw_event"]),
    ("nat_src", lambda r, n: (r["nat"] or {}).get("src")),
    ("nat_src_port", lambda r, n: (r["nat"] or {}).get("src_port")),
    ("nat_dst", lambda r, n: (r["nat"] or {}).get("dst")),
    ("nat_dst_port", lambda r, n: (r["nat"] or {}).get("dst_port")),
    ("flow_end_reason", lambda r, n: r["flow_end_reason"]),
    ("fields_present", lambda r, n: r["fields_present"]),
)
EXPORT_COLUMN_NAMES = tuple(name for name, _ in EXPORT_COLUMNS)
_EXPORT_GETTERS = dict(EXPORT_COLUMNS)


def parse_columns(raw: str | None) -> tuple[str, ...]:
    """Validated export column list (all columns when absent)."""
    if not raw:
        return EXPORT_COLUMN_NAMES
    cols = tuple(dict.fromkeys(c.strip() for c in raw.split(",") if c.strip()))
    unknown = [c for c in cols if c not in _EXPORT_GETTERS]
    if unknown or not cols:
        raise invalid_filter(
            f"Unknown export column {', '.join(unknown[:3]) or '(none)'}; choose from: {', '.join(EXPORT_COLUMN_NAMES)}",
            {"columns": "Unknown column"},
        )
    return cols


def csv_header(columns: tuple[str, ...]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow(columns)
    return buf.getvalue()


def csv_lines(rows: list[dict], columns: tuple[str, ...], names: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    getters = [_EXPORT_GETTERS[c] for c in columns]
    for r in rows:
        w.writerow(["" if (v := g(r, names)) is None else v for g in getters])
    return buf.getvalue()


async def iter_export_rows(spec: SearchSpec, max_rows: int, *, chunk: int = EXPORT_CHUNK_ROWS,
                           budget_s: float = BUDGET_EXPORT) -> AsyncIterator[list[tuple]]:
    """Yield the complete ordered result in keyset chunks (at most ``max_rows``
    rows). Each chunk is its own budgeted query, resumed from the previous one
    exactly like the page cursor, under the same window and snapshot."""
    emitted = 0
    current = spec
    while emitted < max_rows:
        size = min(chunk, max_rows - emitted)
        rows, nxt = await fetch_page(current, size, budget_s=budget_s, label="forensics-export")
        if rows:
            emitted += len(rows)
            yield rows
        if nxt is None:
            return
        current = replace(current, after=nxt)


# ── Request helpers ───────────────────────────────────────────────────────────

def _validate_sort(sort: str, order: str) -> None:
    errors = {}
    if sort not in SORTS:
        errors["sort"] = f"Unknown sort '{sort[:32]}' (expected one of: {', '.join(SORTS)})"
    if order not in ORDERS:
        errors["order"] = "order must be asc or desc"
    if errors:
        raise invalid_filter("; ".join(errors.values()), errors)


def _snapshot_ms(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp() * 1000)


_SNAPSHOT_MIN_MS = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
_SNAPSHOT_BUDGET_S = 5.0


async def _visible_snapshot_ms(vis: FlowVisibility) -> int:
    """The received-at snapshot a new search pins: the newest receipt
    ClickHouse already holds for the caller's exporters, never later than now.

    The collector inserts in receipt order, in batches (up to
    NETFLOW_FLUSH_SECONDS apart), so records still in its queue carry later
    receipt times than anything stored. Pinning the search to "now" instead
    would let those records join a later page or the pinned export although
    the count did not include them. Falls back to now when the newest
    receipt cannot be read (or nothing was stored yet)."""
    now_ms = _snapshot_ms()
    pred, params = visibility_predicate(vis, param="fx_snap_vis")
    try:
        res = await run_query(
            f"SELECT toUnixTimestamp64Milli(max(last_received_at)) FROM {table_ref(EXPORTER_MINUTES)}"
            + (f" WHERE {pred}" if pred else ""),
            params, budget_s=_SNAPSHOT_BUDGET_S, label="forensics-snapshot",
        )
        newest = int(res.result_rows[0][0]) if res.result_rows and res.result_rows[0][0] is not None else 0
    except NetflowError:
        return now_ms
    except (TypeError, ValueError, IndexError):
        return now_ms
    return newest if _SNAPSHOT_MIN_MS <= newest < now_ms else now_ms


def parse_snapshot(raw: str | None, now: datetime | None = None) -> int:
    """The received-at snapshot an export is pinned to, in epoch ms.

    A search answers "records received up to ``snapshot_at``"; exporting that
    search passes the same instant back (ISO-8601 or epoch milliseconds) so
    the file holds exactly the rows the page counted, not the rows that
    arrived since. Absent means "now". A future instant is clamped to now (a
    snapshot can only narrow what was received); garbage is a 400."""
    current = _snapshot_ms(now)
    if raw is None or str(raw).strip() == "":
        return current
    text_ = str(raw).strip()
    try:
        if re.fullmatch(r"\d{1,16}", text_):
            ms = int(text_)
        else:
            dt = datetime.fromisoformat(text_.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("naive")
            ms = int(dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        raise invalid_filter("snapshot must be an ISO-8601 time with a zone or epoch milliseconds",
                             {"snapshot": "Invalid time"}) from None
    if ms < _SNAPSHOT_MIN_MS:
        raise invalid_filter("snapshot is before the year 2000", {"snapshot": "Out of range"})
    return min(ms, current)


async def _plan_raw(win: Window, f: FlowFilter):
    """Raw-retention plan for the window: effective start and the disclosure
    warnings (recorded in meta)."""
    plan = build_plan(win, f, retention=await get_retention(), rollup=None)
    plan.record()
    return plan


async def _unavailable_payload(win: Window) -> dict:
    retention = await get_retention()
    return {
        "reason": "raw_retention",
        "raw_days": retention.raw_days,
        "earliest_raw": _to_iso_utc(retention.earliest.get(RAW)),
        "message": (
            f"Raw flow records are kept for {retention.raw_days} days, so individual records for this "
            "period no longer exist. Traffic totals for it are still available from the hourly rollups "
            "on the Overview and breakdown pages."
            if retention.raw_days is not None else
            "Raw flow records for this period are no longer available."
        ),
    }


def _spec_for(win: Window, f: FlowFilter, vis: FlowVisibility, sort: str, order: str,
              snapshot_ms: int, effective_from: datetime) -> SearchSpec:
    return SearchSpec(start=max(effective_from, win.start), end=win.end, snapshot_ms=snapshot_ms,
                      filter=f, vis=vis, sort=sort, order=order)


# ── Legacy list ───────────────────────────────────────────────────────────────

LEGACY_CSV_COLUMNS = (
    "timestamp", "exporter_ip", "src", "src_port", "dst", "dst_port",
    "protocol", "protocol_name", "service", "tcp_flags", "dscp",
    "input_snmp", "output_snmp", "bytes", "packets", "duration_ms",
)


@router.get("/forensics")
async def netflow_forensics(
    limit: int = Query(default=200, ge=1, le=LEGACY_LIMIT_MAX),
    sort: str = Query(default="timestamp"),
    order: str = Query(default="desc"),
    fmt: str = Query(default="json", alias="format", pattern="^(json|csv)$"),
    win: Window = Depends(window_params(default_hours=1, max_span=MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    """Raw-flow records, first ``limit`` in order (the pre-paging API, kept for
    existing consumers). New clients use ``/forensics/search``. The CSV form
    is an export like any other: it needs ``netflow.export`` and is audited."""
    _validate_sort(sort, order)
    if fmt == "csv":
        _require_export(vis)
    require_filter_exporters_visible(f, vis)
    plan = await _plan_raw(win, f)
    rows: list[tuple] = []
    snapshot_ms = _snapshot_ms()
    more = False
    if not plan.empty:
        spec = _spec_for(win, f, vis, sort, order, snapshot_ms, plan.effective_from)
        page, nxt = await fetch_page(spec, limit, budget_s=BUDGET_FORENSICS, label="forensics-legacy")
        rows = page
        more = nxt is not None
        current_meta().set_truncated(limit, len(rows), more=more)
    names = await interface_names_for(rows, db)
    items = [format_row(r, names, rid) for r, rid in zip(rows, row_ids(rows))]
    if fmt == "csv":
        record = _query_record(win, f, sort, order, LEGACY_CSV_COLUMNS, snapshot_ms,
                               plan.effective_from if not plan.empty else win.start)
        await write_audit_log(db, actor=user, action="netflow.export", resource_type="netflow_forensics",
                              metadata={**record, "rows": len(items), "limit": limit, "truncated": more,
                                        "mode": "legacy"})
        await db.commit()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(LEGACY_CSV_COLUMNS)
        for r in items:
            out = dict(r, protocol_name=csv_safe(r["protocol_name"]), service=csv_safe(r["service"]))
            writer.writerow(["" if out.get(c) is None else out.get(c) for c in LEGACY_CSV_COLUMNS])
        fname = f"netflow-forensics-{win.start:%Y%m%dT%H%M%S}-{win.end:%Y%m%dT%H%M%S}.csv"
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    return items


# ── Search (paged) ────────────────────────────────────────────────────────────

@router.get("/forensics/search")
async def forensics_search(
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    cursor: str | None = Query(default=None, max_length=1024),
    sort: str = Query(default="timestamp"),
    order: str = Query(default="desc"),
    win: Window = Depends(window_params(default_hours=1, max_span=MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """One page of raw flow records in a stable order, with a cursor for the
    next page and (first page) the total number of matching records."""
    _validate_sort(sort, order)
    require_filter_exporters_visible(f, vis)
    meta = current_meta()
    c = decode_cursor(cursor) if cursor else None
    if c is not None:
        if c.sort != sort or c.order != order or c.fingerprint != filter_fingerprint(f):
            raise invalid_filter("The page cursor belongs to a different search; run the search again",
                                 {"cursor": "Cursor does not match this search"})
        # The cursor pins the window of the first page (a relative window
        # would otherwise slide between pages).
        win = resolve_window(from_ts=_to_iso_utc(c.start), to_ts=_to_iso_utc(c.end), tz=f.tz,
                             max_span=MAX_SPAN_RAW, record=False)
        win = replace(win, mode=c.mode)
        meta.set_window(win, force=True)
        snapshot_ms = c.snapshot_ms
    else:
        snapshot_ms = await _visible_snapshot_ms(vis)
    plan = await _plan_raw(win, f)
    base = {
        "page_size": page_size, "sort": sort, "order": order,
        "snapshot_at": _iso_ms(snapshot_ms),
        "snapshot_ms": snapshot_ms,
        "window": {"from": _to_iso_utc(win.start), "to": _to_iso_utc(win.end), "mode": win.mode, "tz": win.tz,
                   "effective_from": _to_iso_utc(plan.effective_from)},
        "offset": c.offset if c else 0,
    }
    if plan.empty:
        meta.set_truncated(page_size, 0, more=False)
        return {**base, "items": [], "has_more": False, "next_cursor": None, "total": None,
                "total_exact": False, "total_bytes": None, "total_packets": None,
                "unavailable": await _unavailable_payload(win)}

    spec = _spec_for(win, f, vis, sort, order, snapshot_ms, plan.effective_from)
    spec = replace(spec, after=c)
    page_task = fetch_page(spec, page_size, budget_s=BUDGET_FORENSICS, label="forensics-search")
    if c is None:
        (rows, nxt), counted = await asyncio.gather(page_task, count_matches(spec))
    else:
        rows, nxt = await page_task
        counted = None
    names = await interface_names_for(rows, db)
    items = [format_row(r, names, rid) for r, rid in zip(rows, row_ids(rows, c))]
    meta.set_truncated(page_size, len(items), more=nxt is not None)
    total = total_exact = None
    if counted is not None:
        if nxt is None:
            # The whole result is on this page: its size is the exact total.
            total, total_exact = len(items), True
        elif counted.exact:
            total, total_exact = max(counted.total, len(items) + 1), True
        else:
            # Stopped early: a lower bound (the page proves at least one more).
            total, total_exact = max(counted.total, len(items) + 1), False
        if counted.total:
            meta.set_sampling(counted.sampled / counted.total, counted.max_sampling)
        if not total_exact:
            meta.warn("total_estimated", f"Counting all matches exceeded {COUNT_BUDGET_S:g} s; "
                                         "the total is a lower bound")
        fw_note = firewall_events_warning(counted, f)
        if fw_note:
            meta.warn("firewall_events_included", fw_note)
    return {
        **base,
        "items": items,
        "has_more": nxt is not None,
        "next_cursor": encode_cursor(nxt) if nxt else None,
        "total": total,
        "total_exact": total_exact if total_exact is not None else False,
        "total_bytes": counted.bytes if counted else None,
        "total_packets": counted.packets if counted else None,
        "fw_events": ({"created": counted.fw_created, "denied": counted.fw_denied, "exact": counted.exact}
                      if counted else None),
        "unavailable": None,
        "export": {"stream_max_rows": EXPORT_STREAM_MAX_ROWS, "job_max_rows": EXPORT_JOB_MAX_ROWS,
                   "allowed": vis.can(EXPORT_PERMISSION)},
    }


# ── Complete export (streamed) ────────────────────────────────────────────────

def _require_export(vis: FlowVisibility) -> None:
    if not vis.can(EXPORT_PERMISSION):
        raise HTTPException(status_code=403, detail="Exporting complete result sets needs the netflow.export permission")


def _export_filename(win_start: datetime, win_end: datetime, ext: str = "csv") -> str:
    return f"netflow-forensics-{win_start:%Y%m%dT%H%M%SZ}-{win_end:%Y%m%dT%H%M%SZ}.{ext}"


def _query_record(win: Window, f: FlowFilter, sort: str, order: str, columns: tuple[str, ...],
                  snapshot_ms: int, effective_from: datetime) -> dict:
    """What an export covers, for the audit log and the export job."""
    return {
        "filter": f.canonical(),
        "tz": f.tz,
        "window": {"from": _to_iso_utc(win.start), "to": _to_iso_utc(win.end), "mode": win.mode, "tz": win.tz,
                   "effective_from": _to_iso_utc(max(effective_from, win.start))},
        "sort": sort,
        "order": order,
        "columns": list(columns),
        "snapshot_at": _iso_ms(snapshot_ms),
        "snapshot_ms": snapshot_ms,
    }


@router.get("/forensics/export")
async def forensics_export(
    sort: str = Query(default="timestamp"),
    order: str = Query(default="desc"),
    columns: str | None = Query(default=None, max_length=2000),
    snapshot: str | None = Query(default=None, max_length=64),
    win: Window = Depends(window_params(default_hours=1, max_span=MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    """Every matching record as CSV, streamed (``netflow.export``). Up to
    NETFLOW_EXPORT_MAX_ROWS rows; a larger result is refused with 413 and
    should be exported with an export job. ``snapshot`` (the search's
    ``snapshot_at``) pins the export to the records the search counted."""
    _require_export(vis)
    _validate_sort(sort, order)
    cols = parse_columns(columns)
    require_filter_exporters_visible(f, vis)
    snapshot_ms = parse_snapshot(snapshot)
    plan = await _plan_raw(win, f)
    if plan.empty:
        raise HTTPException(status_code=409, detail=(await _unavailable_payload(win))["message"])
    spec = _spec_for(win, f, vis, sort, order, snapshot_ms, plan.effective_from)
    counted = await count_matches(spec, budget_s=30.0)
    if not counted.exact or counted.total > EXPORT_STREAM_MAX_ROWS:
        size = f"{counted.total:,}" + ("" if counted.exact else " or more")
        raise HTTPException(
            status_code=413,
            detail=(f"The search matches {size} records; a direct export is limited to "
                    f"{EXPORT_STREAM_MAX_ROWS:,}. Create an export job instead."),
        )
    names = await all_interface_names(db)
    record = _query_record(win, f, sort, order, cols, snapshot_ms, plan.effective_from)
    await write_audit_log(db, actor=user, action="netflow.export", resource_type="netflow_forensics",
                          metadata={**record, "rows": counted.total, "mode": "stream"})
    await db.commit()
    total = counted.total

    async def body() -> AsyncIterator[bytes]:
        yield csv_header(cols).encode()
        written = 0
        async for chunk in iter_export_rows(spec, total):
            items = [format_row(r) for r in chunk]
            written += len(items)
            yield csv_lines(items, cols, names).encode()

    return StreamingResponse(
        body(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_export_filename(spec.start, spec.end)}"',
            "X-Total-Count": str(total),
            "X-Exported-Rows": str(total),
            "X-Truncated": "false",
            "Cache-Control": "no-store",
        },
    )


# ── Export jobs ───────────────────────────────────────────────────────────────

def job_payload(job: NetflowExportJob, username: str | None = None) -> dict:
    q = job.query or {}
    total = q.get("total")
    progress = None
    if job.status == "done":
        progress = 100.0
    elif isinstance(total, int) and total > 0 and job.row_count is not None:
        progress = round(min(99.9, 100.0 * job.row_count / total), 1)
    now = datetime.now(timezone.utc)
    ready = job.status == "done" and bool(job.file_path) and (job.expires_at is None or job.expires_at > now)
    return {
        "id": str(job.id),
        "status": job.status,
        "requested_by": str(job.requested_by) if job.requested_by else None,
        "requested_by_username": username,
        "created_at": _to_iso_utc(job.created_at),
        "started_at": _to_iso_utc(job.started_at),
        "finished_at": _to_iso_utc(job.finished_at),
        "expires_at": _to_iso_utc(job.expires_at),
        "row_count": job.row_count,
        "byte_size": job.byte_size,
        "total": total,
        "total_exact": q.get("total_exact"),
        "progress_pct": progress,
        "error": job.error,
        "download_ready": ready,
        "filename": q.get("filename"),
        "query": {k: q.get(k) for k in ("filter", "tz", "window", "sort", "order", "columns", "snapshot_at")},
    }


async def _load_job(db: AsyncSession, job_id: uuid.UUID, vis: FlowVisibility) -> NetflowExportJob:
    job = (await db.execute(select(NetflowExportJob).where(NetflowExportJob.id == job_id))).scalar_one_or_none()
    # Someone else's job is a 404 (admins see all): ids cannot be probed.
    if job is None or (str(job.requested_by) != vis.user_id and not vis.can("system.admin")):
        raise HTTPException(status_code=404, detail="Export job not found")
    return job


async def _usernames(db: AsyncSession, ids: Iterable) -> dict[str, str]:
    ids = [i for i in {str(x) for x in ids if x}]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.username).where(User.id.in_([uuid.UUID(i) for i in ids])))).all()
    return {str(r.id): r.username for r in rows}


@router.post("/forensics/export-jobs")
async def create_export_job(
    sort: str = Query(default="timestamp"),
    order: str = Query(default="desc"),
    columns: str | None = Query(default=None, max_length=2000),
    snapshot: str | None = Query(default=None, max_length=64),
    win: Window = Depends(window_params(default_hours=1, max_span=MAX_SPAN_RAW)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    """Queue a complete export of the search (``netflow.export``). The window
    is pinned to its current bounds and a received-at snapshot -- the
    search's ``snapshot_at`` when given -- so the file holds exactly what the
    search matched."""
    _require_export(vis)
    _validate_sort(sort, order)
    cols = parse_columns(columns)
    require_filter_exporters_visible(f, vis)
    snapshot_ms = parse_snapshot(snapshot)
    active = (await db.execute(text(
        "SELECT count(*) FROM netflow_export_jobs WHERE requested_by = :u AND status IN ('queued', 'running')"
    ), {"u": user.id})).scalar() or 0
    if active >= EXPORT_JOBS_ACTIVE_PER_USER:
        raise HTTPException(status_code=409, detail=(
            f"You already have {active} export jobs queued or running; wait for one to finish or cancel it"))
    plan = await _plan_raw(win, f)
    if plan.empty:
        raise HTTPException(status_code=409, detail=(await _unavailable_payload(win))["message"])
    spec = _spec_for(win, f, vis, sort, order, snapshot_ms, plan.effective_from)
    counted = await count_matches(spec, budget_s=30.0)
    if counted.total > EXPORT_JOB_MAX_ROWS:
        raise HTTPException(status_code=413, detail=(
            f"The search matches {counted.total:,}{'' if counted.exact else ' or more'} records; an export "
            f"is limited to {EXPORT_JOB_MAX_ROWS:,}. Narrow the time range or add filters."))
    record = _query_record(win, f, sort, order, cols, snapshot_ms, plan.effective_from)
    record.update({
        "total": counted.total, "total_exact": counted.exact, "attempts": 0,
        "filename": _export_filename(spec.start, spec.end, "csv.gz"),
    })
    job = NetflowExportJob(requested_by=user.id, status="queued", query=record, row_count=0)
    db.add(job)
    await db.flush()
    await write_audit_log(db, actor=user, action="netflow.export_job.create", resource_type="netflow_export_job",
                          resource_id=str(job.id), metadata={**record, "mode": "job"})
    await db.commit()
    await db.refresh(job)
    return job_payload(job, user.username)


@router.get("/forensics/export-jobs")
async def list_export_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """The caller's export jobs, newest first, with the export limits."""
    q = select(NetflowExportJob).order_by(NetflowExportJob.created_at.desc()).limit(limit)
    if not vis.can("system.admin"):
        q = q.where(NetflowExportJob.requested_by == (uuid.UUID(vis.user_id) if vis.user_id else None))
    jobs = (await db.execute(q)).scalars().all()
    names = await _usernames(db, (j.requested_by for j in jobs))
    return {
        "items": [job_payload(j, names.get(str(j.requested_by))) for j in jobs],
        "limits": {"stream_max_rows": EXPORT_STREAM_MAX_ROWS, "job_max_rows": EXPORT_JOB_MAX_ROWS,
                   "retention_hours": EXPORT_JOB_RETENTION_HOURS, "active_per_user": EXPORT_JOBS_ACTIVE_PER_USER},
        "allowed": vis.can(EXPORT_PERMISSION),
    }


@router.get("/forensics/export-jobs/{job_id}")
async def get_export_job(
    job_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(db, job_id, vis)
    names = await _usernames(db, [job.requested_by])
    return job_payload(job, names.get(str(job.requested_by)))


@router.get("/forensics/export-jobs/{job_id}/download")
async def download_export_job(
    job_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    job = await _load_job(db, job_id, vis)
    now = datetime.now(timezone.utc)
    if job.status != "done" or not job.file_path:
        raise HTTPException(status_code=409, detail=f"The export is {job.status}; there is no file to download")
    if job.expires_at is not None and job.expires_at <= now:
        raise HTTPException(status_code=410, detail="The export file has expired; run the export again")
    resolved = _job_file(job)
    if resolved is None:
        raise HTTPException(status_code=410, detail="The export file is no longer available")
    await write_audit_log(db, actor=user, action="netflow.export_job.download", resource_type="netflow_export_job",
                          resource_id=str(job.id), metadata={"rows": job.row_count, "bytes": job.byte_size})
    await db.commit()
    return FileResponse(
        resolved,
        media_type="application/gzip",
        filename=(job.query or {}).get("filename") or f"netflow-forensics-{job.id}.csv.gz",
        headers={"X-Exported-Rows": str(job.row_count or 0), "Cache-Control": "no-store"},
    )


@router.delete("/forensics/export-jobs/{job_id}")
async def delete_export_job(
    job_id: uuid.UUID,
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a queued or running job; remove a finished one and its file."""
    job = await _load_job(db, job_id, vis)
    previous = job.status
    if job.status in ("queued", "running"):
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        job.error = "Cancelled by the user"
        action, removed = "netflow.export_job.cancel", False
    else:
        if job.file_path:
            _remove_file(job.file_path, job.id)
        await db.delete(job)
        action, removed = "netflow.export_job.delete", True
    await write_audit_log(db, actor=user, action=action, resource_type="netflow_export_job",
                          resource_id=str(job_id), metadata={"previous_status": previous})
    await db.commit()
    return {"id": str(job_id), "status": "deleted" if removed else "cancelled"}


def _is_export_file(path: Path, job_id: uuid.UUID) -> bool:
    """Only a job's own file is ever served or deleted: ``<job id>.csv.gz``
    (the path is written by the export runner, which may be another worker
    of this appliance, so its directory is not assumed)."""
    return path.is_absolute() and path.name == f"{job_id}.csv.gz"


def _job_file(job: NetflowExportJob) -> Path | None:
    if not job.file_path:
        return None
    try:
        path = Path(job.file_path).resolve()
    except (OSError, RuntimeError):
        return None
    if not _is_export_file(path, job.id) or not path.is_file():
        return None
    return path


def _remove_file(file_path: str, job_id: uuid.UUID) -> None:
    try:
        p = Path(file_path).resolve()
        if not _is_export_file(p, job_id):
            logger.warning("netflow export: refusing to remove unexpected path %s", file_path)
            return
        p.unlink(missing_ok=True)
        Path(str(p) + ".part").unlink(missing_ok=True)
    except OSError:
        logger.warning("netflow export: could not remove %s", file_path, exc_info=True)


__all__ = [
    "router", "Cursor", "SearchSpec", "SORTS", "EXPORT_COLUMN_NAMES", "EXPORT_STREAM_MAX_ROWS",
    "EXPORT_JOB_MAX_ROWS", "EXPORT_JOB_RETENTION_HOURS", "encode_cursor", "decode_cursor",
    "filter_fingerprint", "row_ids", "build_where", "build_page_sql", "build_count_sql", "apply_page", "fetch_page",
    "count_matches", "count_is_partial", "format_row", "csv_safe", "csv_header", "csv_lines",
    "iter_export_rows", "all_interface_names", "export_dir", "job_payload", "parse_columns",
]
