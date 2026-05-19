"""Unit tests for the API-side support_jobs helper.

Covers UUID validation, path-traversal rejection, request/state file shape,
and the systemctl invocation path (mocked).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import support_jobs


@pytest.fixture
def isolated_support_root(tmp_path, monkeypatch):
    """Point the helper at a tmp dir so we don't touch /opt/zenplus/support."""
    monkeypatch.setattr(support_jobs, "SUPPORT_ROOT", tmp_path)
    monkeypatch.setattr(support_jobs, "REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(support_jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(support_jobs, "BUNDLES_DIR", tmp_path / "bundles")
    return tmp_path


def test_uuid_validator_accepts_uuids():
    assert support_jobs.is_valid_job_id("e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2")


@pytest.mark.parametrize("bad", [
    "",
    "not-a-uuid",
    "../etc/passwd",
    "/etc/shadow",
    "e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2; rm -rf /",
    "E4F1C1B0-3B1A-4C2D-9E10-C7F8A9B0C1D2",  # uppercase is rejected — keeps paths predictable
    "e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2.tar",
])
def test_uuid_validator_rejects_garbage(bad):
    assert not support_jobs.is_valid_job_id(bad)


def test_path_helpers_refuse_non_uuid():
    with pytest.raises(ValueError):
        support_jobs.request_path("../escape")
    with pytest.raises(ValueError):
        support_jobs.job_path("not-a-uuid")
    with pytest.raises(ValueError):
        support_jobs.bundle_path("e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2; ls")


def test_enqueue_writes_request_and_state_files(isolated_support_root):
    with patch.object(support_jobs, "_trigger_systemd") as trigger:
        state = support_jobs.enqueue_job(
            issue_category="snmp_discovery",
            issue_summary="cannot assign SNMPv3 cred",
            time_range="24h",
            include_extended_logs=False,
            requested_by="admin",
        )

    job_id = state["id"]
    assert support_jobs.is_valid_job_id(job_id)
    trigger.assert_called_once_with(job_id)

    req = json.loads(support_jobs.request_path(job_id).read_text())
    assert req["issue_category"] == "snmp_discovery"
    assert req["issue_summary"] == "cannot assign SNMPv3 cred"
    assert req["time_range"] == "24h"
    assert req["include_extended_logs"] is False
    assert req["requested_by"] == "admin"

    saved = json.loads(support_jobs.job_path(job_id).read_text())
    assert saved["status"] == support_jobs.STATUS_QUEUED
    assert saved["id"] == job_id


def test_enqueue_rejects_invalid_options(isolated_support_root):
    with patch.object(support_jobs, "_trigger_systemd"):
        with pytest.raises(ValueError):
            support_jobs.enqueue_job(
                issue_category="bogus",
                issue_summary="",
                time_range="24h",
                include_extended_logs=False,
                requested_by="admin",
            )
        with pytest.raises(ValueError):
            support_jobs.enqueue_job(
                issue_category="other",
                issue_summary="",
                time_range="9d",
                include_extended_logs=False,
                requested_by="admin",
            )
        with pytest.raises(ValueError):
            support_jobs.enqueue_job(
                issue_category="other",
                issue_summary="x" * 501,
                time_range="24h",
                include_extended_logs=False,
                requested_by="admin",
            )


def test_get_status_returns_none_for_unknown(isolated_support_root):
    assert support_jobs.get_status("e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2") is None


def test_get_status_refuses_non_uuid(isolated_support_root):
    with pytest.raises(ValueError):
        support_jobs.get_status("../etc/passwd")


def test_list_jobs_returns_newest_first(isolated_support_root):
    import os
    import time

    with patch.object(support_jobs, "_trigger_systemd"):
        ids = [
            support_jobs.enqueue_job(
                issue_category="other",
                issue_summary="",
                time_range="24h",
                include_extended_logs=False,
                requested_by="admin",
            )["id"]
            for _ in range(3)
        ]

    # Force mtimes to a known order so the test doesn't depend on filesystem
    # mtime resolution (some kernels round to whole seconds).
    now = time.time()
    os.utime(support_jobs.job_path(ids[0]), (now - 100, now - 100))
    os.utime(support_jobs.job_path(ids[1]), (now + 100, now + 100))
    os.utime(support_jobs.job_path(ids[2]), (now,       now))

    listed = [s["id"] for s in support_jobs.list_jobs()]
    assert listed[0] == ids[1]
    assert set(listed) == set(ids)


def test_delete_job_removes_all_three_files(isolated_support_root):
    with patch.object(support_jobs, "_trigger_systemd"):
        state = support_jobs.enqueue_job(
            issue_category="other",
            issue_summary="",
            time_range="24h",
            include_extended_logs=False,
            requested_by="admin",
        )
    job_id = state["id"]
    # Pretend the worker produced a bundle.
    support_jobs.bundle_path(job_id).parent.mkdir(parents=True, exist_ok=True)
    support_jobs.bundle_path(job_id).write_bytes(b"fake tar")

    assert support_jobs.delete_job(job_id) is True
    assert not support_jobs.request_path(job_id).exists()
    assert not support_jobs.job_path(job_id).exists()
    assert not support_jobs.bundle_path(job_id).exists()


def test_trigger_systemd_marks_failed_if_sudo_missing(isolated_support_root, monkeypatch):
    """When sudo/systemctl is unavailable (dev box, broken sudoers) we must
    not silently leave the job stuck on 'queued' forever."""
    state = support_jobs.enqueue_job.__wrapped__ if hasattr(support_jobs.enqueue_job, "__wrapped__") else None  # noqa: F841

    with patch("subprocess.run", side_effect=FileNotFoundError("sudo")):
        with patch.object(support_jobs, "_mark_failed") as mark_failed:
            support_jobs._trigger_systemd("e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2")
    mark_failed.assert_called_once()
