from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.api.v1 import sensor_api
from app.api.v1.sensors import _bootstrap_cloud_init, _install_command, _sensor_env


def test_sensor_appliance_manifest_reports_artifact_slots(client):
    response = client.get("/api/v1/sensor/appliance/manifest")

    assert response.status_code == 200
    body = response.json()
    assert body["product"] == "ZenPlus Remote Sensor"
    assert {item["kind"] for item in body["artifacts"]} == {"ova", "ovf", "sha256"}
    assert body["bootstrap"]["required_values"] == [
        "server_url",
        "enrollment_token",
        "sensor_name",
    ]


def test_unpublished_sensor_appliance_download_returns_clear_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor_api, "SENSOR_ARTIFACT_DIR", tmp_path)

    response = client.get("/api/v1/sensor/appliance/ova")

    assert response.status_code == 404
    assert "not published yet" in response.json()["detail"]


def test_published_sensor_appliance_download_serves_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor_api, "SENSOR_ARTIFACT_DIR", tmp_path)
    (tmp_path / "zenplus-sensor.ova").write_bytes(b"preview ova")
    (tmp_path / "SHA256SUMS").write_text(
        "9b915a03fb4eb38fb979cb20cd252d160ee4ee41c471c9e3e903542eb9b4197b  zenplus-sensor.ova\n"
    )

    manifest = client.get("/api/v1/sensor/appliance/manifest").json()
    ova = next(item for item in manifest["artifacts"] if item["kind"] == "ova")
    response = client.get("/api/v1/sensor/appliance/ova")

    assert ova["available"] is True
    assert ova["sha256"] == "9b915a03fb4eb38fb979cb20cd252d160ee4ee41c471c9e3e903542eb9b4197b"
    assert response.status_code == 200
    assert response.content == b"preview ova"


def test_sensor_install_sh_installs_real_ubuntu_service(client):
    response = client.get("/api/v1/sensor/install.sh")

    assert response.status_code == 200
    body = response.text
    assert "ZenPlus remote sensor installer for Ubuntu" in body
    assert "/api/v1/sensor/bin/$PLATFORM/zenplus-sensor" in body
    assert "zenplus-sensor.service" in body
    assert "ExecStart=$INSTALL_BIN" in body
    assert "UMask=0077" in body
    assert 'install -d -m 0770 -o root -g zenplus-sensor "$ENV_DIR"' in body
    assert 'if [[ -s "$STATE_DIR/state.json" ]]' in body
    assert 'ZENPLUS_VERIFY_TLS cannot disable controller certificate verification.' in body
    assert 'ZENPLUS_SENSOR_STATE_DIR overrides are not supported' in body
    assert "curl_args+=(-k)" not in body
    assert "write_env_line ZENPLUS_SENSOR_NAME" in body
    assert "write_env_line HTTPS_PROXY" in body
    assert "mock sensor" not in body.lower()

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable on this test host")
    syntax = subprocess.run(
        [bash, "-n"], input=body, text=True, capture_output=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr


def test_configured_ova_installs_controller_ca_before_enrollment():
    script = (
        Path(__file__).resolve().parents[2]
        / "sensor-appliance"
        / "scripts"
        / "build-configured-ova.sh"
    ).read_text()

    assert 'cfg.get("controller_ca_pem")' in script
    assert "/usr/local/share/ca-certificates/zenplus-controller.crt" in script
    assert "update-ca-certificates" in script


def test_one_line_installer_is_failure_safe_proxy_aware_and_shell_quotes_name():
    command = _install_command(
        "https://192.0.2.10",
        "zps_enr_example",
        "Riyadh O'Brien",
        "http://192.0.2.20:3128",
    )

    assert "ZP_INSTALL=$(mktemp)" in command
    assert '-o "$ZP_INSTALL"' in command
    assert "| sudo" not in command
    assert "--proxy http://192.0.2.20:3128" in command
    assert "ZENPLUS_PROXY_URL=http://192.0.2.20:3128" in command
    assert "Riyadh O'\"'\"'Brien" in command
    assert "exit $ZP_RC" in command


def test_cloud_init_environment_uses_systemd_safe_quoting():
    env_file = _sensor_env(
        "https://192.0.2.10",
        "zps_enr_example",
        "King's \\ \"Branch\"",
        "http://192.0.2.20:3128",
    )

    assert 'ZENPLUS_SENSOR_NAME="King\'s \\\\ \\\"Branch\\\""' in env_file
    assert 'HTTPS_PROXY="http://192.0.2.20:3128"' in env_file

    cloud_init = _bootstrap_cloud_init(
        "https://192.0.2.10", "zps_enr_example", "Riyadh branch"
    )
    assert "owner: zenplus-sensor:zenplus-sensor" in cloud_init
    assert "permissions: '0600'" in cloud_init


def test_arm64_binary_is_rejected_until_release_pipeline_publishes_it(client):
    response = client.get("/api/v1/sensor/bin/linux-arm64/manifest.json")
    assert response.status_code == 404


def test_unpublished_sensor_binary_returns_clear_404(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor_api, "SENSOR_ARTIFACT_DIR", tmp_path)

    response = client.get("/api/v1/sensor/bin/linux-amd64/zenplus-sensor")

    assert response.status_code == 404
    assert "not published yet" in response.json()["detail"]


def test_published_sensor_binary_and_checksum_are_served(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor_api, "SENSOR_ARTIFACT_DIR", tmp_path)
    artifact_dir = tmp_path / "bin" / "linux-amd64"
    artifact_dir.mkdir(parents=True)
    binary = artifact_dir / "zenplus-sensor"
    binary.write_bytes(b"sensor-binary")

    manifest = client.get("/api/v1/sensor/bin/linux-amd64/manifest.json")
    checksum = client.get("/api/v1/sensor/bin/linux-amd64/zenplus-sensor.sha256")
    download = client.get("/api/v1/sensor/bin/linux-amd64/zenplus-sensor")

    assert manifest.status_code == 200
    assert manifest.json()["available"] is True
    assert checksum.status_code == 200
    assert checksum.text.endswith("  zenplus-sensor\n")
    assert download.status_code == 200
    assert download.content == b"sensor-binary"


def test_sensor_update_manifest_requires_valid_offline_signature(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor_api, "SENSOR_ARTIFACT_DIR", tmp_path)
    artifact_dir = tmp_path / "bin" / "linux-amd64"
    artifact_dir.mkdir(parents=True)
    binary = artifact_dir / "zenplus-sensor"
    binary.write_bytes(b"signed-sensor-binary")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest_bytes = (json.dumps({
        "product": "ZenPlus Remote Sensor",
        "platform": "linux-amd64",
        "os": "linux",
        "arch": "amd64",
        "version": "sensor-1.22.4",
        "binary": "zenplus-sensor",
        "binary_url": "zenplus-sensor",
        "sha256": digest,
    }, indent=2) + "\n").encode()
    private_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "release.pub"
    public_path.write_bytes(private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(sensor_api, "SENSOR_RELEASE_PUBLIC_KEY", public_path)
    (artifact_dir / "manifest.json").write_bytes(manifest_bytes)
    (artifact_dir / "manifest.json.sig").write_bytes(private_key.sign(manifest_bytes))

    response = client.get("/api/v1/sensor/bin/linux-amd64/manifest.json")
    assert response.status_code == 200
    assert set(response.json()) == {"signed_manifest", "signature"}

    (artifact_dir / "manifest.json").write_bytes(manifest_bytes + b" ")
    rejected = client.get("/api/v1/sensor/bin/linux-amd64/manifest.json").json()
    assert rejected["self_update_available"] is False
    assert "signed_manifest" not in rejected
