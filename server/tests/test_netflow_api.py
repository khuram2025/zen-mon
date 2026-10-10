"""NetFlow API tests.

Two layers:
  * Pure unit tests for the classification / window / scope helpers (no services).
  * Live smoke tests against a running API + ClickHouse (skipped when down),
    mirroring test_server_monitoring_api.py conventions.

Run the live layer against a specific instance with e.g.
  ZENPLUS_API=http://localhost:8001 pytest tests/test_netflow_api.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.netflow import filters as F  # noqa: E402
from app.api.v1.netflow.common import NetflowError, resolve_window  # noqa: E402
from app.api.v1.netflow.sql import NET_CLASS_NAMES, NET_CLASSES  # noqa: E402
from app.services import netflow_classify as nc  # noqa: E402

API = os.environ.get("ZENPLUS_API", "")
ADMIN_USER = os.environ.get("ZENPLUS_ADMIN", "admin")
ADMIN_PASS = os.environ.get("ZENPLUS_ADMIN_PASS", "admin123")


# ── Unit: application classification (one classifier, services/netflow_classify) ──

def _app(protocol: int, dst_port: int, src_port: int = 40000) -> str:
    return nc.classify(protocol, src_port, dst_port, rules=nc.build_rules()).application


def test_portless_protocols_classify_by_protocol():
    assert _app(50, 0) == nc.APP_VPN    # ESP
    assert _app(47, 0) == nc.APP_VPN    # GRE
    assert _app(51, 0) == nc.APP_VPN    # AH
    assert _app(1, 0) == nc.APP_ICMP
    assert _app(58, 0) == nc.APP_ICMP   # ICMPv6


def test_port_protocols_classify_by_the_service_port():
    assert _app(6, 443) == nc.APP_WEB
    assert _app(17, 53) == "DNS"
    assert _app(6, 515) == nc.APP_SYSTEM
    assert _app(6, 49152, 49153) == nc.APP_OTHER


def test_app_filter_compares_the_shared_classifier():
    # The app filter compares the classifier's application expression, so
    # filtered totals partition exactly like the /applications breakdown.
    for app in (nc.APP_VPN, nc.APP_ICMP, nc.APP_WEB, nc.APP_OTHER):
        sql, params = F.compile_filter(F.parse_flow_filter({"app": app}), "flow_records", F.UNRESTRICTED)
        assert sql.endswith("= %(nf_app)s") and params["nf_app"] == app


# ── Unit: window resolution ──────────────────────────────────────────────────

def test_resolve_window_minutes_beats_hours():
    w = resolve_window(24, 90, None, None, None, max_span=timedelta(days=93))
    assert abs((w.end - w.start) - timedelta(minutes=90)) < timedelta(seconds=2)


def test_resolve_window_custom_bounds():
    w = resolve_window(24, None, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", None, max_span=timedelta(days=93))
    assert w.start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 8, 2, tzinfo=timezone.utc)


def test_resolve_window_rejects_inverted_bounds():
    with pytest.raises(NetflowError) as exc:
        resolve_window(24, None, "2026-08-02T00:00:00Z", "2026-08-01T00:00:00Z", None, max_span=timedelta(days=93))
    assert exc.value.status_code == 400


def test_net_classes_are_valid_cidrs():
    import ipaddress
    for label, cidrs in NET_CLASSES.items():
        for cidr in cidrs:
            net = ipaddress.ip_network(cidr)
            assert int(net.network_address) <= int(net.broadcast_address), label


# ── Live smoke tests ─────────────────────────────────────────────────────────

def _api_up() -> bool:
    # Live smoke tests must explicitly target a disposable appliance.
    if not API:
        return False
    try:
        return requests.get(f"{API}/api/v1/system/health", timeout=2).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _api_up(), reason="Set ZENPLUS_API to a disposable test appliance to run live smoke tests")


@pytest.fixture(scope="module")
def auth() -> dict[str, str]:
    r = requests.post(
        f"{API}/api/v1/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=5,
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@live
def test_overview_shape(auth):
    r = requests.get(f"{API}/api/v1/netflow/overview?hours=1", headers=auth, timeout=30)
    assert r.status_code == 200
    body = r.json()
    for key in ("bytes", "packets", "flows", "exporters", "src_hosts", "dst_hosts", "current_bps"):
        assert key in body


@live
def test_overview_minutes_window(auth):
    hour = requests.get(f"{API}/api/v1/netflow/overview?hours=24", headers=auth, timeout=60).json()
    ten_min = requests.get(f"{API}/api/v1/netflow/overview?minutes=10", headers=auth, timeout=30).json()
    # A 10-minute window can never contain more flows than the surrounding 24h.
    assert ten_min["flows"] <= hour["flows"]


@live
def test_top_talkers_by_src_ranking(auth):
    r = requests.get(f"{API}/api/v1/netflow/top-talkers?hours=1&limit=10&by=src", headers=auth, timeout=60)
    assert r.status_code == 200
    rows = r.json()
    src_bytes = [row["src_bytes"] for row in rows]
    assert src_bytes == sorted(src_bytes, reverse=True)
    for row in rows:
        assert row["bytes"] == row["src_bytes"] + row["dst_bytes"]


@live
def test_top_endpoints_sorted_by_total(auth):
    rows = requests.get(f"{API}/api/v1/netflow/top-endpoints?hours=1&limit=10", headers=auth, timeout=60).json()
    totals = [row["bytes"] for row in rows]
    assert totals == sorted(totals, reverse=True)


@live
def test_top_conversations_shape(auth):
    rows = requests.get(f"{API}/api/v1/netflow/top-conversations?hours=1&limit=5", headers=auth, timeout=120).json()
    for row in rows:
        assert row["src"] and row["dst"]
        assert row["bytes"] >= 0 and row["flows"] >= 1
        assert "application" in row


@live
def test_applications_classify_portless_protocols(auth):
    rows = requests.get(f"{API}/api/v1/netflow/applications?hours=24", headers=auth, timeout=120).json()
    names = {row["name"] for row in rows}
    # This network carries ESP tunnels; they must not be lumped into "Other".
    protos = requests.get(f"{API}/api/v1/netflow/protocols?hours=24", headers=auth, timeout=120).json()
    if any(p["protocol"] in (47, 50, 51) for p in protos):
        assert "VPN / Tunneling" in names


@live
def test_application_filter_partitions(auth):
    total = requests.get(f"{API}/api/v1/netflow/overview?hours=1", headers=auth, timeout=60).json()
    vpn = requests.get(f"{API}/api/v1/netflow/overview?hours=1&app=VPN%20%2F%20Tunneling", headers=auth, timeout=60).json()
    assert vpn["bytes"] <= total["bytes"]


@live
def test_network_classes_labels(auth):
    rows = requests.get(f"{API}/api/v1/netflow/network-classes?hours=1", headers=auth, timeout=120).json()
    valid = set(NET_CLASS_NAMES)
    for row in rows:
        assert row["name"] in valid


@live
def test_forensics_csv_export(auth):
    r = requests.get(f"{API}/api/v1/netflow/forensics?hours=1&limit=5&format=csv", headers=auth, timeout=60)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    header = r.text.splitlines()[0]
    assert header.startswith("timestamp,exporter_ip,src,")


@live
def test_saved_views_roundtrip(auth):
    created = requests.post(
        f"{API}/api/v1/netflow/saved-views",
        json={"name": "pytest-probe", "query": {"hours": 1, "proto": "6"}},
        headers=auth,
        timeout=15,
    )
    assert created.status_code == 200
    view_id = created.json()["id"]
    listed = requests.get(f"{API}/api/v1/netflow/saved-views", headers=auth, timeout=15).json()
    assert any(v["id"] == view_id for v in listed)
    deleted = requests.delete(f"{API}/api/v1/netflow/saved-views/{view_id}", headers=auth, timeout=15)
    assert deleted.status_code == 200
    listed = requests.get(f"{API}/api/v1/netflow/saved-views", headers=auth, timeout=15).json()
    assert not any(v["id"] == view_id for v in listed)


@live
def test_device_status_exposes_honest_fields(auth):
    body = requests.get(f"{API}/api/v1/netflow/device-status?hours=1", headers=auth, timeout=60).json()
    assert body.get("flow_derived") is True
    for key in ("flow_duration_ms", "rst_ratio_pct", "flow_continuity_pct"):
        assert key in body


@live
def test_heatmap_shape(auth):
    body = requests.get(f"{API}/api/v1/netflow/heatmap?hours=168", headers=auth, timeout=120).json()
    assert "cells" in body and "max_bytes" in body
    for cell in body["cells"]:
        assert 1 <= cell["dow"] <= 7
        assert 0 <= cell["hour"] <= 23
