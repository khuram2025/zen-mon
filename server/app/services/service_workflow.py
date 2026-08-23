"""Secure browser-like HTTP service journeys used by the on-demand probe.

The Go poller implements the scheduled equivalent.  This module deliberately
returns only status metadata: response bodies, cookies, tokens and injected
credentials must never appear in API responses or logs.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx
import requests
from requests_ntlm import HttpNtlmAuth


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname, port


def _safe_response_url(url: httpx.URL) -> str:
    """Return useful redirect information without query strings or user info."""
    port = f":{url.port}" if url.port and url.port not in {80, 443} else ""
    return f"{url.scheme}://{url.host}{port}{url.path or '/'}"


def _safe_url_string(value: str) -> str:
    parsed = urlsplit(value)
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path or '/'}"


def _auth_schemes(value: str | None) -> list[str]:
    """Return challenge scheme names without exposing challenge parameters."""
    if not value:
        return []
    known = {"basic", "bearer", "digest", "negotiate", "ntlm"}
    found = []
    for match in re.finditer(r"(?:^|,)\s*([A-Za-z][A-Za-z0-9_-]*)", value):
        scheme = match.group(1).lower()
        if scheme in known and scheme not in found:
            found.append(scheme)
    # IIS often emits multiple WWW-Authenticate header values which Requests
    # folds in a way that may not start each scheme after a comma.
    lowered = value.lower()
    for scheme in ("negotiate", "ntlm", "digest", "basic", "bearer"):
        if re.search(rf"(?:^|[\s,]){scheme}(?:[\s,]|$)", lowered) and scheme not in found:
            found.append(scheme)
    return found


def _request_diagnosis(exc: httpx.HTTPError) -> tuple[str, str]:
    """Map httpx failures to stable, user-facing diagnostic categories."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "The service did not respond before the configured timeout"

    text = " ".join(str(item) for item in (exc, exc.__cause__, exc.__context__) if item).lower()
    if isinstance(exc, httpx.ConnectError):
        if any(marker in text for marker in ("name or service not known", "nodename nor servname", "getaddrinfo", "temporary failure in name resolution")):
            return "dns", "The hostname could not be resolved"
        if any(marker in text for marker in ("connection refused", "errno 111", "errno 61")):
            return "connection_refused", "The host is reachable but refused the connection"
        if any(marker in text for marker in ("certificate verify failed", "ssl", "tls")):
            return "tls", "The TLS connection or certificate validation failed"
        if any(marker in text for marker in ("network is unreachable", "no route to host")):
            return "unreachable", "No network route to the service is available"
        return "connectivity", "A connection to the service could not be established"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect", "The service returned too many redirects"
    return "request", "The HTTP request failed before a valid response was received"


def _inject(template: str | None, values: dict[str, str], content_type: str = "") -> str | None:
    if template is None:
        return None
    rendered = template
    lowered = content_type.lower()
    for key, raw_value in values.items():
        if "application/x-www-form-urlencoded" in lowered:
            value = quote_plus(raw_value)
        elif "application/json" in lowered:
            value = json.dumps(raw_value)[1:-1]
        else:
            value = raw_value
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _status_matches(code: int, patterns: str) -> bool:
    for raw in patterns.split(","):
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if "-" in pattern:
            try:
                low, high = (int(part) for part in pattern.split("-", 1))
            except ValueError:
                continue
            if low <= code <= high:
                return True
        elif "x" in pattern:
            prefix = pattern.rstrip("x")
            try:
                low = int(prefix.ljust(3, "0"))
                high = int(prefix.ljust(3, "9"))
            except ValueError:
                continue
            if low <= code <= high:
                return True
        else:
            try:
                if int(pattern) == code:
                    return True
            except ValueError:
                continue
    return False


def _single_step(check: Any) -> dict[str, Any]:
    return {
        "name": "Service request",
        "url": check.target_url or f"http://{check.target_host}:{check.target_port or 80}",
        "method": (check.http_method or "GET").upper(),
        "headers": dict(getattr(check, "http_headers", None) or {}),
        "body": getattr(check, "http_body", None),
        "expected_statuses": check.http_expected_statuses or str(check.http_expected_status or 200),
        "content_match": check.http_content_match,
        "follow_redirects": check.http_follow_redirects,
    }


async def execute_http_workflow(
    check: Any,
    credential: dict[str, str] | None = None,
    *,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Execute one HTTP request or a cookie-preserving multi-step journey."""
    configured_steps = list(getattr(check, "workflow_steps", None) or [])
    steps = configured_steps or [_single_step(check)]
    operator = (getattr(check, "workflow_operator", None) or "all").lower()
    ignore_tls_errors = bool(getattr(check, "http_ignore_tls_errors", False))
    allow_insecure_http_auth = bool(getattr(check, "http_allow_insecure_auth", False))
    timeout = max(1, min(int(check.timeout or 10), 60))
    values = {
        "username": (credential or {}).get("username", ""),
        "password": (credential or {}).get("secret", ""),
        "token": (credential or {}).get("secret", ""),
    }
    auth_type = (credential or {}).get("auth_type", "")
    base_origin = _origin(steps[0]["url"])
    if credential and base_origin[0] != "https" and auth_type != "ntlm" and not allow_insecure_http_auth:
        raise ValueError("Basic, bearer, and form credentials require HTTPS unless trusted HTTP credential transmission is explicitly enabled")
    if any(_origin(step["url"]) != base_origin for step in steps):
        raise ValueError("All workflow steps must use the same origin")

    if auth_type == "ntlm":
        if _transport is not None:
            raise ValueError("A custom HTTPX transport cannot execute an NTLM probe")
        return await asyncio.to_thread(
            _execute_ntlm_workflow,
            steps,
            operator,
            timeout,
            ignore_tls_errors,
            values,
            base_origin,
        )

    client_auth = None
    if auth_type == "basic":
        client_auth = httpx.BasicAuth(values["username"], values["password"])

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    login_path = urlsplit(steps[0]["url"]).path or "/" if auth_type == "form" else None
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        verify=not ignore_tls_errors,
        auth=client_auth,
        transport=_transport,
    ) as client:
        for index, step in enumerate(steps):
            step_started = time.monotonic()
            headers = {
                str(key): _inject(str(value), values) or ""
                for key, value in dict(step.get("headers") or {}).items()
            }
            if auth_type == "bearer":
                headers["Authorization"] = f"Bearer {values['token']}"
            content_type = next(
                (value for key, value in headers.items() if key.lower() == "content-type"),
                "",
            )
            body = _inject(step.get("body"), values, content_type)
            item = {
                "index": index + 1,
                "name": step.get("name") or f"Step {index + 1}",
                "status": "down",
                "status_code": None,
                "response_time_ms": 0.0,
                "content_matched": None,
                "error": "",
                "diagnosis": None,
                "response_url": None,
                "content_type": None,
                "response_size_bytes": None,
                "redirect_count": 0,
            }
            try:
                response = await client.request(
                    (step.get("method") or "GET").upper(),
                    step["url"],
                    headers=headers,
                    content=body,
                    follow_redirects=bool(step.get("follow_redirects", True)),
                )
                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                item["status_code"] = response.status_code
                item["response_url"] = _safe_response_url(response.url)
                item["response_reason"] = response.reason_phrase or None
                item["content_type"] = response.headers.get("content-type", "").split(";", 1)[0] or None
                item["response_size_bytes"] = len(response.content)
                item["redirect_count"] = len(response.history)
                item["authentication_challenges"] = _auth_schemes(response.headers.get("www-authenticate"))
                if credential and _origin(str(response.url)) != base_origin:
                    item["error"] = "Authenticated redirect left the configured origin"
                    item["diagnosis"] = "redirect"
                elif credential and response.status_code in {401, 403}:
                    item["error"] = f"The service rejected the configured credentials (HTTP {response.status_code})"
                    item["diagnosis"] = "authentication"
                elif (
                    auth_type == "form"
                    and index > 0
                    and login_path
                    and (response.url.path or "/") == login_path
                ):
                    item["error"] = "The service returned to the sign-in page; the credentials or login request were rejected"
                    item["diagnosis"] = "authentication"
                elif not _status_matches(response.status_code, step.get("expected_statuses") or "200"):
                    item["error"] = (
                        f"expected status {step.get('expected_statuses') or '200'}, "
                        f"got {response.status_code}"
                    )
                    item["diagnosis"] = "http_status"
                elif step.get("content_match"):
                    matched = str(step["content_match"]) in response.text[:1_048_576]
                    item["content_matched"] = matched
                    if matched:
                        item["status"] = "up"
                    else:
                        item["error"] = "required response content was not found"
                        item["diagnosis"] = "content"
                else:
                    item["status"] = "up"
            except httpx.HTTPError as exc:
                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                item["diagnosis"], item["error"] = _request_diagnosis(exc)
            results.append(item)

    passed = sum(1 for item in results if item["status"] == "up")
    healthy = passed == len(results) if operator == "all" else passed > 0
    first_failure = next((item for item in results if item["status"] != "up"), None)
    failure_message = ""
    if first_failure:
        failure_message = f"{first_failure['name']}: {first_failure['error'] or 'step failed'}"
    return {
        "status": "up" if healthy else "down",
        "response_time_ms": round((time.monotonic() - started) * 1000, 1),
        "error": "" if healthy else failure_message,
        "diagnosis": None if healthy else (first_failure or {}).get("diagnosis") or "workflow",
        "details": {
            "workflow_operator": operator,
            "tls_verification_disabled": ignore_tls_errors,
            "steps_total": len(results),
            "steps_passed": passed,
            "status_code": results[-1]["status_code"] if results else None,
            "steps": results,
        },
    }


def _execute_ntlm_workflow(
    steps: list[dict[str, Any]],
    operator: str,
    timeout: int,
    ignore_tls_errors: bool,
    values: dict[str, str],
    base_origin: tuple[str, str | None, int | None],
) -> dict[str, Any]:
    """Execute NTLM/Negotiate requests in a private worker thread.

    Requests + requests-ntlm is used because HTTPX has no built-in Windows
    Integrated Authentication. Redirects are followed manually so credentials
    can never be replayed to another origin.
    """
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    session = requests.Session()
    session.auth = HttpNtlmAuth(values["username"], values["password"])
    session.headers["User-Agent"] = "ZenPlus-Monitor/1.0"
    try:
        for index, step in enumerate(steps):
            step_started = time.monotonic()
            headers = {
                str(key): _inject(str(value), values) or ""
                for key, value in dict(step.get("headers") or {}).items()
            }
            content_type = next(
                (value for key, value in headers.items() if key.lower() == "content-type"),
                "",
            )
            body = _inject(step.get("body"), values, content_type)
            method = (step.get("method") or "GET").upper()
            request_url = step["url"]
            redirects = 0
            response = None
            item = {
                "index": index + 1,
                "name": step.get("name") or f"Step {index + 1}",
                "status": "down",
                "status_code": None,
                "response_time_ms": 0.0,
                "content_matched": None,
                "error": "",
                "diagnosis": None,
                "response_url": None,
                "response_reason": None,
                "content_type": None,
                "response_size_bytes": None,
                "redirect_count": 0,
                "authentication_challenges": [],
            }
            try:
                while True:
                    response = session.request(
                        method,
                        request_url,
                        headers=headers,
                        data=body,
                        timeout=timeout,
                        verify=not ignore_tls_errors,
                        allow_redirects=False,
                    )
                    if not step.get("follow_redirects", True) or response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("location")
                    if not location:
                        break
                    next_url = urljoin(request_url, location)
                    if _origin(next_url) != base_origin:
                        item["error"] = "Authenticated redirect left the configured origin"
                        item["diagnosis"] = "redirect"
                        break
                    redirects += 1
                    if redirects > 10:
                        item["error"] = "The service returned too many redirects"
                        item["diagnosis"] = "redirect"
                        break
                    if response.status_code in {301, 302, 303} and method not in {"GET", "HEAD"}:
                        method, body = "GET", None
                    request_url = next_url

                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                if response is not None:
                    item["status_code"] = response.status_code
                    item["response_url"] = _safe_url_string(response.url)
                    item["response_reason"] = response.reason or None
                    item["content_type"] = response.headers.get("content-type", "").split(";", 1)[0] or None
                    item["response_size_bytes"] = len(response.content)
                    item["redirect_count"] = redirects
                    item["authentication_challenges"] = _auth_schemes(response.headers.get("www-authenticate"))
                if item["error"]:
                    pass
                elif response is None:
                    item["error"] = "The service returned no HTTP response"
                    item["diagnosis"] = "request"
                elif response.status_code in {401, 403}:
                    item["error"] = f"The service rejected the configured Windows credentials (HTTP {response.status_code})"
                    item["diagnosis"] = "authentication"
                elif not _status_matches(response.status_code, step.get("expected_statuses") or "200"):
                    item["error"] = f"expected status {step.get('expected_statuses') or '200'}, got {response.status_code}"
                    item["diagnosis"] = "http_status"
                elif step.get("content_match"):
                    matched = str(step["content_match"]) in response.text[:1_048_576]
                    item["content_matched"] = matched
                    if matched:
                        item["status"] = "up"
                    else:
                        item["error"] = "required response content was not found"
                        item["diagnosis"] = "content"
                else:
                    item["status"] = "up"
            except requests.Timeout:
                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                item["diagnosis"] = "timeout"
                item["error"] = "The service did not respond before the configured timeout"
            except requests.RequestException as exc:
                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                message = str(exc).lower()
                if "certificate" in message or "ssl" in message:
                    item["diagnosis"] = "tls"
                    item["error"] = "The TLS connection or certificate validation failed"
                elif "name resolution" in message or "getaddrinfo" in message:
                    item["diagnosis"] = "dns"
                    item["error"] = "The hostname could not be resolved"
                elif "refused" in message:
                    item["diagnosis"] = "connection_refused"
                    item["error"] = "The host is reachable but refused the connection"
                else:
                    item["diagnosis"] = "connectivity"
                    item["error"] = "A connection to the service could not be established"
            results.append(item)
    finally:
        session.close()

    passed = sum(1 for item in results if item["status"] == "up")
    healthy = passed == len(results) if operator == "all" else passed > 0
    first_failure = next((item for item in results if item["status"] != "up"), None)
    failure_message = ""
    if first_failure:
        failure_message = f"{first_failure['name']}: {first_failure['error'] or 'step failed'}"
    return {
        "status": "up" if healthy else "down",
        "response_time_ms": round((time.monotonic() - started) * 1000, 1),
        "error": "" if healthy else failure_message,
        "diagnosis": None if healthy else (first_failure or {}).get("diagnosis") or "workflow",
        "details": {
            "workflow_operator": operator,
            "authentication": "Windows Integrated (NTLM/Negotiate)",
            "transport_security": "https" if base_origin[0] == "https" else "trusted-internal-http",
            "tls_verification_disabled": ignore_tls_errors,
            "steps_total": len(results),
            "steps_passed": passed,
            "status_code": results[-1]["status_code"] if results else None,
            "steps": results,
        },
    }
