"""Secure browser-like HTTP service journeys used by the on-demand probe.

The Go poller implements the scheduled equivalent.  This module deliberately
returns only status metadata: response bodies, cookies, tokens and injected
credentials must never appear in API responses or logs.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname, port


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
    timeout = max(1, min(int(check.timeout or 10), 60))
    values = {
        "username": (credential or {}).get("username", ""),
        "password": (credential or {}).get("secret", ""),
        "token": (credential or {}).get("secret", ""),
    }
    auth_type = (credential or {}).get("auth_type", "")
    base_origin = _origin(steps[0]["url"])
    if credential and base_origin[0] != "https":
        raise ValueError("Authenticated service workflows require HTTPS")
    if any(_origin(step["url"]) != base_origin for step in steps):
        raise ValueError("All workflow steps must use the same origin")

    client_auth = None
    if auth_type == "basic":
        client_auth = httpx.BasicAuth(values["username"], values["password"])

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        verify=credential is not None,
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
                if credential and _origin(str(response.url)) != base_origin:
                    item["error"] = "Authenticated redirect left the configured origin"
                elif not _status_matches(response.status_code, step.get("expected_statuses") or "200"):
                    item["error"] = (
                        f"expected status {step.get('expected_statuses') or '200'}, "
                        f"got {response.status_code}"
                    )
                elif step.get("content_match"):
                    matched = str(step["content_match"]) in response.text[:1_048_576]
                    item["content_matched"] = matched
                    if matched:
                        item["status"] = "up"
                    else:
                        item["error"] = "required response content was not found"
                else:
                    item["status"] = "up"
            except httpx.HTTPError as exc:
                item["response_time_ms"] = round((time.monotonic() - step_started) * 1000, 1)
                item["error"] = f"request failed: {exc.__class__.__name__}"
            results.append(item)

    passed = sum(1 for item in results if item["status"] == "up")
    healthy = passed == len(results) if operator == "all" else passed > 0
    failing = [item["name"] for item in results if item["status"] != "up"]
    return {
        "status": "up" if healthy else "down",
        "response_time_ms": round((time.monotonic() - started) * 1000, 1),
        "error": "" if healthy else f"Failed workflow step(s): {', '.join(failing)}",
        "details": {
            "workflow_operator": operator,
            "steps_total": len(results),
            "steps_passed": passed,
            "status_code": results[-1]["status_code"] if results else None,
            "steps": results,
        },
    }
