from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


def _device(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "id": uuid4(),
        "hostname": "edge-01",
        "ip_address": "192.0.2.10",
        "device_type": "router",
        "location": "dc-1",
        "group_id": None,
        "group": None,
        "tags": ["wan"],
        "ping_enabled": True,
        "ping_interval": 60,
        "status": "unknown",
        "last_seen": None,
        "last_rtt_ms": None,
        "description": "test device",
        "created_at": now,
        "updated_at": now,
        "snmp_enabled": False,
        "snmp_version": "2c",
        "snmp_port": 161,
        "snmp_community": None,
        "snmp_v3_username": None,
        "snmp_v3_context": None,
        "snmp_auth_protocol": None,
        "snmp_auth_passphrase": None,
        "snmp_priv_protocol": None,
        "snmp_priv_passphrase": None,
        "snmp_timeout_ms": 2000,
        "snmp_retries": 2,
        "snmp_max_repetitions": 25,
        "snmp_poll_interval": 60,
        "sys_object_id": None,
        "vendor": None,
        "model": None,
        "os_version": None,
        "profile_id": None,
        "snmp_credential_id": None,
        "poll_mode": "direct",
        "managed_by_device_id": None,
        "serial_number": None,
        "managed_ip": None,
        "managed_source": None,
        "managed_last_seen": None,
        "promote_managed": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _service_check(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "id": uuid4(),
        "device_id": None,
        "device_hostname": None,
        "group_id": None,
        "group_name": None,
        "parent_check_id": None,
        "parent_check_name": None,
        "name": "Website",
        "check_type": "http",
        "level": 1,
        "config": {},
        "tags": ["public"],
        "retry_count": 1,
        "retry_delay_s": 30,
        "in_maintenance": False,
        "enabled": True,
        "target_host": "example.com",
        "target_port": None,
        "target_url": "https://example.com",
        "http_method": "GET",
        "http_expected_status": 200,
        "http_expected_statuses": "200,2xx",
        "http_content_match": None,
        "http_follow_redirects": True,
        "tls_warn_days": 30,
        "tls_critical_days": 7,
        "check_interval": 60,
        "timeout": 10,
        "status": "unknown",
        "last_check_at": None,
        "last_response_ms": None,
        "last_error": None,
        "tls_expiry_date": None,
        "tls_days_remaining": None,
        "tls_issuer": None,
        "tls_subject": None,
        "description": None,
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return data


def test_operator_can_create_device_without_exposing_snmp_passphrases(client, as_operator, monkeypatch):
    async def fake_create_device(db, data, user_id):
        return _device(
            hostname=data.hostname,
            ip_address=data.ip_address,
            snmp_enabled=data.snmp_enabled,
            snmp_auth_passphrase="encrypted-auth",
            snmp_priv_passphrase="encrypted-priv",
        )

    monkeypatch.setattr(
        "app.api.v1.devices.device_service.create_device",
        fake_create_device,
    )

    response = client.post(
        "/api/v1/devices",
        headers={"Authorization": "Bearer test"},
        json={
            "hostname": "edge-01",
            "ip_address": "192.0.2.10",
            "device_type": "router",
            "snmp_enabled": True,
            "snmp_auth_passphrase": "auth-secret",
            "snmp_priv_passphrase": "priv-secret",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["hostname"] == "edge-01"
    assert body["snmp_auth_configured"] is True
    assert body["snmp_priv_configured"] is True
    assert "snmp_auth_passphrase" not in body
    assert "snmp_priv_passphrase" not in body


def test_service_check_create_validates_and_normalizes_status_patterns(client, as_operator, monkeypatch):
    captured = {}

    async def fake_create_service_check(db, data, user_id):
        captured["http_expected_statuses"] = data.http_expected_statuses
        return _service_check(http_expected_statuses=data.http_expected_statuses)

    monkeypatch.setattr(
        "app.api.v1.service_checks.service_check_service.create_service_check",
        fake_create_service_check,
    )

    response = client.post(
        "/api/v1/service-checks",
        headers={"Authorization": "Bearer test"},
        json={
            "name": "Website",
            "check_type": "http",
            "target_host": "example.com",
            "target_url": "https://example.com",
            "http_expected_statuses": "200, 2XX, 300-399",
        },
    )

    assert response.status_code == 201
    assert captured["http_expected_statuses"] == "200,2xx,300-399"
    assert response.json()["http_expected_statuses"] == "200,2xx,300-399"


def test_service_check_create_rejects_invalid_status_patterns(client, as_operator):
    response = client.post(
        "/api/v1/service-checks",
        headers={"Authorization": "Bearer test"},
        json={
            "name": "Website",
            "check_type": "http",
            "target_host": "example.com",
            "target_url": "https://example.com",
            "http_expected_statuses": "2*,700",
        },
    )

    assert response.status_code == 422


def test_report_csv_generation_returns_download_headers(client, as_viewer, monkeypatch):
    async def fake_csv_report(**kwargs):
        return b"name,status\nedge-01,up\n"

    monkeypatch.setattr("app.api.v1.reports.generate_csv_report", fake_csv_report)

    response = client.post(
        "/api/v1/reports/generate",
        headers={"Authorization": "Bearer test"},
        json={"report_type": "device_health", "period": "last_24h", "format": "csv"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "ZenPlus-Device-Health" in response.headers["content-disposition"]
    assert response.content == b"name,status\nedge-01,up\n"


def test_operator_can_create_alert_rule(client, as_operator):
    response = client.post(
        "/api/v1/alert-rules",
        headers={"Authorization": "Bearer test"},
        json={
            "name": "High RTT",
            "metric": "rtt",
            "operator": ">",
            "threshold": 150,
            "severity": "warning",
            "min_duration": 60,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "High RTT"
    assert body["metric"] == "rtt"
    assert body["threshold"] == 150.0
    assert body["created_by"]


def test_alert_rule_rejects_unknown_metric(client, as_operator):
    response = client.post(
        "/api/v1/alert-rules",
        headers={"Authorization": "Bearer test"},
        json={
            "name": "Bad metric",
            "metric": "cpu_usage",
            "operator": ">",
            "threshold": 90,
        },
    )

    assert response.status_code == 422
