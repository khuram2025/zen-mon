"""Persistent job-state JSON files under /opt/zenplus/support/.

The worker writes status updates to ``jobs/<uuid>.json`` as it progresses;
the API process reads the same file to report status to the dashboard. We
write to a temp file and rename so concurrent reads never see a half-written
file.

We keep this small and dependency-free so the API process (FastAPI/SQLAlchemy
stack) and the worker (root, no app imports) can both use it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORT_ROOT = Path("/opt/zenplus/support")
REQUESTS_DIR = SUPPORT_ROOT / "requests"
JOBS_DIR = SUPPORT_ROOT / "jobs"
BUNDLES_DIR = SUPPORT_ROOT / "bundles"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Status values the API and worker must agree on.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

PHASES = ("queued", "inventory", "health", "logs", "package", "done")


def is_valid_job_id(job_id: str) -> bool:
    """Strictly UUID-format only.

    This is the only string allowed to flow into a file path or systemd
    instance name, so the check needs to refuse anything that could escape.
    """
    return bool(UUID_RE.match(job_id))


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


def write_atomic(path: Path, data: dict[str, Any], *, mode: int = 0o640) -> None:
    """Write JSON atomically so partial writes are never visible.

    When this runs as root (the systemd worker), we group-chown to ``zenplus``
    before the rename so the API process — which runs as the zenplus user —
    can read its own job status back. Without this, ``os.replace`` would land
    a ``root:root`` 0640 file the API can't open, and the dashboard would
    spin forever even though the bundle finished cleanly.

    Falls back gracefully if the lookup or chown fails (e.g. running as a
    non-root unit test on a box without the zenplus group).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        os.chmod(tmp, mode)
        if os.geteuid() == 0:
            try:
                import grp
                gid = grp.getgrnam("zenplus").gr_gid
                os.chown(tmp, 0, gid)
            except (KeyError, OSError):
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initial_job_state(job_id: str, requested_by: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "status": STATUS_QUEUED,
        "phase": "queued",
        "created_at": now_iso(),
        "completed_at": None,
        "size_bytes": 0,
        "sha256": None,
        "filename": None,
        "requested_by": requested_by,
        "error": "",
    }
