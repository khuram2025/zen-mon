"""API-side helper for the support-bundle subsystem.

The router uses this module to enqueue a new job and read its status. All the
actual collection happens in a separate unprivileged process (the ``support``
package run by a systemd path-triggered queue), so this code never gathers
diagnostic data; it only commits request/status files on disk.
"""

from __future__ import annotations

import logging
import hashlib
import hmac
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
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
    "server_monitoring",
    "apm",
    "agent_upgrade",
    "appliance_reachability",
    "other",
)

MAX_OUTSTANDING_JOBS = 3
STALE_JOB_SECONDS = 15 * 60
MAX_BUNDLE_BYTES = 50 * 1024 * 1024


class SupportQueueFullError(ValueError):
    pass


class InvalidBundleFileError(ValueError):
    pass


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


def open_bundle_for_download(
    job_id: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
):
    """Open and verify one immutable bundle without a pathname TOCTOU gap.

    The caller streams this already-open descriptor. A path swap after this
    function returns cannot redirect the response to another file.
    """
    path = bundle_path(job_id)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InvalidBundleFileError("bundle cannot be opened safely") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InvalidBundleFileError("bundle is not a regular file")
        if info.st_size <= 0 or info.st_size > MAX_BUNDLE_BYTES:
            raise InvalidBundleFileError("bundle size is outside the allowed range")
        if expected_size and info.st_size != int(expected_size):
            raise InvalidBundleFileError("bundle size does not match job state")

        expected = str(expected_sha256 or "").lower()
        if expected:
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise InvalidBundleFileError("job state has an invalid bundle digest")
            digest = hashlib.sha256()
            for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), expected):
                raise InvalidBundleFileError("bundle digest does not match job state")
            os.lseek(fd, 0, os.SEEK_SET)

        return os.fdopen(fd, "rb"), info.st_size
    except Exception:
        os.close(fd)
        raise


def safe_download_filename(value: object, job_id: str) -> str:
    candidate = str(value or "")
    if re.fullmatch(r"zenplus-support-[A-Za-z0-9._-]+\.tar\.gz", candidate):
        return candidate
    return f"zenplus-support-{job_id}.tar.gz"


def enqueue_job(
    *,
    issue_category: str,
    issue_summary: str,
    time_range: str,
    include_extended_logs: bool,
    requested_by: str,
) -> dict[str, Any]:
    """Validate and atomically commit a request behind its initial state.

    Returns the initial state dict (same shape as ``get_status``) the router
    serializes back to the dashboard with a 202.
    """
    if issue_category not in VALID_ISSUE_CATEGORIES:
        raise ValueError(f"issue_category must be one of {VALID_ISSUE_CATEGORIES}")
    if time_range not in VALID_TIME_RANGES:
        raise ValueError(f"time_range must be one of {VALID_TIME_RANGES}")
    if len(issue_summary) > 500:
        raise ValueError("issue_summary exceeds 500 chars")

    # Users sometimes paste credentials into an incident description despite
    # the UI warning. Scrub it before it reaches either the persistent request
    # file or the status API, then run the same redactor over final metadata in
    # the worker as defense in depth.
    from support.redaction import Redactor
    issue_summary = Redactor().apply(issue_summary)

    SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    with _enqueue_lock():
        outstanding = sum(
            1 for state in list_jobs(limit=MAX_OUTSTANDING_JOBS + 25)
            if state.get("status") in (STATUS_QUEUED, STATUS_RUNNING)
        )
        if outstanding >= MAX_OUTSTANDING_JOBS:
            raise SupportQueueFullError(
                f"support bundle queue is full ({MAX_OUTSTANDING_JOBS} outstanding jobs)"
            )

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
            "skipped_files": [],
            "truncated_files": [],
            "collector_failures": [],
            "collector_warnings": [],
            "bundle_schema_version": None,
            "worker_version": None,
        }
        # The systemd path unit watches requests/. State must exist first or a
        # fast worker can finish and then be overwritten back to "queued".
        _write_json(job_path(job_id), initial_state, mode=0o640)
        try:
            _write_json(request_path(job_id), request_body, mode=0o640)
        except Exception as exc:
            _mark_failed(job_id, f"cannot enqueue support request: {exc}")
            raise
    return initial_state


def get_status(job_id: str) -> dict[str, Any] | None:
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    path = job_path(job_id)
    if not path.exists():
        return None
    import json
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return _reconcile_stale_state(path, state)
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
            state = json.loads(p.read_text(encoding="utf-8"))
            out.append(_reconcile_stale_state(p, state))
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


def _reconcile_stale_state(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") not in (STATUS_QUEUED, STATUS_RUNNING):
        return state
    try:
        stale = time.time() - path.stat().st_mtime > STALE_JOB_SECONDS
    except OSError:
        return state
    if not stale:
        return state
    state.update({
        "status": STATUS_FAILED,
        "phase": "failed",
        "completed_at": _now_iso(),
        "error": "support bundle worker stopped updating before completion",
    })
    _write_json(path, state, mode=0o640)
    return state


@contextmanager
def _enqueue_lock():
    lock_path = SUPPORT_ROOT / ".enqueue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            # Unit-test portability; production appliances are Linux and use
            # the kernel lock above across all API worker processes.
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except ImportError:
            pass
        os.close(fd)
