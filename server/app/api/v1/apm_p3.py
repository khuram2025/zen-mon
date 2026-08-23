"""Phase-3 APM surfaces: browser RUM, database insights, and profiles.

The RUM data plane deliberately uses a public ``zpr_`` key constrained by an
exact origin allowlist.  SDK/profile ingestion continues to use secret ``zpi_``
keys.  All read APIs use the normal ZenPlus authenticated session.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.apm import authenticate_ingest_key
from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user

router = APIRouter(tags=["APM phase 3"])

RANGE_SECONDS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
RUM_COLUMNS = [
    "timestamp", "application_id", "service_name", "env", "event_type",
    "session_id", "view_id", "view_name", "url", "user_id", "country",
    "browser", "device_type", "lcp", "inp", "cls", "fcp", "ttfb",
    "load_ms", "error_message", "backend_trace_id", "attributes", "ts_bucket",
]
PROFILE_COLUMNS = [
    "timestamp", "profile_id", "service_name", "env", "service_version",
    "profile_type", "duration_nano", "sample_count", "encoding", "profile_data",
    "trace_id", "span_id", "attributes", "ts_bucket",
]
_RUM_RATE: dict[str, deque[float]] = defaultdict(deque)
_RUM_RATE_LIMIT = 240
_RUM_RATE_WINDOW = 60.0
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")


def _ch():
    return get_ch_client()


def _window(range_: str) -> tuple[datetime, datetime]:
    seconds = RANGE_SECONDS.get(range_, RANGE_SECONDS["1h"])
    end = datetime.now(timezone.utc)
    return datetime.fromtimestamp(end.timestamp() - seconds, tz=timezone.utc), end


def _origin(value: str) -> str:
    """Return a canonical scheme://host[:port] origin or an empty string."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        default = (parsed.scheme == "http" and parsed.port in {None, 80}) or (
            parsed.scheme == "https" and parsed.port in {None, 443}
        )
        return f"{parsed.scheme}://{parsed.hostname.lower()}" + (
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


def _rate_limit(key_id: object, origin: str) -> None:
    now = time.monotonic()
    bucket = _RUM_RATE[f"{key_id}:{origin}"]
    while bucket and bucket[0] <= now - _RUM_RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RUM_RATE_LIMIT:
        raise HTTPException(429, "RUM beacon rate limit exceeded", headers={"Retry-After": "60"})
    bucket.append(now)


def _cors(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


class RumEvent(BaseModel):
    client_token: str = Field(min_length=12, max_length=256)
    application_id: str = Field(min_length=1, max_length=128)
    service_name: str = Field(default="browser", max_length=255)
    event_type: Literal["view", "action", "error", "resource", "long_task"] = "view"
    timestamp_ms: int | None = None
    session_id: str = Field(min_length=1, max_length=128)
    view_id: str = Field(default="", max_length=128)
    view_name: str = Field(default="/", max_length=512)
    url: str = Field(default="", max_length=2048)
    user_id: str = Field(default="", max_length=255)
    lcp: float = Field(default=0, ge=0, le=600000)
    inp: float = Field(default=0, ge=0, le=600000)
    cls: float = Field(default=0, ge=0, le=100)
    fcp: float = Field(default=0, ge=0, le=600000)
    ttfb: float = Field(default=0, ge=0, le=600000)
    load_ms: float = Field(default=0, ge=0, le=600000)
    error_message: str = Field(default="", max_length=4096)
    backend_trace_id: str = Field(default="", max_length=32)
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("backend_trace_id")
    @classmethod
    def valid_trace_id(cls, value: str) -> str:
        if value and not _TRACE_ID.fullmatch(value):
            raise ValueError("backend_trace_id must be 32 hexadecimal characters")
        return value.lower()

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 RUM attributes are allowed")
        return {str(k)[:128]: str(v)[:1024] for k, v in value.items()}

    @field_validator("url")
    @classmethod
    def strip_url_secrets(cls, value: str) -> str:
        parsed = urlsplit(value)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:2048] if parsed.scheme and parsed.netloc else parsed.path[:2048]


class ProfileEnvelope(BaseModel):
    service_name: str = Field(min_length=1, max_length=255)
    env: str = Field(default="prod", max_length=64)
    service_version: str = Field(default="", max_length=128)
    profile_type: Literal["cpu", "alloc", "lock", "wall"] = "cpu"
    timestamp_ms: int | None = None
    duration_nano: int = Field(default=0, ge=0)
    trace_id: str = Field(default="", max_length=32)
    span_id: str = Field(default="", max_length=16)
    encoding: Literal["collapsed", "pprof"] = "collapsed"
    samples: list[dict] = Field(default_factory=list)
    profile_b64: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("trace_id")
    @classmethod
    def valid_trace_id(cls, value: str) -> str:
        if value and not _TRACE_ID.fullmatch(value):
            raise ValueError("trace_id must be 32 hexadecimal characters")
        return value.lower()

    @field_validator("span_id")
    @classmethod
    def valid_span_id(cls, value: str) -> str:
        if value and not _SPAN_ID.fullmatch(value):
            raise ValueError("span_id must be 16 hexadecimal characters")
        return value.lower()

    @field_validator("attributes")
    @classmethod
    def bounded_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 profile attributes are allowed")
        return {str(k)[:128]: str(v)[:1024] for k, v in value.items()}


@router.options("/api/v1/apm/rum/ingest")
async def rum_preflight(request: Request, db: AsyncSession = Depends(get_db)):
    origin = request.headers.get("origin", "")
    token = request.query_params.get("key", "")
    key = await authenticate_ingest_key(token, db, kind="rum")
    allowed = _assert_allowed_origin(origin, list(key.get("origin_allowlist") or []))
    return Response(status_code=204, headers=_cors(allowed))


@router.post("/api/v1/apm/rum/ingest")
async def ingest_rum(body: RumEvent, request: Request, db: AsyncSession = Depends(get_db)):
    key = await authenticate_ingest_key(body.client_token, db, kind="rum")
    origin = _assert_allowed_origin(
        request.headers.get("origin", ""), list(key.get("origin_allowlist") or [])
    )
    _rate_limit(key["id"], origin)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    event_ms = body.timestamp_ms or now_ms
    if abs(event_ms - now_ms) > 86400000:
        raise HTTPException(400, "RUM event timestamp is outside the accepted 24-hour window")
    timestamp = datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
    ua = request.headers.get("user-agent", "")[:255]
    browser = "Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua else "Safari" if "Safari/" in ua else "Other"
    device = "mobile" if re.search(r"Mobile|Android|iPhone", ua, re.I) else "desktop"
    row = [[
        timestamp, body.application_id, body.service_name, key.get("env_name") or "prod",
        body.event_type, body.session_id, body.view_id, body.view_name, body.url,
        body.user_id, "", browser, device, body.lcp, body.inp, body.cls,
        body.fcp, body.ttfb, body.load_ms, body.error_message,
        body.backend_trace_id, body.attributes, int(event_ms / 1000 // 300 * 300),
    ]]
    try:
        await asyncio.to_thread(
            _ch().insert, "apm_rum_events", row,
            column_names=RUM_COLUMNS, database="zenplus",
        )
    except Exception as exc:
        raise HTTPException(503, "RUM storage is temporarily unavailable", headers={"Retry-After": "1"}) from exc
    return Response(status_code=202, headers=_cors(origin))


@router.get("/api/v1/apm/rum/summary")
async def rum_summary(
    range_: str = Query(default="24h", alias="range"),
    application_id: str | None = None,
    _user=Depends(get_current_user),
):
    frm, to = _window(range_)
    params: dict = {"frm": frm, "to": to}
    app_filter = ""
    if application_id:
        params["app"] = application_id
        app_filter = "AND application_id = {app:String}"
    def query():
        vitals = _ch().query(f"""
            SELECT count(), uniqExact(session_id), uniqExact(view_id),
                   quantileTDigest(0.75)(if(lcp > 0, lcp, NULL)),
                   quantileTDigest(0.75)(if(inp > 0, inp, NULL)),
                   quantileTDigest(0.75)(if(cls > 0, cls, NULL)),
                   countIf(event_type = 'error')
            FROM zenplus.apm_rum_events
            WHERE timestamp >= {{frm:DateTime64(3)}} AND timestamp < {{to:DateTime64(3)}} {app_filter}
        """, parameters=params).result_rows[0]
        views = _ch().query(f"""
            SELECT application_id, view_name, countIf(event_type = 'view'),
                   uniqExact(session_id), quantileTDigest(0.75)(if(lcp > 0, lcp, NULL)),
                   quantileTDigest(0.75)(if(inp > 0, inp, NULL)),
                   quantileTDigest(0.75)(if(cls > 0, cls, NULL)),
                   countIf(event_type = 'error'), max(timestamp)
            FROM zenplus.apm_rum_events
            WHERE timestamp >= {{frm:DateTime64(3)}} AND timestamp < {{to:DateTime64(3)}} {app_filter}
            GROUP BY application_id, view_name ORDER BY count() DESC LIMIT 100
        """, parameters=params).result_rows
        sessions = _ch().query(f"""
            SELECT session_id, any(application_id), min(timestamp), max(timestamp),
                   count(), countIf(event_type = 'error'), any(browser), any(device_type),
                   anyIf(backend_trace_id, backend_trace_id != '')
            FROM zenplus.apm_rum_events
            WHERE timestamp >= {{frm:DateTime64(3)}} AND timestamp < {{to:DateTime64(3)}} {app_filter}
            GROUP BY session_id ORDER BY max(timestamp) DESC LIMIT 100
        """, parameters=params).result_rows
        return vitals, views, sessions
    vitals, views, sessions = await asyncio.to_thread(query)
    return {
        "events": int(vitals[0]), "sessions": int(vitals[1]), "views": int(vitals[2]),
        "lcp_p75": float(vitals[3] or 0), "inp_p75": float(vitals[4] or 0),
        "cls_p75": float(vitals[5] or 0), "errors": int(vitals[6]),
        "routes": [
            {"application_id": r[0], "view_name": r[1], "views": int(r[2]),
             "sessions": int(r[3]), "lcp_p75": float(r[4] or 0),
             "inp_p75": float(r[5] or 0), "cls_p75": float(r[6] or 0),
             "errors": int(r[7]), "last_seen": r[8]} for r in views
        ],
        "recent_sessions": [
            {"session_id": r[0], "application_id": r[1], "started_at": r[2],
             "last_seen": r[3], "events": int(r[4]), "errors": int(r[5]),
             "browser": r[6], "device_type": r[7], "backend_trace_id": r[8]} for r in sessions
        ],
    }


@router.get("/api/v1/apm/database")
async def database_insights(
    service: str,
    range_: str = Query(default="1h", alias="range"),
    env: str | None = None,
    _user=Depends(get_current_user),
):
    frm, to = _window(range_)
    params: dict = {"frm": frm, "to": to, "service": service}
    env_filter = ""
    if env:
        params["env"] = env
        env_filter = "AND env = {env:String}"
    rows = await asyncio.to_thread(lambda: _ch().query(f"""
        SELECT lower(hex(MD5(db_statement))) AS query_digest, any(db_statement),
               any(db_system), any(db_operation), count(), countIf(has_error = 1),
               quantileTDigest(0.95)(duration_nano / 1e6), sum(duration_nano) / 1e6,
               any(trace_id), max(timestamp)
        FROM zenplus.apm_spans
        WHERE timestamp >= {{frm:DateTime64(3)}} AND timestamp < {{to:DateTime64(3)}}
          AND service_name = {{service:String}} AND db_statement != '' {env_filter}
        GROUP BY db_statement ORDER BY sum(duration_nano) DESC LIMIT 100
    """, parameters=params).result_rows)
    return {"service": service, "queries": [
        {"query_digest": r[0], "statement": r[1], "db_system": r[2],
         "operation": r[3], "calls": int(r[4]),
         "error_rate": int(r[5]) / max(int(r[4]), 1), "p95_ms": float(r[6] or 0),
         "total_ms": float(r[7] or 0), "trace_id": r[8], "last_seen": r[9]}
        for r in rows
    ]}


@router.post("/v1development/profiles")
async def ingest_profile(
    body: ProfileEnvelope,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    key = await authenticate_ingest_key(authorization or "", db, kind="sdk")
    if body.encoding == "pprof":
        try:
            raw = base64.b64decode(body.profile_b64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "profile_b64 is not valid base64") from exc
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise HTTPException(413, "profile payload must be between 1 byte and 8 MiB")
        profile_data = body.profile_b64
        sample_count = 0
    else:
        if not body.samples or len(body.samples) > 100000:
            raise HTTPException(400, "collapsed profiles require 1..100000 samples")
        cleaned = []
        for sample in body.samples:
            stack = str(sample.get("stack") or "")[:8192]
            try:
                value = max(float(sample.get("value") or sample.get("count") or 0), 0)
            except (TypeError, ValueError):
                continue
            if stack and value:
                cleaned.append({"stack": stack, "value": value})
        if not cleaned:
            raise HTTPException(400, "profile contains no valid samples")
        profile_data = json.dumps(cleaned, separators=(",", ":"))
        sample_count = len(cleaned)
    event_ms = body.timestamp_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    timestamp = datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
    row = [[timestamp, str(uuid.uuid4()), body.service_name, body.env or key.get("env_name") or "prod",
            body.service_version, body.profile_type, body.duration_nano, sample_count,
            body.encoding, profile_data, body.trace_id, body.span_id, body.attributes,
            int(event_ms / 1000 // 300 * 300)]]
    await asyncio.to_thread(
        _ch().insert, "apm_profiles", row, column_names=PROFILE_COLUMNS, database="zenplus"
    )
    return {"partialSuccess": {}, "acceptedProfiles": 1}


@router.get("/api/v1/apm/profiles")
async def list_profiles(
    service: str,
    range_: str = Query(default="1h", alias="range"),
    trace_id: str | None = None,
    _user=Depends(get_current_user),
):
    frm, to = _window(range_)
    params: dict = {"frm": frm, "to": to, "service": service}
    trace_filter = ""
    if trace_id:
        if not _TRACE_ID.fullmatch(trace_id):
            raise HTTPException(400, "trace_id must be 32 hexadecimal characters")
        params["trace"] = trace_id.lower()
        trace_filter = "AND trace_id = {trace:String}"
    rows = await asyncio.to_thread(lambda: _ch().query(f"""
        SELECT profile_id, timestamp, profile_type, service_version, duration_nano,
               sample_count, encoding, if(encoding = 'collapsed', profile_data, ''), trace_id, span_id
        FROM zenplus.apm_profiles
        WHERE timestamp >= {{frm:DateTime64(3)}} AND timestamp < {{to:DateTime64(3)}}
          AND service_name = {{service:String}} {trace_filter}
        ORDER BY timestamp DESC LIMIT 50
    """, parameters=params).result_rows)
    profiles = []
    for r in rows:
        samples = []
        if r[6] == "collapsed":
            try:
                samples = json.loads(r[7])
            except (TypeError, json.JSONDecodeError):
                samples = []
        profiles.append({"profile_id": str(r[0]), "timestamp": r[1], "profile_type": r[2],
                         "service_version": r[3], "duration_nano": int(r[4]),
                         "sample_count": int(r[5]), "encoding": r[6], "samples": samples,
                         "trace_id": r[8], "span_id": r[9]})
    return {"service": service, "profiles": profiles}


_RUM_SDK = r'''(function(){
  "use strict";
  var script=document.currentScript||{};
  var key=script.dataset?script.dataset.key:"";
  var app=(script.dataset&&script.dataset.app)||location.hostname;
  var service=(script.dataset&&script.dataset.service)||app;
  if(!key){console.warn("ZenPlus RUM: data-key is required");return;}
  var endpoint=new URL("/api/v1/apm/rum/ingest",script.src||location.href);endpoint.searchParams.set("key",key);endpoint=endpoint.toString();
  var sid=sessionStorage.getItem("zp_rum_sid")||crypto.randomUUID();
  sessionStorage.setItem("zp_rum_sid",sid);
  var vid=crypto.randomUUID(), vitals={};
  function hex(n){var a=new Uint8Array(n);crypto.getRandomValues(a);return Array.from(a,function(x){return x.toString(16).padStart(2,"0")}).join("");}
  function send(type,extra){var p=Object.assign({client_token:key,application_id:app,service_name:service,event_type:type,timestamp_ms:Date.now(),session_id:sid,view_id:vid,view_name:location.pathname,url:location.href},vitals,extra||{});originalFetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p),keepalive:true,credentials:"omit"}).catch(function(){});}
  try{new PerformanceObserver(function(l){l.getEntries().forEach(function(e){vitals.lcp=e.startTime;});}).observe({type:"largest-contentful-paint",buffered:true});}catch(e){}
  try{new PerformanceObserver(function(l){l.getEntries().forEach(function(e){vitals.inp=Math.max(vitals.inp||0,e.duration||0);});}).observe({type:"event",buffered:true,durationThreshold:40});}catch(e){}
  try{new PerformanceObserver(function(l){l.getEntries().forEach(function(e){if(!e.hadRecentInput)vitals.cls=(vitals.cls||0)+e.value;});}).observe({type:"layout-shift",buffered:true});}catch(e){}
  addEventListener("error",function(e){send("error",{error_message:String(e.message||"JavaScript error")});});
  addEventListener("unhandledrejection",function(e){send("error",{error_message:String(e.reason||"Unhandled rejection")});});
  var originalFetch=window.fetch;
  if(originalFetch){window.fetch=function(input,init){init=init||{};var target=new URL(typeof input==="string"?input:input.url,location.href);if(target.origin!==location.origin)return originalFetch(input,init);var headers=new Headers(init.headers||{}),trace=hex(16),span=hex(8);if(!headers.has("traceparent"))headers.set("traceparent","00-"+trace+"-"+span+"-01");init.headers=headers;return originalFetch(input,init).then(function(r){send("resource",{backend_trace_id:trace,attributes:{method:init.method||"GET",status:String(r.status)}});return r;});};}
  var nav=performance.getEntriesByType("navigation")[0];if(nav){vitals.ttfb=nav.responseStart;vitals.load_ms=nav.loadEventEnd;vitals.fcp=(performance.getEntriesByName("first-contentful-paint")[0]||{}).startTime||0;}
  addEventListener("pagehide",function(){send("view");});setTimeout(function(){send("view");},5000);
})();'''


@router.get("/api/v1/apm/rum/sdk.js")
async def rum_sdk():
    return PlainTextResponse(
        _RUM_SDK, media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
    )
