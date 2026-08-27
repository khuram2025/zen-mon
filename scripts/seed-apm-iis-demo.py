"""Seed clearly-labelled IIS APM demo telemetry through supported ingest APIs.

Required environment variables:
  ZENPLUS_DEMO_SDK_KEY  temporary secret SDK ingest key
  ZENPLUS_DEMO_RUM_KEY  public exact-origin RUM key

No credentials or keys are written to disk or included in telemetry.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


CONTROLLER = os.environ.get("ZENPLUS_DEMO_CONTROLLER", "https://192.168.8.221").rstrip("/")
PROFILE_CONTROLLER = os.environ.get("ZENPLUS_DEMO_PROFILE_CONTROLLER", CONTROLLER).rstrip("/")
SDK_KEY = os.environ["ZENPLUS_DEMO_SDK_KEY"]
RUM_KEY = os.environ["ZENPLUS_DEMO_RUM_KEY"]
ORIGIN = os.environ.get("ZENPLUS_DEMO_ORIGIN", "http://192.168.8.19")
SERVICE = "default-web-site"
ENV = "dev"
CTX = ssl._create_unverified_context()
RNG = random.Random(112019)


def attr_s(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def attr_i(key: str, value: int) -> dict:
    return {"key": key, "value": {"intValue": str(value)}}


def attr_b(key: str, value: bool) -> dict:
    return {"key": key, "value": {"boolValue": value}}


def post(
    path: str,
    body: dict,
    headers: dict[str, str] | None = None,
    *,
    controller: str = CONTROLLER,
) -> dict:
    data = json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        controller + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, context=CTX, timeout=60) as response:
            payload = response.read()
            return json.loads(payload) if payload else {"status": response.status}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"POST {path} failed with HTTP {exc.code}: {detail}") from exc


def span(
    trace_id: str,
    span_id: str,
    parent_id: str,
    name: str,
    kind: int,
    start_ns: int,
    duration_ms: int,
    attributes: list[dict],
    error: bool = False,
) -> dict:
    result = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + duration_ms * 1_000_000),
        "attributes": attributes,
        "status": {
            "code": 2 if error else 1,
            "message": "ZenPlus controlled demo exception" if error else "",
        },
    }
    if parent_id:
        result["parentSpanId"] = parent_id
    if error:
        result["events"] = [{
            "timeUnixNano": str(start_ns + max(duration_ms - 2, 1) * 1_000_000),
            "name": "exception",
            "attributes": [
                attr_s("exception.type", "System.InvalidOperationException"),
                attr_s("exception.message", "ZenPlus controlled demo exception"),
                attr_s(
                    "exception.stacktrace",
                    "at ZenPlusApmDemo.Api.Page_Load() in Api.aspx:line 31\n"
                    "at System.Web.UI.Control.LoadRecursive()",
                ),
                attr_b("exception.escaped", False),
            ],
        }]
    return result


def resource(service: str, spans: list[dict]) -> dict:
    return {
        "resource": {"attributes": [
            attr_s("service.name", service),
            attr_s("service.version", "1.11.2-demo"),
            attr_s("deployment.environment", ENV),
            attr_s("host.name", "WIN-UR37253MKJR"),
            attr_s("host.ip", "192.168.8.19"),
            attr_s("telemetry.source", "zenplus-iis-demo"),
            attr_b("demo.data", True),
        ]},
        "scopeSpans": [{
            "scope": {"name": "ZenPlus.IIS.Demo", "version": "1.0.0"},
            "spans": spans,
        }],
    }


def build_trace(index: int, now: datetime) -> tuple[list[dict], str, str, int]:
    if index < 140:
        age = timedelta(minutes=RNG.uniform(1, 58))
    else:
        age = timedelta(hours=RNG.uniform(1, 163))
    started = now - age
    start_ns = int(started.timestamp() * 1_000_000_000)
    trace_id = secrets.token_hex(16)
    root_id = secrets.token_hex(8)
    db_id = secrets.token_hex(8)
    client_id = secrets.token_hex(8)
    downstream_id = secrets.token_hex(8)
    routes = [
        "/ZenPlusApmDemo/Api.aspx",
        "/ZenPlusApmDemo/orders",
        "/ZenPlusApmDemo/checkout",
        "/ZenPlusApmDemo/reports",
        "/LocalAuthTest/",
    ]
    route = routes[index % len(routes)]
    is_error = index % 17 == 0
    is_slow = index % 13 == 0
    duration_ms = RNG.randint(35, 210)
    if is_slow:
        duration_ms = RNG.randint(850, 2400)
    if is_error:
        duration_ms = max(duration_ms, RNG.randint(180, 900))
    status = 500 if is_error else 200
    user = f"demo-user-{(index % 18) + 1:02d}"
    common = [
        attr_s("http.request.method", "GET"),
        attr_s("http.route", route),
        attr_i("http.response.status_code", status),
        attr_s("url.path", route),
        attr_s("server.address", "192.168.8.19"),
        attr_s("enduser.id", user),
        attr_s("user.id", user),
        attr_s("client.address", f"192.168.8.{100 + index % 40}"),
        attr_b("demo.data", True),
    ]
    root = span(
        trace_id, root_id, "", f"GET {route}", 2, start_ns,
        duration_ms, common, is_error,
    )
    statements = [
        "SELECT OrderId, Total FROM Orders WHERE CustomerId = 12345 AND Status = 'OPEN'",
        "UPDATE Cart SET UpdatedAt = '2026-08-23' WHERE CartId = 880021",
        "SELECT ProductId, Stock FROM Inventory WHERE ProductId IN (102, 205, 309)",
        "EXEC GetCustomerProfile @CustomerId = 445566",
    ]
    db_ms = min(max(RNG.randint(8, 95), 8), max(duration_ms - 4, 8))
    db_start = start_ns + 3_000_000
    db_error = index % 41 == 0
    db = span(
        trace_id, db_id, root_id, "SQL " + ("UPDATE" if index % 4 == 1 else "SELECT"),
        3, db_start, db_ms,
        [
            attr_s("db.system", "mssql"),
            attr_s("db.operation", "UPDATE" if index % 4 == 1 else "SELECT"),
            attr_s("db.statement", statements[index % len(statements)]),
            attr_s("server.address", "sql-demo.internal"),
            attr_i("server.port", 1433),
            attr_b("demo.data", True),
        ],
        db_error,
    )
    dependency_ms = min(RNG.randint(12, 140), max(duration_ms - 5, 10))
    client_start = start_ns + max(5, db_ms + 5) * 1_000_000
    client = span(
        trace_id, client_id, root_id, "GET iis-auth-dependency/session", 3,
        client_start, dependency_ms,
        [
            attr_s("http.request.method", "GET"),
            attr_s("http.route", "/session/validate"),
            attr_i("http.response.status_code", 200),
            attr_s("peer.service", "iis-auth-dependency"),
            attr_s("server.address", "auth-demo.internal"),
            attr_b("demo.data", True),
        ],
    )
    downstream = span(
        trace_id, downstream_id, client_id, "GET /session/validate", 2,
        client_start + 1_000_000, max(dependency_ms - 2, 1),
        [
            attr_s("http.request.method", "GET"),
            attr_s("http.route", "/session/validate"),
            attr_i("http.response.status_code", 200),
            attr_b("demo.data", True),
        ],
    )
    return [resource(SERVICE, [root, db, client]), resource("iis-auth-dependency", [downstream])], trace_id, root_id, start_ns


def main() -> None:
    now = datetime.now(timezone.utc)
    traces: list[tuple[str, str, int]] = []
    pending: list[dict] = []
    accepted_spans = 0
    pending_span_count = 0
    for index in range(560):
        resources, trace_id, root_id, start_ns = build_trace(index, now)
        pending.extend(resources)
        pending_span_count += 4
        traces.append((trace_id, root_id, start_ns))
        if len(pending) >= 80:
            result = post(
                "/v1/traces",
                {"resourceSpans": pending},
                {"Authorization": f"Bearer {SDK_KEY}"},
            )
            rejected = int((result.get("partialSuccess") or {}).get("rejectedSpans", 0))
            accepted_spans += pending_span_count - rejected
            pending = []
            pending_span_count = 0
    if pending:
        result = post(
            "/v1/traces",
            {"resourceSpans": pending},
            {"Authorization": f"Bearer {SDK_KEY}"},
        )
        rejected = int((result.get("partialSuccess") or {}).get("rejectedSpans", 0))
        accepted_spans += pending_span_count - rejected

    profile_count = 0
    for index in range(0, len(traces), 14):
        trace_id, span_id, start_ns = traces[index]
        profile = {
            "service_name": SERVICE,
            "env": ENV,
            "service_version": "1.11.2-demo",
            "profile_type": "cpu" if index % 28 == 0 else "wall",
            "timestamp_ms": start_ns // 1_000_000,
            "duration_nano": 30_000_000_000,
            "trace_id": trace_id,
            "span_id": span_id,
            "encoding": "collapsed",
            "samples": [
                {"stack": "System.Web.HttpApplication.ExecuteStep;ZenPlusApmDemo.Api.Page_Load", "value": 47 + index % 11},
                {"stack": "ZenPlusApmDemo.Api.Page_Load;System.Data.SqlClient.SqlCommand.ExecuteReader", "value": 24 + index % 9},
                {"stack": "ZenPlusApmDemo.Api.Page_Load;System.Net.WebClient.DownloadString", "value": 13 + index % 7},
                {"stack": "System.Web.UI.Page.ProcessRequest;System.Web.UI.Control.LoadRecursive", "value": 8 + index % 5},
            ],
            "attributes": {"demo.data": "true", "host.name": "WIN-UR37253MKJR"},
        }
        post(
            "/v1development/profiles",
            profile,
            {"Authorization": f"Bearer {SDK_KEY}"},
            controller=PROFILE_CONTROLLER,
        )
        profile_count += 1

    rum_count = 0
    paths = ["/ZenPlusApmDemo/", "/ZenPlusApmDemo/orders", "/ZenPlusApmDemo/checkout", "/LocalAuthTest/"]
    for index in range(160):
        trace_id, _, _ = traces[index]
        timestamp = now - timedelta(minutes=RNG.uniform(1, 23 * 60))
        event_type = "error" if index % 23 == 0 else "view"
        payload = {
            "client_token": RUM_KEY,
            "application_id": "zenplus-iis-demo",
            "service_name": SERVICE,
            "event_type": event_type,
            "timestamp_ms": int(timestamp.timestamp() * 1000),
            "session_id": f"iis-demo-session-{index // 3:03d}",
            "view_id": f"iis-demo-view-{index:04d}",
            "view_name": paths[index % len(paths)],
            "url": ORIGIN + paths[index % len(paths)] + "?demo=true&secret=removed",
            "user_id": f"demo-user-{(index % 18) + 1:02d}",
            "lcp": round(RNG.uniform(650, 2800), 1),
            "inp": round(RNG.uniform(45, 280), 1),
            "cls": round(RNG.uniform(0.01, 0.22), 3),
            "fcp": round(RNG.uniform(300, 1600), 1),
            "ttfb": round(RNG.uniform(40, 450), 1),
            "load_ms": round(RNG.uniform(900, 3900), 1),
            "error_message": "ZenPlus controlled browser demo error" if event_type == "error" else "",
            "backend_trace_id": trace_id,
            "attributes": {"demo.data": "true", "release": "1.11.2-demo"},
        }
        post(
            "/api/v1/apm/rum/ingest",
            payload,
            {"Origin": ORIGIN, "User-Agent": "Mozilla/5.0 Chrome/140 ZenPlusDemo/1.0"},
        )
        rum_count += 1

    print(json.dumps({
        "service": SERVICE,
        "accepted_spans": accepted_spans,
        "profiles": profile_count,
        "rum_events": rum_count,
    }))


if __name__ == "__main__":
    main()
