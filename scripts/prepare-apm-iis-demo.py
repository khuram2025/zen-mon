"""Prepare the 192.168.8.19 IIS host as a fully-populated ZenPlus APM lab.

All secrets are supplied through environment variables. Historical telemetry is
labelled ``demo.data=true`` and sent only through supported ingestion APIs.
"""

from __future__ import annotations

import json
import os
import runpy
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


CONTROLLER = os.environ.get("ZENPLUS_DEMO_CONTROLLER", "https://192.168.8.221").rstrip("/")
WEB_USER = os.environ["ZENPLUS_WEB_USER"]
WEB_PASSWORD = os.environ["ZENPLUS_WEB_PASSWORD"]
WINRM_HOST = os.environ.get("ZENPLUS_WINRM_HOST", "192.168.8.19")
WINRM_USER = os.environ.get("ZENPLUS_WINRM_USER", "Administrator")
WINRM_PASSWORD = os.environ["ZENPLUS_WINRM_PASSWORD"]
ORIGIN = f"http://{WINRM_HOST}"
SERVICE = "default-web-site"
ENVIRONMENT = "dev"
CTX = ssl._create_unverified_context()


def request(method: str, path: str, body: dict | None = None, token: str | None = None) -> object:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(CONTROLLER + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=120) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc


def authenticate() -> str:
    result = request("POST", "/api/v1/auth/login", {"username": WEB_USER, "password": WEB_PASSWORD})
    return str(result["access_token"])


def configure_managed_iis(token: str) -> dict:
    processes = request("GET", "/api/v1/apm/agent-processes?active_hours=720", token=token)
    matches = [
        item for item in processes
        if item.get("iis_app_pool") == "DefaultAppPool"
        and str(item.get("hostname") or item.get("server_name") or "").upper() in {"", "WIN-UR37253MKJR"}
    ]
    if not matches:
        matches = [item for item in processes if item.get("iis_app_pool") == "DefaultAppPool"]
    if not matches:
        raise RuntimeError("DefaultAppPool was not found in APM process discovery")
    process = matches[0]
    try:
        request(
            "POST",
            f"/api/v1/apm/agent-processes/{process['id']}/instrumentation",
            {"enabled": True, "restart": True, "service_name": SERVICE, "environment": ENVIRONMENT},
            token,
        )
    except RuntimeError as exc:
        if "HTTP 409" not in str(exc) or "already pending" not in str(exc):
            raise

    deadline = time.time() + 150
    latest = process
    while time.time() < deadline:
        time.sleep(5)
        rows = request("GET", "/api/v1/apm/agent-processes?active_hours=720", token=token)
        latest = next((row for row in rows if row.get("id") == process["id"]), latest)
        if latest.get("instrumentation_state") == "active" and latest.get("configured_environment") == ENVIRONMENT:
            break
        if latest.get("instrumentation_state") == "failed":
            raise RuntimeError(f"Managed IIS instrumentation failed: {latest.get('last_command_error') or 'unknown error'}")
    if latest.get("instrumentation_state") != "active":
        raise RuntimeError(f"Managed IIS instrumentation did not become active: {latest.get('instrumentation_state')}")
    return latest


def create_ingest_key(token: str, name: str, kind: str, origins: list[str]) -> dict:
    return request(
        "POST",
        "/api/v1/apm/ingest-keys",
        {"name": name, "kind": kind, "env": ENVIRONMENT, "origin_allowlist": origins},
        token,
    )


def configure_iis_page(rum_key: str) -> None:
    extra = os.environ.get("ZENPLUS_PYWINRM_PATH")
    if extra:
        sys.path.insert(0, extra)
    import winrm  # type: ignore

    sdk_tag = (
        f'<script src="http://192.168.8.221/api/v1/apm/rum/sdk.js" '
        f'data-key="{rum_key}" data-app="zenplus-iis-demo" '
        f'data-service="{SERVICE}" defer></script>'
    )
    tag_literal = sdk_tag.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module WebAdministration
$page = 'C:\\inetpub\\wwwroot\\ZenPlusApmDemo\\Default.aspx'
$backupDir = 'C:\\ProgramData\\ZenPlus\\Agent\\backups'
$backup = Join-Path $backupDir 'ZenPlusApmDemo-Default-before-rum.aspx'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $backup)) {{ Copy-Item -LiteralPath $page -Destination $backup }}
$content = Get-Content -LiteralPath $page -Raw
$tag = '{tag_literal}'
if ($content.Contains('__ZENPLUS_RUM_SCRIPT__')) {{
    $content = $content.Replace('__ZENPLUS_RUM_SCRIPT__', $tag)
}} elseif ($content -notmatch 'data-app="zenplus-iis-demo"') {{
    $content = $content.Replace('</body>', $tag + [Environment]::NewLine + '</body>')
}}
Set-Content -LiteralPath $page -Value $content -Encoding UTF8
Set-WebConfigurationProperty -PSPath 'IIS:\\' -Location 'Default Web Site/ZenPlusApmDemo' -Filter 'system.webServer/security/authentication/anonymousAuthentication' -Name enabled -Value $true
Set-WebConfigurationProperty -PSPath 'IIS:\\' -Location 'Default Web Site/ZenPlusApmDemo' -Filter 'system.webServer/security/authentication/windowsAuthentication' -Name enabled -Value $false
Restart-WebAppPool -Name 'DefaultAppPool'
Start-Sleep -Seconds 3
$response = Invoke-WebRequest -UseBasicParsing '{ORIGIN}/ZenPlusApmDemo/'
if ($response.StatusCode -ne 200 -or $response.Content -notmatch 'data-app="zenplus-iis-demo"') {{ throw 'RUM-enabled demo page verification failed' }}
Write-Output 'IIS_PAGE_READY'
"""
    session = winrm.Session(
        f"http://{WINRM_HOST}:5985/wsman",
        auth=(WINRM_USER, WINRM_PASSWORD),
        transport="ntlm",
    )
    result = session.run_ps(script)
    if result.status_code != 0:
        raise RuntimeError(result.std_err.decode(errors="replace") or result.std_out.decode(errors="replace"))


def generate_live_traffic() -> dict[str, int]:
    counts = {"success": 0, "slow": 0, "dependency": 0, "error": 0}
    schedule = (["success"] * 80) + (["slow"] * 12) + (["dependency"] * 10) + (["error"] * 6)
    for action in schedule:
        url = f"{ORIGIN}/ZenPlusApmDemo/Api.aspx?action={action}"
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if action != "error" or exc.code != 500:
                raise
        counts[action] += 1
    return counts


def ensure_slo_and_synthetic(token: str) -> tuple[str, str, list[dict]]:
    slos = request("GET", "/api/v1/apm/slos", token=token).get("items", [])
    slo = next((item for item in slos if item.get("name") == "IIS demo availability"), None)
    if slo is None:
        slo = request("POST", "/api/v1/apm/slos", {
            "name": "IIS demo availability", "service_name": SERVICE, "env": ENVIRONMENT,
            "sli_type": "availability", "target": 99.0, "window_days": 7,
            "burn_alert_enabled": True, "notify_channels": [],
        }, token)
    budget = request("GET", f"/api/v1/apm/slos/{slo['id']}/budget", token=token)

    monitors = request("GET", "/api/v1/apm/synthetics?hours=168", token=token).get("monitors", [])
    monitor = next((item for item in monitors if item.get("name") == "IIS ZenPlus APM Demo"), None)
    if monitor is None:
        created = request("POST", "/api/v1/apm/synthetics", {
            "name": "IIS ZenPlus APM Demo",
            "steps": [{
                "name": "Open demo page", "method": "GET", "url": f"{ORIGIN}/ZenPlusApmDemo/",
                "headers": {},
                "assertions": [
                    {"type": "status_code", "operator": "eq", "value": 200},
                    {"type": "latency_ms", "operator": "lt", "value": 3000},
                ],
                "extract": [],
            }],
            "variables": {}, "verify_tls": False, "notify_channels": [],
            "check_interval": 60, "timeout": 10, "retry_count": 1,
            "enabled": True, "tags": ["demo", "iis", "apm"],
        }, token)
        monitor_id = created["id"]
    else:
        monitor_id = monitor["id"]
    runs = [request("POST", f"/api/v1/apm/synthetics/{monitor_id}/run", token=token) for _ in range(3)]
    return str(slo["id"]), str(monitor_id), [budget, *runs]


def size_of(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("items", "services", "traces", "errors", "profiles", "results", "nodes", "operations"):
            if isinstance(payload.get(key), list):
                return len(payload[key])
        return len(payload)
    return 0


def verify_views(token: str, slo_id: str, monitor_id: str) -> dict[str, int]:
    service_path = urllib.parse.quote(SERVICE, safe="")
    checks = {
        "services": "/api/v1/apm/services?range=7d",
        "service_detail": f"/api/v1/apm/services/{service_path}?env={ENVIRONMENT}&range=7d",
        "red_timeline": f"/api/v1/apm/services/{service_path}/red?env={ENVIRONMENT}&range=7d",
        "operations": f"/api/v1/apm/services/{service_path}/operations?env={ENVIRONMENT}&range=7d",
        "traces": f"/api/v1/apm/traces?service={service_path}&env={ENVIRONMENT}&range=7d&limit=100",
        "errors": f"/api/v1/apm/errors?service={service_path}&env={ENVIRONMENT}&range=7d",
        "database": f"/api/v1/apm/database?service={service_path}&env={ENVIRONMENT}&range=7d",
        "profiles": f"/api/v1/apm/profiles?service={service_path}&env={ENVIRONMENT}&range=7d",
        "rum": "/api/v1/apm/rum/summary?range=24h&application_id=zenplus-iis-demo",
        "service_map": "/api/v1/apm/service-map?range=7d",
        "slo_budget": f"/api/v1/apm/slos/{slo_id}/budget",
        "synthetics": f"/api/v1/apm/synthetics/{monitor_id}/results?hours=168",
    }
    return {name: size_of(request("GET", path, token=token)) for name, path in checks.items()}


def main() -> None:
    token = authenticate()
    process = configure_managed_iis(token)
    stamp = int(time.time())
    sdk = create_ingest_key(token, f"IIS demo temporary SDK {stamp}", "sdk", [])
    rum = create_ingest_key(token, f"IIS demo browser RUM {stamp}", "rum", [ORIGIN])
    try:
        configure_iis_page(str(rum["key"]))
        live = generate_live_traffic()
        os.environ["ZENPLUS_DEMO_SDK_KEY"] = str(sdk["key"])
        os.environ["ZENPLUS_DEMO_RUM_KEY"] = str(rum["key"])
        os.environ["ZENPLUS_DEMO_ORIGIN"] = ORIGIN
        runpy.run_path(str(Path(__file__).with_name("seed-apm-iis-demo.py")), run_name="__main__")
        slo_id, monitor_id, objectives = ensure_slo_and_synthetic(token)
        time.sleep(8)
        views = verify_views(token, slo_id, monitor_id)
        print(json.dumps({
            "agent_version": "1.11.2",
            "instrumentation_state": process.get("instrumentation_state"),
            "service": SERVICE,
            "environment": ENVIRONMENT,
            "live_traffic": live,
            "rum_key_prefix": rum.get("key_prefix"),
            "slo_id": slo_id,
            "synthetic_monitor_id": monitor_id,
            "synthetic_runs": sum(1 for item in objectives[1:] if item.get("success")),
            "view_counts": views,
        }, indent=2))
    finally:
        request("DELETE", f"/api/v1/apm/ingest-keys/{sdk['id']}", token=token)


if __name__ == "__main__":
    main()
