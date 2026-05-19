"""API-side helper for the support-bundle subsystem.

The router uses this module to enqueue a new job and read its status. All the
actual collection happens in a separate root process (``support`` package run
under a systemd template unit), so this code never touches diagnostic data —
it just orchestrates request/status files on disk and triggers systemd.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("zenplus.support")

# We share the same on-disk layout the worker uses. Importing the worker
# module here would be cleaner, but the API process shouldn't grow a hard
# dependency on the worker — these handful of constants are the contract.
SUPPORT_ROOT = Path("/opt/zenplus/support")
REQUESTS_DIR = SUPPORT_ROOT / "requests"
JOBS_DIR = SUPPORT_ROOT / "jobs"
BUNDLES_DIR = SUPPORT_ROOT / "bundles"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

VALID_TIME_RANGES = ("1h", "6h", "24h", "7d")
VALID_ISSUE_CATEGORIES = (
    "all",
    "update_migration",
    "device_management",
    "snmp_discovery",
    "windows_credentials",
    "alerts_notifications",
    "performance_storage",
    "ui_api_error",
    "other",
)


def is_valid_job_id(job_id: str) -> bool:
    return bool(UUID_RE.match(job_id))


def new_job_id() -> str:
    return str(uuid.uuid4())


def request_path(job_id: str) -> Path:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return REQUESTS_DIR / f"{job_id}.json"


def job_path(job_id: str) -> Path:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return JOBS_DIR / f"{job_id}.json"


def bundle_path(job_id: str) -> Path:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return BUNDLES_DIR / f"{job_id}.tar.gz"


def enqueue_job(
    *,
    issue_category: str,
    issue_summary: str,
    time_range: str,
    include_extended_logs: bool,
    requested_by: str,
) -> dict[str, Any]:
    """Validate, write the request file, persist initial status, kick systemd.

    Returns the initial state dict (same shape as ``get_status``) the router
    serializes back to the dashboard with a 202.
    """
    if issue_category not in VALID_ISSUE_CATEGORIES:
        raise ValueError(f"issue_category must be one of {VALID_ISSUE_CATEGORIES}")
    if time_range not in VALID_TIME_RANGES:
        raise ValueError(f"time_range must be one of {VALID_TIME_RANGES}")
    if len(issue_summary) > 500:
        raise ValueError("issue_summary exceeds 500 chars")

    SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    job_id = new_job_id()
    now = _now_iso()
    request_body = {
        "job_id": job_id,
        "issue_category": issue_category,
        "issue_summary": issue_summary,
        "time_range": time_range,
        "include_extended_logs": bool(include_extended_logs),
        "requested_by": requested_by,
        "created_at": now,
    }
    _write_json(request_path(job_id), request_body, mode=0o640)

    initial_state = {
        "id": job_id,
        "status": STATUS_QUEUED,
        "phase": "queued",
        "created_at": now,
        "completed_at": None,
        "size_bytes": 0,
        "sha256": None,
        "filename": None,
        "requested_by": requested_by,
        "error": "",
        "request": request_body,
    }
    _write_json(job_path(job_id), initial_state, mode=0o640)

    _trigger_systemd(job_id)
    return initial_state


def get_status(job_id: str) -> dict[str, Any] | None:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    path = job_path(job_id)
    if not path.exists():
        return None
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        # Worker wrote the file with bad group ownership — surface this as a
        # diagnosable error in the dashboard instead of a generic 500. The
        # underlying fix lives in support/job_state.py:write_atomic.
        logger.error("cannot read job state %s: %s", path, exc)
        return {
            "id": job_id,
            "status": STATUS_FAILED,
            "phase": "failed",
            "created_at": None,
            "completed_at": _now_iso(),
            "size_bytes": 0,
            "sha256": None,
            "filename": None,
            "requested_by": None,
            "error": (
                "API process cannot read job state file (PermissionError). "
                "This usually means the appliance was upgraded across the "
                "job_state ownership fix without rerunning setup-support.sh. "
                "Run: sudo bash /opt/zenplus/scripts/setup-support.sh"
            ),
        }


def list_jobs(*, limit: int = 25) -> list[dict[str, Any]]:
    if not JOBS_DIR.exists():
        return []
    import json
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def delete_job(job_id: str) -> bool:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    removed = False
    for path in (request_path(job_id), job_path(job_id), bundle_path(job_id)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("could not delete %s: %s", path, exc)
    return removed


def _trigger_systemd(job_id: str) -> None:
    """Spawn the systemd template instance for this job.

    Uses ``--no-block`` so the API returns immediately; the worker picks the
    request file up out-of-band. If the systemd call fails (e.g. no sudo
    grant during dev), we mark the job failed so the UI doesn't sit on
    "queued" forever.
    """
    unit = f"zenplus-support-bundle@{job_id}.service"
    cmd = ["sudo", "-n", "/bin/systemctl", "--no-block", "start", unit]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            _mark_failed(job_id, f"systemctl start failed: {proc.stderr.strip() or proc.stdout.strip()}")
    except subprocess.TimeoutExpired:
        _mark_failed(job_id, "systemctl start timed out")
    except FileNotFoundError:
        _mark_failed(job_id, "sudo/systemctl not available")


def _mark_failed(job_id: str, reason: str) -> None:
    try:
        path = job_path(job_id)
        if not path.exists():
            return
        import json
        state = json.loads(path.read_text(encoding="utf-8"))
        state["status"] = STATUS_FAILED
        state["phase"] = "failed"
        state["error"] = reason[:2000]
        state["completed_at"] = _now_iso()
        _write_json(path, state, mode=0o640)
    except Exception:
        logger.exception("could not mark job %s as failed", job_id)


def _write_json(path: Path, data: dict[str, Any], *, mode: int) -> None:
    import json
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
