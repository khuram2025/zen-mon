from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from app.api.v1.sensor_api import _controller_ca_sha256, _heartbeat_health
from app.schemas.sensor import (
    HeartbeatRequest,
    ConfigServiceCheck,
    PingResultsBatch,
    SensorCommandCreate,
    SensorCreate,
    ServiceResultsBatch,
    SnmpResultsBatch,
)


def _certificate(common_name: str, *, issuer_cert=None, issuer_key=None):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = issuer_cert.subject if issuer_cert is not None else name
    signer = issuer_key or key
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(signer, hashes.SHA256())
    )
    return cert, key


def test_static_ipv6_bootstrap_accepts_ipv6_prefix():
    sensor = SensorCreate(
        name="Riyadh branch",
        network_mode="static",
        sensor_ip="2001:db8::20",
        sensor_cidr=64,
        gateway="2001:db8::1",
        dns_servers=["2001:4860:4860::8888"],
    )
    assert sensor.sensor_cidr == 64


def test_sensor_bootstrap_rejects_cross_family_gateway_and_multiline_name():
    with pytest.raises(ValidationError):
        SensorCreate(
            name="branch\ninjected",
            network_mode="static",
            sensor_ip="192.0.2.20",
            sensor_cidr=24,
            gateway="2001:db8::1",
        )
    with pytest.raises(ValidationError):
        SensorCreate(name="insecure branch", controller_url="http://192.0.2.10")


def test_result_batches_require_stable_keys_and_typed_values():
    with pytest.raises(ValidationError):
        PingResultsBatch(items=[])
    with pytest.raises(ValidationError):
        PingResultsBatch(
            idempotency_key="batch-0001",
            items=[{
                "device_id": "f0264410-b97c-450c-a255-62b649764934",
                "timestamp": "2026-09-01T00:00:00Z",
                "is_up": True,
                "rtt_ms": float("nan"),
            }],
        )
    service = ServiceResultsBatch(
        idempotency_key="batch-0002",
        items=[{
            "service_check_id": "6dc06f0d-f684-4910-a956-d1e49db14461",
            "timestamp": "2026-09-01T00:00:00Z",
            "check_type": "tcp",
            "is_up": True,
            "status_code": 0,
        }],
    )
    assert service.items[0].status_code is None
    with pytest.raises(ValidationError):
        SnmpResultsBatch(
            idempotency_key="batch-0003",
            items=[{
                "device_id": "f0264410-b97c-450c-a255-62b649764934",
                "timestamp": "2026-09-01T00:00:00Z",
                "oid": "not-an-oid",
                "value": "not-a-number",
            }],
        )


def test_remote_service_config_preserves_behavior_defining_fields():
    check = ConfigServiceCheck(
        id="6dc06f0d-f684-4910-a956-d1e49db14461",
        name="custom API",
        check_type="http",
        target_url="https://example.invalid/health",
        http_method="POST",
        http_headers={"Content-Type": "application/json"},
        http_body='{"probe": true}',
        http_expected_status=204,
        http_expected_statuses="200,204",
        http_allow_insecure_auth=False,
        config={"record_type": "AAAA", "expected": "2001:db8::1"},
        retry_count=3,
        retry_delay_s=15,
    )
    assert check.http_expected_status == 204
    assert check.http_headers["Content-Type"] == "application/json"
    assert check.retry_delay_s == 15


def test_heartbeat_degrades_on_new_drops_or_old_version():
    base = {
        "queue_dropped_count": 5,
        "version": "sensor-1.22.4",
        "min_supported_version": "sensor-1.22.0",
    }
    assert _heartbeat_health(base, HeartbeatRequest(queue_dropped_count=5))[0] == "online"
    assert _heartbeat_health(base, HeartbeatRequest(queue_dropped_count=6))[0] == "degraded"
    outdated = {**base, "version": "sensor-1.21.9"}
    assert _heartbeat_health(outdated, HeartbeatRequest(queue_dropped_count=5))[0] == "degraded"


def test_sensor_commands_reject_unknown_payload_and_bad_log_level():
    SensorCommandCreate(verb="update", payload={"version": "sensor-1.22.4"})
    with pytest.raises(ValidationError):
        SensorCommandCreate(verb="flush_buffer", payload={"delete": True})
    with pytest.raises(ValidationError):
        SensorCommandCreate(verb="set_log_level", payload={"level": "trace"})


def test_controller_pin_is_self_signed_trust_anchor_not_chained_leaf(tmp_path, monkeypatch):
    root, root_key = _certificate("ZenPlus Root")
    leaf, _ = _certificate("controller.example", issuer_cert=root, issuer_key=root_key)
    cert_path = tmp_path / "controller.pem"
    monkeypatch.delenv("ZENPLUS_CONTROLLER_CA_SHA256", raising=False)
    monkeypatch.setenv("ZENPLUS_CONTROLLER_CA_CERT", str(cert_path))

    cert_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    expected = hashlib.sha256(root.public_bytes(serialization.Encoding.DER)).hexdigest()
    assert _controller_ca_sha256() == expected

    # If a stable root is explicitly bundled, pin that final trust anchor and
    # never the leaf that precedes it.
    cert_path.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + root.public_bytes(serialization.Encoding.PEM)
    )
    assert _controller_ca_sha256() == expected

    # Ordinary nginx fullchains stop at an intermediate. With no stable root
    # present, omit the pin and rely on normal trust validation.
    intermediate, intermediate_key = _certificate(
        "ZenPlus Intermediate", issuer_cert=root, issuer_key=root_key
    )
    chained_leaf, _ = _certificate(
        "controller.example", issuer_cert=intermediate, issuer_key=intermediate_key
    )
    cert_path.write_bytes(
        chained_leaf.public_bytes(serialization.Encoding.PEM)
        + intermediate.public_bytes(serialization.Encoding.PEM)
    )
    assert _controller_ca_sha256() is None


def test_migration_defines_single_owner_and_replay_ledger():
    migration = (
        Path(__file__).resolve().parents[2] / "scripts" / "migrate-105-sensor-fabric.sql"
    ).read_text()
    assert "CREATE OR REPLACE VIEW device_polling_owner" in migration
    assert "s.status IN ('online', 'degraded', 'offline')" in migration
    assert "CREATE TABLE IF NOT EXISTS sensor_ingest_ledger" in migration
    assert "service_check_vantage_status" in migration
    assert "'warning'" in migration
    assert "last_tls_days_remaining" in migration
    assert "sensor_transition_outbox" in migration
    assert "sensor_commands" in migration
    assert "idx_sensor_transition_outbox_entity_fifo" in migration
    assert "bootstrap_config" in migration
