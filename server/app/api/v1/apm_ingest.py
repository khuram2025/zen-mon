"""APM OTLP receiver — FastAPI fallback ingest path.

Implements the OTLP/HTTP **JSON** trace export (POST /v1/traces) as the
single-node fallback to the high-throughput Go collector. Decodes
ExportTraceServiceRequest JSON into `apm_spans` rows, authenticates the `zpi_`
ingest key, and hands rows to a buffered async batch writer that flushes to
ClickHouse by size/interval. Binary OTLP/protobuf is the Go collector's job
(primary path); this router answers 415 for protobuf with a pointer to it.

Mounted at ROOT prefix "" so the path is exactly `/v1/traces` (the OTLP default),
not `/api/v1/v1/traces`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.apm import authenticate_ingest_key
from app.core.database import get_ch_client, get_db
from app.core.security import require_operator_user

logger = logging.getLogger("zenplus.apm.ingest")

router = APIRouter(tags=["APM Ingest (OTLP)"])

# ── batch writer config ──────────────────────────────────────────────────────
BATCH_MAX_ROWS = 2000          # flush when this many spans buffered
BATCH_INTERVAL_S = 1.0         # ...or after this long
QUEUE_MAXSIZE = 512            # number of pending request-payloads before backpressure

SPAN_COLUMNS = [
    "timestamp", "trace_id", "span_id", "parent_span_id", "name", "span_kind",
    "span_kind_str", "service_name", "env", "service_version", "duration_nano",
    "status_code", "status_message", "has_error", "http_method", "http_route",
    "http_status_code", "db_system", "db_operation", "db_statement", "rpc_method",
    "attributes_string", "attributes_number", "attributes_bool", "resource",
    "resource_fingerprint", "events_ts", "events_name", "events_attrs",
    "links_trace_id", "links_span_id", "ts_bucket", "deployment_id",
]
RESOURCE_COLUMNS = ["fingerprint", "labels", "seen_at", "ts_bucket"]
EXC_COLUMNS = [
    "timestamp", "error_id", "group_id", "trace_id", "span_id", "service_name",
    "env", "service_version", "exception_type", "exception_message",
    "exception_stack", "exception_escaped", "http_route", "resource_tags", "ts_bucket",
]

# Stack/message normalisation for stable fingerprints — collapse line-noise so
# the same logical error groups regardless of addresses / ids / line numbers.
_NORM_SUBS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "UUID"),
    (re.compile(r":\d+"), ":N"),
    (re.compile(r"\b\d+\b"), "N"),
]


def _normalize(s: str) -> str:
    for rx, rep in _NORM_SUBS:
        s = rx.sub(rep, s)
    return s


def exception_group_id(exc_type: str, stack: str, message: str) -> str:
    """16-hex fingerprint of (type + normalized stack||message)."""
    basis = (exc_type or "") + "|" + _normalize(stack or message or "")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_SPAN_KIND_STR = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT",
                  4: "PRODUCER", 5: "CONSUMER"}
_STATUS_STR = {0: "UNSET", 1: "OK", 2: "ERROR"}


def _otlp_enum_number(value: object, prefix: str, names: dict[int, str]) -> int:
    """Decode either the numeric or canonical string OTLP/JSON enum form."""
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        normalized = str(value).upper()
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
        return next((number for number, name in names.items() if name == normalized), 0)

# observability counters (read by /v1/traces health + tests)
STATS = {"accepted_spans": 0, "rejected_spans": 0, "dropped_spans": 0,
         "skewed_spans": 0, "flushes": 0}

# Deltas since the last persist, flushed to zenplus.apm_ingest_stats so
# data-quality counters survive restarts and aggregate across workers.
_PENDING_STATS = {"accepted": 0, "rejected": 0, "dropped": 0, "skewed": 0, "flushes": 0}

# Spans stamped further than this into the future are unacceptable data —
# a skewed producer clock would corrupt every time-window query.
CLOCK_SKEW_MAX_FUTURE_S = 300
# Spans older than the raw-span TTL would be deleted on arrival; reject them
# so the producer learns instead of silently losing data.
CLOCK_SKEW_MAX_PAST_S = 7 * 86400

_queue: "asyncio.Queue | None" = None
_writer_task: "asyncio.Task | None" = None


# ── OTLP/JSON decoding ───────────────────────────────────────────────────────

def _attr_list_to_maps(attrs: list) -> tuple[dict, dict, dict]:
    """Split an OTLP attribute list into typed (string, number, bool) maps."""
    s, n, b = {}, {}, {}
    for a in attrs or []:
        k = a.get("key")
        v = a.get("value") or {}
        if k is None:
            continue
        if "stringValue" in v:
            s[k] = v["stringValue"]
        elif "intValue" in v:
            try:
                n[k] = float(int(v["intValue"]))
            except (TypeError, ValueError):
                pass
        elif "doubleValue" in v:
            n[k] = float(v["doubleValue"])
        elif "boolValue" in v:
            b[k] = 1 if v["boolValue"] else 0
        elif "bytesValue" in v:
            s[k] = str(v["bytesValue"])
        elif "arrayValue" in v or "kvlistValue" in v:
            s[k] = json.dumps(v)
    return s, n, b


def _norm_id(raw: str, want_len: int) -> str:
    """Normalise an OTLP id to lowercase hex of `want_len` chars.

    OTLP/JSON encodes trace_id/span_id as hex, but tolerate base64 too.
    """
    if not raw:
        return "0" * want_len
    raw = raw.strip()
    if len(raw) == want_len and all(c in "0123456789abcdefABCDEF" for c in raw):
        return raw.lower()
    # try base64 -> hex
    try:
        pad = "=" * (-len(raw) % 4)
        h = binascii.hexlify(base64.b64decode(raw + pad)).decode()
        if len(h) == want_len:
            return h.lower()
    except (binascii.Error, ValueError):
        pass
    return (raw.lower() + "0" * want_len)[:want_len]


def _ts_from_nano(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def _resource_fingerprint(res_attrs: dict) -> str:
    canonical = json.dumps(res_attrs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode()).hexdigest()


def decode_otlp_traces_json(payload: dict, default_env: str | None) -> tuple[list, dict, list, int, int]:
    """Return (span_rows, resource_rows_by_fp, exc_rows, rejected_count, skewed_count).

    span_rows are lists ordered per SPAN_COLUMNS; resource_rows_by_fp dedupes
    resource side-table rows by fingerprint.
    """
    span_rows: list = []
    res_rows: dict = {}
    exc_rows: list = []
    rejected = 0
    skewed = 0
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    max_start_ns = now_ns + CLOCK_SKEW_MAX_FUTURE_S * 1_000_000_000
    min_start_ns = now_ns - CLOCK_SKEW_MAX_PAST_S * 1_000_000_000

    for rs in payload.get("resourceSpans", []) or []:
        res = rs.get("resource") or {}
        res_s, _res_n, _res_b = _attr_list_to_maps(res.get("attributes", []))
        all_res = {}
        for a in res.get("attributes", []) or []:
            v = a.get("value") or {}
            all_res[a.get("key")] = next(iter(v.values()), None) if v else None
        service_name = res_s.get("service.name") or all_res.get("service.name") or "unknown"
        env = res_s.get("deployment.environment") or default_env or "unknown"
        service_version = res_s.get("service.version") or ""
        fp = _resource_fingerprint(all_res)
        res_json = json.dumps(all_res, separators=(",", ":"))
        produced_spans = False

        for ss in rs.get("scopeSpans", []) or []:
            for sp in ss.get("spans", []) or []:
                try:
                    start_ns = int(sp.get("startTimeUnixNano") or 0)
                    end_ns = int(sp.get("endTimeUnixNano") or start_ns)
                    if start_ns <= 0:
                        rejected += 1
                        continue
                    if start_ns > max_start_ns or start_ns < min_start_ns:
                        skewed += 1
                        rejected += 1
                        continue
                    dur = max(0, end_ns - start_ns)
                    a_s, a_n, a_b = _attr_list_to_maps(sp.get("attributes", []))
                    kind = _otlp_enum_number(
                        sp.get("kind", 0), "SPAN_KIND_", _SPAN_KIND_STR,
                    )
                    status = sp.get("status") or {}
                    scode = _otlp_enum_number(
                        status.get("code", 0), "STATUS_CODE_", _STATUS_STR,
                    )
                    ev = sp.get("events", []) or []
                    lk = sp.get("links", []) or []
                    http_sc = a_n.get("http.status_code") or a_n.get("http.response.status_code") or 0
                    ts_bucket = (start_ns // 1_000_000_000 // 1800) * 1800

                    span_rows.append([
                        _ts_from_nano(start_ns),                       # timestamp
                        _norm_id(sp.get("traceId", ""), 32),           # trace_id
                        _norm_id(sp.get("spanId", ""), 16),            # span_id
                        _norm_id(sp.get("parentSpanId", ""), 16) if sp.get("parentSpanId") else "",
                        sp.get("name", "") or "",                      # name
                        kind,                                          # span_kind
                        _SPAN_KIND_STR.get(kind, "UNSPECIFIED"),       # span_kind_str
                        service_name, env, service_version,
                        dur,                                           # duration_nano
                        _STATUS_STR.get(scode, "UNSET"),               # status_code
                        status.get("message", "") or "",               # status_message
                        1 if scode == 2 else 0,                        # has_error
                        a_s.get("http.method") or a_s.get("http.request.method") or "",
                        a_s.get("http.route") or "",
                        int(http_sc) if http_sc else 0,                # http_status_code
                        a_s.get("db.system") or "",
                        a_s.get("db.operation") or "",
                        a_s.get("db.statement") or "",                 # digest (collector scrubs)
                        a_s.get("rpc.method") or "",
                        a_s, a_n, a_b,                                 # typed attr maps
                        res_json,                                      # resource
                        fp,                                            # resource_fingerprint
                        [_ts_from_nano(int(e.get("timeUnixNano") or 0)) for e in ev],
                        [e.get("name", "") or "" for e in ev],
                        [json.dumps(e.get("attributes", []), separators=(",", ":")) for e in ev],
                        [_norm_id(l.get("traceId", ""), 32) for l in lk],
                        [_norm_id(l.get("spanId", ""), 16) for l in lk],
                        int(ts_bucket),                                # ts_bucket
                        _ZERO_UUID,                                    # deployment_id
                    ])
                    produced_spans = True

                    # ── exceptions -> apm_exceptions ──
                    tid = _norm_id(sp.get("traceId", ""), 32)
                    sid = _norm_id(sp.get("spanId", ""), 16)
                    route = a_s.get("http.route") or ""
                    rtags = {k: str(v) for k, v in all_res.items() if v is not None}
                    found_exc = False
                    for e in ev:
                        if (e.get("name") or "") != "exception":
                            continue
                        ea = {}
                        for at in e.get("attributes", []) or []:
                            av = at.get("value") or {}
                            ea[at.get("key")] = av.get("stringValue") or av.get("intValue") or av.get("boolValue")
                        etype = ea.get("exception.type") or "Exception"
                        emsg = ea.get("exception.message") or ""
                        estack = ea.get("exception.stacktrace") or ""
                        eesc = 1 if str(ea.get("exception.escaped")).lower() in ("true", "1") else 0
                        et_ns = int(e.get("timeUnixNano") or start_ns)
                        exc_rows.append([
                            _ts_from_nano(et_ns), str(uuid.uuid4()),
                            exception_group_id(etype, estack, emsg), tid, sid,
                            service_name, env, service_version, etype, emsg, estack,
                            eesc, route, rtags, int(ts_bucket),
                        ])
                        found_exc = True
                    if not found_exc and scode == 2:
                        # error-status span with no exception event -> synthesize one
                        emsg = (status.get("message") or "") or sp.get("name", "")
                        exc_rows.append([
                            _ts_from_nano(start_ns), str(uuid.uuid4()),
                            exception_group_id("Error", "", f"{service_name}|{sp.get('name','')}"),
                            tid, sid, service_name, env, service_version, "Error", emsg, "",
                            0, route, rtags, int(ts_bucket),
                        ])
                except Exception:  # one bad span never sinks the batch
                    rejected += 1
                    continue

        if produced_spans and fp not in res_rows:
            res_rows[fp] = [fp, res_json, datetime.now(timezone.utc), 0]

    return span_rows, res_rows, exc_rows, rejected, skewed


# ── batch writer ─────────────────────────────────────────────────────────────

def _insert(table: str, rows: list, cols: list) -> None:
    # Resolve the client INSIDE the worker thread so it uses that thread's own
    # session (clickhouse_connect forbids concurrent queries on one session).
    get_ch_client().insert(table, rows, column_names=cols, database="zenplus")


async def _flush(span_rows: list, res_rows: dict, exc_rows: list) -> None:
    if span_rows:
        await asyncio.to_thread(_insert, "apm_spans", span_rows, SPAN_COLUMNS)
    if res_rows:
        await asyncio.to_thread(_insert, "apm_traces_resource", list(res_rows.values()), RESOURCE_COLUMNS)
    if exc_rows:
        await asyncio.to_thread(_insert, "apm_exceptions", exc_rows, EXC_COLUMNS)
    STATS["flushes"] += 1
    STATS["accepted_spans"] += len(span_rows)
    _PENDING_STATS["flushes"] += 1
    _PENDING_STATS["accepted"] += len(span_rows)


async def _persist_pending_stats() -> None:
    """Write accumulated counter deltas to apm_ingest_stats (per-minute rows;
    SummingMergeTree collapses concurrent workers). Best-effort: on failure the
    deltas stay pending and ride the next attempt."""
    if not any(_PENDING_STATS.values()):
        return
    snapshot = dict(_PENDING_STATS)
    try:
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        await asyncio.to_thread(
            _insert, "apm_ingest_stats",
            [[minute, snapshot["accepted"], snapshot["rejected"],
              snapshot["dropped"], snapshot["skewed"], snapshot["flushes"]]],
            ["timestamp", "accepted", "rejected", "dropped", "skewed", "flushes"],
        )
        for k, v in snapshot.items():
            _PENDING_STATS[k] -= v
    except Exception:
        logger.debug("apm ingest-stats persist deferred (clickhouse unavailable)")


async def _writer_loop() -> None:
    assert _queue is not None
    spans: list = []
    res: dict = {}
    excs: list = []
    while True:
        try:
            item = await asyncio.wait_for(_queue.get(), timeout=BATCH_INTERVAL_S)
            spans.extend(item[0])
            res.update(item[1])
            excs.extend(item[2])
            _queue.task_done()
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if spans or excs:
                try:
                    await _flush(spans, res, excs)
                except Exception:
                    logger.exception("apm final flush failed")
            raise
        if (spans or excs) and (len(spans) >= BATCH_MAX_ROWS or _queue.empty()):
            try:
                await _flush(spans, res, excs)
            except Exception:
                STATS["dropped_spans"] += len(spans)
                _PENDING_STATS["dropped"] += len(spans)
                logger.exception("apm flush to ClickHouse failed; dropped %d spans", len(spans))
            spans, res, excs = [], {}, []
        await _persist_pending_stats()


async def start_batch_writer() -> None:
    global _queue, _writer_task
    if _writer_task is not None:
        return
    _queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    _writer_task = asyncio.create_task(_writer_loop())
    logger.info("APM OTLP batch writer started")


async def stop_batch_writer() -> None:
    global _writer_task
    if _writer_task is not None:
        _writer_task.cancel()
        try:
            await _writer_task
        except (asyncio.CancelledError, Exception):
            pass
        _writer_task = None


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/v1/traces")
async def otlp_traces(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    key = await authenticate_ingest_key(authorization or "", db, kind="sdk")

    ct = (content_type or "").lower()
    if "protobuf" in ct:
        raise HTTPException(
            415,
            "OTLP/protobuf is served by the ZenPlus Go collector on :4317/:4318. "
            "For the FastAPI fallback set OTEL_EXPORTER_OTLP_PROTOCOL=http/json.",
        )

    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid OTLP/JSON body")

    span_rows, res_rows, exc_rows, rejected, skewed = decode_otlp_traces_json(payload, key.get("env_name"))

    if span_rows:
        if _queue is None:
            raise HTTPException(503, "APM ingest not ready")
        try:
            _queue.put_nowait((span_rows, res_rows, exc_rows))
        except asyncio.QueueFull:
            STATS["rejected_spans"] += len(span_rows)
            _PENDING_STATS["rejected"] += len(span_rows)
            response.headers["Retry-After"] = "1"
            raise HTTPException(503, "APM ingest backpressure; retry shortly")

    STATS["rejected_spans"] += rejected
    STATS["skewed_spans"] += skewed
    _PENDING_STATS["rejected"] += rejected
    _PENDING_STATS["skewed"] += skewed
    if rejected:
        msg = "some spans failed to decode"
        if skewed:
            msg = (f"{skewed} span(s) rejected for clock skew "
                   f"(timestamp beyond ±{CLOCK_SKEW_MAX_FUTURE_S}s future / "
                   f"{CLOCK_SKEW_MAX_PAST_S // 86400}d past); check the producer's clock")
        return {"partialSuccess": {"rejectedSpans": rejected, "errorMessage": msg}}
    return {"partialSuccess": {}}


@router.get("/v1/apm/ingest-stats")
async def ingest_stats(user=Depends(require_operator_user)):
    """Per-process liveness/counters for the ingest path (operator+).

    Cluster-wide, restart-surviving counters live in zenplus.apm_ingest_stats
    (see GET /api/v1/apm/data-quality).
    """
    return {"queue_depth": _queue.qsize() if _queue else None, **STATS}
