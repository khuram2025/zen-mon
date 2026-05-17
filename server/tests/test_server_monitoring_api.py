"""End-to-end smoke tests for the server-monitoring API.

Touches a real Postgres + ClickHouse via the live API process so the
full enroll → heartbeat → config → results pipeline is exercised.

These tests assume the dev stack from docker-compose + the systemd
zenplus-api service is up. They authenticate as the default admin user
(seeded by migrations).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

API = os.environ.get("ZENPLUS_API", "http://localhost:8000")
ADMIN_USER = os.environ.get("ZENPLUS_ADMIN", "admin")
ADMIN_PASS = os.environ.get("ZENPLUS_ADMIN_PASS", "admin123")


def _api_up() -> bool:
    try:
        r = requests.get(f"{API}/api/v1/system/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="ZenPlus API not running")


@pytest.fixture(scope="module")
def admin_token() -> str:
    r = requests.post(
        f"{API}/api/v1/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture()
def auth(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


def test_overview_endpoint(auth):
    r = requests.get(f"{API}/api/v1/server-monitoring/overview", headers=auth, timeout=5)
    r.raise_for_status()
    j = r.json()
    assert "total" in j
    assert "status_counts" in j
    assert isinstance(j.get("top_cpu"), list)


def test_builtin_policies_seeded(auth):
    r = requests.get(f"{API}/api/v1/agent-policies", headers=auth, timeout=5)
    r.raise_for_status()
    names = {p["name"] for p in r.json()["items"]}
    assert "Windows Baseline" in names
    assert "Linux Baseline" in names


def test_install_token_lifecycle(auth):
    # 1) create server
    suffix = uuid.uuid4().hex[:8]
    create = requests.post(
        f"{API}/api/v1/servers",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "display_name": f"pytest-host-{suffix}",
            "hostname": f"pytest-host-{suffix}",
            "os_type": "windows",
            "collection_mode": "agent",
        },
        timeout=5,
    )
    create.raise_for_status()
    server_id = create.json()["id"]

    # 2) issue install token
    tok = requests.post(
        f"{API}/api/v1/servers/{server_id}/install-token",
        headers={**auth, "Content-Type": "application/json"},
        json={"platform": "windows", "ttl_hours": 1, "max_uses": 1},
        timeout=5,
    ).json()
    assert tok["enrollment_token"].startswith("zpa_enr_")
    assert tok["platform"] == "windows"
    assert "msiexec" in tok["install_command"]

    # 3) agent enrolls
    enroll = requests.post(
        f"{API}/api/v1/agents/enroll",
        headers={"Content-Type": "application/json"},
        json={
            "enrollment_token": tok["enrollment_token"],
            "agent_uid": f"uid-{suffix}",
            "hostname": f"pytest-host-{suffix}",
            "platform": "windows",
            "version": "0.1.0",
            "os_name": "Windows Server 2022",
            "os_version": "10.0.20348",
            "architecture": "x64",
        },
        timeout=5,
    )
    assert enroll.status_code == 200, enroll.text
    e = enroll.json()
    assert e["server_id"] == server_id
    assert e["api_key"].startswith("zpa_key_")

    agent_headers = {
        "Authorization": f"Bearer {e['api_key']}",
        "X-Agent-Id": e["agent_id"],
        "Content-Type": "application/json",
    }

    # 4) heartbeat updates last_heartbeat_at
    hb = requests.post(
        f"{API}/api/v1/agents/heartbeat",
        headers=agent_headers,
        json={"version": "0.1.0", "uptime_seconds": 10, "queue_depth": 0, "spool_bytes": 0},
        timeout=5,
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["ok"] is True

    # 5) config endpoint returns a policy with an etag
    cfg = requests.get(
        f"{API}/api/v1/agents/config",
        headers=agent_headers,
        timeout=5,
    )
    assert cfg.status_code == 200, cfg.text
    cfg_json = cfg.json()
    assert cfg_json["etag"]
    assert cfg_json["metric_interval_s"] > 0

    # 6) config endpoint honors If-None-Match
    cfg2 = requests.get(
        f"{API}/api/v1/agents/config",
        headers={**agent_headers, "If-None-Match": cfg_json["etag"]},
        timeout=5,
    )
    assert cfg2.status_code == 304, cfg2.text

    # 7) results/host accepts a batch
    results = requests.post(
        f"{API}/api/v1/agents/results/host",
        headers=agent_headers,
        json={
            "agent_id": e["agent_id"],
            "server_id": server_id,
            "batch_id": f"batch-{suffix}",
            "sequence_start": 1,
            "sequence_end": 1,
            "agent_version": "0.1.0",
            "collected_at": "2026-05-15T18:00:00Z",
            "sent_at": "2026-05-15T18:00:05Z",
            "metrics": [
                {
                    "kind": "cpu",
                    "timestamp": "2026-05-15T18:00:00Z",
                    "data": {"cpu_total_pct": 12.5, "per_core": [10, 15]},
                },
                {
                    "kind": "memory",
                    "timestamp": "2026-05-15T18:00:00Z",
                    "data": {"total_bytes": 8_000_000_000, "used_bytes": 4_000_000_000, "used_pct": 50},
                },
            ],
        },
        timeout=5,
    )
    assert results.status_code == 200, results.text
    j = results.json()
    assert j["accepted"] >= 2

    # 8) cleanup — decommission the test server
    requests.post(f"{API}/api/v1/servers/{server_id}/decommission", headers=auth, timeout=5)
    requests.delete(f"{API}/api/v1/servers/{server_id}", headers=auth, timeout=5)


def test_enroll_rejects_invalid_token():
    r = requests.post(
        f"{API}/api/v1/agents/enroll",
        headers={"Content-Type": "application/json"},
        json={
            "enrollment_token": "zpa_enr_definitely-not-a-real-token-xxxx",
            "agent_uid": "uid-invalid",
            "hostname": "bogus",
            "platform": "windows",
            "version": "0.1.0",
        },
        timeout=5,
    )
    assert r.status_code == 401


def test_agent_endpoints_require_credentials():
    r = requests.post(f"{API}/api/v1/agents/heartbeat", json={"version": "0"}, timeout=5)
    assert r.status_code == 401

    r = requests.get(f"{API}/api/v1/agents/config", timeout=5)
    assert r.status_code == 401


def test_servers_list_requires_auth():
    r = requests.get(f"{API}/api/v1/servers", timeout=5)
    assert r.status_code in (401, 403)


def test_agent_fleet_filter(auth):
    r = requests.get(f"{API}/api/v1/agent-fleet?platform=windows", headers=auth, timeout=5)
    assert r.status_code == 200
    items = r.json()["items"]
    for a in items:
        assert a["platform"] == "windows"
