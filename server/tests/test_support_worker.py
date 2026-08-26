from __future__ import annotations

import hashlib
import json
import sys
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import support.__main__ as worker  # noqa: E402
from support.collectors import CollectorResult  # noqa: E402


JOB_ID = "e4f1c1b0-3b1a-4c2d-9e10-c7f8a9b0c1d2"


def _configure_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker.js, "SUPPORT_ROOT", tmp_path)
    monkeypatch.setattr(worker.js, "REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(worker.js, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker.js, "BUNDLES_DIR", tmp_path / "bundles")


def test_worker_builds_verified_archive_and_redacts_all_metadata(monkeypatch, tmp_path):
    _configure_root(monkeypatch, tmp_path)
    for directory in (worker.js.REQUESTS_DIR, worker.js.JOBS_DIR, worker.js.BUNDLES_DIR):
        directory.mkdir(parents=True)

    request = {
        # A forged JSON ID must not override the canonical pathname/CLI ID.
        "job_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "issue_category": "server_monitoring",
        "issue_summary": "password is issue-secret",
        "time_range": "24h",
        "include_extended_logs": False,
        "requested_by": "admin",
        "created_at": "2026-08-26T00:00:00+00:00",
    }
    worker.js.request_path(JOB_ID).write_text(json.dumps(request), encoding="utf-8")
    worker.js.write_atomic(worker.js.job_path(JOB_ID), worker.js.initial_job_state(JOB_ID, "admin"))

    def diagnostic(_ctx):
        result = CollectorResult(section="diagnostic")
        result.files["diagnostic/config.txt"] = (
            b'CUSTOM_VENDOR_SECRET="collector-secret"\n'
            b'Cookie: session=cookie-secret\n'
        )
        result.warn("controller was https://token-only@example.test/api")
        return result

    monkeypatch.setattr(worker, "all_collectors", lambda: [("diagnostic", diagnostic)])
    monkeypatch.setattr(
        worker,
        "_identity",
        lambda: ("abc12345\r\nunsafe-header", "appliance", "1.20.3"),
    )

    assert worker.main(["--job", JOB_ID]) == 0

    bundle = worker.js.bundle_path(JOB_ID)
    state = json.loads(worker.js.job_path(JOB_ID).read_text(encoding="utf-8"))
    assert state["status"] == "ready"
    assert state["size_bytes"] == bundle.stat().st_size
    assert state["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert "\r" not in state["filename"] and "\n" not in state["filename"]

    with tarfile.open(bundle, "r:gz") as archive:
        names = archive.getnames()
        bodies = {
            name: archive.extractfile(name).read()
            for name in names
            if archive.getmember(name).isfile()
        }

    assert {"manifest.json", "README.txt", "redaction-report.json", "checksums.sha256"} <= set(names)
    combined = b"\n".join(bodies.values())
    for secret in (b"issue-secret", b"collector-secret", b"cookie-secret", b"token-only"):
        assert secret not in combined

    manifest = json.loads(bodies["manifest.json"])
    assert manifest["request"]["job_id"] == JOB_ID
    assert manifest["sections"]["diagnostic"]["status"] == "warning"

    checksum_lines = bodies["checksums.sha256"].decode("utf-8").splitlines()
    for line in checksum_lines:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(bodies[name]).hexdigest() == digest


def test_enqueue_redacts_summary_before_persisting(monkeypatch, tmp_path):
    from app.services import support_jobs

    monkeypatch.setattr(support_jobs, "SUPPORT_ROOT", tmp_path)
    monkeypatch.setattr(support_jobs, "REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(support_jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(support_jobs, "BUNDLES_DIR", tmp_path / "bundles")

    state = support_jobs.enqueue_job(
        issue_category="other",
        issue_summary="api key is summary-secret",
        time_range="24h",
        include_extended_logs=False,
        requested_by="admin",
    )
    persisted = support_jobs.request_path(state["id"]).read_text(encoding="utf-8")
    assert "summary-secret" not in persisted
    assert "[REDACTED:prose_secret]" in persisted
