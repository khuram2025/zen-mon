"""Update-state collector.

Whatever the customer just tried to upgrade to, plus the trail of recent
attempts. This is the collector that would have made today's first
migration-drift incident a 30-second triage instead of an email chain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import CollectorContext, CollectorResult


VERSION_FILE = Path("/opt/zenplus/.version")
UPDATER_HISTORY = Path("/opt/zenplus/updater/logs/update-history.json")
UPDATER_BACKUPS = Path("/opt/zenplus/updater/backups")

TIMERS_AND_SERVICES = (
    "zenplus-updater.timer",
    "zenplus-updater.service",
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="updates")

    if VERSION_FILE.exists():
        try:
            result.files["updates/version.txt"] = VERSION_FILE.read_bytes()
        except OSError as exc:
            result.warn(f"version: {exc}")
    else:
        result.warn(".version not found")

    if UPDATER_HISTORY.exists():
        try:
            result.files["updates/update-history.json"] = UPDATER_HISTORY.read_bytes()
        except OSError as exc:
            result.warn(f"update-history: {exc}")
    else:
        result.notes.append("update-history.json missing")

    if shutil.which("systemctl"):
        for unit in TIMERS_AND_SERVICES:
            result.files[f"updates/systemctl-{unit}.txt"] = _systemctl_status(unit)

    # Summary of backups directory — names and sizes only, never contents.
    result.files["updates/backups-summary.json"] = _backups_summary().encode("utf-8")

    return result


def _systemctl_status(unit: str) -> bytes:
    try:
        proc = subprocess.run(
            ["systemctl", "status", "--no-pager", "--full", unit],
            capture_output=True, timeout=8,
        )
        return proc.stdout + b"\n" + proc.stderr
    except subprocess.TimeoutExpired:
        return b"[support-bundle: systemctl status timed out]\n"


def _backups_summary() -> str:
    if not UPDATER_BACKUPS.exists():
        return json.dumps({"available": False, "backups": []}, indent=2)
    out = []
    try:
        for entry in sorted(UPDATER_BACKUPS.iterdir()):
            try:
                stat = entry.stat()
            except OSError:
                continue
            out.append({
                "name": entry.name,
                "size_bytes": stat.st_size if entry.is_file() else None,
                "mtime": stat.st_mtime,
                "is_dir": entry.is_dir(),
            })
    except OSError as exc:
        return json.dumps({"available": True, "error": str(exc), "backups": []}, indent=2)
    return json.dumps({"available": True, "backups": out}, indent=2, default=str)
