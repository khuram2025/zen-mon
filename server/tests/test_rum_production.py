from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1.apm import IngestKeyCreate, IngestKeyResponse, create_ingest_key
from app.api.v1 import rum
from app.api.v1.rum import RumEvent
from app.api.v1.rum_sdk import RUM_SDK
from app.main import _DynamicRumCORSMiddleware


def _request(body: bytes = b"{}", *, origin: str = "https://portal.example.com",
             user_agent: str = "Mozilla/5.0 Chrome/126.0.0.0") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [
        (b"origin", origin.encode()),
        (b"user-agent", user_agent.encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    return Request({
        "type": "http", "method": "POST", "path": "/api/v1/apm/rum/ingest",
        "query_string": b"", "headers": headers, "client": ("203.0.113.9", 4321),
        "server": ("test", 80), "scheme": "https",
    }, receive)


def _event(**overrides) -> RumEvent:
    values = {
        "client_token": "zpr_1234567890123456",
        "event_id": "event-12345678",
        "application_id": "portal",
        "session_id": "session-12345678",
        "view_id": "view-12345678",
        "url": "https://portal.example.com/orders/123456?token=secret#x",
    }
    values.update(overrides)
    return RumEvent(**values)


def test_rum_schema_forbids_unknown_fields_and_preserves_null_vitals():
    event = _event()
    assert event.lcp is None
    assert event.cls is None
    with pytest.raises(ValidationError):
        _event(unbounded_payload="no")


def test_server_privacy_scrubs_urls_attributes_and_pseudonymizes_users():
    event = _event(
        user_id="alice@example.com",
        attributes={"authorization": "Bearer secret", "team": "alice@example.com"},
        error_message="password=hunter2 for alice@example.com",
    )
    assert event.url == "https://portal.example.com/orders/:id"
    assert "authorization" not in event.attributes
    assert event.attributes["team"] == "[email]"
    assert "hunter2" not in event.error_message
    row = rum._row(event, {"env_name": "prod"}, _request())
    assert row[9].startswith("usr_")
    assert "alice" not in row[9]
    assert _event(url="https://portal.example.com/users/42").url.endswith("/users/:id")
    assert _event(url="https://portal.example.com/users/alice%40example.com").url.endswith(
        "/users/:id"
    )


def test_row_has_explicit_vital_presence_and_rich_user_agent_fields():
    event = _event(lcp=1200, cls=0.0, is_final=True, event_type="view")
    row = rum._row(event, {"env_name": "prod"}, _request())
    mapped = dict(zip(rum.RUM_COLUMNS, row))
    assert mapped["has_lcp"] == 1
    assert mapped["has_inp"] == 0
    assert mapped["has_cls"] == 1
    assert mapped["browser"] == "Chrome"
    assert mapped["browser_version"] == "126.0.0.0"
    assert mapped["is_final"] == 1
    assert mapped["sampled"] == 1


def test_unsampled_error_is_typed_and_zero_sample_rate_is_valid():
    event = _event(event_type="error", sampled=False, sample_rate=0, error_message="boom")
    row = dict(zip(rum.RUM_COLUMNS, rum._row(event, {"env_name": "prod"}, _request())))
    assert row["sampled"] == 0
    assert row["sample_rate"] == 0


@pytest.mark.asyncio
async def test_payload_cap_and_batch_contract():
    oversized = _request(b"{}")
    oversized.scope["headers"] = [(b"content-length", str(rum.MAX_BODY_BYTES + 1).encode())]
    with pytest.raises(HTTPException) as exc:
        await rum._parse_payload(oversized)
    assert exc.value.status_code == 413

    streamed = _request(b"x" * (rum.MAX_BODY_BYTES + 1))
    streamed.scope["headers"] = [
        header for header in streamed.scope["headers"] if header[0] != b"content-length"
    ]
    with pytest.raises(HTTPException) as exc:
        await rum._parse_payload(streamed)
    assert exc.value.status_code == 413

    payload = json.dumps({"events": [_event().model_dump(mode="json")]}).encode()
    events = await rum._parse_payload(_request(payload))
    assert len(events) == 1
    assert events[0].event_id == "event-12345678"


@pytest.mark.asyncio
async def test_bad_timestamp_is_rejected_before_dedupe_reservation(monkeypatch):
    old = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
    event = _event(timestamp_ms=old)
    touched = False

    async def parse(_request):
        return [event]

    async def auth(*_args, **_kwargs):
        return {"id": "key", "origin_allowlist": ["https://portal.example.com"],
                "application_id": "portal", "env_name": "prod"}

    async def quota(*_args, **_kwargs):
        return None

    async def dedupe(*_args, **_kwargs):
        nonlocal touched
        touched = True
        return [event]

    monkeypatch.setattr(rum, "_parse_payload", parse)
    monkeypatch.setattr(rum, "authenticate_ingest_key", auth)
    monkeypatch.setattr(rum, "_enforce_quota", quota)
    monkeypatch.setattr(rum, "_dedupe", dedupe)
    with pytest.raises(HTTPException) as exc:
        await rum.ingest_rum(_request(), SimpleNamespace())
    assert exc.value.status_code == 400
    assert touched is False


@pytest.mark.asyncio
async def test_application_binding_is_enforced_before_storage(monkeypatch):
    event = _event(application_id="wrong-app")

    async def parse(_request):
        return [event]

    async def auth(*_args, **_kwargs):
        return {"id": "key", "origin_allowlist": ["https://portal.example.com"],
                "application_id": "portal", "env_name": "prod"}

    monkeypatch.setattr(rum, "_parse_payload", parse)
    monkeypatch.setattr(rum, "authenticate_ingest_key", auth)
    with pytest.raises(HTTPException) as exc:
        await rum.ingest_rum(_request(), SimpleNamespace())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unbound_legacy_rum_key_is_rejected_at_intake(monkeypatch):
    event = _event()

    async def parse(_request):
        return [event]

    async def auth(*_args, **_kwargs):
        return {"id": "legacy-key", "origin_allowlist": ["https://portal.example.com"],
                "application_id": None, "env_name": "prod"}

    monkeypatch.setattr(rum, "_parse_payload", parse)
    monkeypatch.setattr(rum, "authenticate_ingest_key", auth)
    with pytest.raises(HTTPException) as exc:
        await rum.ingest_rum(_request(), SimpleNamespace())
    assert exc.value.status_code == 403
    assert "application-bound" in exc.value.detail
    assert exc.value.headers["Access-Control-Allow-Origin"] == "https://portal.example.com"


@pytest.mark.asyncio
async def test_storage_failure_releases_dedupe_reservation(monkeypatch):
    event = _event()
    released = []

    async def parse(_request):
        return [event]

    async def auth(*_args, **_kwargs):
        return {"id": "key", "origin_allowlist": ["https://portal.example.com"],
                "application_id": "portal", "env_name": "prod"}

    async def noop(*_args, **_kwargs):
        return None

    async def dedupe(*_args, **_kwargs):
        return [event]

    async def release(key_id, events):
        released.extend((key_id, [e.event_id for e in events]))

    class BrokenCH:
        def insert(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(rum, "_parse_payload", parse)
    monkeypatch.setattr(rum, "authenticate_ingest_key", auth)
    monkeypatch.setattr(rum, "_enforce_quota", noop)
    monkeypatch.setattr(rum, "_dedupe", dedupe)
    monkeypatch.setattr(rum, "_release_dedupe", release)
    monkeypatch.setattr(rum, "_ch", lambda: BrokenCH())
    with pytest.raises(HTTPException) as exc:
        await rum.ingest_rum(_request(), SimpleNamespace())
    assert exc.value.status_code == 503
    assert released == ["key", ["event-12345678"]]


@pytest.mark.asyncio
async def test_quota_charges_shared_counters_once_and_sessions_by_event_count(monkeypatch):
    calls = []

    class FakeRedis:
        async def eval(self, script, num_keys, *args):
            amount = int(args[-2])
            calls.append((num_keys, args[:-2], amount))
            return [amount, amount] if num_keys == 2 else amount

    async def redis_client():
        return FakeRedis()

    monkeypatch.setattr(rum, "_redis_client", redis_client)
    await rum._enforce_quota(
        "key-1", "https://portal.example.com", "203.0.113.9",
        {"session-a": 2, "session-b": 1}, 3,
    )
    assert [call[0] for call in calls] == [2, 1, 1]
    assert [call[2] for call in calls] == [3, 2, 1]
    assert all("203.0.113.9" not in str(call[1]) for call in calls)


@pytest.mark.asyncio
async def test_ingest_dual_writes_raw_and_rollup_with_stable_tokens(monkeypatch):
    first = _event(event_type="error", error_message="first")
    second = _event(event_type="error", error_message="second")
    inserted = {}
    rolled_up = {}

    async def parse(_request):
        return [first, second]

    async def auth(*_args, **_kwargs):
        return {"id": "key", "origin_allowlist": ["https://portal.example.com"],
                "application_id": "portal", "env_name": "prod"}

    async def noop(*_args, **_kwargs):
        return None

    async def dedupe(*_args, **_kwargs):
        return [first]

    class CH:
        def insert(self, table, rows, **kwargs):
            inserted.update(table=table, rows=rows, **kwargs)

        def command(self, sql, **kwargs):
            rolled_up.update(sql=sql, **kwargs)

    monkeypatch.setattr(rum, "_parse_payload", parse)
    monkeypatch.setattr(rum, "authenticate_ingest_key", auth)
    monkeypatch.setattr(rum, "_enforce_quota", noop)
    monkeypatch.setattr(rum, "_dedupe", dedupe)
    monkeypatch.setattr(rum, "_ch", lambda: CH())
    response = await rum.ingest_rum(_request(), SimpleNamespace())
    assert response.status_code == 202
    mapped = dict(zip(rum.RUM_COLUMNS, inserted["rows"][0]))
    assert mapped["error_message"] == "first"
    assert mapped["dedupe_id"] == hashlib.sha256(
        rum._dedupe_identity("key", first).encode()
    ).hexdigest()
    settings = inserted["settings"]
    assert "async_insert" not in settings
    assert settings["insert_deduplicate"] == 1
    assert settings["insert_deduplication_token"].startswith("rum-")
    assert "apm_rum_metrics_5m" in rolled_up["sql"]
    assert rolled_up["parameters"]["dedupe_ids"] == [mapped["dedupe_id"]]
    exact_ms = int(mapped["timestamp"].timestamp() * 1000)
    assert rolled_up["parameters"]["batch_from_ms"] == exact_ms
    assert rolled_up["parameters"]["batch_to_ms"] == exact_ms
    assert "fromUnixTimestamp64Milli" in rolled_up["sql"]
    assert "AS bucket_timestamp" in rolled_up["sql"]
    assert "WHERE r.timestamp" in rolled_up["sql"]
    assert rolled_up["settings"]["insert_deduplication_token"] == (
        "rollup-" + settings["insert_deduplication_token"]
    )


@pytest.mark.asyncio
async def test_intake_errors_are_cors_visible_to_the_sdk(monkeypatch):
    async def reject(_request):
        raise HTTPException(422, "bad payload")

    monkeypatch.setattr(rum, "_parse_payload", reject)
    with pytest.raises(HTTPException) as exc:
        await rum.ingest_rum(_request(), SimpleNamespace())
    assert exc.value.headers["Access-Control-Allow-Origin"] == "https://portal.example.com"


def test_key_models_expose_backward_compatible_application_binding():
    assert IngestKeyCreate(name="legacy", kind="rum", origin_allowlist=["https://a.test"]).application_id is None
    response = IngestKeyResponse(
        id="7eb7fdb0-ee5f-49b1-ab8e-15a5c52aca44", name="portal", kind="rum",
        key_prefix="zpr_example", origin_allowlist=["https://a.test"],
        application_id="portal", enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    assert response.application_id == "portal"
    with pytest.raises(ValidationError):
        IngestKeyCreate(
            name="invalid", kind="rum", application_id="bad app/id",
            origin_allowlist=["https://a.test"],
        )


@pytest.mark.asyncio
async def test_new_rum_keys_require_application_binding():
    body = IngestKeyCreate(
        name="unsafe-unbound", kind="rum", origin_allowlist=["https://portal.example.com"]
    )
    with pytest.raises(HTTPException) as exc:
        await create_ingest_key(body, SimpleNamespace(), SimpleNamespace(id=None))
    assert exc.value.status_code == 400


def test_sdk_contains_standards_algorithms_safe_instrumentation_and_privacy_controls():
    assert "e.startTime-clsLast>1000" in RUM_SDK
    assert "e.startTime-clsStart>5000" in RUM_SDK
    assert "Math.floor(interactionCount()/50)" in RUM_SDK
    assert "performance.interactionCount" in RUM_SDK
    assert "nativeCount-interactionBase" in RUM_SDK
    assert "(maxInteractionId-minInteractionId)/7" in RUM_SDK
    assert 'observe("first-input"' not in RUM_SDK
    assert 'supportedEntryTypes.indexOf("layout-shift")>=0' in RUM_SDK
    assert "if(clsSupported)view.vitals.cls=clsValue" in RUM_SDK
    assert "if(view.finalized)return" in RUM_SDK
    assert 'is_final:true' in RUM_SDK
    assert '["pushState","replaceState"]' in RUM_SDK
    assert 'new Request(input,init)' in RUM_SDK
    assert 'if(target.origin===location.origin&&!tp)' in RUM_SDK
    assert '(sampled?"01":"00")' in RUM_SDK
    assert 'supplied=Object.prototype.hasOwnProperty.call(z.headers,"traceparent")' in RUM_SDK
    assert '||target.origin!==location.origin' not in RUM_SDK
    assert 'if(/^(fetch|xmlhttprequest)$/' in RUM_SDK
    assert 'storageSet(sk+"inflight"' in RUM_SDK
    assert 'storageSet(sk+"retry_length"' in RUM_SDK
    assert "retryBatchLength=Math.min(failed.length,queue.length)" in RUM_SDK
    assert "MAX_BATCH_BYTES=256*1024" in RUM_SDK
    assert "MAX_UNLOAD_BYTES=60*1024" in RUM_SDK
    assert "byteSize(batchBody(candidate))<limit" in RUM_SDK
    assert "if(r.status===413)" in RUM_SDK
    assert "r.status===408||r.status===429||r.status>=500" in RUM_SDK
    assert 'intakeError("network",true,batch)' in RUM_SDK
    assert 'namespace=endpoint+"|"+key+"|"+app' in RUM_SDK
    assert "t-last>1800000" in RUM_SDK
    assert "ensureSession(!isCheckpoint)" in RUM_SDK
    assert "rotated=!!touch&&(!sid||t-last>1800000)" in RUM_SDK
    assert "Date.now()-last<=1800000" in RUM_SDK
    assert "view.finalized=true" in RUM_SDK
    assert "finalized:false,hard:false" in RUM_SDK
    assert "if(rotated&&isFinal){view.finalized=true;persist();return;}" in RUM_SDK
    assert 'if(rotated&&sampled&&!isViewStart)queue.push(Object.assign(base("view")' in RUM_SDK
    assert 'end_reason:"view_start"' in RUM_SDK
    assert 'end_reason:"checkpoint"' in RUM_SDK
    assert 'reason==="pagehide"||reason==="hidden"' in RUM_SDK
    assert "if(unloading)sendUnloadTail()" in RUM_SDK
    assert "textContent" not in RUM_SDK
    assert "target.value" not in RUM_SDK
    assert "e.target.value" not in RUM_SDK
    assert "sessionReplay:false" in RUM_SDK
    assert "capture and storage are not enabled" in RUM_SDK
    assert 'sampled:sampled' in RUM_SDK
    assert 'else if(u.email)' in RUM_SDK
    assert 'Object.keys(context).length>=32' in RUM_SDK
    assert 'else if(document.visibilityState==="visible")' in RUM_SDK
    assert 'route.indexOf("#/")===0' in RUM_SDK
    assert r"/^(?:\d+|" in RUM_SDK
    assert "decodeURIComponent(p)" in RUM_SDK
    assert "epoch!==consentEpoch||!consent" in RUM_SDK
    assert "if(!next){consentEpoch++;queue=[];inflight=[];retryBatchLength=0;}" in RUM_SDK
    assert 'privacy==="strict"?[]' in RUM_SDK
    assert 'if(privacy==="strict")return' in RUM_SDK
    for method in ("grantConsent", "setUser", "setContext", "addAction", "startView"):
        assert method in RUM_SDK


@pytest.mark.asyncio
async def test_sdk_response_allows_anonymous_cross_origin_loading():
    response = await rum.rum_sdk()
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"


def test_dynamic_rum_cors_bypasses_only_the_key_scoped_intake():
    inner = FastAPI()

    @inner.api_route("/api/v1/apm/rum/ingest", methods=["OPTIONS", "POST"])
    async def intake_probe():
        return Response(status_code=204, headers={"X-Route-Reached": "rum"})

    @inner.options("/unrelated")
    async def unrelated_probe():
        return Response(status_code=204, headers={"X-Route-Reached": "unrelated"})

    app = _DynamicRumCORSMiddleware(
        inner,
        allow_origins=["https://admin.example.com"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    client = TestClient(app)
    headers = {
        "Origin": "https://customer.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    rum_response = client.options("/api/v1/apm/rum/ingest", headers=headers)
    assert rum_response.status_code == 204
    assert rum_response.headers["x-route-reached"] == "rum"

    unrelated = client.options("/unrelated", headers=headers)
    assert unrelated.status_code == 400
    assert "x-route-reached" not in unrelated.headers


def test_sdk_is_valid_javascript_when_node_is_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    completed = subprocess.run(
        [node, "--check", "-"], input=RUM_SDK, text=True,
        capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_views_and_session_timeline_response_contract(monkeypatch):
    now = datetime.now(timezone.utc)

    class Result:
        def __init__(self, rows):
            self.result_rows = rows

    class ViewCH:
        def query(self, sql, parameters=None):
            if "SELECT count() FROM (" in sql:
                return Result([[1]])
            return Result([[
                "portal", "prod", "/orders", "https://portal.example.com/orders",
                3, 2, 1, 0.5, 2100.0, 2, 180.0, 2, 0.08, 2,
                1200.0, 2, 600.0, 2, 2500.0, 2, now,
                "a" * 32, ["a" * 32, "b" * 32], "2026.8.27",
            ]])

    monkeypatch.setattr(rum, "_ch", lambda: ViewCH())
    views = await rum.rum_views(
        range_="24h", application_id=None, env=None, view_name=None, browser=None,
        device_type=None, country=None, service_version=None, browser_version=None,
        os=None, page=1, page_size=25, sort="views", order="desc", _user=object(),
    )
    assert views["items"][0]["errors"] == views["items"][0]["error_count"] == 1
    assert views["items"][0]["lcp_samples"] == 2
    assert views["items"][0]["backend_trace_ids"] == ["a" * 32, "b" * 32]

    class SessionCH:
        def __init__(self):
            self.calls = 0
            self.sql = []
            self.parameters = []

        def query(self, sql, parameters=None):
            self.calls += 1
            self.sql.append(sql)
            self.parameters.append(parameters)
            if "GROUP BY application_id" in sql:
                return Result([[
                    "portal", "prod", now, now + timedelta(seconds=2), 1, 1, 1, 0, 1,
                    "Chrome", "126", "Windows", "desktop", "SA", "usr_x",
                    ["a" * 32], "2.0.0", "2026.8.27", 1, 2, "203.0.113.7",
                    "4g", 50.0, 9.6, "en-US", "Asia/Riyadh", "1920x1080", "1280x720",
                ]])
            if "apm_spans" in sql:
                return Result([[
                    "a" * 32, 92.5, 40.25, 2, 5, "orders-api", ["postgresql"], 0,
                ]])
            return Result([[
                now, "event-12345678", "resource", "view-1", "/orders",
                "https://portal.example.com/orders", "", "", "", 125.0,
                "https://portal.example.com/api/orders", "fetch", "GET", 200,
                512, 400, "", "", "", "", "", "a" * 32,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                0, 0, 0, 0, 0, 0, 0, "", {}, {},
                "2.0.0", "2026.8.27", "126", "Windows", 1,
                0.0, 1.5, 3.0, 2.0, 110.0, 6.5, 0.5, 0.0, 92.5, 40.25, 1, 1, "h2",
            ]])

    session_ch = SessionCH()
    monkeypatch.setattr(rum, "_ch", lambda: session_ch)
    detail = await rum.rum_session_detail(
        "session-12345678", range_="24h", application_id="portal", env="prod",
        view_name=None, browser=None, device_type=None, country=None,
        service_version=None, browser_version=None, os=None,
        page=1, page_size=100, _user=object(),
    )
    event = detail["timeline"][0]
    assert event["name"] == "orders"
    assert event["url"] == "https://portal.example.com/api/orders"
    assert event["size_bytes"] == 512
    assert event["lcp"] is None and event["vitals"]["lcp"] is None
    assert event["service_version"] == "2026.8.27"
    assert event["sampled"] is True
    # NSX-style request breakdown: browser phases plus server execution split.
    assert event["timing"]["wait_ms"] == 110.0
    assert event["timing"]["server_ms"] == 92.5
    assert event["timing"]["db_ms"] == 40.25
    assert event["timing"]["has_server_timing"] is True
    assert event["timing"]["protocol"] == "h2"
    assert event["backend"]["service"] == "orders-api"
    assert event["backend"]["db_calls"] == 2
    assert detail["backend_summary"]["services"] == ["orders-api"]
    assert detail["backend_summary"]["avg_server_ms"] == 92.5
    assert detail["session"]["connection_type"] == "4g"
    assert detail["session"]["connection_rtt_ms"] == 50.0
    assert detail["session"]["timezone"] == "Asia/Riyadh"
    assert detail["session"]["viewport"] == "1280x720"
    assert detail["coverage"]["raw_retention_days"] == 14
    assert detail["coverage"]["partial"] is False
    assert session_ch.parameters[1]["selected_app"] == "portal"
    assert session_ch.parameters[1]["selected_env"] == "prod"
    assert "application_id = {selected_app:String}" in session_ch.sql[1]
    assert "env = {selected_env:String}" in session_ch.sql[1]


@pytest.mark.asyncio
async def test_raw_timeseries_counts_only_canonical_sampled_view_starts(monkeypatch):
    statements = []

    class Result:
        result_rows = []

    class CH:
        def query(self, sql, parameters=None):
            statements.append(sql)
            return Result()

    monkeypatch.setattr(rum, "_ch", lambda: CH())
    result = await rum.rum_timeseries(
        range_="24h", application_id=None, env=None, view_name=None, browser=None,
        device_type=None, country=None, service_version=None, browser_version=None,
        os=None, _user=object(),
    )
    assert result["series"] == []
    assert "is_final = 0 AND end_reason = 'view_start'" in statements[0]
    assert "countIf(event_type = 'error') AS errors" in statements[0]
    assert "(application_id, env, view_id)" in statements[0]
    assert "sdk_version = '' AND lcp > 0" in statements[0]


@pytest.mark.asyncio
async def test_resource_unknown_status_size_and_duration_are_null(monkeypatch):
    now = datetime.now(timezone.utc)

    class Result:
        def __init__(self, rows):
            self.result_rows = rows

    class ResourceCH:
        def query(self, sql, parameters=None):
            if "SELECT count() FROM (" in sql:
                return Result([[1]])
            return Result([[
                "portal", "prod", "/", "https://cdn.example.com/app.js", "script",
                "GET", 0, 2, 0, 0.0, 0, 0.0, 0, 0.0, now, "1.0.0", "", [],
                0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0, "",
            ]])

    monkeypatch.setattr(rum, "_ch", lambda: ResourceCH())
    result = await rum.rum_resources(
        range_="24h", application_id=None, env=None, view_name=None, browser=None,
        device_type=None, country=None, service_version=None, browser_version=None,
        os=None, page=1, page_size=25, sort="duration_p75", order="desc", _user=object(),
    )
    item = result["items"][0]
    assert item["status_code"] is None
    assert item["duration_p75"] is None and item["duration_samples"] == 0
    assert item["size_avg"] is None and item["size_samples"] == 0
    assert item["wait_p75"] is None and item["timing_samples"] == 0
    assert item["server_p75"] is None and item["server_samples"] == 0


@pytest.mark.asyncio
async def test_legacy_summary_contract_uses_nullable_corrected_vitals(monkeypatch):
    seen = []

    async def overview(**kwargs):
        seen.append(("overview", kwargs))
        return {
            "totals": {"events": 4, "sessions": 1, "views": 1, "errors": 0,
                       "error_sessions": 0, "resources": 2, "actions": 1,
                       "long_tasks": 0, "resource_failures": 0},
            "vitals": {
                name: {"p75": None, "samples": 0}
                for name in ("lcp", "inp", "cls", "fcp", "ttfb", "load")
            },
        }

    async def views(**kwargs):
        seen.append(("views", kwargs))
        return {"items": [{"view_name": "/", "errors": 0}]}

    async def sessions(**kwargs):
        seen.append(("sessions", kwargs))
        return {"items": [{"session_id": "s1", "backend_trace_ids": []}]}

    monkeypatch.setattr(rum, "rum_overview", overview)
    monkeypatch.setattr(rum, "rum_views", views)
    monkeypatch.setattr(rum, "rum_sessions", sessions)
    result = await rum.rum_summary(
        range_="24h", application_id="portal", env="prod", view_name=None,
        browser=None, device_type=None, country=None, service_version="1.2.3",
        browser_version=None, os=None, _user=object(),
    )
    assert result["lcp_p75"] is None and result["lcp_samples"] == 0
    assert result["routes"][0]["errors"] == 0
    assert all(call[1]["service_version"] == "1.2.3" for call in seen)
    assert dict(seen)["views"]["page"] == 1


def test_rollup_and_control_plane_migrations_cover_retention_and_binding():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    ch = (root / "scripts" / "migrate-096-rum-production-clickhouse.sql").read_text()
    pg = (root / "scripts" / "migrate-095-rum-production.sql").read_text()
    demo = (root / "scripts" / "prepare-apm-iis-demo.py").read_text()
    assert "apm_rum_metrics_5m" in ch
    assert "toIntervalDay(90)" in ch
    assert "count() AS events" in ch
    assert "countIf(event_type = 'error') AS errors" in ch
    assert "sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start')" in ch
    assert "concat(application_id, char(31), env, char(31), view_id)" in ch
    assert "non_replicated_deduplication_window = 100000" in ch
    assert "idx_rum_dedupe_id dedupe_id TYPE bloom_filter" in ch
    assert "CREATE MATERIALIZED VIEW" not in ch
    assert "application_id VARCHAR(128)" in pg
    assert 'src="{CONTROLLER}/api/v1/apm/rum/sdk.js"' in demo
    assert "older_rum_key_ids = active_demo_rum_key_ids(token)" in demo
    assert "if rum is not None and not completed:" in demo
    assert "for key_id in older_rum_key_ids:" in demo
    assert 'request("PUT", f"/api/v1/apm/synthetics/{monitor_id}"' in demo


def test_vitals_query_is_compatible_with_clickhouse_24_alias_resolution():
    sql = rum._vitals_query("timestamp >= {frm:DateTime64(3)}")
    assert "argMaxIf(raw.lcp" in sql
    assert "AS lcp_value" in sql
    assert "AS lcp_present" in sql
    assert "AS lcp," not in sql


def test_rum_aggregate_queries_do_not_shadow_raw_trace_columns():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/api/v1/rum.py").read_text()
    assert re.search(r"\bAS backend_trace_id\b", source) is None
    assert "argMax(service_version, timestamp) AS service_version" not in source
    # An aggregate aliased to a filter column is resolved inside WHERE by
    # ClickHouse (ILLEGAL_AGGREGATION) as soon as that filter is applied.
    for column in ("client_ip", "view_name", "browser", "browser_version", "os",
                   "device_type", "country", "application_id", "env"):
        assert re.search(rf"\)\s+AS {column}\b", source) is None, column


# ── Phase 1: route grouping, GeoIP, rollup bands ─────────────────────────────

def test_route_rules_group_slugs_and_sections_after_id_scrubbing():
    from app.services.rum_routes import apply_route_rules, rules_from_options

    rules = rules_from_options({
        "route_rules": [
            {"match": "/products/*", "name": "/products/:slug"},
            {"match": "/docs/**", "name": "/docs"},
            {"match": "/users/*/orders/*", "name": "/users/:user/orders/:order"},
            {"match": "relative", "name": "/ignored"},   # must start with "/"
        ]
    })
    assert len(rules) == 3
    assert apply_route_rules("/products/blue-shoes", rules) == "/products/:slug"
    assert apply_route_rules("/products/blue-shoes/", rules) == "/products/:slug"
    assert apply_route_rules("/products/a/b", rules) == "/products/a/b"  # "*" is one segment
    assert apply_route_rules("/docs/getting-started/install", rules) == "/docs"
    assert apply_route_rules("/users/:id/orders/:id", rules) == "/users/:user/orders/:order"
    assert apply_route_rules("/cart", rules) == "/cart"
    # asyncpg hands jsonb back as text through SQLAlchemy text(); still parsed.
    assert rules_from_options('{"route_rules": [{"match": "/a/*", "name": "/a/:x"}]}') == (("/a/*", "/a/:x"),)
    assert rules_from_options(None) == ()
    # Numeric identifiers are grouped by the intake validator without any rule.
    event = _event(view_name="/orders/1234/items/9")
    assert event.view_name == "/orders/:id/items/:id"


def test_route_rules_are_validated_on_the_key_options():
    from app.api.v1.apm import RumSdkOptions

    options = RumSdkOptions(service_name="web", route_rules=[{"match": "/p/*", "name": "/p/:slug"}])
    assert options.model_dump()["route_rules"] == [{"match": "/p/*", "name": "/p/:slug"}]
    with pytest.raises(ValidationError):
        RumSdkOptions(service_name="web", route_rules=[{"match": "p/*", "name": "/p/:slug"}])


def test_country_falls_back_to_geoip_when_no_cdn_header(monkeypatch):
    from app.services import geoip

    class Req:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    monkeypatch.setattr(geoip, "country_of", lambda ip: ("sa", "Saudi Arabia") if ip == "203.0.113.9" else (None, None))
    assert rum._resolve_country(Req({"cf-ipcountry": "DE"}, "203.0.113.9"), "203.0.113.9") == "DE"
    assert rum._resolve_country(Req({}, "203.0.113.9"), "203.0.113.9") == "SA"
    assert rum._resolve_country(Req({}, "10.0.0.5"), "10.0.0.5") == ""
    assert rum._resolve_country(Req({"cf-ipcountry": "XXX"}, ""), "") == ""


def test_rollup_insert_writes_vital_band_counters():
    sql = rum._RUM_ROLLUP_INSERT_SQL
    assert "__ROLLUP_BAND_COLUMNS__" not in sql
    for name in ("lcp", "inp", "cls", "fcp", "ttfb", "load"):
        assert f"AS {name}_good" in sql and f"AS {name}_poor" in sql
    assert "r.lcp <= 2500) AS lcp_good" in sql
    assert "r.cls > 0.25) AS cls_poor" in sql
    # Band counters follow load_samples, matching the migrate-106 column order.
    assert sql.index("AS load_samples") < sql.index("AS lcp_good") < sql.index("FROM zenplus.apm_rum_events")

    for name in ("lcp", "inp", "cls", "fcp", "ttfb", "load"):
        assert f"AS {name}_rated" in sql
    assert sql.index("AS load_poor") < sql.index("AS lcp_rated")   # migrate-107 appends after the bands
    digests = [2100.0, 10, 150.0, 10, 0.05, 10, 900.0, 10, 400.0, 10, 2000.0, 10]
    bands = [8, 1, 10, 10, 0, 10, 5, 2, 10, 10, 0, 10, 10, 0, 10, 6, 3, 10]   # (good, poor, rated) per vital
    payload = rum._rollup_vitals_payload(digests + bands)
    assert payload["lcp"]["good_pct"] == 80.0 and payload["lcp"]["poor_pct"] == 10.0
    assert payload["cls"]["needs_improvement_pct"] == 30.0
    # Shares divide by the rated samples, so buckets that predate the
    # migration (rated = 0) never drag a window toward "0 % good".
    mixed = rum._rollup_vitals_payload([2100.0, 100, *digests[2:]] + [4, 1, 5] + bands[3:])
    assert mixed["lcp"]["samples"] == 100 and mixed["lcp"]["rated_samples"] == 5 and mixed["lcp"]["good_pct"] == 80.0
    legacy = rum._rollup_vitals_payload(digests + [0] * 18)
    assert legacy["lcp"]["p75"] == 2100.0 and legacy["lcp"]["good_pct"] is None


# ── Phase 1: custom windows, search, comparison, export ──────────────────────

def test_windows_resolve_presets_custom_bounds_and_previous_period():
    preset = rum._resolve_window("7d")
    assert preset.range == "7d" and not preset.rollup and preset.bucket_seconds == 21600
    assert abs(preset.seconds - 7 * 86400) < 5
    prev = preset.previous()
    assert prev.to == preset.frm and abs(prev.seconds - preset.seconds) < 1

    custom = rum._resolve_window("custom", "2026-09-01T00:00:00Z", "2026-09-01T06:00:00Z")
    assert custom.range == "custom" and custom.seconds == 6 * 3600 and custom.bucket_seconds == 900
    assert not custom.rollup
    long = rum._resolve_window("custom", "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z")
    assert long.rollup and long.bucket_seconds == 86400
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 60_000
    epoch = rum._resolve_window("7d", str(end_ms - 6 * 3_600_000), str(end_ms))   # epoch ms wins over the preset
    assert epoch.range == "custom" and abs(epoch.seconds - 6 * 3600) < 1
    with pytest.raises(HTTPException):
        rum._resolve_window("custom")                       # needs both bounds
    with pytest.raises(HTTPException):
        rum._resolve_window("custom", "2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z")
    with pytest.raises(HTTPException):
        rum._resolve_window("custom", "2026-05-01T00:00:00Z", "2026-08-30T00:00:00Z")   # > 90 days
    # FastAPI FieldInfo defaults (direct calls) are treated as "not supplied".
    from fastapi import Query
    assert rum._resolve_window("24h", Query(default=None), Query(default=None)).range == "24h"


def test_scope_adds_search_and_user_filters():
    params, sql = rum._scope("24h", q=" abc ", user_id="usr_1")
    assert "user_id = {user_id:String}" in sql and params["user_id"] == "usr_1"
    assert params["q"] == "abc"
    assert "positionCaseInsensitiveUTF8(session_id, {q:String}) > 0" in sql
    assert "positionCaseInsensitiveUTF8(error_message, {q:String}) > 0" in sql
    _, plain = rum._scope("24h")
    assert "{q:String}" not in plain and "user_id" not in plain


@pytest.mark.asyncio
async def test_export_streams_csv_of_the_explorer(monkeypatch):
    now = datetime.now(timezone.utc)

    async def fake_views(**kwargs):
        assert kwargs["page_size"] == 100 and kwargs["sort"] == "views"
        return {"total": 1, "items": [{
            "application_id": "portal", "env": "prod", "view_name": "/orders/:id",
            "views": 3, "lcp_p75": 2100.5, "last_seen": now, "backend_trace_ids": ["a", "b"],
        }]}

    monkeypatch.setattr(rum, "rum_views", fake_views)
    response = await rum.rum_export(
        tab="views", range_="24h", frm=None, to=None, q=None, user_id=None,
        application_id=None, env=None, view_name=None, browser=None, device_type=None,
        country=None, service_version=None, browser_version=None, os=None, client_ip=None,
        sort=None, order="desc", limit=5000, _user=object(),
    )
    body = response.body.decode()
    header, row = body.strip().splitlines()
    assert header == "application_id,env,view_name,views,lcp_p75,last_seen"   # trace-id arrays are skipped
    assert row.startswith("portal,prod,/orders/:id,3,2100.5,")
    assert response.headers["content-disposition"].startswith('attachment; filename="rum-views-24h-')


# ── Phase 2: Web Vitals depth ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vitals_endpoint_shapes_distribution_breakdown_attribution_and_releases(monkeypatch):
    now = datetime.now(timezone.utc)
    statements = []

    class Result:
        def __init__(self, rows):
            self.result_rows = rows

    edges = rum.VITAL_HISTOGRAM_EDGES

    def dist_row():
        row = []
        for name in rum._VITAL_NAMES:
            counts = [0] * len(edges[name])
            counts[0] = 3
            row += [4, 3, 1, [100.0, 200.0, 300.0, 400.0], *counts]
        return row

    class CH:
        def query(self, sql, parameters=None):
            statements.append(sql)
            if "quantilesTDigestIf" in sql:
                return Result([dist_row()])
            if "dim_value" in sql:
                return Result([["/checkout", 40, 38, 2339.0, 30, 4, 38, 2339.0, 38, 230.0, 38, 0.15, 38]])
            if "vital_attribution[" in sql:
                return Result([["/checkout", "img.hero", "https://cdn/hero.jpg", 12, 4100.0, 7, 2]])
            if "service_version != ''" in sql:
                return Result([["2026.09.01", now, 40]])
            return Result([])

    monkeypatch.setattr(rum, "_ch", lambda: CH())
    result = await rum.rum_vitals(
        range_="24h", frm=None, to=None, q=None, user_id=None, application_id=None, env=None,
        view_name=None, browser=None, device_type=None, country=None, service_version=None,
        browser_version=None, os=None, client_ip=None, dimension="view_name", vital="lcp",
        _user=object(),
    )
    lcp = result["distribution"]["lcp"]
    assert lcp["samples"] == 4 and lcp["good_pct"] == 75.0 and lcp["poor_pct"] == 25.0
    assert lcp["percentiles"] == {"p50": 100.0, "p75": 200.0, "p90": 300.0, "p95": 400.0}
    assert lcp["buckets"][0] == {"from": 0, "to": 500, "count": 3}
    assert lcp["buckets"][-1]["to"] is None                     # overflow bucket
    assert lcp["thresholds"] == {"good": 2500, "poor": 4000}
    row = result["breakdown"]["rows"][0]
    assert row["value"] == "/checkout" and row["poor_pct"] == pytest.approx(4 / 38 * 100)
    assert row["vitals"]["inp"]["p75"] == 230.0
    assert result["breakdown"]["available"] is True
    lcp_attr = result["attribution"]["lcp"][0]
    assert lcp_attr["element"] == "img.hero" and lcp_attr["detail"] == "https://cdn/hero.jpg"
    assert lcp_attr["poor_pct"] == pytest.approx(7 / 12 * 100)
    assert result["releases"][0]["service_version"] == "2026.09.01"
    # Per-view dedupe feeds both the histogram and the breakdown; the breakdown
    # carries the dimension in via anyIf so a partially-empty column (connection
    # type) still groups one view once.
    assert any("anyIf(raw.view_name, raw.view_name != '') AS dim_value" in sql for sql in statements)
    assert any("ORDER BY poor / greatest(rated, 1) DESC" in sql for sql in statements)
