"""Logs collector — journalctl + app logs.

Bounded by the request's time-range (1h / 6h / 24h / 7d). Per-file size cap
is enforced by the archiver, so this collector returns full buffers and lets
the archiver truncate consistently.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import CollectorContext, CollectorResult
from ..archiver import DEFAULT_PER_FILE_CAP, EXTENDED_PER_FILE_CAP


TRACKED_UNITS = (
    "zenplus-api",
    "zenplus-poller",
    "zenplus-updater.service",
    "zenplus-wait-deps",
    "zenplus-netflow-collector.service",
    "zenplus-support-cleanup.service",
    "nginx",
    "postgresql",
    "redis-server",
    "docker",
)

APP_LOG_PATHS = (
    ("logs/updater-update.log", "/opt/zenplus/updater/logs/update.log"),
    ("logs/updater-update-history.json", "/opt/zenplus/updater/logs/update-history.json"),
    ("logs/nginx-error.log", "/var/log/nginx/error.log"),
    ("logs/nginx-access.log", "/var/log/nginx/access.log"),
)

EXTENDED_APP_LOG_PATHS = (
    ("logs/updater-update.log.1", "/opt/zenplus/updater/logs/update.log.1"),
    ("logs/nginx-error.log.1", "/var/log/nginx/error.log.1"),
    ("logs/nginx-access.log.1", "/var/log/nginx/access.log.1"),
)

SOURCE_TRUNCATION_MARKER = b"[support-bundle: source truncated; newest bytes retained]\n"


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="logs")

    if not shutil.which("journalctl"):
        result.fail("journalctl not available — skipping journals")
    else:
        max_bytes = ctx_max_bytes(ctx)
        max_lines = 200_000 if ctx.include_extended_logs else 50_000
        for unit in TRACKED_UNITS:
            arc = f"logs/journal-{unit}.log"
            result.files[arc] = _journalctl(
                unit, ctx.since_arg(), max_bytes=max_bytes, max_lines=max_lines,
            )

        # Kernel: warnings and above only, to keep size bounded.
        result.files["logs/journal-kernel.log"] = _journalctl_kernel(
            ctx.since_arg(), max_bytes=max_bytes, max_lines=max_lines,
        )

    paths = APP_LOG_PATHS + (EXTENDED_APP_LOG_PATHS if ctx.include_extended_logs else ())
    for arcname, src in paths:
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

    This matches the archive's selected per-file cap. Source truncation keeps
    the newest bytes and adds a marker at the beginning; the old two-stage
    25-MiB-read/5-MiB-archive behavior kept an old middle slice and discarded
    the newest incident evidence.
    """
    return EXTENDED_PER_FILE_CAP if ctx.include_extended_logs else DEFAULT_PER_FILE_CAP


def _journalctl(unit: str, since: str, *, max_bytes: int, max_lines: int) -> bytes:
    cmd = [
        "journalctl", "-u", unit, "--since", since, "--lines", str(max_lines),
        "--no-pager", "-o", "short-iso",
    ]
    return _run(cmd, timeout=30, max_bytes=max_bytes)


def _journalctl_kernel(since: str, *, max_bytes: int, max_lines: int) -> bytes:
    cmd = [
        "journalctl", "-k", "--since", since, "--lines", str(max_lines),
        "--no-pager", "-p", "warning", "-o", "short-iso",
    ]
    return _run(cmd, timeout=30, max_bytes=max_bytes)


def _run(cmd: list[str], *, timeout: int, max_bytes: int) -> bytes:
    # Write command output to private temporary files instead of
    # ``capture_output=True``. A noisy seven-day journal can be gigabytes; the
    # worker must not hold it all in RAM just to keep the last few MiB.
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                return b"[support-bundle: journalctl timed out]\n"

            body = _tail_file(stdout, max_bytes=max_bytes)
            if proc.returncode != 0:
                error = _tail_file(stderr, max_bytes=min(max_bytes, 256 * 1024))
                body += (
                    b"\n[support-bundle: command exited "
                    + str(proc.returncode).encode()
                    + b"]\n"
                )
                if error:
                    body += b"[stderr]\n" + error
            return _keep_newest(body, max_bytes=max_bytes)
    except FileNotFoundError:
        return b"[support-bundle: journalctl not found]\n"
    except OSError as exc:
        return f"[support-bundle: journalctl failed: {exc}]\n".encode("utf-8", errors="replace")


def _tail_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Return the last ``max_bytes`` of a file, or the whole file if smaller."""
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_bytes()
    with path.open("rb") as f:
        return _tail_file(f, max_bytes=max_bytes)


def _tail_file(stream, *, max_bytes: int) -> bytes:
    stream.seek(0, 2)
    size = stream.tell()
    if size <= max_bytes:
        stream.seek(0)
        return stream.read()
    keep = max(0, max_bytes - len(SOURCE_TRUNCATION_MARKER))
    if keep == 0:
        return SOURCE_TRUNCATION_MARKER[:max_bytes]
    stream.seek(size - keep)
    return SOURCE_TRUNCATION_MARKER[: max_bytes - keep] + stream.read(keep)


def _keep_newest(body: bytes, *, max_bytes: int) -> bytes:
    if len(body) <= max_bytes:
        return body
    keep = max(0, max_bytes - len(SOURCE_TRUNCATION_MARKER))
    if keep == 0:
        return SOURCE_TRUNCATION_MARKER[:max_bytes]
    return SOURCE_TRUNCATION_MARKER[: max_bytes - keep] + body[-keep:]
