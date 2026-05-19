"""Inventory collector — system identity, services, versions.

Reuses ``updater.inventory.collect_inventory`` for the core fields so the
appliance check-in code and the support bundle agree on what "this appliance
is" looks like.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path

from . import CollectorContext, CollectorResult


TRACKED_SERVICES = (
    "zenplus-api",
    "zenplus-poller",
    "zenplus-updater.service",
    "zenplus-updater.timer",
    "zenplus-wait-deps",
    "nginx",
    "postgresql",
    "redis-server",
    "docker",
)

VERSION_PROBES = (
    ("python", ["python3", "--version"]),
    ("node", ["node", "--version"]),
    ("npm", ["npm", "--version"]),
    ("go", ["go", "version"]),
    ("docker", ["docker", "--version"]),
    ("postgres", ["psql", "--version"]),
    ("clickhouse_client", ["clickhouse-client", "--version"]),
    ("nginx", ["nginx", "-v"]),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="inventory")

    # 1. Core system snapshot — reuse the updater's inventory module if it ships
    #    alongside us on the appliance. Fall back to a local version if not.
    system = _system_snapshot(ctx, result)
    result.files["inventory/system.json"] = _dump(system)

    # 2. systemd service state for every tracked unit.
    services = {unit: _systemctl_is_active(unit) for unit in TRACKED_SERVICES}
    result.files["inventory/services.json"] = _dump({"services": services})
    if any(state not in ("active", "running") for state in services.values()):
        result.warn("one or more tracked services not active")

    # 3. Docker containers (ClickHouse runs in one).
    result.files["inventory/docker.json"] = _dump(_docker_ps(result))

    # 4. Versions of installed binaries.
    versions = {}
    for name, cmd in VERSION_PROBES:
        versions[name] = _capture(cmd, timeout=5)
    result.files["inventory/versions.json"] = _dump({"versions": versions})

    # 5. Resource limits and quick CPU/memory snapshot.
    result.files["inventory/limits.json"] = _dump(_limits())

    return result


def _system_snapshot(ctx: CollectorContext, result: CollectorResult) -> dict:
    try:
        import sys
        # Make /opt/zenplus importable so updater.inventory works on the
        # appliance. When running in dev/tests this path likely doesn't exist;
        # we fall back below.
        if str(ctx.zenplus_root) not in sys.path:
            sys.path.insert(0, str(ctx.zenplus_root))
        from updater import inventory as updater_inventory  # type: ignore
        return updater_inventory.collect_inventory()
    except Exception as exc:  # noqa: BLE001
        result.warn(f"updater.inventory unavailable: {exc.__class__.__name__}")
        return _fallback_snapshot()


def _fallback_snapshot() -> dict:
    return {
        "hostname": platform.node(),
        "arch": platform.machine(),
        "os_version": platform.platform(),
        "kernel": platform.release(),
        "python_version": platform.python_version(),
    }


def _systemctl_is_active(unit: str) -> str:
    return _capture(["systemctl", "is-active", unit], timeout=5).get("stdout", "unknown") or "unknown"


def _docker_ps(result: CollectorResult) -> dict:
    if not shutil.which("docker"):
        result.warn("docker not found in PATH")
        return {"available": False}
    captured = _capture(
        ["docker", "ps", "-a", "--format",
         "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.RunningFor}}"],
        timeout=8,
    )
    rows = []
    for line in (captured.get("stdout") or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({"name": parts[0], "image": parts[1], "status": parts[2], "running_for": parts[3]})
    return {"available": True, "containers": rows, "raw_error": captured.get("stderr", "")}


def _limits() -> dict:
    import resource
    soft_nofile, hard_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft_nproc, hard_nproc = resource.getrlimit(resource.RLIMIT_NPROC)
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines()[:10]:
            k, _, v = line.partition(":")
            mem[k.strip()] = v.strip()
    except OSError:
        pass
    return {
        "rlimit_nofile": {"soft": soft_nofile, "hard": hard_nofile},
        "rlimit_nproc": {"soft": soft_nproc, "hard": hard_nproc},
        "meminfo_head": mem,
    }


def _capture(cmd: list[str], *, timeout: int) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "command": " ".join(cmd),
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except FileNotFoundError:
        return {"command": " ".join(cmd), "exit_code": -1, "stdout": "", "stderr": "not found"}
    except subprocess.TimeoutExpired:
        return {"command": " ".join(cmd), "exit_code": -1, "stdout": "", "stderr": "timeout"}


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
