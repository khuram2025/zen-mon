"""Storage collector — disks, mounts, LVM, and where ZenPlus's data lives.

Run from the same toolbelt a support engineer would use first: ``df``,
``lsblk``, ``findmnt``, plus LVM commands when present (the appliance image
uses LVM for the ``/data`` volume so ClickHouse storage can grow).
"""

from __future__ import annotations

import shutil
import subprocess

from . import CollectorContext, CollectorResult


SIMPLE_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("storage/df.txt", ["df", "-h"]),
    ("storage/df-inodes.txt", ["df", "-ih"]),
    ("storage/lsblk.txt", ["lsblk", "-f"]),
    ("storage/findmnt.txt", ["findmnt"]),
    ("storage/pvs.txt", ["pvs", "--reportformat", "json"]),
    ("storage/vgs.txt", ["vgs", "--reportformat", "json"]),
    ("storage/lvs.txt", ["lvs", "--reportformat", "json"]),
)

DU_TARGETS = (
    "/data",
    "/data/clickhouse",
    "/opt/zenplus",
    "/opt/zenplus/updater/backups",
    "/opt/zenplus/support/bundles",
    "/var/log",
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="storage")

    for arcname, cmd in SIMPLE_COMMANDS:
        if not shutil.which(cmd[0]):
            result.notes.append(f"{cmd[0]} not in PATH; skipping {arcname}")
            continue
        result.files[arcname] = _run(cmd)

    # Per-directory size summary so a ballooning ClickHouse volume or
    # forgotten update backup is obvious.
    du_lines: list[bytes] = []
    for target in DU_TARGETS:
        proc = _capture_proc(["du", "-sh", target])
        du_lines.append(proc)
    result.files["storage/du-summary.txt"] = b"\n".join(du_lines)

    if not result.files:
        result.fail("no storage commands ran")
    return result


def _run(cmd: list[str]) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        out = proc.stdout
        if proc.returncode != 0:
            out += b"\n[support-bundle: exit=" + str(proc.returncode).encode() + b"]\n" + proc.stderr
        return out
    except subprocess.TimeoutExpired:
        return f"[support-bundle: {' '.join(cmd)} timed out]\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"[support-bundle: {' '.join(cmd)} failed: {exc!r}]\n".encode("utf-8")


def _capture_proc(cmd: list[str]) -> bytes:
    if not shutil.which(cmd[0]):
        return f"# {cmd[0]} not in PATH\n".encode("utf-8")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10)
        return proc.stdout if proc.returncode == 0 else (
            b"# " + " ".join(cmd).encode() + b" exit=" + str(proc.returncode).encode() + b" stderr=" + proc.stderr + b"\n"
        )
    except subprocess.TimeoutExpired:
        return f"# {' '.join(cmd)} timeout\n".encode("utf-8")
