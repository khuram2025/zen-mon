"""Logs collector — journalctl + app logs.

Bounded by the request's time-range (1h / 6h / 24h / 7d). Per-file size cap
is enforced by the archiver, so this collector returns full buffers and lets
the archiver truncate consistently.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import CollectorContext, CollectorResult


TRACKED_UNITS = (
    "zenplus-api",
    "zenplus-poller",
    "zenplus-updater.service",
    "zenplus-wait-deps",
    "nginx",
    "postgresql",
    "redis-server",
    "docker",
)

APP_LOG_PATHS = (
    ("logs/updater-update.log", "/opt/zenplus/updater/logs/update.log"),
    ("logs/updater-update-history.json", "/opt/zenplus/updater/logs/update-history.json"),
    ("logs/nginx-error.log", "/var/log/nginx/error.log"),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="logs")

    if not shutil.which("journalctl"):
        result.fail("journalctl not available — skipping journals")
    else:
        for unit in TRACKED_UNITS:
            arc = f"logs/journal-{unit}.log"
            result.files[arc] = _journalctl(unit, ctx.since_arg())

        # Kernel: warnings and above only, to keep size bounded.
        result.files["logs/journal-kernel.log"] = _journalctl_kernel(ctx.since_arg())

    for arcname, src in APP_LOG_PATHS:
        path = Path(src)
        if not path.exists():
            result.notes.append(f"missing app log: {src}")
            continue
        try:
            result.files[arcname] = _tail_bytes(path, max_bytes=ctx_max_bytes(ctx))
        except OSError as exc:
            result.warn(f"cannot read {src}: {exc}")

    if not result.files:
        result.fail("no log files collected")
    return result


def ctx_max_bytes(ctx: CollectorContext) -> int:
    """Per-file read cap on raw inputs.

    Extended-logs OFF: read up to ~5 MiB so the archiver doesn't have to
    truncate every file. Extended-logs ON: read up to ~25 MiB so the archiver
    truncates rather than us, and the truncation marker lands in the bundle.
    """
    return 25 * 1024 * 1024 if ctx.include_extended_logs else 5 * 1024 * 1024


def _journalctl(unit: str, since: str) -> bytes:
    cmd = ["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "short-iso"]
    return _run(cmd, timeout=30)


def _journalctl_kernel(since: str) -> bytes:
    cmd = ["journalctl", "-k", "--since", since, "--no-pager", "-p", "warning", "-o", "short-iso"]
    return _run(cmd, timeout=30)


def _run(cmd: list[str], *, timeout: int) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        body = proc.stdout
        if proc.returncode != 0:
            body += b"\n[support-bundle: command exited " + str(proc.returncode).encode() + b"]\n"
            if proc.stderr:
                body += b"[stderr]\n" + proc.stderr
        return body
    except subprocess.TimeoutExpired:
        return b"[support-bundle: journalctl timed out]\n"
    except FileNotFoundError:
        return b"[support-bundle: journalctl not found]\n"


def _tail_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Return the last ``max_bytes`` of a file, or the whole file if smaller."""
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_bytes()
    with path.open("rb") as f:
        f.seek(size - max_bytes)
        return f.read()
