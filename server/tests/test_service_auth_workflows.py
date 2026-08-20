import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.service_check import ServiceCheckCreate, ServiceCredentialResponse
from app.services.service_workflow import _inject, _status_matches, execute_http_workflow


def _payload(**overrides):
    payload = {
        "name": "Authenticated portal",
        "check_type": "http",
        "target_host": "portal.example.com",
        "target_url": "https://portal.example.com/health",
        "credential_id": uuid4(),
        "workflow_steps": [
            {
                "name": "Sign in",
                "url": "https://portal.example.com/login",
                "method": "POST",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "body": "username={{username}}&password={{password}}",
                "expected_statuses": "200,302",
            },
            {
                "name": "Open dashboard",
                "url": "https://portal.example.com/dashboard",
                "expected_statuses": "2xx",
                "content_match": "Operational",
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_workflow_requires_same_origin():
    payload = _payload()
    payload["workflow_steps"][1]["url"] = "https://attacker.example.net/capture"
    with pytest.raises(ValidationError, match="same origin"):
        ServiceCheckCreate.model_validate(payload)


def test_authenticated_workflow_requires_https():
    payload = _payload(target_url="http://portal.example.com/health")
    for step in payload["workflow_steps"]:
        step["url"] = step["url"].replace("https://", "http://")
    with pytest.raises(ValidationError, match="require HTTPS"):
        ServiceCheckCreate.model_validate(payload)


def test_secret_injection_encodes_form_and_json_values():
    values = {"username": "ops@example.com", "password": 'p@ss "word"&'}
    assert _inject(
        "username={{username}}&password={{password}}",
        values,
        "application/x-www-form-urlencoded",
    ) == "username=ops%40example.com&password=p%40ss+%22word%22%26"
    assert _inject(
        '{"username":"{{username}}","password":"{{password}}"}',
        values,
        "application/json",
    ) == '{"username":"ops@example.com","password":"p@ss \\"word\\"&"}'


def test_status_patterns_cover_exact_wildcard_and_range():
    assert _status_matches(204, "200-299")
    assert _status_matches(302, "3xx")
    assert _status_matches(401, "200,401")
    assert not _status_matches(503, "2xx,301")


def test_credential_response_has_no_secret_field():
    response = ServiceCredentialResponse(
        id=uuid4(),
        name="Production portal",
        auth_type="form",
        username="ops@example.com",
        created_at="2026-08-19T00:00:00Z",
    )
    assert "secret" not in response.model_dump()
    assert "secret_cipher" not in response.model_dump()


def test_on_demand_workflow_keeps_cookie_and_redacts_secret():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            assert request.content == b"username=ops%40example.com&password=p%40ss+word%26safe"
            return httpx.Response(204, headers={"Set-Cookie": "session=valid; Path=/; Secure"})
        assert request.headers.get("cookie") == "session=valid"
        return httpx.Response(200, text="Business service is Operational")

    check = SimpleNamespace(
        timeout=10,
        workflow_operator="all",
        workflow_steps=[
            {
                "name": "Sign in",
                "url": "https://portal.example.com/login",
                "method": "POST",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "body": "username={{username}}&password={{password}}",
                "expected_statuses": "204",
                "content_match": None,
                "follow_redirects": True,
            },
            {
                "name": "Open dashboard",
                "url": "https://portal.example.com/dashboard",
                "method": "GET",
                "headers": {},
                "body": None,
                "expected_statuses": "200",
                "content_match": "Operational",
                "follow_redirects": True,
            },
        ],
    )
    credential = {
        "auth_type": "form",
        "username": "ops@example.com",
        "secret": "p@ss word&safe",
    }
    result = asyncio.run(
        execute_http_workflow(check, credential, _transport=httpx.MockTransport(handler))
    )
    assert result["status"] == "up"
    assert result["details"]["steps_passed"] == 2
    assert credential["secret"] not in str(result)


def test_on_demand_workflow_identifies_rejected_credentials():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    check = SimpleNamespace(
        timeout=5,
        workflow_operator="all",
        workflow_steps=[],
        target_url="https://portal.example.com/private",
        target_host="portal.example.com",
        target_port=443,
        http_method="GET",
        http_headers={},
        http_body=None,
        http_expected_statuses="200",
        http_expected_status=200,
        http_content_match=None,
        http_follow_redirects=True,
    )
    credential = {"auth_type": "basic", "username": "ops", "secret": "wrong"}

    result = asyncio.run(
        execute_http_workflow(check, credential, _transport=httpx.MockTransport(handler))
    )

    assert result["status"] == "down"
    assert result["diagnosis"] == "authentication"
    assert "rejected" in result["error"]
    assert result["details"]["steps"][0]["status_code"] == 401
    assert result["details"]["steps"][0]["content_type"] == "text/plain"
    assert credential["secret"] not in str(result)


def test_on_demand_workflow_identifies_connection_refused():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    check = SimpleNamespace(
        timeout=5,
        workflow_operator="all",
        workflow_steps=[],
        target_url="https://portal.example.com/health",
        target_host="portal.example.com",
        target_port=443,
        http_method="GET",
        http_headers={},
        http_body=None,
        http_expected_statuses="200",
        http_expected_status=200,
        http_content_match=None,
        http_follow_redirects=True,
    )

    result = asyncio.run(execute_http_workflow(check, _transport=httpx.MockTransport(handler)))

    assert result["status"] == "down"
    assert result["diagnosis"] == "connection_refused"
    assert "refused" in result["error"]


def test_tls_verification_can_be_explicitly_disabled(monkeypatch):
    observed_verify: list[bool] = []
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        observed_verify.append(kwargs.get("verify"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Operational")

    check = SimpleNamespace(
        timeout=5,
        workflow_operator="all",
        workflow_steps=[],
        target_url="https://internal.example.com/health",
        target_host="internal.example.com",
        target_port=443,
        http_method="GET",
        http_headers={},
        http_body=None,
        http_expected_statuses="200",
        http_expected_status=200,
        http_content_match="Operational",
        http_follow_redirects=True,
        http_ignore_tls_errors=True,
    )

    result = asyncio.run(
        execute_http_workflow(check, _transport=httpx.MockTransport(handler))
    )

    assert observed_verify == [False]
    assert result["status"] == "up"
    assert result["details"]["tls_verification_disabled"] is True


def test_tls_verification_remains_enabled_by_default(monkeypatch):
    observed_verify: list[bool] = []
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        observed_verify.append(kwargs.get("verify"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    check = SimpleNamespace(
        timeout=5,
        workflow_operator="all",
        workflow_steps=[],
        target_url="https://internal.example.com/health",
        target_host="internal.example.com",
        target_port=443,
        http_method="GET",
        http_headers={},
        http_body=None,
        http_expected_statuses="200",
        http_expected_status=200,
        http_content_match=None,
        http_follow_redirects=True,
    )

    result = asyncio.run(
        execute_http_workflow(check, _transport=httpx.MockTransport(handler))
    )

    assert observed_verify == [True]
    assert result["status"] == "up"
    assert result["details"]["tls_verification_disabled"] is False
