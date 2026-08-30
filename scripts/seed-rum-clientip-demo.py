#!/usr/bin/env python3
"""Ingest one clearly-labelled demo RUM session through the real intake API.

Demonstrates end-to-end client-IP capture (from the X-Forwarded-For hop that
nginx forwards) and the reorganised session timeline. Adds new demo telemetry
only -- it never rewrites existing rows. No secrets are printed.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://127.0.0.1"
CTX = ssl._create_unverified_context()
APP = "zenplus-iis-demo"
ORIGIN = "https://demo.zenplus.local"
CLIENT_IP = "203.0.113.42"  # TEST-NET-3 documentation address (safe demo value)
# Real backend traces so the "View trace" pivot resolves.
TRACE_A = "2da453cd7f716a4c3bbb66f41294bc3b"
TRACE_B = "c7776f2f70f12ba74f28c0700ffd8546"


def _req(path, payload, headers, method="POST"):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        body = resp.read()
        return resp.status, dict(resp.headers), (json.loads(body) if body else {})


def main() -> int:
    _, _, login = _req("/api/v1/auth/login", {"username": "admin", "password": "admin123"}, {})
    token = login["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Create a short-lived application-bound RUM key for the demo ingest.
    _, _, created = _req(
        "/api/v1/apm/ingest-keys",
        {"name": "clientip-demo", "kind": "rum",
         "origin_allowlist": [ORIGIN], "application_id": APP},
        auth,
    )
    rum_key = created["key"]
    key_id = created["id"]

    now = datetime.now(timezone.utc)
    base_ms = int(now.timestamp() * 1000) - 120_000
    sid = f"zpr-clientip-demo-{int(now.timestamp())}"

    def ev(offset_ms, etype, **kw):
        e = {"client_token": rum_key, "application_id": APP, "service_name": "default-web-site",
             "service_version": "2026.08.30-demo", "sdk_version": "2.0.0", "event_type": etype,
             "timestamp_ms": base_ms + offset_ms, "session_id": sid,
             "attributes": {"demo.data": "true"}}
        e.update(kw)
        return e

    vid_a, vid_b = "demo-view-home", "demo-view-orders"
    events = [
        ev(0, "view", view_id=vid_a, view_name="/ZenPlusApmDemo/", url=ORIGIN + "/ZenPlusApmDemo/",
           end_reason="view_start"),
        ev(120, "resource", view_id=vid_a, view_name="/ZenPlusApmDemo/",
           resource_url=ORIGIN + "/ZenPlusApmDemo/sdk.js", resource_type="script", method="GET",
           status_code=200, duration_ms=42, transfer_size=18240),
        ev(650, "resource", view_id=vid_a, view_name="/ZenPlusApmDemo/",
           resource_url=ORIGIN + "/ZenPlusApmDemo/Api.aspx", resource_type="xhr", method="GET",
           status_code=200, duration_ms=88, transfer_size=1024, backend_trace_id=TRACE_A),
        ev(1200, "action", view_id=vid_a, view_name="/ZenPlusApmDemo/", action_name="Add to cart",
           action_type="click", target="button#add", duration_ms=36),
        ev(4200, "view", view_id=vid_a, view_name="/ZenPlusApmDemo/", url=ORIGIN + "/ZenPlusApmDemo/",
           end_reason="pagehide", is_final=True, lcp=1820.0, inp=96.0, cls=0.04, fcp=980.0,
           ttfb=210.0, load_ms=2410.0),
        ev(4300, "view", view_id=vid_b, view_name="/ZenPlusApmDemo/orders",
           url=ORIGIN + "/ZenPlusApmDemo/orders", end_reason="view_start"),
        ev(4600, "resource", view_id=vid_b, view_name="/ZenPlusApmDemo/orders",
           resource_url=ORIGIN + "/ZenPlusApmDemo/orders/list.json", resource_type="fetch",
           method="GET", status_code=200, duration_ms=140, transfer_size=8300, backend_trace_id=TRACE_B),
        ev(5200, "action", view_id=vid_b, view_name="/ZenPlusApmDemo/orders", action_name="Filter orders",
           action_type="click", target="select#status", duration_ms=52),
        ev(6100, "error", view_id=vid_b, view_name="/ZenPlusApmDemo/orders",
           error_message="Cannot read properties of undefined (reading 'total')",
           error_type="TypeError", error_source=ORIGIN + "/ZenPlusApmDemo/orders.js",
           error_stack="TypeError: Cannot read properties of undefined\n  at renderTotals (orders.js:42)",
           error_fingerprint="demo-orders-total", backend_trace_id=TRACE_B),
        ev(9000, "view", view_id=vid_b, view_name="/ZenPlusApmDemo/orders",
           url=ORIGIN + "/ZenPlusApmDemo/orders", end_reason="hidden", is_final=True,
           lcp=2260.0, inp=150.0, cls=0.09, fcp=1180.0, ttfb=260.0, load_ms=3020.0),
    ]

    status, headers, _ = _req(
        "/api/v1/apm/rum/ingest",
        {"events": events},
        {"Origin": ORIGIN, "X-Forwarded-For": f"{CLIENT_IP}, 10.0.0.1",
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"},
    )
    # Revoke the temporary demo key; the events are already stored.
    try:
        _req(f"/api/v1/apm/ingest-keys/{key_id}", {}, auth, method="DELETE")
    except Exception:
        pass

    print(json.dumps({
        "session_id": sid,
        "ingest_status": status,
        "accepted": headers.get("X-RUM-Accepted"),
        "expected_client_ip": CLIENT_IP,
    }))
    time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
