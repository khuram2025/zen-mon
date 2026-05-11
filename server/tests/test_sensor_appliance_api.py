from __future__ import annotations

from app.api.v1 import sensor_api


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
    assert "mock sensor" not in body.lower()


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
