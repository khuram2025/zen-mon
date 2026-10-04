"""Shared NetFlow API helpers.

The contract-level pieces every endpoint builds on (docs/netflow-review-contract.md):

* errors (§9.2): ``NetflowError`` carries the safe ``{detail, error_id, code}`` body;
* time (§7): ``Window`` / ``resolve_window`` / ``window_params``;
* query execution (§9.1): ``run_query`` with a ClickHouse budget and safe error mapping;
* response metadata (§9.3): the per-request ``QueryMeta``, the ``envelope=1`` wrapper and
  ``NetflowRoute``, the route class that renders errors and envelopes for every
  /netflow route;
* retention: table TTLs and earliest data, cached (``get_retention``), and the
  NetFlow v5 sampling repair state (``get_sampling_repair``, X-02 disclosure).

Below those sit the older helpers the endpoint modules still use: protocol, port and
application labels, host enrichment (reverse DNS, IP groups, GeoIP), exporter and
interface name resolution and DSCP labels. Filter compilation lives in ``filters``,
address expressions and the raw/rollup source planner in ``sql``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import json
import logging
import math
import re
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import available_timezones

from fastapi import HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as _database
from app.services import geoip, netflow_groups
from app.models.device_interface import DeviceInterface

from .access import canonical_ip, resolve_exporter_owners

logger = logging.getLogger(__name__)


# ── Tables ────────────────────────────────────────────────────────────────────
# Every table name is qualified through table_ref() so tests can point the
# query builders at a scratch database (monkeypatch CH_DB) without rewriting SQL.
CH_DB = "zenplus"

RAW = "flow_records"
CONVERSATIONS_1H = "flow_conversations_1h"
QOS_1H = "flow_qos_1h"
INTERFACE_5M = "flow_interface_5m"
EXPORTER_MINUTES = "flow_exporter_minutes"
TRAFFIC_5M = "flow_traffic_5m"


def table_ref(name: str) -> str:
    """``<db>.<table>`` for a bare table name (qualified names pass through)."""
    return name if "." in name else f"{CH_DB}.{name}"


# ── Errors (§9.2) ─────────────────────────────────────────────────────────────

ERROR_CODES = frozenset({"query_failed", "query_timeout", "invalid_filter", "invalid_window", "unavailable"})

# Query-string keys that describe the time window; a validation failure on one
# of them is reported as invalid_window rather than invalid_filter.
WINDOW_KEYS = frozenset({"hours", "minutes", "from", "to", "tz"})


def new_error_id() -> str:
    """Correlation id shown to the user and written to the server log."""
    return "nf-" + secrets.token_hex(4)


class NetflowError(HTTPException):
    """An HTTPException whose body is the NetFlow error shape:
    ``{"detail": str, "error_id": str | None, "code": str, "fields"?: {key: msg}}``.

    ``detail`` is always a plain string (a failure's includes "(ref nf-…)"), so
    generic clients that only read ``detail`` keep working. Outside a NetflowRoute
    (for example a report section calling run_query) FastAPI renders just
    ``{"detail": …}``, which is still free of database text.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str,
        error_id: str | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown NetFlow error code {code!r}")
        detail = f"{message} (ref {error_id})" if error_id else message
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.error_id = error_id
        self.fields = dict(fields) if fields else None

    def body(self) -> dict:
        out: dict[str, Any] = {"detail": self.detail, "error_id": self.error_id, "code": self.code}
        if self.fields:
            out["fields"] = self.fields
        return out


def invalid_filter(message: str, fields: dict[str, str] | None = None) -> NetflowError:
    return NetflowError(400, message, code="invalid_filter", fields=fields)


def invalid_window(message: str, key: str | None = None) -> NetflowError:
    return NetflowError(400, message, code="invalid_window", fields={key: message} if key else None)


def _validation_error(exc: RequestValidationError) -> NetflowError:
    """FastAPI's 422 (a list of objects) as a 400 with a readable string.

    Rendering FastAPI's array ``detail`` verbatim crashed the Forensics page;
    every NetFlow client can rely on ``detail`` being a string instead.
    """
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("query", "path", "body", "header")]
        key = ".".join(loc) or "request"
        fields.setdefault(key, str(err.get("msg") or "Invalid value"))
    message = "; ".join(f"Invalid value for {k}: {m}" for k, m in fields.items()) or "Invalid request"
    code = "invalid_window" if fields and set(fields) <= WINDOW_KEYS else "invalid_filter"
    return NetflowError(400, message, code=code, fields=fields)


# ── Time (§7) ─────────────────────────────────────────────────────────────────

MAX_RELATIVE_HOURS = 2232                  # 93 days
MAX_SPAN_ROLLUP = timedelta(days=93)       # endpoints that can read the rollups
MAX_SPAN_RAW = timedelta(days=31)          # raw-only (forensics search, ad-hoc anomaly run)
FUTURE_TOLERANCE = timedelta(seconds=60)   # a `to` beyond server now + this is clamped
MIN_YEAR = 2000


def now_utc() -> datetime:
    """The appliance clock, whole seconds (ClickHouse binds datetimes at second
    precision, so bounds with microseconds would not stay half-open)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


@lru_cache(maxsize=1)
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones()) | {"UTC"}


def validate_tz(tz: str | None, key: str = "tz") -> str:
    """An IANA zone name (default UTC); 400 for anything zoneinfo does not know."""
    if tz is None or tz == "":
        return "UTC"
    if tz not in _timezones():
        raise invalid_window(f"Unknown time zone '{tz[:64]}'", key)
    return tz


@dataclass(frozen=True)
class Window:
    """A resolved query window: half-open ``[start, end)`` in aware UTC."""

    start: datetime
    end: datetime
    mode: str        # 'relative' | 'fixed'
    tz: str = "UTC"  # IANA zone for hour/day-of-week/day buckets

    @property
    def seconds(self) -> int:
        return max(1, int((self.end - self.start).total_seconds()))

    def shifted(self, delta: timedelta) -> "Window":
        """The same-length window moved by ``delta`` (negative = earlier)."""
        return replace(self, start=self.start + delta, end=self.end + delta)

    def as_meta(self) -> dict:
        return {"from": _to_iso_utc(self.start), "to": _to_iso_utc(self.end), "mode": self.mode, "tz": self.tz}


def _parse_bound(value: str, key: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise invalid_window(
            f"Invalid {key} timestamp '{value[:64]}'; use ISO 8601, e.g. 2026-09-01T00:00:00Z", key
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _span_label(span: timedelta) -> str:
    days = span.total_seconds() / 86400
    return f"{days:.0f} days" if days >= 2 else f"{span.total_seconds() / 3600:.0f} hours"


def resolve_window(
    hours: int | None = None,
    minutes: int | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    tz: str | None = None,
    *,
    max_span: timedelta,
    now: datetime | None = None,
    default_hours: int = 24,
    record: bool = True,
) -> Window:
    """Resolve and validate a query window (contract §7).

    * ``from``/``to`` come together or not at all; when present ``hours`` and
      ``minutes`` are ignored (the UI may still send a rounded ``hours``).
    * Years outside [2000, next year] are rejected before they can reach
      ClickHouse (``0001-01-01`` used to surface as a CANNOT_PARSE_DATETIME 500).
    * ``to`` is clamped to server now + 60 s; explicit bounds are otherwise
      never shifted. Bounds are truncated to whole seconds.
    * A relative window is ``minutes`` (wins) or ``hours`` (1..2232) ending at
      server now.
    * Any span longer than ``max_span`` is a 400 naming the limit.

    The first window resolved during a request is recorded as the response's
    ``meta.window`` unless ``record`` is false.
    """
    now = (now or now_utc()).replace(microsecond=0)
    tz_name = validate_tz(tz)
    if (from_ts is None or from_ts == "") != (to_ts is None or to_ts == ""):
        raise invalid_window("A custom range needs both from and to", "from" if not from_ts else "to")
    if from_ts:
        start = _parse_bound(from_ts, "from").replace(microsecond=0)
        end = _parse_bound(to_ts, "to").replace(microsecond=0)
        for key, dt in (("from", start), ("to", end)):
            if not MIN_YEAR <= dt.year <= now.year + 1:
                raise invalid_window(f"{key} must be between {MIN_YEAR} and {now.year + 1}", key)
        if end <= start:
            raise invalid_window("The range end must be after its start", "to")
        end = min(end, now + FUTURE_TOLERANCE)
        if end <= start:
            raise invalid_window("The range starts in the future", "from")
        mode = "fixed"
        key = "from"
    else:
        if minutes is not None:
            limit = int(max_span.total_seconds() // 60)
            if not 1 <= minutes <= limit:
                raise invalid_window(f"minutes must be between 1 and {limit}", "minutes")
            span = timedelta(minutes=minutes)
            key = "minutes"
        else:
            h = default_hours if hours is None else hours
            if not 1 <= h <= MAX_RELATIVE_HOURS:
                raise invalid_window(f"hours must be between 1 and {MAX_RELATIVE_HOURS}", "hours")
            span = timedelta(hours=h)
            key = "hours"
        start, end, mode = now - span, now, "relative"
    if end - start > max_span:
        raise invalid_window(
            f"This view covers at most {_span_label(max_span)} per query "
            f"(requested {_span_label(end - start)}); choose a shorter range",
            key,
        )
    window = Window(start=start, end=end, mode=mode, tz=tz_name)
    if record:
        current_meta().set_window(window)
    return window


def window_params(default_hours: int = 24, max_span: timedelta = MAX_SPAN_ROLLUP) -> Callable[..., Window]:
    """FastAPI dependency factory: ``win: Window = Depends(window_params(24))``.

    ``hours``/``minutes`` carry no FastAPI bounds on purpose: they are ignored
    when ``from``/``to`` are present (the UI sends a rounded ``hours`` with
    custom ranges, and a 20-minute custom range rounds to 0), and validated by
    resolve_window when they are used.
    """

    def dependency(
        hours: int | None = Query(default=None, description=f"Relative window, 1..{MAX_RELATIVE_HOURS} (default {default_hours})"),
        minutes: int | None = Query(default=None, description="Relative window in minutes (wins over hours)"),
        from_ts: str | None = Query(default=None, alias="from", description="Custom range start (ISO 8601)"),
        to_ts: str | None = Query(default=None, alias="to", description="Custom range end, exclusive (ISO 8601)"),
        tz: str | None = Query(default=None, description="IANA time zone for hour/day buckets (default UTC)"),
    ) -> Window:
        return resolve_window(hours, minutes, from_ts, to_ts, tz, max_span=max_span, default_hours=default_hours)

    return dependency


def _to_iso_utc(value) -> str | None:
    """Format a datetime as RFC3339 UTC (with `Z` suffix) so JS clients don't parse it as local."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.startswith("1970-01-01"):
            return None
        return value if value.endswith("Z") or "+" in value else value + "Z"
    if isinstance(value, datetime):
        if value.year < 2000:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    return str(value)


# ── Per-request response metadata (§9.3) ──────────────────────────────────────

@dataclass
class QueryMeta:
    """What a NetFlow response knows about how it was produced.

    One instance per request (set by NetflowRoute); helpers write into it —
    resolve_window the window, the flow_filter dependency the filters, the
    source planner the source and effective bounds, compile_filter the
    visibility scope — and endpoints add truncation, sampling and warnings.
    It becomes ``meta`` when the client asks for ``envelope=1``.
    """

    route: str = ""
    query: Any = None                        # the request's query parameters
    window: Window | None = None
    source: str | None = None
    tables: set[str] = field(default_factory=set)   # flow tables that served the answer
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    granularity_s: int | None = None
    restricted: bool = False
    filter: Any = None                       # the request's FlowFilter
    dropped: set[str] = field(default_factory=set)
    truncated: dict | None = None
    sampling: dict | None = None
    warnings: list[dict] = field(default_factory=list)
    retention_limited: bool = False
    # The effective bounds already account for retention (a source plan, or
    # an endpoint that measured them); otherwise build_meta infers the cut.
    retention_checked: bool = False
    started: float = field(default_factory=time.monotonic)
    # ClickHouse gave no answer at all within a budget during this request
    # (NF-31): the request's remaining flow queries fail fast instead of each
    # waiting out its own budget.
    ch_unanswered: bool = False

    def query_value(self, key: str) -> str | None:
        """A query parameter of the current request (None outside a request)."""
        return self.query.get(key) if self.query is not None else None

    def set_window(self, window: Window, *, force: bool = False) -> None:
        if force or self.window is None:
            self.window = window

    def set_source(
        self,
        source: str,
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
        granularity_s: int | None = None,
        *,
        force: bool = False,
        retention_checked: bool = False,
    ) -> None:
        """Record the data source; the first one recorded wins (an endpoint's
        primary query plans first) unless ``force``. ``retention_checked``:
        the bounds given already exclude what retention removed."""
        if self.source is not None and not force:
            return
        self.source = source
        self.effective_from = effective_from
        self.effective_to = effective_to
        self.granularity_s = granularity_s
        self.retention_checked = retention_checked

    def warn(self, code: str, message: str) -> None:
        if not any(w["code"] == code for w in self.warnings):
            self.warnings.append({"code": code, "message": message})

    def set_truncated(self, limit: int, returned: int, more: bool | None = None) -> None:
        self.truncated = {"limit": int(limit), "returned": int(returned),
                          "more": bool(returned >= limit) if more is None else bool(more)}

    def set_sampling(self, sampled_share: float | None, max_interval: int | None) -> None:
        self.sampling = {"sampled_share": sampled_share, "max_interval": max_interval}


_META: contextvars.ContextVar[QueryMeta | None] = contextvars.ContextVar("netflow_meta", default=None)


def current_meta() -> QueryMeta:
    """The request's QueryMeta. Outside a NetflowRoute request (tests, report
    sections, background loops) a throwaway instance absorbs the writes."""
    meta = _META.get()
    return meta if meta is not None else QueryMeta()


async def build_meta(meta: QueryMeta) -> dict:
    """The ``meta`` object of an ``envelope=1`` response (§9.3)."""
    retention = await get_retention()
    now = now_utc()
    window = None
    source = meta.source
    limited = meta.retention_limited
    warnings = list(meta.warnings)
    if meta.window is not None:
        w = meta.window
        source = source or "raw"
        effective_from = meta.effective_from
        if not limited and not meta.retention_checked:
            # A source planner already reported its retention cuts; for the
            # endpoints that pick a table themselves, infer them here.
            code, label, days = {
                "raw": ("raw_retention", "Raw flow records", retention.raw_days),
                "hybrid": ("rollup_retention", "Hourly rollups", retention.rollup_days),
                "rollup": ("rollup_retention", "Hourly rollups", retention.rollup_days),
                "interface_rollup": ("interface_retention", "Interface rollups", retention.interface_days),
            }.get(source, (None, None, None))
            horizon = now - timedelta(days=days) if days is not None else None
            limited = horizon is not None and w.start < horizon
            if limited:
                # Nothing older than the table's retention can be in the answer.
                effective_from = min(max(effective_from or w.start, horizon), w.end)
                warnings.append({"code": code,
                                 "message": f"{label} are kept for {days} days; "
                                            "the part of the range before that is not included"})
        window = {
            **w.as_meta(),
            "effective_from": _to_iso_utc(effective_from or w.start),
            "effective_to": _to_iso_utc(meta.effective_to or w.end),
            "source": source,
            "granularity_s": meta.granularity_s if meta.granularity_s is not None else (1 if source == "raw" else None),
        }
    filters: dict = {}
    if meta.filter is not None:
        applied = meta.filter.without(*meta.dropped) if meta.dropped else meta.filter
        filters = applied.canonical()
    if not retention.known:
        warnings.append({"code": "retention_unknown", "message": "Data retention could not be read"})
    if meta.window is not None and meta.tables & SAMPLING_REPAIRED_TABLES:
        warnings.extend(sampling_repair_warnings(await get_sampling_repair(), meta.window))
    return {
        "window": window,
        "server_time": _to_iso_utc(now),
        "query_ms": int((time.monotonic() - meta.started) * 1000),
        "restricted": bool(meta.restricted),
        "filters": filters,
        "retention": {
            "raw_days": retention.raw_days,
            "rollup_days": retention.rollup_days,
            "interface_days": retention.interface_days,
            "limited": bool(limited),
        },
        "truncated": meta.truncated,
        "sampling": meta.sampling,
        "warnings": warnings,
    }


def wants_envelope(request: Request) -> bool:
    return request.query_params.get("envelope", "").lower() in ("1", "true", "yes")


async def envelope(data: Any, meta: QueryMeta | None = None) -> dict:
    """``{"data": data, "meta": {...}}`` for callers that build the response themselves."""
    return {"data": data, "meta": await build_meta(meta or current_meta())}


_HOP_HEADERS = {"content-length", "content-type"}


class NetflowRoute(APIRoute):
    """Route class for every /netflow route (installed by the package ``__init__``).

    * gives each request its own QueryMeta;
    * renders NetflowError as the §9.2 body, and FastAPI's request-validation
      422 as a 400 with a string ``detail``;
    * turns any other unexpected exception into a 500 that names only an
      error id — ClickHouse, SQL or driver text never reaches the client;
    * wraps a JSON result in ``{"data", "meta"}`` when ``envelope=1`` is asked
      for, leaving the legacy shape untouched otherwise.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()
        route_path = self.path

        async def netflow_route_handler(request: Request) -> Response:
            meta = QueryMeta(route=route_path, query=request.query_params)
            token = _META.set(meta)
            try:
                try:
                    response = await handler(request)
                except NetflowError as exc:
                    return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)
                except RequestValidationError as exc:
                    err = _validation_error(exc)
                    return JSONResponse(err.body(), status_code=err.status_code)
                except StarletteHTTPException:
                    raise
                except Exception:
                    error_id = new_error_id()
                    logger.exception("netflow request failed [%s] %s %s", error_id, request.method, route_path)
                    err = NetflowError(500, "The NetFlow request failed", code="query_failed", error_id=error_id)
                    return JSONResponse(err.body(), status_code=500)
                if wants_envelope(request) and response.media_type == "application/json" and 200 <= response.status_code < 300:
                    response = await _envelope_response(response, meta)
                return response
            finally:
                _META.reset(token)

        return netflow_route_handler


async def _envelope_response(response: Response, meta: QueryMeta) -> Response:
    data = json.loads(response.body) if response.body else None
    headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP_HEADERS}
    return JSONResponse(
        await envelope(data, meta),
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )


# ── Query execution (§9.1) ────────────────────────────────────────────────────

# NetFlow's own thread-local ClickHouse clients (NF-31). The shared factory
# (app.core.database.get_ch_client) waits up to 300 s for any answer --
# including the version query a new client sends when it is created -- so on
# a server that accepts connections and never answers, each new worker thread
# stalled for five minutes. These clients connect and initialise with short
# timeouts; every query then sets its own read timeout from its budget
# (``_read_timeout``), so no worker thread outlives the request that used it.
CH_CONNECT_TIMEOUT_S = 10
CH_INIT_TIMEOUT_S = 15
_nf_local = threading.local()


def get_ch_client():
    """Thread-local NetFlow ClickHouse client (one session per worker thread,
    like the shared factory; tests replace this function)."""
    client = getattr(_nf_local, "client", None)
    if client is None:
        import clickhouse_connect

        cfg = _database.settings
        client = clickhouse_connect.get_client(
            host=cfg.CLICKHOUSE_HOST, port=cfg.CLICKHOUSE_HTTP_PORT, database=cfg.CLICKHOUSE_DB,
            username=cfg.CLICKHOUSE_USER, password=cfg.CLICKHOUSE_PASSWORD,
            connect_timeout=CH_CONNECT_TIMEOUT_S, send_receive_timeout=CH_INIT_TIMEOUT_S,
        )
        _nf_local.client = client
    return client

BUDGET_INTERACTIVE = 20.0
BUDGET_FORENSICS = 30.0
BUDGET_EXPORT = 300.0
BUDGET_BACKGROUND = 120.0
# ClickHouse enforces max_execution_time itself; this grace covers the HTTP
# round trip before the API gives up on a query the server did not stop.
_BUDGET_GRACE_S = 5.0

_CH_CODE_RE = re.compile(r"(?:error code|Code:)\s*(\d+)", re.IGNORECASE)
_TIMEOUT_CODES = frozenset({159, 160})            # TIMEOUT_EXCEEDED, TOO_SLOW
_BUSY_CODES = frozenset({202, 203, 241, 242})     # too many queries, no free connection, memory limit, table read-only
_NETWORK_CODES = frozenset({209, 210})            # SOCKET_TIMEOUT, NETWORK_ERROR
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _ch_error_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    m = _CH_CODE_RE.search(str(exc))
    return int(m.group(1)) if m else None


def _is_transport_error(exc: BaseException) -> bool:
    from clickhouse_connect.driver.exceptions import OperationalError

    return isinstance(exc, (OperationalError, ConnectionError, TimeoutError, OSError))


def map_query_error(
    exc: BaseException,
    *,
    budget_s: float,
    label: str,
    sql: str | None = None,
    timed_out: bool = False,
    unanswered: bool = False,
) -> NetflowError:
    """A ClickHouse/driver exception as a safe NetflowError; the original is
    logged (with its id) for operators, never returned."""
    error_id = new_error_id()
    code = _ch_error_code(exc)
    unreachable = False
    if unanswered:
        status, err_code = 503, "unavailable"
        message = "Flow data is temporarily unavailable"
        logger.error("netflow query not sent [%s] label=%s: %s", error_id, label, exc)
        return NetflowError(status, message, code=err_code, error_id=error_id)
    if timed_out or code in _TIMEOUT_CODES:
        status, err_code = 504, "query_timeout"
        message = f"The flow query exceeded its {budget_s:g} s budget; narrow the time range or add filters"
    elif code in _BUSY_CODES:
        status, err_code = 503, "unavailable"
        message = "The flow database is busy; try again shortly"
    elif code in _NETWORK_CODES or (code is None and _is_transport_error(exc)):
        status, err_code = 503, "unavailable"
        message = "Flow data is temporarily unavailable"
        unreachable = True
    else:
        status, err_code = 500, "query_failed"
        message = "The flow query failed"
    logger.error(
        "netflow query failed [%s] label=%s clickhouse_code=%s: %s",
        error_id, label, code, exc, exc_info=exc,
    )
    if sql is not None:
        logger.debug("netflow query [%s] sql=%s", error_id, sql)
    err = NetflowError(status, message, code=err_code, error_id=error_id)
    err.unreachable = unreachable       # no connection or no answer (run_query fails the rest fast)
    return err


def _run_sync(sql: str, params: dict | None, budget_s: float, query_id: str, label: str):
    try:
        client = get_ch_client()
        # The thread-local client waits up to its default receive timeout
        # (300 s) for an answer; bound this query's read to its budget plus
        # the grace, so a server that never answers frees the worker thread
        # when the request gives up, not minutes later (NF-31).
        with _read_timeout(client, budget_s + _BUDGET_GRACE_S):
            return client.query(
                sql,
                parameters=params or {},
                settings={"max_execution_time": max(1, math.ceil(budget_s)), "query_id": query_id},
            )
    except Exception as exc:
        raise map_query_error(exc, budget_s=budget_s, label=label, sql=sql) from exc


def _bounded_timeout(previous: Any, read_s: float):
    """``previous`` (a urllib3 Timeout) with its read timeout set to
    ``read_s``; None when the client has no such timeout (test doubles)."""
    try:
        from urllib3 import Timeout
    except ImportError:  # pragma: no cover - clickhouse_connect depends on urllib3
        return None
    if not isinstance(previous, Timeout):
        return None
    return Timeout(connect=previous.connect_timeout, read=read_s)


@contextlib.contextmanager
def _read_timeout(client: Any, seconds: float):
    """The thread-local client's read timeout for one call (NF-31): the
    call's budget plus grace, whatever the client was created with."""
    previous = getattr(client, "timeout", None)
    bounded = _bounded_timeout(previous, seconds)
    if bounded is not None:
        client.timeout = bounded
    try:
        yield client
    finally:
        if bounded is not None:
            client.timeout = previous


def _kill_query(query_id: str) -> None:
    try:
        client = get_ch_client()
        with _read_timeout(client, 10):
            client.command("KILL QUERY WHERE query_id = %(q)s ASYNC", parameters={"q": query_id})
    except Exception:
        logger.debug("netflow: KILL QUERY %s failed", query_id, exc_info=True)


async def run_query(sql: str, params: dict | None = None, *, budget_s: float = BUDGET_INTERACTIVE, label: str | None = None):
    """Run a ClickHouse query in the thread pool under a time budget.

    ClickHouse stops the query at ``budget_s`` (``max_execution_time``); if the
    server does not answer within the budget plus a short grace the query is
    killed by id. Failures raise NetflowError: 504 ``query_timeout``, 503
    ``unavailable`` (unreachable or busy), 500 ``query_failed`` — each with an
    error id that is also in the server log next to the real exception.
    """
    meta = current_meta()
    label = _LABEL_RE.sub("-", label or meta.route or "netflow").strip("-") or "netflow"
    if meta.ch_unanswered:
        # An earlier query of this request got no answer within its budget:
        # do not make the caller wait out another budget (NF-31).
        raise map_query_error(
            TimeoutError("the flow database did not answer an earlier query of this request"),
            budget_s=budget_s, label=label, unanswered=True,
        )
    query_id = f"nf-{label[:40]}-{secrets.token_hex(6)}"
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run_sync, sql, params, budget_s, query_id, label),
            timeout=budget_s + _BUDGET_GRACE_S,
        )
    except NetflowError as err:
        if getattr(err, "unreachable", False):
            meta.ch_unanswered = True       # no connection / no answer: fail the rest fast
        raise
    except asyncio.TimeoutError:
        meta.ch_unanswered = True
        asyncio.get_running_loop().run_in_executor(None, _kill_query, query_id)
        raise map_query_error(
            TimeoutError(f"no answer within {budget_s + _BUDGET_GRACE_S:g} s; killing {query_id}"),
            budget_s=budget_s, label=label, timed_out=True,
        ) from None


# ── Retention ─────────────────────────────────────────────────────────────────

_RETENTION_TABLES = (RAW, CONVERSATIONS_1H, QOS_1H, INTERFACE_5M, EXPORTER_MINUTES)
_RETENTION_TTL_S = 300.0


@dataclass(frozen=True)
class Retention:
    """Configured retention (table TTL, applied by Settings -> Storage) and the
    earliest data actually present, per flow table. ``None`` days = no TTL."""

    days: dict[str, int | None]
    earliest: dict[str, datetime | None]
    known: bool = True

    @property
    def raw_days(self) -> int | None:
        return self.days.get(RAW)

    @property
    def rollup_days(self) -> int | None:
        return self.days.get(CONVERSATIONS_1H)

    @property
    def qos_days(self) -> int | None:
        return self.days.get(QOS_1H)

    @property
    def interface_days(self) -> int | None:
        return self.days.get(INTERFACE_5M)

    @property
    def exporter_minutes_days(self) -> int | None:
        return self.days.get(EXPORTER_MINUTES)

    def horizon(self, table: str, now: datetime) -> datetime | None:
        """The oldest instant ``table`` still holds by its TTL (None = unlimited)."""
        days = self.days.get(table.split(".")[-1])
        return None if days is None else now - timedelta(days=days)


# Used when ClickHouse cannot be asked: retention unknown (null, never a
# guessed number); plans then treat every table as unlimited.
_UNKNOWN_RETENTION = Retention(days={}, earliest={}, known=False)
_retention_cache: dict[str, Any] = {"value": None, "at": 0.0, "lock": None, "loop": None}


def _retention_lock() -> asyncio.Lock:
    # One lock per event loop: a lock that once waited is bound to its loop.
    loop = asyncio.get_running_loop()
    if _retention_cache["loop"] is not loop:
        _retention_cache.update(lock=asyncio.Lock(), loop=loop)
    return _retention_cache["lock"]


def _retention_sync() -> Retention:
    from app.services.storage_service import _parse_ttl_days

    client = get_ch_client()
    with _read_timeout(client, 15):
        tables = client.query(
            "SELECT name, engine_full FROM system.tables WHERE database = %(db)s AND name IN %(names)s",
            parameters={"db": CH_DB, "names": _RETENTION_TABLES},
            settings={"max_execution_time": 10},
        ).result_rows
        parts = client.query(
            "SELECT table, min(min_time) FROM system.parts "
            "WHERE active AND database = %(db)s AND table IN %(names)s GROUP BY table",
            parameters={"db": CH_DB, "names": _RETENTION_TABLES},
            settings={"max_execution_time": 10},
        ).result_rows
    days = {name: _parse_ttl_days(engine_full) for name, engine_full in tables}
    earliest: dict[str, datetime | None] = {}
    for table, min_time in parts:
        if isinstance(min_time, datetime) and min_time.year >= MIN_YEAR:
            earliest[table] = min_time if min_time.tzinfo else min_time.replace(tzinfo=timezone.utc)
    return Retention(days=days, earliest=earliest, known=True)


async def get_retention(*, refresh: bool = False) -> Retention:
    """Flow-table retention, cached for five minutes. Never raises: when
    ClickHouse cannot be read every value is None with ``known=False`` (and
    responses carry a ``retention_unknown`` warning)."""
    cached = _retention_cache["value"]
    if not refresh and cached is not None and time.monotonic() - _retention_cache["at"] < _RETENTION_TTL_S:
        return cached
    unreachable_before = _retention_cache.get("unreachable_seq", 0)
    async with _retention_lock():
        cached = _retention_cache["value"]
        if not refresh and cached is not None and time.monotonic() - _retention_cache["at"] < _RETENTION_TTL_S:
            if _retention_cache.get("unreachable_seq", 0) != unreachable_before:
                # The lookup this request waited for found ClickHouse not
                # answering: its flow queries fail fast too (NF-31).
                current_meta().ch_unanswered = True
            return cached
        try:
            value = await asyncio.wait_for(asyncio.to_thread(_retention_sync), timeout=15)
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError) or (_ch_error_code(exc) is None and _is_transport_error(exc)):
                # No answer (or no connection): the request's flow queries
                # fail fast instead of each waiting out a budget (NF-31).
                current_meta().ch_unanswered = True
                _retention_cache["unreachable_seq"] = _retention_cache.get("unreachable_seq", 0) + 1
            logger.warning("netflow: retention lookup failed", exc_info=True)
            # Retry sooner than a successful read would.
            _retention_cache.update(value=_UNKNOWN_RETENTION, at=time.monotonic() - _RETENTION_TTL_S + 30)
            return _UNKNOWN_RETENTION
        _retention_cache.update(value=value, at=time.monotonic())
        return value


# ── NetFlow v5 sampling repair (X-02 disclosure) ──────────────────────────────
# Before schema v2 the collector stored sampled NetFlow v5 traffic ~165x too
# large (unmasked sampling-mode bits). migrate-137 corrects raw records and the
# rollup healer rebuilds the rollup hours raw still covers; older rollup hours
# (and hours whose raw expired before their rebuild) cannot be corrected.
# netflow_rollup.repair_status() says how far that got; responses served from
# the conversation/QoS rollups disclose it when it matters.

# The rollups that existed before the correction and so held inflated bytes;
# flow_interface_5m and flow_exporter_minutes were built from corrected raw.
SAMPLING_REPAIRED_TABLES = frozenset({CONVERSATIONS_1H, QOS_1H, TRAFFIC_5M})
_REPAIR_TTL_S = 300.0
_REPAIR_TIMEOUT_S = 5.0
_repair_cache: dict[str, Any] = {"value": None, "at": 0.0, "lock": None, "loop": None}
_UNREADABLE = object()


def _repair_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _repair_cache["loop"] is not loop:
        _repair_cache.update(lock=asyncio.Lock(), loop=loop)
    return _repair_cache["lock"]


def sampling_repair_affected(status: dict) -> bool:
    """True when this appliance held sampled v5 traffic the repair had to act
    on (any hour queued, rebuilt or lost, or bad rows seen after the upgrade).
    An appliance that never received sampled v5 has nothing to disclose."""
    return bool(
        status.get("pending_hours")
        or any((status.get("rebuilt_hours") or {}).values())
        or any((status.get("unrecoverable_hours") or {}).values())
        or status.get("rows_found_after_upgrade")
    )


async def get_sampling_repair(*, refresh: bool = False) -> dict | None:
    """``netflow_rollup.repair_status()`` plus ``affected``, cached for five
    minutes; None when it cannot be read (unknown, never "nothing to repair")."""
    from app.services import netflow_rollup

    cached = _repair_cache["value"]
    if not refresh and cached is not None and time.monotonic() - _repair_cache["at"] < _REPAIR_TTL_S:
        return None if cached is _UNREADABLE else cached
    async with _repair_lock():
        cached = _repair_cache["value"]
        if not refresh and cached is not None and time.monotonic() - _repair_cache["at"] < _REPAIR_TTL_S:
            return None if cached is _UNREADABLE else cached
        try:
            status = await asyncio.wait_for(netflow_rollup.repair_status(), timeout=_REPAIR_TIMEOUT_S)
        except Exception:
            logger.warning("netflow: sampling repair status unavailable", exc_info=True)
            _repair_cache.update(value=_UNREADABLE, at=time.monotonic() - _REPAIR_TTL_S + 30)
            return None
        value = {**status, "affected": sampling_repair_affected(status)}
        _repair_cache.update(value=value, at=time.monotonic())
        return value


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sampling_repair_warnings(status: dict | None, window: Window) -> list[dict]:
    """Warnings for a response whose totals come (partly) from a rollup in
    SAMPLING_REPAIRED_TABLES."""
    if not status or not status.get("affected"):
        return []
    out: list[dict] = []
    if status.get("pending_hours"):
        out.append({
            "code": "sampling_repair_pending",
            "message": "Hourly rollups holding sampled NetFlow v5 traffic are still being corrected; "
                       "long-range totals may still change",
        })
    verified_from = _parse_iso(status.get("verified_from"))
    if verified_from is not None and window.start < verified_from:
        out.append({
            "code": "sampling_unverified",
            "message": f"Rollup totals before {verified_from:%Y-%m-%d %H:%M} UTC predate the NetFlow v5 "
                       "sampling correction and could not be re-checked; sampled v5 exporters may be "
                       "overstated there",
        })
    # Hours queued at reconciliation (so after verified_from, before
    # reconciled_at) whose raw records expired before they were rebuilt.
    lost = sum((status.get("unrecoverable_hours") or {}).values())
    reconciled_at = _parse_iso(status.get("reconciled_at"))
    if lost and reconciled_at is not None and window.start < reconciled_at:
        out.append({
            "code": "sampling_unrecoverable",
            "message": f"{lost} rollup hour{'s' if lost != 1 else ''} holding sampled NetFlow v5 traffic "
                       "could not be corrected because their raw records had expired; they remain overstated",
        })
    return out


# ── Protocol, port and application labels ─────────────────────────────────────

PROTO_LABELS = {
    1: "ICMP",
    2: "IGMP",
    6: "TCP",
    17: "UDP",
    41: "IPv6",
    47: "GRE",
    50: "ESP",
    51: "AH",
    58: "ICMPv6",
    88: "EIGRP",
    89: "OSPF",
    103: "PIM",
    112: "VRRP",
    115: "L2TP",
    132: "SCTP",
}

COMMON_PORTS = {
    20: "FTP Data",
    21: "FTP",
    22: "SSH",
    23: "Telnet",
    25: "SMTP",
    37: "Time",
    42: "WINS",
    43: "WHOIS",
    49: "TACACS",
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    69: "TFTP",
    79: "Finger",
    80: "HTTP",
    88: "Kerberos",
    102: "MS-RPC/ISO-TSAP",
    110: "POP3",
    111: "RPCbind",
    113: "Ident",
    119: "NNTP",
    123: "NTP",
    135: "MS-RPC",
    137: "NetBIOS-NS",
    138: "NetBIOS-DGM",
    139: "NetBIOS-SSN",
    143: "IMAP",
    161: "SNMP",
    162: "SNMP Trap",
    179: "BGP",
    194: "IRC",
    201: "AppleTalk",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    465: "SMTPS",
    500: "IKE",
    514: "Syslog",
    515: "LPD",
    520: "RIP",
    523: "DB2",
    540: "UUCP",
    546: "DHCPv6",
    547: "DHCPv6",
    554: "RTSP",
    587: "SMTP Submission",
    593: "MS-RPC HTTP",
    623: "IPMI",
    636: "LDAPS",
    646: "LDP",
    873: "rsync",
    902: "VMware",
    989: "FTPS Data",
    990: "FTPS",
    993: "IMAPS",
    995: "POP3S",
    1080: "SOCKS",
    1194: "OpenVPN",
    1352: "Lotus Notes",
    1433: "MSSQL",
    1434: "MSSQL Monitor",
    1521: "Oracle DB",
    1645: "RADIUS",
    1646: "RADIUS",
    1701: "L2TP",
    1719: "H.323",
    1720: "H.323",
    1723: "PPTP",
    1755: "MMS",
    1812: "RADIUS",
    1813: "RADIUS Acct",
    1883: "MQTT",
    1900: "SSDP/UPnP",
    1935: "RTMP",
    2049: "NFS",
    2055: "NetFlow",
    2082: "cPanel",
    2083: "cPanel SSL",
    2181: "ZooKeeper",
    2375: "Docker",
    2376: "Docker TLS",
    2379: "etcd",
    3000: "Grafana/Dev",
    3128: "Squid Proxy",
    3260: "iSCSI",
    3268: "Global Catalog",
    3269: "Global Catalog SSL",
    3306: "MySQL",
    3389: "RDP",
    3478: "STUN/TURN",
    3690: "SVN",
    4369: "EPMD",
    4500: "IPsec NAT-T",
    4789: "VXLAN",
    5004: "RTP",
    5005: "RTP",
    5060: "SIP",
    5061: "SIP TLS",
    5222: "XMPP",
    5269: "XMPP Server",
    5353: "mDNS",
    5432: "PostgreSQL",
    5601: "Kibana",
    5666: "NRPE",
    5672: "AMQP",
    5900: "VNC",
    5938: "TeamViewer",
    5985: "WinRM",
    5986: "WinRM SSL",
    6343: "sFlow",
    6379: "Redis",
    6443: "Kubernetes API",
    6514: "Syslog TLS",
    6660: "IRC",
    7001: "WebLogic",
    8000: "HTTP Alt",
    8006: "Proxmox",
    8080: "HTTP Alt",
    8086: "InfluxDB",
    8123: "ClickHouse HTTP",
    8443: "HTTPS Alt",
    8554: "RTSP Alt",
    8883: "MQTT TLS",
    9000: "ClickHouse/PHP-FPM",
    9042: "Cassandra",
    9090: "Prometheus",
    9092: "Kafka",
    9100: "Node Exporter",
    9200: "Elasticsearch",
    9300: "Elasticsearch",
    9418: "Git",
    10050: "Zabbix Agent",
    11211: "Memcached",
    15672: "RabbitMQ Mgmt",
    16384: "RTP",
    19302: "STUN",
    27017: "MongoDB",
    50000: "SAP",
}


def _protocol_name(proto: int) -> str:
    return PROTO_LABELS.get(int(proto), f"Protocol {proto}")


def _port_name(port: int, proto: int | None = None) -> str:
    if port == 0:
        return "none"
    label = COMMON_PORTS.get(int(port))
    if label:
        return label
    if proto is not None and proto == 17:
        return f"UDP/{port}"
    if proto is not None and proto == 6:
        return f"TCP/{port}"
    return str(port)


# ── Reverse-DNS enrichment (Phase 2) ──────────────────────────────────────────
# Best-effort PTR lookups for the small set of IPs shown in top-N views. TTL
# cached; bounded by an overall deadline so a slow resolver never stalls the API.
# Opt-in per request (?resolve=1) because it adds latency and outbound DNS.
#
# One bounded pool for the process (not one per request). A lookup still
# running at the deadline is neither cancelled nor recorded as "no name": it
# keeps running and writes its answer to the cache when it finishes, and the
# row says the name is pending. Only answers are cached; "no PTR record" for a
# shorter time than a name, so a transient resolver failure heals quickly.
_RDNS_TTL = 3600.0
_RDNS_NEGATIVE_TTL = 300.0
_rdns_cache: dict[str, tuple[float, str | None]] = {}
_rdns_inflight: dict[str, concurrent.futures.Future] = {}
_rdns_lock = threading.Lock()
_rdns_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="netflow-rdns")
RDNS_PENDING = object()


def _rdns_one(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror, socket.gaierror):
        return None


def _rdns_store(ip: str, fut: concurrent.futures.Future) -> None:
    host = fut.result() if not fut.cancelled() and fut.exception() is None else None
    ttl = _RDNS_TTL if host else _RDNS_NEGATIVE_TTL
    with _rdns_lock:
        _rdns_cache[ip] = (time.monotonic() + ttl, host)
        _rdns_inflight.pop(ip, None)


def _resolve_hostnames(ips: list[str], deadline: float = 2.0) -> dict[str, object]:
    """{ip: hostname | None (no PTR record) | RDNS_PENDING (still resolving)}."""
    now = time.monotonic()
    out: dict[str, object] = {}
    waiting: dict[str, concurrent.futures.Future] = {}
    started: list[tuple[str, concurrent.futures.Future]] = []
    with _rdns_lock:
        for ip in dict.fromkeys(ips):  # de-dup, preserve order
            ent = _rdns_cache.get(ip)
            if ent and ent[0] > now:
                out[ip] = ent[1]
                continue
            fut = _rdns_inflight.get(ip)
            if fut is None:
                fut = _rdns_pool.submit(_rdns_one, ip)
                _rdns_inflight[ip] = fut
                started.append((ip, fut))
            waiting[ip] = fut
    # Outside the lock: a callback on an already-finished future runs inline.
    for ip, fut in started:
        fut.add_done_callback(lambda f, ip=ip: _rdns_store(ip, f))
    if waiting:
        concurrent.futures.wait(list(waiting.values()), timeout=deadline)
        for ip, fut in waiting.items():
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                out[ip] = fut.result()
            else:
                out[ip] = RDNS_PENDING
    return out


# ── IP groups / sites ─────────────────────────────────────────────────────────
# Groups are DB-backed (services/netflow_groups). These read its cached
# snapshot, which the flow_filter dependency refreshes on every request.

def _classify_ip(ip: str) -> str | None:
    return netflow_groups.classify_ip(ip)


def _enrich_hosts(rows: list[dict]) -> list[dict]:
    """Attach `group` (IP-group membership) and GeoIP `country`/`asn` (no-op
    without a DB) to each per-IP row. Reverse DNS is breakdowns._rdns, which
    honours the operator's switch and deadline (settings.rdns_config)."""
    for row in rows:
        ip = str(row.get("ip", ""))
        row["group"] = _classify_ip(ip)
        g = geoip.enrich(ip)
        row["country"] = g["country"]
        row["country_name"] = g["country_name"]
        row["asn"] = g["asn"]
        row["as_name"] = g["as_name"]
        row["hostname"] = None
    return rows


# Below this span the rollup's hour-bucket edges would visibly skew the
# numbers, and raw scans are fast enough anyway.
ROLLUP_MIN_SECONDS = 72 * 3600


# ── Exporter and interface names ──────────────────────────────────────────────

async def _resolve_interface_names(rows: list[tuple], db: AsyncSession) -> dict[tuple[str, int], dict]:
    """{(exporter_ip, ifindex) -> {if_name, if_descr, if_alias, if_speed,
    device_hostname}} for ``rows`` of (exporter, ifindex), from the interfaces
    of the device that owns each exporter.

    Ownership comes from the one resolver visibility uses
    (access.resolve_exporter_owners): an explicit exporter mapping wins over a
    management-IP match (X-46), so interface names and who may read the
    exporter's flows always agree. Keys keep the exporter text as given.
    """
    keys = {(str(r[0]), int(r[1])) for r in rows}
    if not keys:
        return {}
    owners = await resolve_exporter_owners(db, list({ip for ip, _ in keys}))
    ip_to_device: dict[str, tuple[uuid.UUID, str | None]] = {}
    for ip, _ in keys:
        owner = owners.get(canonical_ip(ip) or "")
        if owner is not None:
            ip_to_device[ip] = (uuid.UUID(owner.device_id), owner.hostname)
    if not ip_to_device:
        return {}
    ifs_q = await db.execute(
        select(
            DeviceInterface.device_id,
            DeviceInterface.if_index,
            DeviceInterface.if_name,
            DeviceInterface.if_descr,
            DeviceInterface.if_alias,
            DeviceInterface.if_speed,
        ).where(DeviceInterface.device_id.in_({d[0] for d in ip_to_device.values()}))
    )
    by_device: dict = {}
    for row in ifs_q.all():
        by_device.setdefault(row.device_id, {})[row.if_index] = {
            "if_name": row.if_name,
            "if_descr": row.if_descr,
            "if_alias": row.if_alias,
            "if_speed": int(row.if_speed) if row.if_speed else None,
        }
    out: dict[tuple[str, int], dict] = {}
    for ip, idx in keys:
        dev = ip_to_device.get(ip)
        if not dev:
            continue
        meta = (by_device.get(dev[0]) or {}).get(idx)
        if meta:
            out[(ip, idx)] = {**meta, "device_hostname": dev[1]}
    return out


def _interface_display_name(meta: dict, ifindex: int) -> str:
    return meta.get("if_name") or meta.get("if_descr") or meta.get("if_alias") or f"ifIndex {ifindex}"


def _interface_payload(exporter_ip: str, ifindex: int, meta: dict | None = None) -> dict:
    meta = meta or {}
    return {
        "exporter_ip": exporter_ip,
        "ifindex": ifindex,
        "if_name": meta.get("if_name"),
        "if_descr": meta.get("if_descr"),
        "if_alias": meta.get("if_alias"),
        "if_speed": meta.get("if_speed"),
        "device_hostname": meta.get("device_hostname"),
        "display_name": _interface_display_name(meta, ifindex),
    }


# RFC mapping: DSCP code-point -> friendly name. Most operators only really see
# a handful of these; the rest fall back to the raw "DSCP N" label.
DSCP_LABELS = {
    0: "BE (Best Effort)",
    8: "CS1",
    10: "AF11",
    12: "AF12",
    14: "AF13",
    16: "CS2",
    18: "AF21",
    20: "AF22",
    22: "AF23",
    24: "CS3",
    26: "AF31",
    28: "AF32",
    30: "AF33",
    32: "CS4",
    34: "AF41",
    36: "AF42",
    38: "AF43",
    40: "CS5",
    46: "EF (Voice)",
    48: "CS6 (Network Control)",
    56: "CS7",
}


def _dscp_label(dscp: int) -> str:
    return DSCP_LABELS.get(int(dscp), f"DSCP {dscp}")
