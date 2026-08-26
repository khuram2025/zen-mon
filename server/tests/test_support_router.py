"""FastAPI route tests for /api/v1/support/*.

We mock the support_jobs module so the router can be exercised without
touching disk or systemd.
"""

from __future__ import annotations

import pytest


VALID_ID = "e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2"


@pytest.fixture
def fake_jobs(monkeypatch):
    from app.services import support_jobs

    state = {
        "id": VALID_ID,
        "status": "queued",
        "phase": "queued",
        "created_at": "2026-05-19T12:00:00+00:00",
        "completed_at": None,
        "size_bytes": 0,
        "sha256": None,
        "filename": None,
        "requested_by": "admin-user",
        "error": "",
        "request": {
            "job_id": VALID_ID,
            "issue_category": "snmp_discovery",
            "issue_summary": "",
            "time_range": "24h",
            "include_extended_logs": False,
            "requested_by": "admin-user",
            "created_at": "2026-05-19T12:00:00+00:00",
        },
    }

    monkeypatch.setattr(support_jobs, "enqueue_job", lambda **kw: state)
    monkeypatch.setattr(support_jobs, "get_status", lambda jid: state if jid == VALID_ID else None)
    monkeypatch.setattr(support_jobs, "list_jobs", lambda **kw: [state])
    monkeypatch.setattr(support_jobs, "delete_job", lambda jid: True)
    return state


def test_post_bundle_requires_admin(client, as_viewer, fake_jobs):
    r = client.post("/api/v1/support/bundles", json={"issue_category": "other"})
    assert r.status_code == 403


def test_post_bundle_returns_202_for_admin(client, as_admin, fake_jobs):
    r = client.post("/api/v1/support/bundles", json={
        "issue_category": "snmp_discovery",
        "issue_summary": "test",
        "time_range": "24h",
        "include_extended_logs": False,
    })
    assert r.status_code == 202
    body = r.json()
    assert body["id"] == VALID_ID
    assert body["status"] == "queued"


def test_list_bundles_requires_admin(client, as_viewer, fake_jobs):
    r = client.get("/api/v1/support/bundles")
    assert r.status_code == 403


def test_list_bundles_returns_array(client, as_admin, fake_jobs):
    r = client.get("/api/v1/support/bundles")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert r.json()[0]["id"] == VALID_ID


def test_get_bundle_rejects_invalid_uuid(client, as_admin, fake_jobs):
    r = client.get("/api/v1/support/bundles/../etc/passwd")
    # FastAPI maps non-matching path params to 404 before our 400 fires for
    # the slash case; verify a non-UUID without slashes hits our validator.
    assert r.status_code in (400, 404)

    r = client.get("/api/v1/support/bundles/not-a-uuid")
    assert r.status_code == 400


def test_get_bundle_returns_404_for_unknown_uuid(client, as_admin, fake_jobs):
    r = client.get("/api/v1/support/bundles/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert r.status_code == 404


def test_download_blocked_unless_ready(client, as_admin, fake_jobs):
    r = client.get(f"/api/v1/support/bundles/{VALID_ID}/download")
    assert r.status_code == 409  # not yet ready


def test_download_serves_file_when_ready(client, as_admin, fake_jobs, tmp_path, monkeypatch):
    from app.services import support_jobs

    bundle_file = tmp_path / "bundle.tar.gz"
    bundle_file.write_bytes(b"pretend-tarball")

    fake_jobs["status"] = "ready"
    fake_jobs["filename"] = "zenplus-support-deadbeef-20260519T120000Z.tar.gz"
    monkeypatch.setattr(support_jobs, "bundle_path", lambda jid: bundle_file)

    r = client.get(f"/api/v1/support/bundles/{VALID_ID}/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/gzip")
    assert "zenplus-support-deadbeef" in r.headers.get("content-disposition", "")
    assert r.content == b"pretend-tarball"


def test_delete_bundle_requires_admin(client, as_viewer, fake_jobs):
    r = client.delete(f"/api/v1/support/bundles/{VALID_ID}")
    assert r.status_code == 403


def test_delete_bundle_returns_204(client, as_admin, fake_jobs):
    fake_jobs["status"] = "ready"
    r = client.delete(f"/api/v1/support/bundles/{VALID_ID}")
    assert r.status_code == 204


def test_delete_bundle_rejects_running_job(client, as_admin, fake_jobs):
    fake_jobs["status"] = "running"
    r = client.delete(f"/api/v1/support/bundles/{VALID_ID}")
    assert r.status_code == 409


def test_delete_bundle_404_for_unknown(client, as_admin, fake_jobs):
    r = client.delete("/api/v1/support/bundles/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert r.status_code == 404
