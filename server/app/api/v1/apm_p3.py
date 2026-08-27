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
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.apm import authenticate_ingest_key
from app.api.v1.rum import RumEvent, _assert_allowed_origin, _origin, router as rum_router
from app.api.v1.rum_sdk import RUM_SDK as _RUM_SDK
from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user

router = APIRouter(tags=["APM phase 3"])
router.include_router(rum_router)

RANGE_SECONDS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}
PROFILE_COLUMNS = [
    "timestamp", "profile_id", "service_name", "env", "service_version",
    "profile_type", "duration_nano", "sample_count", "encoding", "profile_data",
    "trace_id", "span_id", "attributes", "ts_bucket",
]
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")


def _ch():
    return get_ch_client()


def _window(range_: str) -> tuple[datetime, datetime]:
    seconds = RANGE_SECONDS.get(range_, RANGE_SECONDS["1h"])
    end = datetime.now(timezone.utc)
    return datetime.fromtimestamp(end.timestamp() - seconds, tz=timezone.utc), end


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
