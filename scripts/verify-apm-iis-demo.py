"""Read-only verification for the ZenPlus IIS APM demonstration host."""

from __future__ import annotations

import json
import os
import runpy
import sys
import urllib.error
import urllib.request
from pathlib import Path


helpers = runpy.run_path(str(Path(__file__).with_name("prepare-apm-iis-demo.py")))
request = helpers["request"]
authenticate = helpers["authenticate"]


def main() -> None:
    token = authenticate()
    processes = request("GET", "/api/v1/apm/agent-processes?active_hours=720", token=token)
    pool = next(item for item in processes if item.get("iis_app_pool") == "DefaultAppPool")
    database = request(
        "GET",
        "/api/v1/apm/database?service=default-web-site&env=dev&range=7d",
        token=token,
    )
    serialized_database = json.dumps(database)
    sql_redacted = not any(secret in serialized_database for secret in ("12345", "OPEN", "880021", "445566"))
    keys = request("GET", "/api/v1/apm/ingest-keys", token=token)
    demo_keys = [item for item in keys if str(item.get("name", "")).startswith("IIS demo")]
    active_temporary_sdk = [item for item in demo_keys if item.get("kind") == "sdk" and item.get("enabled")]
    active_rum = [item for item in demo_keys if item.get("kind") == "rum" and item.get("enabled")]

    page_url = "http://192.168.8.19/ZenPlusApmDemo/"
    with urllib.request.urlopen(page_url, timeout=15) as response:
        page = response.read().decode(errors="replace")
        page_status = response.status
    try:
        urllib.request.urlopen("http://192.168.8.19/LocalAuthTest/", timeout=15)
        protected_status = 200
    except urllib.error.HTTPError as exc:
        protected_status = exc.code

    extra = os.environ.get("ZENPLUS_PYWINRM_PATH")
    if extra:
        sys.path.insert(0, extra)
    import winrm  # type: ignore
    session = winrm.Session(
        "http://192.168.8.19:5985/wsman",
        auth=(os.environ.get("ZENPLUS_WINRM_USER", "Administrator"), os.environ["ZENPLUS_WINRM_PASSWORD"]),
        transport="ntlm",
    )
    result = session.run_ps("& 'C:\\Program Files\\ZenPlus\\Agent\\zenplus-agentctl.exe' version")
    if result.status_code:
        raise RuntimeError(result.std_err.decode(errors="replace"))
    version = result.std_out.decode(errors="replace").strip()

    manifest = request("GET", "/api/v1/agents/packages/manifest?platform=windows&arch=amd64")
    output = {
        "installed_agent": version,
        "published_agent": manifest.get("latest_version"),
        "agent_service_state": pool.get("agent_status"),
        "instrumentation_state": pool.get("instrumentation_state"),
        "configured_service": pool.get("configured_service_name"),
        "configured_environment": pool.get("configured_environment"),
        "telemetry_status": pool.get("telemetry_status"),
        "traces_15m": pool.get("traces_15m"),
        "demo_page_http": page_status,
        "rum_tag_present": 'data-app="zenplus-iis-demo"' in page,
        "protected_app_unauthenticated_http": protected_status,
        "active_temporary_sdk_keys": len(active_temporary_sdk),
        "active_demo_rum_keys": len(active_rum),
        "sql_literals_redacted": sql_redacted,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
