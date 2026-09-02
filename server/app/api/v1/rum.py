"""Production browser Real User Monitoring data plane and analytics.

The browser token is public by design.  Trust therefore comes from exact-origin
and optional application binding, strict bounded schemas, deterministic event
identifiers, per-session/distributed quotas, privacy scrubbing and authenticated
read APIs -- never from treating the token as a secret.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.apm import authenticate_ingest_key
from app.api.v1.rum_sdk import RUM_SDK
from app.core.config import get_settings
from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user
from app.services import geoip
from app.services.rum_routes import apply_route_rules, rules_from_options
from app.services import rum_symbolicate
from app.core.security import require_operator_user

router = APIRouter(tags=["APM RUM"])

RANGE_SECONDS = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
    "90d": 7776000,
}
RANGE_BUCKET_SECONDS = {
    "15m": 60,
    "1h": 300,
    "6h": 900,
    "24h": 1800,
    "7d": 21600,
    "30d": 86400,
    "90d": 86400,
}
MAX_BODY_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 50
MAX_TIMELINE_EVENTS = 2000

RUM_COLUMNS = [
    "timestamp", "application_id", "service_name", "env", "event_type",
    "session_id", "view_id", "view_name", "url", "user_id", "country",
    "browser", "device_type", "lcp", "inp", "cls", "fcp", "ttfb",
    "load_ms", "error_message", "backend_trace_id", "attributes", "ts_bucket",
    "event_id", "sdk_version", "service_version", "browser_version", "os",
    "action_name", "action_type", "target", "duration_ms", "resource_url",
    "resource_type", "method", "status_code", "transfer_size",
    "encoded_body_size", "error_type", "error_stack", "error_source",
    "error_fingerprint", "end_reason", "is_final", "sample_rate",
    "vital_attribution", "has_lcp", "has_inp", "has_cls", "has_fcp",
    "has_ttfb", "has_load", "sampled",
    "client_ip",
    "redirect_ms", "dns_ms", "connect_ms", "tls_ms", "wait_ms", "download_ms",
    "blocked_ms", "processing_ms", "server_ms", "db_ms", "has_timing",
    "has_server_timing", "protocol", "connection_type", "connection_rtt_ms",
    "connection_downlink", "language", "timezone", "screen_res", "viewport",
    "dedupe_id",
]

_RUM_ROLLUP_INSERT_SQL = """
INSERT INTO zenplus.apm_rum_metrics_5m
SELECT
    toStartOfFiveMinutes(r.timestamp) AS bucket_timestamp,
    application_id, env, service_version, view_name, browser,
    browser_version, os, device_type, country,
    count() AS events,
    countIf(event_type = 'error') AS errors,
    countIf(event_type = 'error' AND sampled = 1) AS sampled_errors,
    countIf(event_type = 'error' AND sampled = 0) AS unsampled_errors,
    countIf(event_type = 'resource' AND sampled = 1) AS resources,
    countIf(event_type = 'resource' AND sampled = 1
            AND (status_code >= 400 OR attributes['failed'] = 'true')) AS resource_failures,
    countIf(event_type = 'action' AND sampled = 1) AS actions,
    countIf(event_type = 'long_task' AND sampled = 1) AS long_tasks,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id), sampled = 1
    ) AS sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), session_id),
        event_type = 'error' AND sampled = 1
    ) AS error_sessions,
    uniqCombined64StateIf(
        concat(application_id, char(31), env, char(31), view_id),
        sampled = 1 AND event_type = 'view'
            AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))
    ) AS views,
    quantileTDigestStateIf(0.75)(r.lcp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp,
    quantileTDigestStateIf(0.75)(r.inp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp,
    quantileTDigestStateIf(0.75)(r.cls, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls,
    quantileTDigestStateIf(0.75)(r.fcp, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp,
    quantileTDigestStateIf(0.75)(r.ttfb, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb,
    quantileTDigestStateIf(0.75)(r.load_ms, sampled = 1 AND event_type = 'view'
        AND (is_final = 1 OR sdk_version = '')
        AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_ms,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_lcp = 1 OR (sdk_version = '' AND r.lcp > 0))) AS lcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_inp = 1 OR (sdk_version = '' AND r.inp > 0))) AS inp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_cls = 1 OR (sdk_version = '' AND r.cls > 0))) AS cls_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_fcp = 1 OR (sdk_version = '' AND r.fcp > 0))) AS fcp_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_ttfb = 1 OR (sdk_version = '' AND r.ttfb > 0))) AS ttfb_samples,
    countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '')
            AND (has_load = 1 OR (sdk_version = '' AND r.load_ms > 0))) AS load_samples,
    __ROLLUP_BAND_COLUMNS__
FROM zenplus.apm_rum_events AS r
WHERE r.timestamp >= fromUnixTimestamp64Milli({batch_from_ms:Int64})
  AND r.timestamp <= fromUnixTimestamp64Milli({batch_to_ms:Int64})
  AND dedupe_id IN {dedupe_ids:Array(String)}
GROUP BY bucket_timestamp, application_id, env, service_version, view_name,
         browser, browser_version, os, device_type, country
"""

# Core Web Vitals rating thresholds (web.dev): value <= good is "good",
# value > poor is "poor", anything between "needs improvement".
VITAL_THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp": (2500, 4000), "inp": (200, 500), "cls": (0.1, 0.25),
    "fcp": (1800, 3000), "ttfb": (800, 1800), "load": (2500, 4000),
}
_VITAL_RAW_COLUMN = {"load": "load_ms"}


def _rollup_band_columns() -> str:
    """Good / poor sample counters per vital for the 5-minute rollup.

    Stored as plain sums next to the t-digests so the 30- and 90-day ranges
    can show the same good / needs-improvement / poor distribution as the raw
    ranges (a digest alone cannot answer "share of samples under 2.5 s").
    Column order must follow migrate-106 (appended after load_samples).
    """
    parts = []
    for name, (good, poor) in VITAL_THRESHOLDS.items():
        column = _VITAL_RAW_COLUMN.get(name, name)
        present = (
            f"sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') "
            f"AND (has_{name} = 1 OR (sdk_version = '' AND r.{column} > 0))"
        )
        parts.append(f"countIf({present} AND r.{column} <= {good}) AS {name}_good")
        parts.append(f"countIf({present} AND r.{column} > {poor}) AS {name}_poor")
    for name, _ in VITAL_THRESHOLDS.items():
        column = _VITAL_RAW_COLUMN.get(name, name)
        present = (
            f"sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') "
            f"AND (has_{name} = 1 OR (sdk_version = '' AND r.{column} > 0))"
        )
        # Samples that carry band counters (migrate-107); shares divide by this,
        # never by the digest sample count, so pre-migration buckets cannot
        # drag a distribution toward 0 % good.
        parts.append(f"countIf({present}) AS {name}_rated")
    return ",\n    ".join(parts)


_RUM_ROLLUP_INSERT_SQL = _RUM_ROLLUP_INSERT_SQL.replace("__ROLLUP_BAND_COLUMNS__", _rollup_band_columns())

_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_SAFE_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|authorization|cookie|token|api[_-]?key|session)", re.I
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CREDENTIAL = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*([^\s,;&]+)"
)
_UUID_PATH = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_LONG_ID_PATH = re.compile(r"^(?:\d+|[0-9a-fA-F]{16,})$")

# Local fallback for appliances where Redis is momentarily unavailable.  The
# distributed path below is preferred and uses the same limits.
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_SEEN_EVENTS: dict[str, float] = {}
_REDIS = None
_REDIS_DISABLED_UNTIL = 0.0
_KEY_ORIGIN_LIMIT = 60_000
_SESSION_LIMIT = 1_200
_RATE_WINDOW = 60
_HEALTH = Counter()
_HEALTH_META: dict[str, object] = {"last_event_at": None, "sdk_versions": Counter()}


@router.get("/api/v1/apm/rum/sdk.js")
async def rum_sdk():
    return PlainTextResponse(
        RUM_SDK,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=300",
            "Access-Control-Allow-Origin": "*",
            # Lets the customer's page read DNS/connect/TTFB phases for this
            # cross-origin script instead of an opaque duration-only entry.
            "Timing-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )


def _ch():
    return get_ch_client()


def _window(range_: str) -> tuple[datetime, datetime]:
    seconds = RANGE_SECONDS.get(range_)
    if seconds is None:
        raise HTTPException(400, f"Unsupported range '{range_}'")
    end = datetime.now(timezone.utc)
    return datetime.fromtimestamp(end.timestamp() - seconds, tz=timezone.utc), end


def _origin(value: str) -> str:
    """Return a canonical scheme://host[:port] origin or an empty string."""
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return ""
        default = (parsed.scheme == "http" and parsed.port in {None, 80}) or (
            parsed.scheme == "https" and parsed.port in {None, 443}
        )
        hostname = parsed.hostname.lower()
        if ":" in hostname:  # URL origins retain brackets around IPv6 literals.
            hostname = f"[{hostname}]"
        return f"{parsed.scheme}://{hostname}" + (
            "" if default else f":{parsed.port}"
        )
    except (TypeError, ValueError):
        return ""


def _assert_allowed_origin(origin: str, allowlist: list[str]) -> str:
    canonical = _origin(origin)
    allowed = {_origin(item) for item in allowlist}
    allowed.discard("")
    if not canonical or canonical not in allowed:
        raise HTTPException(403, "RUM origin is not allow-listed for this key")
    return canonical


def _cors(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


def _scrub_text(value: str, limit: int) -> str:
    value = _EMAIL.sub("[email]", str(value))
    value = _BEARER.sub("Bearer [redacted]", value)
    value = _CREDENTIAL.sub(lambda m: f"{m.group(1)}=[redacted]", value)
    return value[:limit]


def _scrub_path(path: str) -> str:
    segments = []
    for segment in path.split("/"):
        decoded = unquote(segment)
        if (_UUID_PATH.fullmatch(decoded) or _LONG_ID_PATH.fullmatch(decoded)
                or "@" in decoded or _EMAIL.search(decoded)):
            segments.append(":id")
        else:
            segments.append(segment[:255])
    return "/".join(segments)[:2048]


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value))
        path = _scrub_path(parsed.path)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port else ""
            return urlunsplit((parsed.scheme, host.lower() + port, path, "", ""))[:2048]
        return path[:2048]
    except (TypeError, ValueError):
        return ""


def _bounded_map(value: dict[str, object], *, maximum: int = 32) -> dict[str, str]:
    if len(value) > maximum:
        raise ValueError(f"at most {maximum} attributes are allowed")
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)[:128]
        if _SENSITIVE_KEY.search(key):
            continue
        cleaned[key] = _scrub_text(str(raw_value), 1024)
    return cleaned


class RumEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=12, max_length=256)
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=8, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    service_name: str = Field(default="browser", max_length=255)
    service_version: str = Field(default="", max_length=128)
    sdk_version: str = Field(default="", max_length=64)
    event_type: Literal["view", "action", "error", "resource", "long_task"] = "view"
    timestamp_ms: int | None = None
    session_id: str = Field(min_length=1, max_length=128)
    view_id: str = Field(default="", max_length=128)
    view_name: str = Field(default="/", max_length=512)
    url: str = Field(default="", max_length=2048)
    user_id: str = Field(default="", max_length=255)
    lcp: float | None = Field(default=None, ge=0, le=600000)
    inp: float | None = Field(default=None, ge=0, le=600000)
    cls: float | None = Field(default=None, ge=0, le=100)
    fcp: float | None = Field(default=None, ge=0, le=600000)
    ttfb: float | None = Field(default=None, ge=0, le=600000)
    load_ms: float | None = Field(default=None, ge=0, le=600000)
    is_final: bool = False
    end_reason: str = Field(default="", max_length=64)
    sample_rate: float = Field(default=1, ge=0, le=1)
    sampled: bool = True
    action_name: str = Field(default="", max_length=512)
    action_type: str = Field(default="", max_length=64)
    target: str = Field(default="", max_length=512)
    duration_ms: float = Field(default=0, ge=0, le=3_600_000)
    resource_url: str = Field(default="", max_length=2048)
    resource_type: str = Field(default="", max_length=64)
    method: str = Field(default="", max_length=16)
    status_code: int = Field(default=0, ge=0, le=599)
    transfer_size: int = Field(default=0, ge=0, le=10_000_000_000)
    encoded_body_size: int = Field(default=0, ge=0, le=10_000_000_000)
    error_message: str = Field(default="", max_length=4096)
    error_type: str = Field(default="", max_length=255)
    error_stack: str = Field(default="", max_length=16384)
    error_source: str = Field(default="", max_length=2048)
    error_fingerprint: str = Field(default="", max_length=128)
    backend_trace_id: str = Field(default="", max_length=32)
    attributes: dict[str, str] = Field(default_factory=dict)
    vital_attribution: dict[str, str] = Field(default_factory=dict)
    # Request phase breakdown (Navigation/Resource Timing, milliseconds).
    redirect_ms: float = Field(default=0, ge=0, le=600_000)
    dns_ms: float = Field(default=0, ge=0, le=600_000)
    connect_ms: float = Field(default=0, ge=0, le=600_000)
    tls_ms: float = Field(default=0, ge=0, le=600_000)
    wait_ms: float = Field(default=0, ge=0, le=600_000)
    download_ms: float = Field(default=0, ge=0, le=600_000)
    blocked_ms: float = Field(default=0, ge=0, le=600_000)
    processing_ms: float = Field(default=0, ge=0, le=600_000)
    # Server-declared execution split from Server-Timing response headers.
    server_ms: float = Field(default=0, ge=0, le=600_000)
    db_ms: float = Field(default=0, ge=0, le=600_000)
    has_timing: bool = False
    has_server_timing: bool = False
    protocol: str = Field(default="", max_length=32)
    # Client environment context (sent on view events).
    connection_type: str = Field(default="", max_length=32)
    connection_rtt_ms: float = Field(default=0, ge=0, le=600_000)
    connection_downlink: float = Field(default=0, ge=0, le=100_000)
    language: str = Field(default="", max_length=35)
    timezone: str = Field(default="", max_length=64)
    screen_res: str = Field(default="", max_length=32)
    viewport: str = Field(default="", max_length=32)

    @field_validator("event_id")
    @classmethod
    def valid_event_id(cls, value: str) -> str:
        if not _EVENT_ID.fullmatch(value):
            raise ValueError("event_id contains unsupported characters")
        return value

    @field_validator("backend_trace_id")
    @classmethod
    def valid_trace_id(cls, value: str) -> str:
        if value and not _TRACE_ID.fullmatch(value):
            raise ValueError("backend_trace_id must be 32 hexadecimal characters")
        return value.lower()

    @field_validator("attributes", "vital_attribution")
    @classmethod
    def bounded_attributes(cls, value: dict[str, object]) -> dict[str, str]:
        return _bounded_map(value)

    @field_validator("url", "resource_url")
    @classmethod
    def strip_url_secrets(cls, value: str) -> str:
        return _safe_url(value)

    @field_validator("view_name")
    @classmethod
    def normalize_view_name(cls, value: str) -> str:
        return _scrub_path(urlsplit(value).path or "/")[:512]

    @field_validator("error_message", "error_stack", "error_source")
    @classmethod
    def scrub_error_text(cls, value: str, info) -> str:
        limits = {"error_message": 4096, "error_stack": 16384, "error_source": 2048}
        return _scrub_text(value, limits[info.field_name])

    @field_validator("action_name", "target")
    @classmethod
    def scrub_action_text(cls, value: str, info) -> str:
        return _scrub_text(value, 512)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()


class RumBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[RumEvent] = Field(min_length=1, max_length=MAX_BATCH_EVENTS)


async def _parse_payload(request: Request) -> list[RumEvent]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > MAX_BODY_BYTES:
                raise HTTPException(413, f"RUM payload exceeds {MAX_BODY_BYTES} bytes")
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length") from exc
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise HTTPException(413, f"RUM payload exceeds {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "events" in data:
            return RumBatch.model_validate(data).events
        return [RumEvent.model_validate(data)]
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        _HEALTH["rejected"] += 1
        raise HTTPException(422, "Invalid or unsupported RUM payload") from exc


def _fallback_rate_limit(key: str, amount: int, limit: int) -> None:
    now = time.monotonic()
    if len(_RATE_BUCKETS) > 10_000:
        cutoff = now - _RATE_WINDOW
        for bucket_key, values in list(_RATE_BUCKETS.items()):
            while values and values[0] <= cutoff:
                values.popleft()
            if not values:
                _RATE_BUCKETS.pop(bucket_key, None)
        # Keep the process fallback bounded even under attacker-controlled
        # session churn while Redis is unavailable.
        while len(_RATE_BUCKETS) > 8_000:
            _RATE_BUCKETS.pop(next(iter(_RATE_BUCKETS)), None)
    bucket = _RATE_BUCKETS[key]
    cutoff = now - _RATE_WINDOW
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) + amount > limit:
        raise HTTPException(429, "RUM event quota exceeded", headers={"Retry-After": "60"})
    bucket.extend([now] * amount)


async def _redis_client():
    global _REDIS, _REDIS_DISABLED_UNTIL
    if time.monotonic() < _REDIS_DISABLED_UNTIL:
        return None
    if _REDIS is None:
        try:
            import redis.asyncio as redis
            _REDIS = redis.from_url(
                get_settings().REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=0.15,
                socket_timeout=0.15,
            )
        except Exception:
            _REDIS_DISABLED_UNTIL = time.monotonic() + 30
            return None
    return _REDIS


_SHARED_QUOTA_SCRIPT = """
local a = redis.call('INCRBY', KEYS[1], ARGV[1])
if a == tonumber(ARGV[1]) then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
local b = redis.call('INCRBY', KEYS[2], ARGV[1])
if b == tonumber(ARGV[1]) then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
return {a, b}
"""

_SESSION_QUOTA_SCRIPT = """
local n = redis.call('INCRBY', KEYS[1], ARGV[1])
if n == tonumber(ARGV[1]) then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return n
"""


async def _enforce_quota(
    key_id: object, origin: str, client_ip: str,
    session_counts: dict[str, int], amount: int,
) -> None:
    origin_hash = hashlib.sha256(origin.encode()).hexdigest()[:16]
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:24]
    client = await _redis_client()
    if client is not None:
        try:
            shared = await client.eval(
                _SHARED_QUOTA_SCRIPT,
                2,
                f"zp:rum:q:k:{key_id}:{origin_hash}",
                f"zp:rum:q:i:{key_id}:{ip_hash}",
                amount,
                _RATE_WINDOW + 2,
            )
            if int(shared[0]) > _KEY_ORIGIN_LIMIT or int(shared[1]) > 10_000:
                raise HTTPException(429, "RUM event quota exceeded", headers={"Retry-After": "60"})
            for session_id, session_amount in session_counts.items():
                session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:24]
                count = await client.eval(
                    _SESSION_QUOTA_SCRIPT,
                    1,
                    f"zp:rum:q:s:{key_id}:{session_hash}",
                    session_amount,
                    _RATE_WINDOW + 2,
                )
                if int(count) > _SESSION_LIMIT:
                    raise HTTPException(429, "RUM event quota exceeded", headers={"Retry-After": "60"})
            return
        except HTTPException:
            raise
        except Exception:
            global _REDIS_DISABLED_UNTIL
            _REDIS_DISABLED_UNTIL = time.monotonic() + 30
    _fallback_rate_limit(f"key:{key_id}:{origin}", amount, _KEY_ORIGIN_LIMIT)
    _fallback_rate_limit(f"ip:{key_id}:{ip_hash}", amount, 10_000)
    for session_id, session_amount in session_counts.items():
        _fallback_rate_limit(
            f"session:{key_id}:{hashlib.sha256(session_id.encode()).hexdigest()[:24]}",
            session_amount,
            _SESSION_LIMIT,
        )


def _dedupe_identity(key_id: object, event: RumEvent) -> str:
    if event.event_type == "view" and event.is_final and event.view_id:
        identity = f"final:{event.application_id}:{event.view_id}"
    else:
        identity = f"event:{event.event_id}"
    return f"{key_id}:{identity}"


async def _dedupe(key_id: object, events: list[RumEvent]) -> list[RumEvent]:
    global _REDIS_DISABLED_UNTIL
    client = await _redis_client()
    if client is not None:
        try:
            bucket = int(time.time() // 86_400)
            current_key = f"zp:rum:d:{key_id}:{bucket}"
            previous_key = f"zp:rum:d:{key_id}:{bucket - 1}"
            reserve_script = """
                if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 or
                   redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then
                    return 0
                end
                redis.call('SADD', KEYS[1], ARGV[1])
                redis.call('EXPIRE', KEYS[1], 172800)
                return 1
            """
            pipe = client.pipeline(transaction=False)
            for event in events:
                pipe.eval(
                    reserve_script, 2, current_key, previous_key,
                    _dedupe_identity(key_id, event),
                )
            fresh = await pipe.execute()
            return [event for event, inserted in zip(events, fresh) if inserted]
        except Exception:
            _REDIS_DISABLED_UNTIL = time.monotonic() + 30

    now = time.monotonic()
    if len(_SEEN_EVENTS) > 50_000:
        expired = [event_id for event_id, expiry in _SEEN_EVENTS.items() if expiry <= now]
        for event_id in expired:
            _SEEN_EVENTS.pop(event_id, None)
        if len(_SEEN_EVENTS) > 50_000:
            for event_id in list(_SEEN_EVENTS)[:10_000]:
                _SEEN_EVENTS.pop(event_id, None)
    result = []
    for event in events:
        cache_key = _dedupe_identity(key_id, event)
        if _SEEN_EVENTS.get(cache_key, 0) > now:
            continue
        _SEEN_EVENTS[cache_key] = now + 86400
        result.append(event)
    return result


async def _release_dedupe(key_id: object, events: list[RumEvent]) -> None:
    """Release reservations after a storage failure so the browser can retry."""
    global _REDIS_DISABLED_UNTIL
    client = await _redis_client()
    if client is not None:
        try:
            if events:
                bucket = int(time.time() // 86_400)
                identities = [_dedupe_identity(key_id, event) for event in events]
                pipe = client.pipeline(transaction=False)
                pipe.srem(f"zp:rum:d:{key_id}:{bucket}", *identities)
                pipe.srem(f"zp:rum:d:{key_id}:{bucket - 1}", *identities)
                await pipe.execute()
        except Exception:
            _REDIS_DISABLED_UNTIL = time.monotonic() + 30
    for event in events:
        _SEEN_EVENTS.pop(_dedupe_identity(key_id, event), None)


def _pseudonymize_user(value: str) -> str:
    if not value:
        return ""
    digest = hmac.new(
        get_settings().JWT_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"usr_{digest[:32]}"


def _user_agent(value: str) -> tuple[str, str, str, str]:
    browser, version = "Other", ""
    for name, pattern in (
        ("Edge", r"Edg/([\d.]+)"),
        ("Chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
        ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
        ("Safari", r"Version/([\d.]+).+Safari/"),
    ):
        match = re.search(pattern, value)
        if match:
            browser, version = name, match.group(1)[:32]
            break
    if re.search(r"iPhone|iPad|iPod", value, re.I):
        os_name = "iOS"
    elif re.search(r"Android", value, re.I):
        os_name = "Android"
    elif re.search(r"Windows", value, re.I):
        os_name = "Windows"
    elif re.search(r"Macintosh|Mac OS X", value, re.I):
        os_name = "macOS"
    elif re.search(r"Linux", value, re.I):
        os_name = "Linux"
    else:
        os_name = "Other"
    device = "mobile" if re.search(r"Mobile|Android|iPhone", value, re.I) else (
        "tablet" if re.search(r"iPad|Tablet", value, re.I) else "desktop"
    )
    return browser, version, os_name, device


def _client_ip(request: Request) -> str:
    """Best-effort real client IP behind the reverse proxy.

    nginx forwards the caller in X-Forwarded-For / X-Real-IP; the left-most
    X-Forwarded-For hop is the browser.  Falls back to the socket peer.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:45]
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip[:45]
    return (request.client.host if request.client else "")[:45]


def _resolve_country(request: Request, client_ip: str) -> str:
    """ISO 3166-1 alpha-2 country for the visitor.

    A CDN in front of the site is authoritative (Cloudflare's CF-IPCountry or a
    generic X-Country-Code); otherwise the client address is resolved against
    the on-box DB-IP database shared with NetFlow. Private and reserved
    addresses (lab traffic, TEST-NET) have no country and stay blank.
    """
    raw_country = request.headers.get("cf-ipcountry") or request.headers.get("x-country-code") or ""
    if _SAFE_COUNTRY.fullmatch(raw_country):
        return raw_country.upper()
    if not client_ip:
        return ""
    iso, _name = geoip.country_of(client_ip)
    return iso.upper() if iso and _SAFE_COUNTRY.fullmatch(iso) else ""


def _row(event: RumEvent, key: dict, request: Request) -> list[object]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    event_ms = event.timestamp_ms or now_ms
    if abs(event_ms - now_ms) > 86_400_000:
        raise HTTPException(400, "RUM event timestamp is outside the accepted 24-hour window")
    timestamp = datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
    browser, browser_version, os_name, device = _user_agent(
        request.headers.get("user-agent", "")[:512]
    )
    client_ip = _client_ip(request)
    country = _resolve_country(request, client_ip)
    return [
        timestamp, event.application_id, event.service_name,
        key.get("env_name") or "prod", event.event_type, event.session_id,
        event.view_id, event.view_name, event.url, _pseudonymize_user(event.user_id), country,
        browser, device, event.lcp or 0, event.inp or 0,
        event.cls if event.cls is not None else 0, event.fcp or 0,
        event.ttfb or 0, event.load_ms or 0, event.error_message,
        event.backend_trace_id, event.attributes, int(event_ms / 1000 // 300 * 300),
        event.event_id, event.sdk_version, event.service_version,
        browser_version, os_name, event.action_name, event.action_type,
        event.target, event.duration_ms, event.resource_url, event.resource_type,
        event.method, event.status_code, event.transfer_size,
        event.encoded_body_size, event.error_type, event.error_stack,
        event.error_source, event.error_fingerprint, event.end_reason,
        int(event.is_final), event.sample_rate, event.vital_attribution,
        int(event.lcp is not None), int(event.inp is not None),
        int(event.cls is not None), int(event.fcp is not None),
        int(event.ttfb is not None), int(event.load_ms is not None),
        int(event.sampled),
        client_ip,
        event.redirect_ms, event.dns_ms, event.connect_ms, event.tls_ms,
        event.wait_ms, event.download_ms, event.blocked_ms, event.processing_ms,
        event.server_ms, event.db_ms, int(event.has_timing),
        int(event.has_server_timing), event.protocol, event.connection_type,
        event.connection_rtt_ms, event.connection_downlink, event.language,
        event.timezone, event.screen_res, event.viewport,
        "",  # stable per-key semantic identity is attached after deduplication
    ]


@router.options("/api/v1/apm/rum/ingest")
async def rum_preflight(request: Request, db: AsyncSession = Depends(get_db)):
    origin = request.headers.get("origin", "")
    try:
        token = request.query_params.get("key", "")
        key = await authenticate_ingest_key(token, db, kind="rum")
        if not key.get("application_id"):
            raise HTTPException(403, "Legacy RUM key must be replaced with an application-bound key")
        allowed = _assert_allowed_origin(origin, list(key.get("origin_allowlist") or []))
        return Response(status_code=204, headers=_cors(allowed))
    except HTTPException as exc:
        canonical = _origin(origin)
        if canonical:
            exc.headers = {**_cors(canonical), **(exc.headers or {})}
        raise


@router.post("/api/v1/apm/rum/ingest")
async def ingest_rum(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        return await _ingest_rum(request, db)
    except HTTPException as exc:
        # Browsers must be able to observe terminal validation/auth/quota errors;
        # otherwise the Fetch API exposes an opaque network failure and an SDK
        # cannot distinguish a permanent 4xx from a retryable outage.
        canonical = _origin(request.headers.get("origin", ""))
        if canonical:
            exc.headers = {**_cors(canonical), **(exc.headers or {})}
        raise


async def _ingest_rum(request: Request, db: AsyncSession):
    events = await _parse_payload(request)
    token = events[0].client_token
    if any(not hmac.compare_digest(event.client_token, token) for event in events[1:]):
        _HEALTH["rejected"] += len(events)
        raise HTTPException(400, "A RUM batch must use one client token")
    key = await authenticate_ingest_key(token, db, kind="rum")
    if not key.get("application_id"):
        _HEALTH["rejected"] += len(events)
        raise HTTPException(403, "Legacy RUM key must be replaced with an application-bound key")
    origin = _assert_allowed_origin(
        request.headers.get("origin", ""), list(key.get("origin_allowlist") or [])
    )
    bound_app = key.get("application_id")
    if bound_app and any(event.application_id != bound_app for event in events):
        _HEALTH["rejected"] += len(events)
        raise HTTPException(403, "RUM key is bound to a different application")
    # Fold URLs into routes per the key's grouping rules. Runs after the
    # validator's identifier scrubbing, so rules see "/orders/:id" not "/orders/42".
    route_rules = rules_from_options(key.get("rum_options"))
    if route_rules:
        for event in events:
            event.view_name = apply_route_rules(event.view_name, route_rules)
    try:
        client_ip = _client_ip(request) or "unknown"
        await _enforce_quota(
            key["id"], origin, client_ip,
            dict(Counter(event.session_id for event in events)), len(events),
        )
    except HTTPException:
        _HEALTH["rate_limited"] += len(events)
        raise
    # Validate timestamps and construct every row before reserving event IDs.
    # A single bad event must never make valid retry IDs look accepted.
    prepared = {id(event): _row(event, key, request) for event in events}
    fresh = await _dedupe(key["id"], events)
    duplicates = len(events) - len(fresh)
    _HEALTH["duplicates"] += duplicates
    if fresh:
        rows = [prepared[id(event)] for event in fresh]
        identities = [_dedupe_identity(key["id"], event) for event in fresh]
        dedupe_ids = [hashlib.sha256(identity.encode()).hexdigest() for identity in identities]
        for row, dedupe_id in zip(rows, dedupe_ids, strict=True):
            row[-1] = dedupe_id
        token_material = "\n".join(sorted(identities))
        insert_token = "rum-" + hashlib.sha256(token_material.encode()).hexdigest()

        def store_batch() -> None:
            # ClickHouse 24.x cannot safely deduplicate an async insert through
            # a dependent materialized view.  Write both tables synchronously,
            # with independent stable tokens.  If the raw write succeeds and
            # the rollup write fails, an identical retry is harmless and still
            # completes the missing rollup write.
            client = _ch()
            client.insert(
                "apm_rum_events",
                rows,
                column_names=RUM_COLUMNS,
                database="zenplus",
                settings={
                    "insert_deduplicate": 1,
                    "insert_deduplication_token": insert_token,
                },
            )
            client.command(
                _RUM_ROLLUP_INSERT_SQL,
                parameters={
                    "dedupe_ids": dedupe_ids,
                    # clickhouse-connect 0.8 formats a bare datetime query
                    # parameter at whole-second precision, even for
                    # DateTime64(3).  Integer epoch milliseconds preserve the
                    # exact raw-event bounds for single-event batches.
                    "batch_from_ms": min(
                        int(row[0].timestamp() * 1000) for row in rows
                    ),
                    "batch_to_ms": max(
                        int(row[0].timestamp() * 1000) for row in rows
                    ),
                },
                settings={
                    "insert_deduplicate": 1,
                    "insert_deduplication_token": "rollup-" + insert_token,
                },
            )

        try:
            await asyncio.to_thread(store_batch)
        except Exception as exc:
            await _release_dedupe(key["id"], fresh)
            _HEALTH["storage_errors"] += len(fresh)
            raise HTTPException(
                503,
                "RUM storage is temporarily unavailable",
                headers={"Retry-After": "1", **_cors(origin)},
            ) from exc
        _HEALTH["accepted"] += len(fresh)
        _HEALTH["batches"] += 1
        _HEALTH_META["last_event_at"] = datetime.now(timezone.utc)
        versions: Counter = _HEALTH_META["sdk_versions"]  # type: ignore[assignment]
        versions.update(event.sdk_version or "legacy" for event in fresh)
    headers = {**_cors(origin), "X-RUM-Accepted": str(len(fresh)), "X-RUM-Duplicate": str(duplicates)}
    return Response(status_code=202, headers=headers)


# ── authenticated analytics ─────────────────────────────────────────────────


RangeName = Literal["15m", "1h", "6h", "24h", "7d", "30d", "90d", "custom"]
OrderName = Literal["asc", "desc"]

# Bucket widths for custom windows: the widest preset step that still yields
# at least ~40 points over the span, so charts stay readable at any length.
_CUSTOM_BUCKET_STEPS = (60, 300, 900, 1800, 3600, 21600, 86400)
MAX_CUSTOM_WINDOW_SECONDS = 90 * 86_400
RAW_RETENTION_SECONDS = 14 * 86_400


@dataclass(frozen=True)
class RumWindow:
    """The time span every analytics query is scoped to.

    Either a preset (``range=7d``: the trailing seven days) or an absolute
    ``from``/``to`` pair. Windows longer than the raw-event retention read the
    5-minute rollup, exactly like the 30d / 90d presets always did.
    """
    range: str
    frm: datetime
    to: datetime

    @property
    def seconds(self) -> float:
        return max(1.0, (self.to - self.frm).total_seconds())

    @property
    def rollup(self) -> bool:
        return self.range in {"30d", "90d"} or (self.range == "custom" and self.seconds > RAW_RETENTION_SECONDS)

    @property
    def bucket_seconds(self) -> int:
        if self.range in RANGE_BUCKET_SECONDS:
            return RANGE_BUCKET_SECONDS[self.range]
        target = self.seconds / 40
        for step in _CUSTOM_BUCKET_STEPS:
            if step >= target:
                return step
        return _CUSTOM_BUCKET_STEPS[-1]

    def previous(self) -> "RumWindow":
        """The window of equal length immediately before this one."""
        span = self.to - self.frm
        return RumWindow(self.range, self.frm - span, self.frm)

    def payload(self) -> dict[str, object]:
        return {
            "range": self.range, "from": self.frm, "to": self.to,
            "seconds": int(self.seconds), "bucket_seconds": self.bucket_seconds,
            "rollup": self.rollup,
        }


def _parse_timestamp(value: str, name: str) -> datetime:
    text_value = value.strip()
    try:
        if re.fullmatch(r"\d{10,13}", text_value):
            number = int(text_value)
            return datetime.fromtimestamp(number / 1000 if number > 10**11 else number, tz=timezone.utc)
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"'{name}' must be an ISO 8601 timestamp or epoch milliseconds") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _str_param(value: object) -> str | None:
    """Query parameters called directly (tests, the legacy summary) arrive as
    FastAPI FieldInfo defaults rather than None; only real strings count."""
    return value if isinstance(value, str) and value != "" else None


def _resolve_window(range_: str, frm: str | None = None, to: str | None = None) -> RumWindow:
    """Turn the ``range`` / ``from`` / ``to`` query parameters into a RumWindow.

    Absolute bounds win whenever both are supplied (the UI sends
    ``range=custom`` with them); a preset alone is the trailing window.
    """
    frm, to = _str_param(frm), _str_param(to)
    if frm or to:
        if not (frm and to):
            raise HTTPException(400, "Custom windows need both 'from' and 'to'")
        start, end = _parse_timestamp(frm, "from"), _parse_timestamp(to, "to")
        now = datetime.now(timezone.utc)
        end = min(end, now)
        if end <= start:
            raise HTTPException(400, "'to' must be after 'from'")
        if (end - start).total_seconds() > MAX_CUSTOM_WINDOW_SECONDS:
            raise HTTPException(400, "Custom windows are limited to 90 days")
        if (now - start).total_seconds() > MAX_CUSTOM_WINDOW_SECONDS + 86_400:
            raise HTTPException(400, "RUM data is retained for 90 days")
        return RumWindow("custom", start, end)
    if range_ == "custom":
        raise HTTPException(400, "range=custom requires 'from' and 'to'")
    start, end = _window(range_)
    return RumWindow(range_, start, end)




# Columns searched by the free-text `q` filter, in the order a person would
# expect a hit: the identifiers they copied from somewhere, then page and
# request URLs, then error text.
_SEARCH_COLUMNS = ("session_id", "user_id", "view_name", "url", "resource_url", "error_message", "action_name")


def _scope(
    range_: str | RumWindow,
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    *,
    q: str | None = None,
    user_id: str | None = None,
) -> tuple[dict[str, object], str]:
    window = range_ if isinstance(range_, RumWindow) else _resolve_window(range_)
    frm, to = window.frm, window.to
    params: dict[str, object] = {"frm": frm, "to": to}
    clauses = ["timestamp >= {frm:DateTime64(3)}", "timestamp < {to:DateTime64(3)}"]
    user_id, q = _str_param(user_id), _str_param(q)
    if user_id:
        params["user_id"] = user_id
        clauses.append("user_id = {user_id:String}")
    needle = (q or "").strip()[:200]
    if needle:
        params["q"] = needle
        clauses.append("(" + " OR ".join(
            f"positionCaseInsensitiveUTF8({column}, {{q:String}}) > 0" for column in _SEARCH_COLUMNS
        ) + ")")
    for column, value in (
        ("application_id", application_id),
        ("env", env),
        ("view_name", view_name),
        ("browser", browser),
        ("device_type", device_type),
        ("country", country.upper() if country else None),
        ("service_version", service_version),
        ("browser_version", browser_version),
        ("os", os),
        ("client_ip", client_ip),
    ):
        if value is not None and value != "":
            params[column] = value
            clauses.append(f"{column} = {{{column}:String}}")
    return params, " AND ".join(clauses)


# Grouping expressions shared by the explorers and the overview tab counts, so
# "Displaying N issues" and the Errors tab badge are computed the same way.
_ERROR_FINGERPRINT_SQL = (
    "if(error_fingerprint != '', error_fingerprint, "
    "lower(hex(MD5(concat(error_type, ':', error_message)))))"
)
_ACTION_NAME_SQL = "if(event_type = 'long_task', 'Long task', action_name)"
_ACTION_TYPE_SQL = "if(event_type = 'long_task', 'long_task', action_type)"


def _filters_payload(**values) -> dict[str, str]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _raw_coverage(window: RumWindow | str) -> dict[str, object]:
    partial = window.rollup if isinstance(window, RumWindow) else window in {"30d", "90d"}
    return {
        "raw_retention_days": 14,
        "rollup_retention_days": 90,
        "partial": partial,
        "message": (
            "Event-level drill-down is limited to the most recent 14 days."
            if partial else None
        ),
    }


def _nullable(value: object, samples: object) -> float | None:
    count = int(samples or 0)
    if count <= 0 or value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _vital(value: object, samples: object, good: object, poor: object) -> dict[str, object]:
    count = int(samples or 0)
    return {
        "p75": _nullable(value, count),
        "samples": count,
        "good_pct": (float(good or 0) / count * 100) if count else None,
        "needs_improvement_pct": (
            max(count - int(good or 0) - int(poor or 0), 0) / count * 100
            if count else None
        ),
        "poor_pct": (float(poor or 0) / count * 100) if count else None,
    }


_VITAL_NAMES = ("lcp", "inp", "cls", "fcp", "ttfb", "load")
_VITAL_COLUMNS = {"load": "load_ms"}


def _per_view_vitals_sql(
    scope_sql: str, group_by: str = "application_id, env, view_id", extra_select: str = "",
) -> str:
    """One row per page view with its finalized Web Vitals.

    Collapses the view lifecycle (view_start, checkpoints, pagehide, final) —
    and legacy clients that re-sent a finalized record under a new event_id —
    into a single sample per view_id, so every aggregate that reads from it
    counts each navigation exactly once. `group_by` may carry extra grouping
    columns (view_name, service_version, …) for the callers that need them.
    """
    selects = []
    for name in _VITAL_NAMES:
        column = _VITAL_COLUMNS.get(name, name)
        present = f"raw.final_view AND (raw.has_{name} = 1 OR (raw.sdk_version = '' AND raw.{column} > 0))"
        selects.append(f"argMaxIf(raw.{column}, raw.timestamp, {present}) AS {name}_value")
        selects.append(f"max({present}) AS {name}_present")
    if extra_select:
        selects.append(extra_select)
    joined = ",\n                ".join(selects)
    return f"""
            SELECT {group_by},
                {joined}
            FROM (
                SELECT *, event_type = 'view' AND (is_final = 1 OR sdk_version = '') AS final_view
                FROM zenplus.apm_rum_events
                WHERE {scope_sql} AND view_id != '' AND sampled = 1
            ) AS raw
            GROUP BY {group_by}
    """


def _vitals_query(scope_sql: str) -> str:
    # The nested query enforces one sample per view even for legacy clients that
    # retried a finalized record with a different event_id.
    return f"""
        SELECT
            quantileTDigestIf(0.75)(lcp_value, lcp_present = 1), countIf(lcp_present = 1),
            countIf(lcp_present = 1 AND lcp_value <= 2500), countIf(lcp_present = 1 AND lcp_value > 4000),
            quantileTDigestIf(0.75)(inp_value, inp_present = 1), countIf(inp_present = 1),
            countIf(inp_present = 1 AND inp_value <= 200), countIf(inp_present = 1 AND inp_value > 500),
            quantileTDigestIf(0.75)(cls_value, cls_present = 1), countIf(cls_present = 1),
            countIf(cls_present = 1 AND cls_value <= 0.1), countIf(cls_present = 1 AND cls_value > 0.25),
            quantileTDigestIf(0.75)(fcp_value, fcp_present = 1), countIf(fcp_present = 1),
            countIf(fcp_present = 1 AND fcp_value <= 1800), countIf(fcp_present = 1 AND fcp_value > 3000),
            quantileTDigestIf(0.75)(ttfb_value, ttfb_present = 1), countIf(ttfb_present = 1),
            countIf(ttfb_present = 1 AND ttfb_value <= 800), countIf(ttfb_present = 1 AND ttfb_value > 1800),
            quantileTDigestIf(0.75)(load_value, load_present = 1), countIf(load_present = 1),
            countIf(load_present = 1 AND load_value <= 2500), countIf(load_present = 1 AND load_value > 4000)
        FROM (
            {_per_view_vitals_sql(scope_sql)}
        )
    """


def _per_route_vitals_sql(scope_sql: str) -> str:
    """Per (application, env, view_name) p75 + sample counts, one sample per view_id."""
    selects = ", ".join(
        f"quantileTDigestIf(0.75)({name}_value, {name}_present = 1) AS {name}_p75, "
        f"countIf({name}_present = 1) AS {name}_samples"
        for name in _VITAL_NAMES
    )
    return f"""
        SELECT application_id, env, view_name, {selects}
        FROM (
            {_per_view_vitals_sql(scope_sql, "application_id, env, view_name, view_id")}
        )
        GROUP BY application_id, env, view_name
    """


def _vitals_payload(row: list | tuple) -> dict[str, dict[str, object]]:
    return {
        "lcp": _vital(row[0], row[1], row[2], row[3]),
        "inp": _vital(row[4], row[5], row[6], row[7]),
        "cls": _vital(row[8], row[9], row[10], row[11]),
        "fcp": _vital(row[12], row[13], row[14], row[15]),
        "ttfb": _vital(row[16], row[17], row[18], row[19]),
        "load": _vital(row[20], row[21], row[22], row[23]),
    }


def _rollup_vitals_payload(row: list | tuple) -> dict[str, dict[str, object]]:
    """Rollup vitals: 12 digest/sample pairs, then (good, poor, rated) per vital.

    Shares are computed over the *rated* samples — those written with band
    counters (migrate-106/107) — so buckets from before the migration show
    p75 alone instead of a fabricated 0 % good.
    """
    result = {}
    names = tuple(VITAL_THRESHOLDS)
    for index, name in enumerate(names):
        value, samples = row[index * 2], int(row[index * 2 + 1] or 0)
        band_offset = len(names) * 2 + index * 3
        good = int(row[band_offset] or 0) if len(row) > band_offset + 2 else 0
        poor = int(row[band_offset + 1] or 0) if len(row) > band_offset + 2 else 0
        rated = int(row[band_offset + 2] or 0) if len(row) > band_offset + 2 else 0
        entry = {"p75": _nullable(value, samples), "samples": samples}
        if rated:
            entry.update(_band_pcts(rated, good, poor))
            entry["rated_samples"] = rated
        else:
            entry.update({"good_pct": None, "needs_improvement_pct": None, "poor_pct": None})
        result[name] = entry
    return result


@router.get("/api/v1/apm/rum/overview")
async def rum_overview(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    compare: bool = Query(default=False),
    _user=Depends(get_current_user),
):
    window = _resolve_window(range_, frm, to)
    filters = dict(
        application_id=application_id, env=env, view_name=view_name, browser=browser,
        device_type=device_type, country=country, service_version=service_version,
        browser_version=browser_version, os=os, client_ip=client_ip,
    )

    def query(window: RumWindow):
        params, scope_sql = _scope(window, q=q, user_id=user_id, **filters)
        if window.rollup:
            totals = _ch().query(f"""
                SELECT sum(events), uniqCombined64Merge(sessions),
                       uniqCombined64Merge(views), sum(errors),
                       uniqCombined64Merge(error_sessions), sum(resources),
                       sum(actions), sum(long_tasks), sum(resource_failures),
                       sum(sampled_errors), sum(unsampled_errors)
                FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql}
            """, parameters=params).result_rows[0]
            band_sums = ", ".join(
                f"sum({name}_good), sum({name}_poor), sum({name}_rated)" for name in VITAL_THRESHOLDS
            )
            vitals = _ch().query(f"""
                SELECT quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                       quantileTDigestMerge(0.75)(inp), sum(inp_samples),
                       quantileTDigestMerge(0.75)(cls), sum(cls_samples),
                       quantileTDigestMerge(0.75)(fcp), sum(fcp_samples),
                       quantileTDigestMerge(0.75)(ttfb), sum(ttfb_samples),
                       quantileTDigestMerge(0.75)(load_ms), sum(load_samples),
                       {band_sums}
                FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql}
            """, parameters=params).result_rows[0]
            releases = _ch().query(f"""
                SELECT service_version, uniqCombined64Merge(sessions) AS sessions,
                       uniqCombined64Merge(views) AS views, sum(sampled_errors) AS errors,
                       uniqCombined64Merge(error_sessions) / greatest(sessions, 1) AS error_session_rate,
                       quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                       quantileTDigestMerge(0.75)(inp), sum(inp_samples),
                       quantileTDigestMerge(0.75)(cls), sum(cls_samples), max(timestamp),
                       min(timestamp)
                FROM zenplus.apm_rum_metrics_5m
                WHERE {scope_sql} AND service_version != ''
                GROUP BY service_version ORDER BY max(timestamp) DESC LIMIT 20
            """, parameters=params).result_rows
            return totals, vitals, releases
        totals = _ch().query(f"""
            SELECT count(),
                   uniqExactIf((application_id, env, session_id), sampled = 1),
                   uniqExactIf((application_id, env, view_id), event_type = 'view' AND sampled = 1),
                   countIf(event_type = 'error'),
                   uniqExactIf((application_id, env, session_id), event_type = 'error' AND sampled = 1),
                   countIf(event_type = 'resource' AND sampled = 1),
                   countIf(event_type = 'action' AND sampled = 1),
                   countIf(event_type = 'long_task' AND sampled = 1),
                   countIf(event_type = 'resource' AND sampled = 1 AND (status_code >= 400 OR attributes['failed'] = 'true')),
                   countIf(event_type = 'error' AND sampled = 1),
                   countIf(event_type = 'error' AND sampled = 0)
            FROM zenplus.apm_rum_events WHERE {scope_sql}
        """, parameters=params).result_rows[0]
        vitals = _ch().query(_vitals_query(scope_sql), parameters=params).result_rows[0]
        releases = _ch().query(f"""
            SELECT service_version,
                   uniqExactIf((application_id, env, session_id), sampled = 1) AS sessions,
                   uniqExactIf((application_id, env, view_id), event_type = 'view' AND sampled = 1) AS views,
                   countIf(event_type = 'error' AND sampled = 1) AS errors,
                   uniqExactIf(session_id, event_type = 'error' AND sampled = 1) / greatest(sessions, 1) AS error_session_rate,
                   quantileTDigestIf(0.75)(lcp, sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_lcp = 1),
                   countIf(sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_lcp = 1),
                   quantileTDigestIf(0.75)(inp, sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_inp = 1),
                   countIf(sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_inp = 1),
                   quantileTDigestIf(0.75)(cls, sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_cls = 1),
                   countIf(sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_cls = 1), max(timestamp),
                   min(timestamp)
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND service_version != ''
            GROUP BY service_version ORDER BY max(timestamp) DESC LIMIT 20
        """, parameters=params).result_rows
        return totals, vitals, releases

    def explorer_query():
        # Row counts of the explorer tabs (routes, sessions, error groups, …).
        # Always read from raw events because that is what the explorers list,
        # so the tab badges agree with "Displaying N" even on rollup ranges.
        params, scope_sql = _scope(window, q=q, user_id=user_id, **filters)
        return _ch().query(f"""
            SELECT uniqExactIf((application_id, env, view_name), sampled = 1),
                   uniqExactIf((application_id, env, session_id), sampled = 1),
                   uniqExactIf((application_id, env, {_ERROR_FINGERPRINT_SQL}), event_type = 'error'),
                   uniqExactIf(
                       (application_id, env, view_name, resource_url, resource_type, method, status_code),
                       event_type = 'resource' AND sampled = 1
                   ),
                   uniqExactIf(
                       (application_id, env, view_name, {_ACTION_NAME_SQL}, {_ACTION_TYPE_SQL}, target),
                       event_type IN ('action', 'long_task') AND sampled = 1
                   )
            FROM zenplus.apm_rum_events WHERE {scope_sql}
        """, parameters=params).result_rows[0]

    def totals_payload(window: RumWindow, totals, vital_row) -> dict[str, object]:
        sessions = int(totals[1])
        return {
            "totals": {
                "events": int(totals[0]), "sessions": sessions, "views": int(totals[2]),
                "errors": int(totals[3]), "error_sessions": int(totals[4]),
                "resources": int(totals[5]), "actions": int(totals[6]),
                "long_tasks": int(totals[7]), "resource_failures": int(totals[8]),
                "sampled_errors": int(totals[9]), "unsampled_errors": int(totals[10]),
            },
            "rates": {
                "error_session_rate": (int(totals[4]) / sessions) if sessions else None,
                "resource_failure_rate": (
                    int(totals[8]) / int(totals[5]) if int(totals[5]) else None
                ),
            },
            "vitals": (
                _rollup_vitals_payload(vital_row)
                if window.rollup else _vitals_payload(vital_row)
            ),
        }

    def run():
        current = query(window)
        explorer = explorer_query()
        # The same figures for the window of equal length just before, so the
        # dashboard can show "+12 % vs. the previous 7 days" on every tile.
        previous = query(window.previous()) if compare else None
        return current, explorer, previous

    (totals, vital_row, releases), explorer, previous = await asyncio.to_thread(run)
    previous_payload = None
    if previous is not None:
        previous_window = window.previous()
        previous_payload = {
            "window": previous_window.payload(),
            **totals_payload(previous_window, previous[0], previous[1]),
        }
    return {
        "range": window.range,
        "window": window.payload(),
        "previous": previous_payload,
        **totals_payload(window, totals, vital_row),
        "filters": _filters_payload(
            application_id=application_id, env=env, view_name=view_name,
            browser=browser, device_type=device_type, country=country,
            service_version=service_version, browser_version=browser_version, os=os,
            client_ip=client_ip, user_id=user_id, q=q,
        ),
        "explorer": {
            "views": int(explorer[0]), "sessions": int(explorer[1]),
            "errors": int(explorer[2]), "resources": int(explorer[3]),
            "actions": int(explorer[4]),
        },
        "releases": [
            {
                "service_version": r[0], "sessions": int(r[1]), "views": int(r[2]),
                "errors": int(r[3]), "error_session_rate": float(r[4] or 0),
                "lcp_p75": _nullable(r[5], r[6]), "lcp_samples": int(r[6]),
                "inp_p75": _nullable(r[7], r[8]), "inp_samples": int(r[8]),
                "cls_p75": _nullable(r[9], r[10]), "cls_samples": int(r[10]),
                "last_seen": r[11], "first_seen": r[12] if len(r) > 12 else None,
            }
            for r in releases
        ],
        "ingest_health": {
            "accepted_since_process_start": int(_HEALTH["accepted"]),
            "rejected": int(_HEALTH["rejected"]),
            "rate_limited": int(_HEALTH["rate_limited"]),
            "duplicates": int(_HEALTH["duplicates"]),
            "storage_errors": int(_HEALTH["storage_errors"]),
            "last_event_at": _HEALTH_META["last_event_at"],
        },
    }


@router.get("/api/v1/apm/rum/timeseries")
async def rum_timeseries(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    _user=Depends(get_current_user),
):
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    bucket = window.bucket_seconds
    if window.rollup:
        rows = await asyncio.to_thread(lambda: _ch().query(f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS bucket,
                   uniqCombined64Merge(views), uniqCombined64Merge(sessions), sum(errors),
                   quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                   quantileTDigestMerge(0.75)(inp), sum(inp_samples),
                   quantileTDigestMerge(0.75)(cls), sum(cls_samples),
                   quantileTDigestMerge(0.75)(fcp), sum(fcp_samples),
                   quantileTDigestMerge(0.75)(ttfb), sum(ttfb_samples),
                   quantileTDigestMerge(0.75)(load_ms), sum(load_samples),
                   sum(resources), sum(resource_failures), sum(actions), sum(long_tasks),
                   uniqCombined64Merge(error_sessions)
            FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql}
            GROUP BY bucket ORDER BY bucket
        """, parameters=params).result_rows)
    else:
        rows = await asyncio.to_thread(lambda: _ch().query(f"""
        SELECT toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS bucket,
               uniqExactIf(
                   (application_id, env, view_id),
                   sampled = 1 AND event_type = 'view'
                       AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))
               ) AS views,
               uniqExactIf((application_id, env, session_id), sampled = 1) AS sessions,
               countIf(event_type = 'error') AS errors,
               quantileTDigestIf(0.75)(lcp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND lcp > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_lcp = 1 OR (sdk_version = '' AND lcp > 0))),
               quantileTDigestIf(0.75)(inp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND inp > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_inp = 1 OR (sdk_version = '' AND inp > 0))),
               quantileTDigestIf(0.75)(cls, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND cls > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_cls = 1 OR (sdk_version = '' AND cls > 0))),
               quantileTDigestIf(0.75)(fcp, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND fcp > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_fcp = 1 OR (sdk_version = '' AND fcp > 0))),
               quantileTDigestIf(0.75)(ttfb, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND ttfb > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_ttfb = 1 OR (sdk_version = '' AND ttfb > 0))),
               quantileTDigestIf(0.75)(load_ms, sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND load_ms > 0))),
               countIf(sampled = 1 AND event_type = 'view' AND (is_final = 1 OR sdk_version = '') AND (has_load = 1 OR (sdk_version = '' AND load_ms > 0))),
               countIf(event_type = 'resource' AND sampled = 1) AS resources,
               countIf(event_type = 'resource' AND sampled = 1 AND (status_code >= 400 OR attributes['failed'] = 'true')) AS resource_failures,
               countIf(event_type = 'action' AND sampled = 1) AS actions,
               countIf(event_type = 'long_task' AND sampled = 1) AS long_tasks,
               uniqExactIf((application_id, env, session_id), event_type = 'error' AND sampled = 1) AS error_sessions
        FROM zenplus.apm_rum_events WHERE {scope_sql}
        GROUP BY bucket ORDER BY bucket
        """, parameters=params).result_rows)
    return {
        "range": window.range, "window": window.payload(), "bucket_seconds": bucket,
        "series": [
            {
                "timestamp": row[0], "views": int(row[1]), "sessions": int(row[2]),
                "errors": int(row[3]), "lcp_p75": _nullable(row[4], row[5]),
                "lcp_samples": int(row[5]), "inp_p75": _nullable(row[6], row[7]),
                "inp_samples": int(row[7]), "cls_p75": _nullable(row[8], row[9]),
                "cls_samples": int(row[9]), "fcp_p75": _nullable(row[10], row[11]),
                "fcp_samples": int(row[11]), "ttfb_p75": _nullable(row[12], row[13]),
                "ttfb_samples": int(row[13]), "load_p75": _nullable(row[14], row[15]),
                "load_samples": int(row[15]),
                "resources": int(row[16] or 0), "resource_failures": int(row[17] or 0),
                "actions": int(row[18] or 0), "long_tasks": int(row[19] or 0),
                "error_sessions": int(row[20] or 0),
            }
            for row in rows
        ],
    }


@router.get("/api/v1/apm/rum/facets")
async def rum_facets(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    _user=Depends(get_current_user),
):
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )

    def query():
        result = {}
        for column in (
            "application_id", "env", "view_name", "browser", "browser_version",
            "os", "device_type", "country", "service_version", "client_ip", "user_id",
        ):
            # client_ip and user_id only live on raw events (never in the 5m
            # rollup), so they always read from apm_rum_events.
            use_rollup = window.rollup and column not in ("client_ip", "user_id")
            table = "zenplus.apm_rum_metrics_5m" if use_rollup else "zenplus.apm_rum_events"
            weight = "sum(events)" if use_rollup else "count()"
            rows = _ch().query(f"""
                SELECT {column}, {weight} FROM {table}
                WHERE {scope_sql} AND {column} != ''
                GROUP BY {column} ORDER BY {weight} DESC LIMIT 200
            """, parameters=params).result_rows
            result[column] = [{"value": row[0], "count": int(row[1])} for row in rows]
        return result

    return await asyncio.to_thread(query)


def _trace_timings(trace_ids: list[str]) -> dict[str, dict[str, object]]:
    """Server / database execution time per correlated APM trace.

    Server time is the longest SERVER span (the request handler); database
    time is the sum of spans that carry a db_system. Missing or expired traces
    are simply absent from the result, and a storage failure yields {} so the
    RUM read path never fails because APM is unavailable.
    """
    ids = sorted({t for t in trace_ids if t})[:300]
    if not ids:
        return {}
    try:
        span_rows = _ch().query("""
            SELECT lower(toString(trace_id)) AS tid,
                   maxIf(duration_nano, span_kind_str = 'SERVER') / 1e6,
                   sumIf(duration_nano, db_system != '') / 1e6,
                   countIf(db_system != ''), count(),
                   argMaxIf(service_name, duration_nano, span_kind_str = 'SERVER'),
                   groupUniqArrayIf(3)(db_system, db_system != ''),
                   max(has_error)
            FROM zenplus.apm_spans
            WHERE trace_id IN {tids:Array(String)}
            GROUP BY tid
        """, parameters={"tids": ids}).result_rows
    except Exception:
        return {}
    return {
        row[0]: {
            "server_ms": float(row[1] or 0), "db_ms": float(row[2] or 0),
            "db_calls": int(row[3]), "spans": int(row[4]),
            "service": row[5], "db_systems": list(row[6] or []),
            "has_error": bool(row[7]),
        }
        for row in span_rows
    }


def _summarize_traces(
    trace_ids: list[str], timings: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    """Average execution split over the correlated traces of one resource group."""
    found = [timings[t] for t in trace_ids if t in timings]
    with_server = [t for t in found if float(t["server_ms"]) > 0]
    if not with_server:
        return None
    services = Counter(str(t["service"]) for t in with_server if t["service"])
    return {
        "server_ms": sum(float(t["server_ms"]) for t in with_server) / len(with_server),
        "db_ms": sum(float(t["db_ms"]) for t in with_server) / len(with_server),
        "db_calls": sum(int(t["db_calls"]) for t in with_server),
        "spans": sum(int(t["spans"]) for t in with_server),
        "service": services.most_common(1)[0][0] if services else "",
        "db_systems": sorted({s for t in with_server for s in t["db_systems"]}),
        "has_error": any(bool(t["has_error"]) for t in with_server),
        "traces": len(with_server),
    }


_PHASE_COLUMNS = (
    ("redirect", "redirect_ms"), ("dns", "dns_ms"), ("connect", "connect_ms"),
    ("tls", "tls_ms"), ("wait", "wait_ms"), ("download", "download_ms"),
    ("blocked", "blocked_ms"), ("processing", "processing_ms"),
)


def _phase_select(duration_column: str = "duration_ms") -> str:
    # For views, duration_ms is dwell time on the page — the meaningful
    # end-to-end figure there is load_ms (navigation start → load event).
    phase_sql = ", ".join(
        f"quantileTDigestIf(0.75)({column}, has_timing = 1)"
        for _, column in _PHASE_COLUMNS
    )
    # The headline duration is measured over the same timing-capable rows as
    # the phases, so the number above the phase bar and the bar itself describe
    # one population (older SDKs report a duration but no phase split).
    return f"""
        countIf(has_timing = 1), {phase_sql},
        quantileTDigestIf(0.75)(server_ms, has_server_timing = 1),
        quantileTDigestIf(0.75)(db_ms, has_server_timing = 1),
        countIf(has_server_timing = 1),
        quantileTDigestIf(0.75)({duration_column}, has_timing = 1 AND {duration_column} > 0),
        countIf(has_timing = 1 AND {duration_column} > 0)
    """


def _phase_payload(row: list | tuple) -> dict[str, object]:
    samples = int(row[0] or 0)
    phases = {
        name: _nullable(row[index + 1], samples)
        for index, (name, _) in enumerate(_PHASE_COLUMNS)
    }
    offset = len(_PHASE_COLUMNS) + 1
    server_samples = int(row[offset + 2] or 0)
    return {
        "samples": samples,
        "phases": phases,
        "server_p75": _nullable(row[offset], server_samples),
        "db_p75": _nullable(row[offset + 1], server_samples),
        "server_samples": server_samples,
        "duration_p75": _nullable(row[offset + 3], row[offset + 4]),
        "duration_samples": int(row[offset + 4] or 0),
    }


# ── Web Vitals depth ─────────────────────────────────────────────────────────

# Histogram edges per vital; the last edge opens an overflow bucket. Edges are
# placed so the good / poor thresholds fall on a boundary.
VITAL_HISTOGRAM_EDGES: dict[str, list[float]] = {
    "lcp": [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 5000, 6000, 8000, 10000],
    "inp": [0, 50, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000],
    "cls": [0, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.35, 0.5, 0.75, 1.0],
    "fcp": [0, 500, 1000, 1500, 1800, 2200, 2600, 3000, 4000, 5000, 7000],
    "ttfb": [0, 200, 400, 600, 800, 1000, 1300, 1600, 1800, 2500, 3500],
    "load": [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 5000, 6000, 8000, 10000],
}
_PERCENTILES = (0.5, 0.75, 0.9, 0.95)
VitalDimension = Literal[
    "view_name", "device_type", "browser", "browser_version", "os", "country",
    "connection_type", "service_version",
]
VitalName = Literal["lcp", "inp", "cls", "fcp", "ttfb", "load"]
# Rollup rows carry every dimension except the client's connection type.
_ROLLUP_DIMENSIONS = {"view_name", "device_type", "browser", "browser_version", "os", "country", "service_version"}
_ATTRIBUTION = {
    "lcp": ("lcp.element", "lcp.url"),
    "cls": ("cls.element", None),
    "inp": ("inp.target", "inp.event_type"),
}


def _band_pcts(samples: int, good: int, poor: int) -> dict[str, float | None]:
    if not samples:
        return {"good_pct": None, "needs_improvement_pct": None, "poor_pct": None}
    return {
        "good_pct": good / samples * 100,
        "needs_improvement_pct": max(samples - good - poor, 0) / samples * 100,
        "poor_pct": poor / samples * 100,
    }


def _distribution_select(name: str) -> str:
    good, poor = VITAL_THRESHOLDS[name]
    edges = VITAL_HISTOGRAM_EDGES[name]
    present = f"{name}_present = 1"
    buckets = [
        f"countIf({present} AND {name}_value >= {lo} AND {name}_value < {hi})"
        for lo, hi in zip(edges, edges[1:])
    ]
    buckets.append(f"countIf({present} AND {name}_value >= {edges[-1]})")
    return ", ".join([
        f"countIf({present})",
        f"countIf({present} AND {name}_value <= {good})",
        f"countIf({present} AND {name}_value > {poor})",
        f"quantilesTDigestIf({', '.join(map(str, _PERCENTILES))})({name}_value, {present})",
        *buckets,
    ])


def _distribution_payload(name: str, row: list | tuple, offset: int) -> tuple[dict[str, object], int]:
    edges = VITAL_HISTOGRAM_EDGES[name]
    samples, good, poor = int(row[offset] or 0), int(row[offset + 1] or 0), int(row[offset + 2] or 0)
    quantiles = list(row[offset + 3] or [])
    counts = [int(value or 0) for value in row[offset + 4: offset + 4 + len(edges)]]
    good_limit, poor_limit = VITAL_THRESHOLDS[name]
    buckets = [
        {"from": lo, "to": hi, "count": count}
        for (lo, hi), count in zip(zip(edges, edges[1:]), counts)
    ]
    buckets.append({"from": edges[-1], "to": None, "count": counts[-1] if len(counts) == len(edges) else 0})
    return {
        "samples": samples,
        "thresholds": {"good": good_limit, "poor": poor_limit},
        "percentiles": {
            f"p{int(q * 100)}": (_nullable(quantiles[i], samples) if i < len(quantiles) else None)
            for i, q in enumerate(_PERCENTILES)
        },
        **_band_pcts(samples, good, poor),
        "buckets": buckets,
    }, offset + 4 + len(edges)


@router.get("/api/v1/apm/rum/vitals")
async def rum_vitals(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    dimension: VitalDimension = "view_name",
    vital: VitalName = "lcp",
    _user=Depends(get_current_user),
):
    """Web Vitals depth: value distributions, a slow-segment breakdown by one
    dimension, the page elements behind poor LCP / CLS / INP, and release
    first-seen markers for the trend charts.

    Raw windows measure one sample per page view (see _per_view_vitals_sql);
    windows beyond raw retention read the rollup, which carries percentiles
    and good/poor counters but no histogram or attribution.
    """
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    good_limit, poor_limit = VITAL_THRESHOLDS[vital]
    raw_column = _VITAL_RAW_COLUMN.get(vital, vital)

    def query():
        ch = _ch()
        if window.rollup:
            selects = ", ".join(
                f"quantilesTDigestMerge({', '.join(map(str, _PERCENTILES))})({_VITAL_RAW_COLUMN.get(n, n)}), "
                f"sum({n}_samples), sum({n}_good), sum({n}_poor), sum({n}_rated)"
                for n in _VITAL_NAMES
            )
            dist_row = ch.query(f"""
                SELECT {selects} FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql}
            """, parameters=params).result_rows[0]
            if dimension in _ROLLUP_DIMENSIONS:
                breakdown_rows = ch.query(f"""
                    SELECT {dimension} AS dim_value, uniqCombined64Merge(views) AS views,
                           sum({vital}_samples) AS samples,
                           quantileTDigestMerge(0.75)({raw_column}) AS p75,
                           sum({vital}_good) AS good, sum({vital}_poor) AS poor,
                           sum({vital}_rated) AS rated,
                           quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                           quantileTDigestMerge(0.75)(inp), sum(inp_samples),
                           quantileTDigestMerge(0.75)(cls), sum(cls_samples)
                    FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql}
                    GROUP BY dim_value HAVING samples > 0
                    ORDER BY poor / greatest(rated, 1) DESC, samples DESC LIMIT 50
                """, parameters=params).result_rows
            else:
                breakdown_rows = []
            release_rows = ch.query(f"""
                SELECT service_version, min(timestamp), uniqCombined64Merge(views)
                FROM zenplus.apm_rum_metrics_5m WHERE {scope_sql} AND service_version != ''
                GROUP BY service_version ORDER BY min(timestamp)
            """, parameters=params).result_rows
        else:
            dist_row = ch.query(f"""
                SELECT {', '.join(_distribution_select(n) for n in _VITAL_NAMES)}
                FROM ({_per_view_vitals_sql(scope_sql)})
            """, parameters=params).result_rows[0]
            per_view = _per_view_vitals_sql(
                scope_sql, extra_select=f"anyIf(raw.{dimension}, raw.{dimension} != '') AS dim_value",
            )
            breakdown_rows = ch.query(f"""
                SELECT dim_value, count() AS views,
                       countIf({vital}_present = 1) AS samples,
                       quantileTDigestIf(0.75)({vital}_value, {vital}_present = 1) AS p75,
                       countIf({vital}_present = 1 AND {vital}_value <= {good_limit}) AS good,
                       countIf({vital}_present = 1 AND {vital}_value > {poor_limit}) AS poor,
                       countIf({vital}_present = 1) AS rated,
                       quantileTDigestIf(0.75)(lcp_value, lcp_present = 1), countIf(lcp_present = 1),
                       quantileTDigestIf(0.75)(inp_value, inp_present = 1), countIf(inp_present = 1),
                       quantileTDigestIf(0.75)(cls_value, cls_present = 1), countIf(cls_present = 1)
                FROM ({per_view})
                GROUP BY dim_value HAVING samples > 0
                ORDER BY poor / greatest(rated, 1) DESC, samples DESC LIMIT 50
            """, parameters=params).result_rows
            release_rows = ch.query(f"""
                SELECT service_version, min(timestamp),
                       uniqExactIf((application_id, env, view_id), event_type = 'view')
                FROM zenplus.apm_rum_events WHERE {scope_sql} AND service_version != '' AND sampled = 1
                GROUP BY service_version ORDER BY min(timestamp)
            """, parameters=params).result_rows
        # Attribution lives on raw final view events only (14-day retention).
        attribution: dict[str, list] = {}
        raw_params, raw_scope = params, scope_sql
        for name, (primary, secondary) in _ATTRIBUTION.items():
            column = _VITAL_RAW_COLUMN.get(name, name)
            good_n, poor_n = VITAL_THRESHOLDS[name]
            secondary_sql = f"vital_attribution['{secondary}']" if secondary else "''"
            rows = ch.query(f"""
                SELECT view_name, vital_attribution['{primary}'] AS element, {secondary_sql} AS detail,
                       count() AS n, quantileTDigest(0.75)({column}) AS p75,
                       countIf({column} > {poor_n}) AS poor, countIf({column} <= {good_n}) AS good
                FROM zenplus.apm_rum_events
                WHERE {raw_scope} AND sampled = 1 AND event_type = 'view' AND is_final = 1
                  AND has_{name} = 1 AND vital_attribution['{primary}'] != ''
                GROUP BY view_name, element, detail
                ORDER BY poor DESC, n DESC LIMIT 12
            """, parameters=raw_params).result_rows
            attribution[name] = [
                {
                    "view_name": r[0], "element": r[1], "detail": r[2] or None,
                    "count": int(r[3]), "p75": _nullable(r[4], r[3]),
                    **_band_pcts(int(r[3]), int(r[6]), int(r[5])),
                }
                for r in rows
            ]
        return dist_row, breakdown_rows, release_rows, attribution

    dist_row, breakdown_rows, release_rows, attribution = await asyncio.to_thread(query)

    distribution: dict[str, object] = {}
    if window.rollup:
        for index, name in enumerate(_VITAL_NAMES):
            quantiles = list(dist_row[index * 5] or [])
            samples = int(dist_row[index * 5 + 1] or 0)
            good, poor = int(dist_row[index * 5 + 2] or 0), int(dist_row[index * 5 + 3] or 0)
            rated = int(dist_row[index * 5 + 4] or 0)
            good_limit_n, poor_limit_n = VITAL_THRESHOLDS[name]
            distribution[name] = {
                "samples": samples,
                "rated_samples": rated,
                "thresholds": {"good": good_limit_n, "poor": poor_limit_n},
                "percentiles": {
                    f"p{int(q_ * 100)}": (_nullable(quantiles[i], samples) if i < len(quantiles) else None)
                    for i, q_ in enumerate(_PERCENTILES)
                },
                **_band_pcts(rated, good, poor),
                "buckets": [],
            }
    else:
        offset = 0
        for name in _VITAL_NAMES:
            distribution[name], offset = _distribution_payload(name, dist_row, offset)

    return {
        "range": window.range,
        "window": window.payload(),
        "coverage": _raw_coverage(window),
        "distribution": distribution,
        "breakdown": {
            "dimension": dimension,
            "vital": vital,
            "available": not window.rollup or dimension in _ROLLUP_DIMENSIONS,
            "rows": [
                {
                    "value": r[0], "views": int(r[1]), "samples": int(r[2]),
                    "p75": _nullable(r[3], r[2]),
                    "rated_samples": int(r[6]),
                    **_band_pcts(int(r[6]), int(r[4]), int(r[5])),
                    "vitals": {
                        "lcp": {"p75": _nullable(r[7], r[8]), "samples": int(r[8])},
                        "inp": {"p75": _nullable(r[9], r[10]), "samples": int(r[10])},
                        "cls": {"p75": _nullable(r[11], r[12]), "samples": int(r[12])},
                    },
                }
                for r in breakdown_rows
            ],
        },
        "attribution": attribution,
        "releases": [
            {"service_version": r[0], "first_seen": r[1], "views": int(r[2] or 0)}
            for r in release_rows
        ],
    }


@router.get("/api/v1/apm/rum/breakdown")
async def rum_breakdown(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    _user=Depends(get_current_user),
):
    """NSX-ALB-style end-to-end latency split across the selected scope.

    Aggregates the Navigation/Resource Timing phases plus the Server-Timing
    execution split (raw events only, so drill-down retention applies).
    """
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )

    def query():
        nav = _ch().query(f"""
            SELECT {_phase_select("load_ms")}
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND sampled = 1 AND event_type = 'view' AND is_final = 1
        """, parameters=params).result_rows[0]
        api = _ch().query(f"""
            SELECT {_phase_select()}
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND sampled = 1 AND event_type = 'resource'
              AND resource_type IN ('fetch', 'xhr')
        """, parameters=params).result_rows[0]
        slow = _ch().query(f"""
            SELECT resource_url, anyIf(method, method != '') AS req_method,
                   count() AS requests,
                   quantileTDigestIf(0.75)(duration_ms, duration_ms > 0) AS duration_p75,
                   countIf(duration_ms > 0) AS duration_samples,
                   quantileTDigestIf(0.75)(wait_ms, has_timing = 1) AS wait_p75,
                   countIf(has_timing = 1) AS timing_samples,
                   quantileTDigestIf(0.75)(server_ms, has_server_timing = 1) AS server_p75,
                   quantileTDigestIf(0.75)(db_ms, has_server_timing = 1) AS db_p75,
                   countIf(has_server_timing = 1) AS server_samples,
                   countIf(status_code >= 400 OR attributes['failed'] = 'true') AS failures,
                   groupUniqArrayIf(50)(backend_trace_id, backend_trace_id != '') AS trace_ids
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND sampled = 1 AND event_type = 'resource'
              AND resource_type IN ('fetch', 'xhr') AND resource_url != ''
            GROUP BY resource_url
            HAVING countIf(duration_ms > 0) >= 2
            ORDER BY duration_p75 DESC
            LIMIT 8
        """, parameters=params).result_rows
        timings = _trace_timings([t for row in slow for t in (row[11] or [])])
        return nav, api, slow, timings

    nav, api, slow, timings = await asyncio.to_thread(query)

    def endpoint(row):
        server_samples = int(row[9])
        item = {
            "url": row[0], "method": row[1], "count": int(row[2]),
            "duration_p75": _nullable(row[3], row[4]),
            "wait_p75": _nullable(row[5], row[6]),
            "server_p75": _nullable(row[7], server_samples),
            "db_p75": _nullable(row[8], server_samples),
            "server_samples": server_samples,
            "server_source": "server-timing" if server_samples else None,
            "failures": int(row[10]),
        }
        backend = _summarize_traces(list(row[11] or []), timings)
        if not server_samples and backend:
            # Fall back to the correlated APM traces for the execution split.
            item.update(
                server_p75=backend["server_ms"], db_p75=backend["db_ms"],
                server_samples=int(backend["traces"]), server_source="trace",
            )
        return item

    return {
        "range": window.range,
        "window": window.payload(),
        "coverage": _raw_coverage(window),
        "page_loads": _phase_payload(nav),
        "api_requests": _phase_payload(api),
        "slowest_endpoints": [endpoint(row) for row in slow],
    }


_VIEW_SORTS = {
    "views": "views", "sessions": "sessions", "errors": "error_count",
    "error_session_rate": "error_session_rate", "lcp_p75": "lcp_p75",
    "inp_p75": "inp_p75", "cls_p75": "cls_p75", "fcp_p75": "fcp_p75",
    "ttfb_p75": "ttfb_p75", "load_p75": "load_p75", "last_seen": "last_seen",
}


@router.get("/api/v1/apm/rum/views")
async def rum_views(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "views",
    order: OrderName = "desc",
    _user=Depends(get_current_user),
):
    sort_sql = _VIEW_SORTS.get(sort)
    if sort_sql is None:
        raise HTTPException(400, f"Unsupported views sort '{sort}'")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(limit=page_size, offset=(page - 1) * page_size)

    def query():
        total = _ch().query(f"""
            SELECT count() FROM (
                SELECT application_id, env, view_name FROM zenplus.apm_rum_events
                WHERE {scope_sql} AND sampled = 1 GROUP BY application_id, env, view_name
            )
        """, parameters=params).result_rows[0][0]
        # Traffic/error figures aggregate every sampled event on the route; the
        # Web Vitals come from the per-view dedupe so a navigation that emitted
        # several finalized records (legacy SDKs, retries) is one sample, matching
        # the overview and Web Vitals tabs.
        vital_columns = ", ".join(
            f"vitals.{name}_p75 AS {name}_p75, vitals.{name}_samples AS {name}_samples"
            for name in _VITAL_NAMES
        )
        rows = _ch().query(f"""
            SELECT base.application_id AS application_id, base.env AS env,
                   base.view_name AS view_name, base.latest_url AS latest_url,
                   base.views AS views, base.sessions AS sessions,
                   base.error_count AS error_count,
                   base.error_session_rate AS error_session_rate,
                   {vital_columns},
                   base.last_seen AS last_seen, base.primary_trace_id AS primary_trace_id,
                   base.backend_trace_ids AS backend_trace_ids,
                   base.latest_service_version AS latest_service_version
            FROM (
                SELECT application_id, env, view_name, argMax(url, timestamp) AS latest_url,
                       uniqExactIf(view_id, event_type = 'view') AS views,
                       uniqExact(session_id) AS sessions,
                       countIf(event_type = 'error') AS error_count,
                       uniqExactIf(session_id, event_type = 'error') / greatest(uniqExact(session_id), 1) AS error_session_rate,
                       max(timestamp) AS last_seen,
                       anyIf(backend_trace_id, backend_trace_id != '') AS primary_trace_id,
                       groupUniqArrayIf(20)(backend_trace_id, backend_trace_id != '') AS backend_trace_ids,
                       argMax(service_version, timestamp) AS latest_service_version
                FROM zenplus.apm_rum_events WHERE {scope_sql} AND sampled = 1
                GROUP BY application_id, env, view_name
            ) AS base
            LEFT JOIN (
                {_per_route_vitals_sql(scope_sql)}
            ) AS vitals
            ON base.application_id = vitals.application_id
               AND base.env = vitals.env AND base.view_name = vitals.view_name
            ORDER BY {sort_sql} {order.upper()}, view_name ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=params).result_rows
        return int(total), rows

    total, rows = await asyncio.to_thread(query)
    return {
        "total": total, "page": page, "page_size": page_size, "coverage": _raw_coverage(window),
        "items": [
            {
                "application_id": r[0], "env": r[1], "view_name": r[2], "url": r[3],
                "views": int(r[4]), "sessions": int(r[5]), "error_count": int(r[6]),
                "errors": int(r[6]),
                "error_session_rate": float(r[7] or 0),
                "lcp_p75": _nullable(r[8], r[9]), "lcp_samples": int(r[9]),
                "inp_p75": _nullable(r[10], r[11]), "inp_samples": int(r[11]),
                "cls_p75": _nullable(r[12], r[13]), "cls_samples": int(r[13]),
                "fcp_p75": _nullable(r[14], r[15]), "fcp_samples": int(r[15]),
                "ttfb_p75": _nullable(r[16], r[17]), "ttfb_samples": int(r[17]),
                "load_p75": _nullable(r[18], r[19]), "load_samples": int(r[19]),
                "last_seen": r[20], "backend_trace_id": r[21],
                "backend_trace_ids": list(r[22] or []), "service_version": r[23],
            }
            for r in rows
        ],
    }


_SESSION_SORTS = {
    "last_seen": "last_seen", "started_at": "started_at",
    "duration_ms": "duration_ms", "views": "views", "actions": "actions",
    "resources": "resources", "long_tasks": "long_tasks", "errors": "errors",
}


@router.get("/api/v1/apm/rum/sessions")
async def rum_sessions(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "last_seen",
    order: OrderName = "desc",
    _user=Depends(get_current_user),
):
    sort_sql = _SESSION_SORTS.get(sort)
    if sort_sql is None:
        raise HTTPException(400, f"Unsupported sessions sort '{sort}'")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(limit=page_size, offset=(page - 1) * page_size)

    def query():
        total = _ch().query(f"""
            SELECT uniqExact((application_id, env, session_id))
            FROM zenplus.apm_rum_events WHERE {scope_sql} AND sampled = 1
        """, parameters=params).result_rows[0][0]
        rows = _ch().query(f"""
            SELECT session_id, application_id, env,
                   min(timestamp) AS started_at, max(timestamp) AS last_seen,
                   dateDiff('millisecond', min(timestamp), max(timestamp)) AS duration_ms,
                   uniqExactIf(view_id, event_type = 'view') AS views,
                   countIf(event_type = 'action') AS actions,
                   countIf(event_type = 'resource') AS resources,
                   countIf(event_type = 'long_task') AS long_tasks,
                   countIf(event_type = 'error') AS errors,
                   argMax(browser, timestamp), argMax(browser_version, timestamp),
                   argMax(os, timestamp), argMax(device_type, timestamp),
                   argMax(country, timestamp), anyIf(user_id, user_id != ''),
                   anyIf(backend_trace_id, backend_trace_id != '') AS primary_trace_id,
                   groupUniqArrayIf(100)(backend_trace_id, backend_trace_id != '') AS backend_trace_ids,
                   argMax(sdk_version, timestamp), argMax(service_version, timestamp),
                   anyIf(client_ip, client_ip != '') AS session_client_ip
            FROM zenplus.apm_rum_events WHERE {scope_sql} AND sampled = 1
            GROUP BY session_id, application_id, env
            ORDER BY {sort_sql} {order.upper()}, session_id ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=params).result_rows
        return int(total), rows

    total, rows = await asyncio.to_thread(query)
    return {
        "total": total, "page": page, "page_size": page_size, "coverage": _raw_coverage(window),
        "items": [
            {
                "session_id": r[0], "application_id": r[1], "env": r[2],
                "started_at": r[3], "last_seen": r[4], "duration_ms": int(r[5]),
                "views": int(r[6]), "actions": int(r[7]), "resources": int(r[8]),
                "long_tasks": int(r[9]), "errors": int(r[10]), "browser": r[11],
                "browser_version": r[12], "os": r[13], "device_type": r[14],
                "country": r[15], "user_id": r[16], "backend_trace_id": r[17],
                "backend_trace_ids": list(r[18] or []), "sdk_version": r[19],
                "service_version": r[20], "client_ip": r[21], "sampled": True,
            }
            for r in rows
        ],
    }


@router.get("/api/v1/apm/rum/sessions/{session_id}")
async def rum_session_detail(
    session_id: str,
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    _user=Depends(get_current_user),
):
    if not 1 <= len(session_id) <= 128:
        raise HTTPException(400, "Invalid RUM session identifier")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(
        session=session_id, limit=page_size, offset=(page - 1) * page_size,
    )

    def query():
        summary_rows = _ch().query(f"""
            SELECT application_id, env, min(timestamp), max(timestamp),
                   uniqExactIf(view_id, event_type = 'view'),
                   countIf(event_type = 'action'), countIf(event_type = 'resource'),
                   countIf(event_type = 'long_task'), countIf(event_type = 'error'),
                   argMax(browser, timestamp), argMax(browser_version, timestamp),
                   argMax(os, timestamp), argMax(device_type, timestamp),
                   argMax(country, timestamp), anyIf(user_id, user_id != ''),
                   groupUniqArrayIf(500)(backend_trace_id, backend_trace_id != ''),
                   argMax(sdk_version, timestamp), argMax(service_version, timestamp),
                   max(sampled), count(), anyIf(client_ip, client_ip != ''),
                   argMaxIf(connection_type, timestamp, connection_type != ''),
                   maxIf(connection_rtt_ms, connection_rtt_ms > 0),
                   maxIf(connection_downlink, connection_downlink > 0),
                   argMaxIf(language, timestamp, language != ''),
                   argMaxIf(timezone, timestamp, timezone != ''),
                   argMaxIf(screen_res, timestamp, screen_res != ''),
                   argMaxIf(viewport, timestamp, viewport != '')
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND session_id = {{session:String}}
            GROUP BY application_id, env ORDER BY max(timestamp) DESC LIMIT 1
        """, parameters=params).result_rows
        if not summary_rows:
            return None, [], 0, {}
        total = int(summary_rows[0][19])
        event_params = {
            **params,
            "selected_app": summary_rows[0][0],
            "selected_env": summary_rows[0][1],
        }
        events = _ch().query(f"""
            SELECT timestamp, event_id, event_type, view_id, view_name, url,
                   action_name, action_type, target, duration_ms,
                   resource_url, resource_type, method, status_code, transfer_size,
                   encoded_body_size, error_message, error_type, error_stack,
                   error_source, error_fingerprint, backend_trace_id,
                   lcp, inp, cls, fcp, ttfb, load_ms,
                   has_lcp, has_inp, has_cls, has_fcp, has_ttfb, has_load,
                   is_final, end_reason, attributes, vital_attribution,
                   sdk_version, service_version, browser_version, os, sampled,
                   redirect_ms, dns_ms, connect_ms, tls_ms, wait_ms, download_ms,
                   blocked_ms, processing_ms, server_ms, db_ms, has_timing,
                   has_server_timing, protocol
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND session_id = {{session:String}}
              AND application_id = {{selected_app:String}}
              AND env = {{selected_env:String}}
            ORDER BY timestamp ASC, is_final DESC, event_id ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=event_params).result_rows
        # Correlate this page's backend traces so the UI can split browser wait
        # time into network vs. application execution vs. database time.
        trace_ids = sorted(
            {row[21] for row in events if row[21]}
            | set(list(summary_rows[0][15] or [])[:100])
        )[:300]
        return summary_rows[0], events, total, _trace_timings(trace_ids)

    summary, events, total, trace_timings = await asyncio.to_thread(query)
    if summary is None:
        raise HTTPException(404, "RUM session not found")
    timeline = []
    for r in events:
        item_url = r[10] or r[5]
        item_name = r[6] or (r[10].rsplit("/", 1)[-1] if r[10] else "") or r[17] or r[2]
        timing = None
        if r[53]:
            timing = {
                "redirect_ms": float(r[43]), "dns_ms": float(r[44]),
                "connect_ms": float(r[45]), "tls_ms": float(r[46]),
                "wait_ms": float(r[47]), "download_ms": float(r[48]),
                "blocked_ms": float(r[49]), "processing_ms": float(r[50]),
                "server_ms": float(r[51]), "db_ms": float(r[52]),
                "has_server_timing": bool(r[54]), "protocol": r[55],
            }
        timeline.append({
            "timestamp": r[0], "event_id": r[1], "event_type": r[2],
            "view_id": r[3], "view_name": r[4], "url": item_url,
            "page_url": r[5], "name": item_name,
            "action_name": r[6], "action_type": r[7], "target": r[8],
            "duration_ms": float(r[9] or 0), "resource_url": r[10],
            "resource_type": r[11], "method": r[12],
            "status_code": int(r[13]) if r[13] else None,
            "transfer_size": int(r[14]), "size_bytes": int(r[14]),
            "encoded_body_size": int(r[15]),
            "error_message": r[16], "error_type": r[17], "error_stack": r[18],
            "stack": r[18], "error_source": r[19], "source": r[19],
            "error_fingerprint": r[20],
            "backend_trace_id": r[21],
            "vitals": {
                "lcp": float(r[22]) if r[28] else None,
                "inp": float(r[23]) if r[29] else None,
                "cls": float(r[24]) if r[30] else None,
                "fcp": float(r[25]) if r[31] else None,
                "ttfb": float(r[26]) if r[32] else None,
                "load": float(r[27]) if r[33] else None,
            },
            "lcp": float(r[22]) if r[28] else None,
            "inp": float(r[23]) if r[29] else None,
            "cls": float(r[24]) if r[30] else None,
            "fcp": float(r[25]) if r[31] else None,
            "ttfb": float(r[26]) if r[32] else None,
            "load_ms": float(r[27]) if r[33] else None,
            "is_final": bool(r[34]), "end_reason": r[35],
            "attributes": dict(r[36] or {}), "vital_attribution": dict(r[37] or {}),
            "sdk_version": r[38], "service_version": r[39],
            "browser_version": r[40], "os": r[41], "sampled": bool(r[42]),
            "timing": timing,
            "backend": trace_timings.get(r[21]) if r[21] else None,
        })
    return {
        "session": {
            "session_id": session_id, "application_id": summary[0], "env": summary[1],
            "started_at": summary[2], "last_seen": summary[3],
            "duration_ms": int((summary[3] - summary[2]).total_seconds() * 1000),
            "views": int(summary[4]), "actions": int(summary[5]),
            "resources": int(summary[6]), "long_tasks": int(summary[7]),
            "errors": int(summary[8]), "browser": summary[9],
            "browser_version": summary[10], "os": summary[11],
            "device_type": summary[12], "country": summary[13], "user_id": summary[14],
            "backend_trace_ids": list(summary[15] or []),
            "backend_trace_id": (summary[15][0] if summary[15] else ""),
            "sdk_version": summary[16], "service_version": summary[17],
            "client_ip": (summary[20] if len(summary) > 20 else ""),
            "sampled": bool(summary[18]),
            "connection_type": summary[21], "connection_rtt_ms": float(summary[22] or 0) or None,
            "connection_downlink": float(summary[23] or 0) or None,
            "language": summary[24], "timezone": summary[25],
            "screen_res": summary[26], "viewport": summary[27],
        },
        "backend_summary": (
            {
                "traces": len(trace_timings),
                "services": sorted({
                    str(t["service"]) for t in trace_timings.values() if t["service"]
                }),
                "db_systems": sorted({
                    str(system)
                    for t in trace_timings.values() for system in t["db_systems"]
                }),
                "avg_server_ms": (
                    sum(t["server_ms"] for t in trace_timings.values() if t["server_ms"] > 0)
                    / max(sum(1 for t in trace_timings.values() if t["server_ms"] > 0), 1)
                ) if any(t["server_ms"] > 0 for t in trace_timings.values()) else None,
                "avg_db_ms": (
                    sum(t["db_ms"] for t in trace_timings.values() if t["db_ms"] > 0)
                    / max(sum(1 for t in trace_timings.values() if t["db_ms"] > 0), 1)
                ) if any(t["db_ms"] > 0 for t in trace_timings.values()) else None,
            }
            if trace_timings else None
        ),
        "total": total, "page": page, "page_size": page_size,
        "coverage": {
            **_raw_coverage(window),
            "message": "Session event detail is retained for the most recent 14 days.",
        },
        "timeline": timeline,
    }


_ERROR_SORTS = {"count": "event_count", "sessions": "sessions", "first_seen": "first_seen", "last_seen": "last_seen"}


@router.get("/api/v1/apm/rum/errors")
async def rum_errors(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "count",
    order: OrderName = "desc",
    status: IssueStatusFilter | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    sort_sql = _ERROR_SORTS.get(sort)
    if sort_sql is None:
        raise HTTPException(400, f"Unsupported errors sort '{sort}'")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(limit=page_size, offset=(page - 1) * page_size)
    fingerprint = _ERROR_FINGERPRINT_SQL
    status = _str_param(status)
    # Lifecycle state lives in Postgres; translate a status filter into a
    # fingerprint allow/deny list for the ClickHouse query.
    issue_rows = await _issue_rows(db, application_id, env)
    status_of = {(r["application_id"], r["env"], r["fingerprint"]): r for r in issue_rows}
    known_filter = ""
    if status in ("resolved", "ignored", "regressed"):
        wanted = [r["fingerprint"] for r in issue_rows if r["status"] == ("resolved" if status == "regressed" else status)]
        params["status_fps"] = wanted or ["__none__"]
        known_filter = f" AND {fingerprint} IN {{status_fps:Array(String)}}"
    elif status == "open":
        closed = [r["fingerprint"] for r in issue_rows if r["status"] in ("resolved", "ignored")]
        if closed:
            params["status_fps"] = closed
            known_filter = f" AND {fingerprint} NOT IN {{status_fps:Array(String)}}"
    elif status == "new":
        params["retention_from"] = datetime.now(timezone.utc) - timedelta(seconds=RAW_RETENTION_SECONDS)
        known_filter = f"""
            AND {fingerprint} IN (
                SELECT {fingerprint} FROM zenplus.apm_rum_events
                WHERE timestamp >= {{retention_from:DateTime64(3)}} AND event_type = 'error'
                GROUP BY {fingerprint} HAVING min(timestamp) >= {{frm:DateTime64(3)}}
            )"""
    scope_sql = scope_sql + known_filter

    def query():
        total = _ch().query(f"""
            SELECT uniqExact((application_id, env, {fingerprint})) FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type = 'error'
        """, parameters=params).result_rows[0][0]
        rows = _ch().query(f"""
            SELECT {fingerprint} AS fingerprint,
                   argMax(error_message, timestamp), argMax(error_source, timestamp),
                   argMax(error_stack, timestamp), argMax(error_type, timestamp),
                   count() AS event_count, uniqExact(session_id) AS sessions,
                   min(timestamp) AS first_seen, max(timestamp) AS last_seen,
                   argMax(view_name, timestamp), application_id, env,
                   argMax(browser, timestamp), argMax(browser_version, timestamp),
                   argMax(os, timestamp), argMax(device_type, timestamp),
                   argMax(country, timestamp), argMax(service_version, timestamp),
                   anyIf(backend_trace_id, backend_trace_id != '') AS primary_trace_id,
                   groupUniqArrayIf(50)(backend_trace_id, backend_trace_id != '') AS backend_trace_ids,
                   countIf(sampled = 1) AS sampled_count,
                   countIf(sampled = 0) AS unsampled_count
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type = 'error'
            GROUP BY application_id, env, fingerprint
            ORDER BY {sort_sql} {order.upper()}, fingerprint ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=params).result_rows
        # Earliest occurrence within raw retention, to tell a brand-new group
        # from one that merely re-appeared in this window.
        earliest: dict[tuple, datetime] = {}
        if rows:
            fps = [r[0] for r in rows]
            for app_, env_, fp_, first in _ch().query(f"""
                SELECT application_id, env, {fingerprint} AS fp, min(timestamp)
                FROM zenplus.apm_rum_events
                WHERE timestamp >= now() - INTERVAL {RAW_RETENTION_SECONDS} SECOND
                  AND event_type = 'error' AND fp IN {{fps:Array(String)}}
                GROUP BY application_id, env, fp
            """, parameters={"fps": fps}).result_rows:
                earliest[(app_, env_, fp_)] = first
        return int(total), rows, earliest

    total, rows, earliest = await asyncio.to_thread(query)
    items = []
    for r in rows:
        issue = _issue_payload(status_of.get((r[10], r[11], r[0])), r[8], earliest.get((r[10], r[11], r[0])), window)
        if status == "regressed" and issue["status"] != "regressed":
            continue
        items.append({"issue": issue, "_row": r})
    if status == "regressed":
        total = len(items)
    return {
        "total": total, "page": page, "page_size": page_size, "coverage": _raw_coverage(window),
        "sampling": {
            "includes_retained_unsampled_errors": True,
            "aggregate_error_session_rates_use_sampled_sessions_only": True,
        },
        "items": [
            {
                "issue": item["issue"],
                "fingerprint": r[0], "message": r[1], "source": r[2], "stack": r[3],
                "error_type": r[4], "count": int(r[5]), "sessions": int(r[6]),
                "first_seen": r[7], "last_seen": r[8], "view_name": r[9],
                "application_id": r[10], "env": r[11], "browser": r[12],
                "browser_version": r[13], "os": r[14], "device_type": r[15],
                "country": r[16], "service_version": r[17], "backend_trace_id": r[18],
                "backend_trace_ids": list(r[19] or []), "sampled_count": int(r[20]),
                "unsampled_count": int(r[21]),
            }
            for item in items for r in [item["_row"]]
        ],
    }


_RESOURCE_SORTS = {
    "count": "event_count", "failed_count": "failed_count",
    "duration_p75": "duration_p75", "size_avg": "size_avg", "last_seen": "last_seen",
}


# ── Error groups: lifecycle, detail, source maps ─────────────────────────────

IssueStatus = Literal["open", "resolved", "ignored"]
IssueStatusFilter = Literal["open", "new", "regressed", "resolved", "ignored"]
NEW_ISSUE_WINDOW_SECONDS = 24 * 3600
SOURCE_MAP_MAX_BYTES = 25 * 1024 * 1024


async def _issue_rows(db: AsyncSession, application_id: str | None, env: str | None) -> list[dict]:
    clauses, params = [], {}
    if _str_param(application_id):
        clauses.append("application_id = :app"); params["app"] = application_id
    if _str_param(env):
        clauses.append("env = :env"); params["env"] = env
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = (await db.execute(text(f"""
            SELECT application_id, env, fingerprint, status, note, first_seen_release,
                   resolved_at, resolved_release, updated_by, updated_at
            FROM rum_issues {where}
        """), params)).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _issue_payload(row: dict | None, last_seen, earliest, window: RumWindow) -> dict:
    """Effective lifecycle state of one group.

    ``new``: first ever occurrence (within raw retention) is inside the window
    and less than a day old. ``regressed``: an operator resolved it, and it
    has occurred again since. Everything else is the stored status.
    """
    stored = (row or {}).get("status") or "open"
    resolved_at = (row or {}).get("resolved_at")
    effective = stored
    if stored == "resolved" and resolved_at is not None and last_seen is not None:
        last = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
        if last > resolved_at:
            effective = "regressed"
    elif stored == "open" and earliest is not None:
        first = earliest if earliest.tzinfo else earliest.replace(tzinfo=timezone.utc)
        if first >= window.frm and (datetime.now(timezone.utc) - first).total_seconds() <= NEW_ISSUE_WINDOW_SECONDS:
            effective = "new"
    return {
        "status": effective,
        "stored_status": stored,
        "note": (row or {}).get("note") or "",
        "first_seen_release": (row or {}).get("first_seen_release") or "",
        "resolved_at": resolved_at,
        "resolved_release": (row or {}).get("resolved_release") or "",
        "updated_by": (row or {}).get("updated_by") or "",
        "updated_at": (row or {}).get("updated_at"),
        "first_seen_ever": earliest,
    }


class IssueStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: IssueStatus
    note: str = Field(default="", max_length=2000)
    application_id: str = Field(..., min_length=1, max_length=128)
    env: str = Field(default="", max_length=64)
    release: str = Field(default="", max_length=128)


@router.patch("/api/v1/apm/rum/errors/{fingerprint}/status")
async def rum_error_status(
    fingerprint: str,
    body: IssueStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Resolve, ignore or reopen an error group."""
    if not 1 <= len(fingerprint) <= 128:
        raise HTTPException(400, "Invalid fingerprint")
    actor = str(getattr(user, "username", "") or getattr(user, "email", "") or "")
    resolving = body.status == "resolved"
    await db.execute(text("""
        INSERT INTO rum_issues (application_id, env, fingerprint, status, note, first_seen_release,
                                resolved_at, resolved_release, updated_by, updated_at)
        VALUES (:app, :env, :fp, :status, :note, CAST(:release AS VARCHAR),
                CASE WHEN CAST(:resolving AS BOOLEAN) THEN now() END,
                CASE WHEN CAST(:resolving AS BOOLEAN) THEN CAST(:release AS VARCHAR) ELSE '' END,
                :actor, now())
        ON CONFLICT (application_id, env, fingerprint) DO UPDATE SET
            status = EXCLUDED.status,
            note = EXCLUDED.note,
            resolved_at = CASE WHEN CAST(:resolving AS BOOLEAN) THEN now() ELSE NULL END,
            resolved_release = CASE WHEN CAST(:resolving AS BOOLEAN) THEN CAST(:release AS VARCHAR) ELSE '' END,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
    """), {
        "app": body.application_id, "env": body.env, "fp": fingerprint, "status": body.status,
        "note": body.note.strip(), "release": body.release, "resolving": resolving, "actor": actor,
    })
    await db.commit()
    row = (await db.execute(text("""
        SELECT application_id, env, fingerprint, status, note, first_seen_release,
               resolved_at, resolved_release, updated_by, updated_at
        FROM rum_issues WHERE application_id = :app AND env = :env AND fingerprint = :fp
    """), {"app": body.application_id, "env": body.env, "fp": fingerprint})).mappings().first()
    return {"fingerprint": fingerprint, "issue": _issue_payload(dict(row) if row else None, None, None, _resolve_window("24h"))}


async def _source_maps_for(db: AsyncSession, application_id: str, release: str) -> dict[str, tuple[str, bytes]]:
    """Minified file name → (map id, gzipped map) for a release, falling back to
    maps uploaded without a release."""
    try:
        rows = (await db.execute(text("""
            SELECT id, file_name, release, map_gzip FROM rum_source_maps
            WHERE application_id = :app AND (release = :release OR release = '')
            ORDER BY (release = '') ASC
        """), {"app": application_id, "release": release or ""})).all()
    except Exception:
        return {}
    maps: dict[str, tuple[str, bytes]] = {}
    for map_id, file_name, _release, blob in rows:
        maps.setdefault(file_name, (str(map_id), bytes(blob)))
    return maps


@router.get("/api/v1/apm/rum/errors/{fingerprint}")
async def rum_error_detail(
    fingerprint: str,
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Everything about one error group: impact, trend, breakdowns, the latest
    occurrence with a symbolicated stack, and what the user did just before."""
    if not 1 <= len(fingerprint) <= 128:
        raise HTTPException(400, "Invalid fingerprint")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params["fp"] = fingerprint
    params["retention_from"] = datetime.now(timezone.utc) - timedelta(seconds=RAW_RETENTION_SECONDS)
    group_sql = f"{scope_sql} AND event_type = 'error' AND {_ERROR_FINGERPRINT_SQL} = {{fp:String}}"
    bucket = window.bucket_seconds

    def query():
        ch = _ch()
        summary = ch.query(f"""
            SELECT count(), uniqExact(session_id), uniqExactIf(user_id, user_id != ''),
                   min(timestamp), max(timestamp),
                   argMax(error_message, timestamp), argMax(error_type, timestamp),
                   argMax(error_source, timestamp), argMax(error_stack, timestamp),
                   argMax(session_id, timestamp), argMax(view_name, timestamp), argMax(url, timestamp),
                   argMax(browser, timestamp), argMax(browser_version, timestamp), argMax(os, timestamp),
                   argMax(service_version, timestamp), argMax(backend_trace_id, timestamp),
                   argMax(user_id, timestamp), argMax(client_ip, timestamp), argMax(country, timestamp),
                   any(application_id), any(env), countIf(sampled = 0),
                   groupUniqArrayIf(50)(backend_trace_id, backend_trace_id != ''),
                   argMax(device_type, timestamp)
            FROM zenplus.apm_rum_events WHERE {group_sql}
        """, parameters=params).result_rows[0]
        if not int(summary[0] or 0):
            return None
        earliest = ch.query(f"""
            SELECT min(timestamp), argMin(service_version, timestamp) FROM zenplus.apm_rum_events
            WHERE timestamp >= {{retention_from:DateTime64(3)}} AND event_type = 'error'
              AND {_ERROR_FINGERPRINT_SQL} = {{fp:String}}
              AND application_id = {{app:String}} AND env = {{env:String}}
        """, parameters={**params, "app": summary[20], "env": summary[21]}).result_rows[0]
        trend = ch.query(f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {bucket} SECOND) AS b, count(), uniqExact(session_id)
            FROM zenplus.apm_rum_events WHERE {group_sql} GROUP BY b ORDER BY b
        """, parameters=params).result_rows
        def facet(column):
            return ch.query(f"""
                SELECT {column}, count(), uniqExact(session_id) FROM zenplus.apm_rum_events
                WHERE {group_sql} GROUP BY {column} ORDER BY count() DESC LIMIT 8
            """, parameters=params).result_rows
        facets = {name: facet(name) for name in ("view_name", "browser", "os", "service_version", "country", "device_type")}
        releases = ch.query(f"""
            SELECT service_version, count(), min(timestamp), max(timestamp) FROM zenplus.apm_rum_events
            WHERE {group_sql} GROUP BY service_version ORDER BY min(timestamp)
        """, parameters=params).result_rows
        sessions = ch.query(f"""
            SELECT session_id, max(timestamp), count(), any(user_id), any(browser), any(view_name)
            FROM zenplus.apm_rum_events WHERE {group_sql}
            GROUP BY session_id ORDER BY max(timestamp) DESC LIMIT 6
        """, parameters=params).result_rows
        # What the user did in the seconds before the latest occurrence.
        crumbs = ch.query("""
            SELECT timestamp, event_type, view_name, action_name, action_type, target,
                   resource_url, method, status_code, duration_ms, error_message, url
            FROM zenplus.apm_rum_events
            WHERE session_id = {sid:String} AND application_id = {app:String} AND env = {env:String}
              AND timestamp <= {at:DateTime64(3)} AND timestamp >= {at:DateTime64(3)} - INTERVAL 10 MINUTE
              AND NOT (event_type = 'view' AND is_final = 0 AND end_reason = 'checkpoint')
            ORDER BY timestamp DESC, is_final ASC LIMIT 12
        """, parameters={"sid": summary[9], "app": summary[20], "env": summary[21], "at": summary[4]}).result_rows
        return summary, earliest, trend, facets, releases, sessions, crumbs

    result = await asyncio.to_thread(query)
    if result is None:
        raise HTTPException(404, "Error group not found in this window")
    summary, earliest, trend, facets, releases, sessions, crumbs = result
    app_id, env_id = summary[20], summary[21]
    issue_rows = await _issue_rows(db, app_id, env_id)
    stored = next((r for r in issue_rows if r["fingerprint"] == fingerprint), None)
    issue = _issue_payload(stored, summary[4], earliest[0], window)
    issue["first_seen_release"] = issue["first_seen_release"] or (earliest[1] or "")

    stack = summary[8] or ""
    maps = await _source_maps_for(db, app_id, summary[15] or "")
    frames, resolved = rum_symbolicate.symbolicate(stack, maps) if stack else ([], 0)
    frame_rows = [rum_symbolicate.frame_payload(f) for f in frames]
    breadcrumbs = []
    for r in reversed(crumbs):
        kind = r[1]
        if kind == "error":
            title = r[10] or "JavaScript error"
        elif kind == "action":
            title = r[3] or r[4] or "User action"
        elif kind == "resource":
            title = f"{r[7] or 'GET'} {r[6]}" + (f" → {int(r[8])}" if r[8] else "")
        elif kind == "long_task":
            title = "Main thread blocked"
        else:
            title = r[2] or r[11] or "Page view"
        breadcrumbs.append({
            "timestamp": r[0], "event_type": kind, "title": title, "view_name": r[2],
            "target": r[5] or None, "duration_ms": float(r[9] or 0) or None,
            "status_code": int(r[8]) if r[8] else None,
        })
    return {
        "fingerprint": fingerprint,
        "range": window.range,
        "window": window.payload(),
        "application_id": app_id,
        "env": env_id,
        "message": summary[5], "error_type": summary[6], "source": summary[7],
        "count": int(summary[0]), "sessions": int(summary[1]), "users": int(summary[2]),
        "unsampled_count": int(summary[22]),
        "first_seen": summary[3], "last_seen": summary[4],
        "first_seen_ever": earliest[0], "first_seen_release": earliest[1] or "",
        "issue": issue,
        "trend": {
            "bucket_seconds": bucket,
            "series": [{"timestamp": r[0], "errors": int(r[1]), "sessions": int(r[2])} for r in trend],
        },
        "facets": {
            name: [{"value": r[0], "count": int(r[1]), "sessions": int(r[2])} for r in rows]
            for name, rows in facets.items()
        },
        "releases": [
            {"service_version": r[0], "count": int(r[1]), "first_seen": r[2], "last_seen": r[3]} for r in releases
        ],
        "recent_sessions": [
            {"session_id": r[0], "last_seen": r[1], "count": int(r[2]), "user_id": r[3], "browser": r[4], "view_name": r[5]}
            for r in sessions
        ],
        "latest": {
            "timestamp": summary[4], "session_id": summary[9], "view_name": summary[10], "url": summary[11],
            "browser": summary[12], "browser_version": summary[13], "os": summary[14],
            "service_version": summary[15], "backend_trace_id": summary[16], "user_id": summary[17],
            "client_ip": summary[18], "country": summary[19], "device_type": summary[24],
            "stack": stack,
        },
        "backend_trace_ids": list(summary[23] or []),
        "frames": frame_rows,
        "symbolication": {
            "frames": len([f for f in frame_rows if f["line"] is not None]),
            "resolved": resolved,
            "maps_available": len(maps),
            "release": summary[15] or "",
        },
        "breadcrumbs": breadcrumbs,
    }


@router.get("/api/v1/apm/rum/source-maps")
async def rum_source_maps(
    application_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    clause = "WHERE application_id = :app" if _str_param(application_id) else ""
    rows = (await db.execute(text(f"""
        SELECT id, application_id, release, file_name, size_bytes, sources_count, uploaded_by, created_at
        FROM rum_source_maps {clause} ORDER BY created_at DESC LIMIT 500
    """), {"app": application_id} if clause else {})).mappings().all()
    return {"items": [{**dict(r), "id": str(r["id"])} for r in rows]}


@router.post("/api/v1/apm/rum/source-maps", status_code=201)
async def rum_upload_source_map(
    request: Request,
    application_id: str = Query(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
    file_name: str = Query(..., min_length=1, max_length=512),
    release: str = Query(default="", max_length=128),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_operator_user),
):
    """Upload one source map. The request body is the map file itself:

        curl -X POST "$ZENPLUS/api/v1/apm/rum/source-maps?application_id=web&release=1.4.0&file_name=app.3f2a1c.js" \\
             -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
             --data-binary @dist/app.3f2a1c.js.map

    ``file_name`` is the minified file the browser loads (a trailing ``.map``
    is stripped). Re-uploading the same application/release/file replaces it.
    """
    body = await request.body()
    if len(body) > SOURCE_MAP_MAX_BYTES:
        raise HTTPException(413, "Source map exceeds 25 MB")
    try:
        payload = json.loads(body)
        assert isinstance(payload, dict) and isinstance(payload.get("mappings"), str)
    except Exception as exc:
        raise HTTPException(400, "Body must be a Source Map v3 JSON document") from exc
    name = rum_symbolicate.file_name_of(file_name)
    if name.endswith(".map"):
        name = name[:-4]
    if not name:
        raise HTTPException(400, "file_name must be the minified file's name, e.g. app.3f2a1c.js")
    gz = gzip.compress(body, compresslevel=6)
    actor = str(getattr(user, "username", "") or getattr(user, "email", "") or "")
    row = (await db.execute(text("""
        INSERT INTO rum_source_maps (application_id, release, file_name, map_gzip, size_bytes, sources_count, uploaded_by)
        VALUES (:app, :release, :file_name, :blob, :size, :sources, :actor)
        ON CONFLICT (application_id, release, file_name) DO UPDATE SET
            map_gzip = EXCLUDED.map_gzip, size_bytes = EXCLUDED.size_bytes,
            sources_count = EXCLUDED.sources_count, uploaded_by = EXCLUDED.uploaded_by, created_at = now()
        RETURNING id, created_at
    """), {
        "app": application_id, "release": release, "file_name": name, "blob": gz,
        "size": len(body), "sources": len(payload.get("sources") or []), "actor": actor,
    })).first()
    await db.commit()
    rum_symbolicate.decoded_map.cache_clear() if hasattr(rum_symbolicate.decoded_map, "cache_clear") else None
    rum_symbolicate._decoded.cache_clear()
    return {
        "id": str(row[0]), "application_id": application_id, "release": release, "file_name": name,
        "size_bytes": len(body), "sources_count": len(payload.get("sources") or []), "created_at": row[1],
    }


@router.delete("/api/v1/apm/rum/source-maps/{map_id}", status_code=204)
async def rum_delete_source_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_operator_user),
):
    result = await db.execute(text("DELETE FROM rum_source_maps WHERE id = :id"), {"id": str(map_id)})
    await db.commit()
    if not result.rowcount:
        raise HTTPException(404, "Source map not found")
    rum_symbolicate._decoded.cache_clear()
    return Response(status_code=204)


@router.get("/api/v1/apm/rum/resources")
async def rum_resources(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "duration_p75",
    order: OrderName = "desc",
    _user=Depends(get_current_user),
):
    sort_sql = _RESOURCE_SORTS.get(sort)
    if sort_sql is None:
        raise HTTPException(400, f"Unsupported resources sort '{sort}'")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(limit=page_size, offset=(page - 1) * page_size)
    group = "application_id, env, view_name, resource_url, resource_type, method, status_code"

    def query():
        total = _ch().query(f"""
            SELECT count() FROM (SELECT 1 FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type = 'resource' AND sampled = 1 GROUP BY {group})
        """, parameters=params).result_rows[0][0]
        rows = _ch().query(f"""
            SELECT application_id, env, view_name, resource_url, resource_type,
                   method, status_code, count() AS event_count,
                   countIf(status_code >= 400 OR attributes['failed'] = 'true') AS failed_count,
                   quantileTDigestIf(0.75)(duration_ms, duration_ms > 0) AS duration_p75,
                   countIf(duration_ms > 0) AS duration_samples,
                   avgIf(transfer_size, transfer_size > 0) AS size_avg,
                   countIf(transfer_size > 0) AS size_samples,
                   countIf(status_code >= 400 OR attributes['failed'] = 'true') / count() AS failure_rate,
                   max(timestamp) AS last_seen, argMax(service_version, timestamp),
                   anyIf(backend_trace_id, backend_trace_id != '') AS primary_trace_id,
                   groupUniqArrayIf(50)(backend_trace_id, backend_trace_id != '') AS backend_trace_ids,
                   quantileTDigestIf(0.75)(dns_ms, has_timing = 1) AS dns_p75,
                   quantileTDigestIf(0.75)(connect_ms, has_timing = 1) AS connect_p75,
                   quantileTDigestIf(0.75)(tls_ms, has_timing = 1) AS tls_p75,
                   quantileTDigestIf(0.75)(wait_ms, has_timing = 1) AS wait_p75,
                   quantileTDigestIf(0.75)(download_ms, has_timing = 1) AS download_p75,
                   countIf(has_timing = 1) AS timing_samples,
                   quantileTDigestIf(0.75)(server_ms, has_server_timing = 1) AS server_p75,
                   quantileTDigestIf(0.75)(db_ms, has_server_timing = 1) AS db_p75,
                   countIf(has_server_timing = 1) AS server_samples,
                   anyIf(protocol, protocol != '') AS protocol,
                   groupUniqArray(5)(sdk_version) AS sdk_versions,
                   countIf(
                       has_timing = 1 AND wait_ms = 0 AND dns_ms = 0 AND connect_ms = 0
                       AND redirect_ms = 0 AND download_ms = 0
                   ) AS opaque_samples
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type = 'resource' AND sampled = 1
            GROUP BY {group}
            ORDER BY {sort_sql} {order.upper()}, resource_url ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=params).result_rows
        # Resources without Server-Timing still get an app/db split when their
        # requests carried a trace header into an APM-instrumented backend.
        timings = _trace_timings([t for r in rows for t in (r[17] or [])])
        return int(total), rows, timings

    total, rows, timings = await asyncio.to_thread(query)
    return {
        "total": total, "page": page, "page_size": page_size, "coverage": _raw_coverage(window),
        "items": [
            {
                "application_id": r[0], "env": r[1], "view_name": r[2],
                "name": r[3].rsplit("/", 1)[-1] or r[3], "url": r[3],
                "resource_type": r[4], "method": r[5],
                "status_code": int(r[6]) if r[6] else None,
                "count": int(r[7]), "failed_count": int(r[8]),
                "duration_p75": _nullable(r[9], r[10]), "duration_samples": int(r[10]),
                "size_avg": _nullable(r[11], r[12]), "size_samples": int(r[12]),
                "failure_rate": float(r[13] or 0), "last_seen": r[14],
                "service_version": r[15], "backend_trace_id": r[16],
                "backend_trace_ids": list(r[17] or []),
                "dns_p75": _nullable(r[18], r[23]), "connect_p75": _nullable(r[19], r[23]),
                "tls_p75": _nullable(r[20], r[23]), "wait_p75": _nullable(r[21], r[23]),
                "download_p75": _nullable(r[22], r[23]), "timing_samples": int(r[23]),
                "server_p75": _nullable(r[24], r[26]), "db_p75": _nullable(r[25], r[26]),
                "server_samples": int(r[26]), "protocol": r[27],
                "sdk_versions": sorted(v for v in (r[28] if len(r) > 28 else []) if v),
                "opaque_samples": int(r[29]) if len(r) > 29 else 0,
                "backend": _summarize_traces(list(r[17] or []), timings),
            }
            for r in rows
        ],
    }


_ACTION_SORTS = {
    "count": "event_count", "error_count": "error_count",
    "duration_p75": "duration_p75", "last_seen": "last_seen",
}


@router.get("/api/v1/apm/rum/actions")
async def rum_actions(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = "count",
    order: OrderName = "desc",
    _user=Depends(get_current_user),
):
    sort_sql = _ACTION_SORTS.get(sort)
    if sort_sql is None:
        raise HTTPException(400, f"Unsupported actions sort '{sort}'")
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    params.update(limit=page_size, offset=(page - 1) * page_size)
    name_expr = _ACTION_NAME_SQL
    type_expr = _ACTION_TYPE_SQL
    group = f"application_id, env, view_name, {name_expr}, {type_expr}, target"

    def query():
        total = _ch().query(f"""
            SELECT count() FROM (SELECT 1 FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type IN ('action', 'long_task') AND sampled = 1 GROUP BY {group})
        """, parameters=params).result_rows[0][0]
        rows = _ch().query(f"""
            SELECT application_id, env, view_name, {name_expr} AS name,
                   {type_expr} AS type, target, count() AS event_count,
                   countIf(attributes['error'] = 'true') AS error_count,
                   quantileTDigestIf(0.75)(duration_ms, duration_ms > 0) AS duration_p75,
                   countIf(duration_ms > 0) AS duration_samples,
                   max(timestamp) AS last_seen, argMax(service_version, timestamp),
                   anyIf(backend_trace_id, backend_trace_id != '') AS primary_trace_id,
                   groupUniqArrayIf(50)(backend_trace_id, backend_trace_id != '') AS backend_trace_ids
            FROM zenplus.apm_rum_events
            WHERE {scope_sql} AND event_type IN ('action', 'long_task') AND sampled = 1
            GROUP BY {group}
            ORDER BY {sort_sql} {order.upper()}, name ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
        """, parameters=params).result_rows
        return int(total), rows

    total, rows = await asyncio.to_thread(query)
    return {
        "total": total, "page": page, "page_size": page_size, "coverage": _raw_coverage(window),
        "items": [
            {
                "application_id": r[0], "env": r[1], "view_name": r[2],
                "name": r[3], "action_name": r[3], "action_type": r[4], "target": r[5],
                "count": int(r[6]), "error_count": int(r[7]),
                "duration_p75": _nullable(r[8], r[9]), "duration_samples": int(r[9]),
                "last_seen": r[10], "service_version": r[11], "backend_trace_id": r[12],
                "backend_trace_ids": list(r[13] or []),
            }
            for r in rows
        ],
    }


@router.get("/api/v1/apm/rum/health")
async def rum_health(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    _user=Depends(get_current_user),
):
    window = _resolve_window(range_, frm, to)
    params, scope_sql = _scope(
        window, application_id, env, view_name, browser, device_type, country,
        service_version, browser_version, os, client_ip, q=q, user_id=user_id,
    )
    try:
        row = await asyncio.to_thread(lambda: _ch().query(f"""
            SELECT count(), max(timestamp),
                   sum(toUInt64OrZero(attributes['sdk.dropped_events'])),
                   groupUniqArrayIf(20)(sdk_version, sdk_version != ''),
                   countIf(country != ''),
                   uniqExactIf(client_ip, client_ip != '')
            FROM zenplus.apm_rum_events WHERE {scope_sql}
        """, parameters=params).result_rows[0])
        accepted, last_event, dropped, versions = int(row[0]), row[1], int(row[2] or 0), list(row[3] or [])
        with_country, distinct_ips = int(row[4] or 0), int(row[5] or 0)
        storage_ok = True
    except Exception:
        accepted, last_event, dropped, versions, storage_ok = 0, None, 0, [], False
        with_country, distinct_ips = 0, 0
    issues = []
    if not storage_ok:
        issues.append("RUM storage query failed")
    if int(_HEALTH["storage_errors"]):
        issues.append("Recent RUM writes failed")
    if int(_HEALTH["rate_limited"]):
        issues.append("Recent RUM events exceeded an intake quota")
    geoip_available = geoip.available()
    if not geoip_available:
        issues.append("GeoIP database missing: visitor countries are not resolved (run scripts/fetch-geoip.py)")
    return {
        "status": "healthy" if not issues else "degraded",
        "geoip": {
            "available": geoip_available,
            "directory": geoip.GEOIP_DIR,
            "events_with_country": with_country,
            "events_total": accepted,
            "distinct_client_ips": distinct_ips,
        },
        "accepted": accepted,
        "accepted_since_process_start": int(_HEALTH["accepted"]),
        "rejected": int(_HEALTH["rejected"]),
        "rate_limited": int(_HEALTH["rate_limited"]),
        "duplicates": int(_HEALTH["duplicates"]),
        "dropped": dropped,
        "sampled_out": 0,
        "storage_errors": int(_HEALTH["storage_errors"]),
        "last_event_at": last_event or _HEALTH_META["last_event_at"],
        "sdk_versions": versions or list((_HEALTH_META["sdk_versions"] or {}).keys()),
        "issues": issues,
    }


_EXPORT_TABS = {
    "views": ("rum_views", "views"),
    "sessions": ("rum_sessions", "last_seen"),
    "errors": ("rum_errors", "count"),
    "resources": ("rum_resources", "count"),
    "actions": ("rum_actions", "count"),
}
EXPORT_MAX_ROWS = 5000
_EXPORT_SKIP = {"backend_trace_ids", "backend", "stack", "sdk_versions"}


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


@router.get("/api/v1/apm/rum/export")
async def rum_export(
    tab: Literal["views", "sessions", "errors", "resources", "actions"],
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    sort: str | None = None,
    order: OrderName = "desc",
    limit: int = Query(default=EXPORT_MAX_ROWS, ge=1, le=EXPORT_MAX_ROWS),
    _user=Depends(get_current_user),
):
    """CSV of one explorer at the current window and filters (up to 5 000 rows).

    Reuses the explorer queries page by page so the file matches what the
    table shows, including sort order.
    """
    import csv
    import io

    function_name, default_sort = _EXPORT_TABS[tab]
    fetch = globals()[function_name]
    common = {
        "range_": range_, "frm": frm, "to": to, "q": q, "user_id": user_id,
        "application_id": application_id, "env": env, "view_name": view_name,
        "browser": browser, "device_type": device_type, "country": country,
        "service_version": service_version, "browser_version": browser_version,
        "os": os, "client_ip": client_ip, "_user": _user,
    }
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        result = await fetch(**common, page=page, page_size=100, sort=sort or default_sort, order=order)
        items = result.get("items") or []
        rows.extend(items)
        if len(items) < 100 or len(rows) >= int(result.get("total") or 0):
            break
        page += 1
    rows = rows[:limit]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in _EXPORT_SKIP and key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    window = _resolve_window(range_, frm, to)
    stamp = window.to.strftime("%Y%m%d-%H%M")
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="rum-{tab}-{window.range}-{stamp}.csv"'},
    )


@router.get("/api/v1/apm/rum/summary")
async def rum_summary(
    range_: RangeName = Query(default="24h", alias="range"),
    frm: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user_id: str | None = Query(default=None, max_length=255),
    application_id: str | None = None,
    env: str | None = None,
    view_name: str | None = None,
    browser: str | None = None,
    device_type: str | None = None,
    country: str | None = None,
    service_version: str | None = None,
    browser_version: str | None = None,
    os: str | None = None,
    client_ip: str | None = None,
    _user=Depends(get_current_user),
):
    """Legacy dashboard contract backed by the corrected analytics queries."""
    common = {
        "range_": range_, "frm": frm, "to": to, "q": q, "user_id": user_id,
        "application_id": application_id, "env": env,
        "view_name": view_name, "browser": browser, "device_type": device_type,
        "country": country, "service_version": service_version,
        "browser_version": browser_version, "os": os, "client_ip": client_ip,
        "_user": _user,
    }
    window = _resolve_window(range_, frm, to)
    overview = await rum_overview(**common)
    views = await rum_views(**common, page=1, page_size=100, sort="views", order="desc")
    sessions = await rum_sessions(
        **common, page=1, page_size=100, sort="last_seen", order="desc"
    )
    vital = overview["vitals"]
    return {
        **overview["totals"],
        "lcp_p75": vital["lcp"]["p75"], "lcp_samples": vital["lcp"]["samples"],
        "inp_p75": vital["inp"]["p75"], "inp_samples": vital["inp"]["samples"],
        "cls_p75": vital["cls"]["p75"], "cls_samples": vital["cls"]["samples"],
        "fcp_p75": vital["fcp"]["p75"], "fcp_samples": vital["fcp"]["samples"],
        "ttfb_p75": vital["ttfb"]["p75"], "ttfb_samples": vital["ttfb"]["samples"],
        "load_p75": vital["load"]["p75"], "load_samples": vital["load"]["samples"],
        "routes": views["items"], "recent_sessions": sessions["items"],
        "coverage": _raw_coverage(window),
    }
