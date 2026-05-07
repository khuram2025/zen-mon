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
