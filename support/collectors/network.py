"""Network state collector — listeners, routes, addresses, DNS, firewall.

Captures appliance-side network configuration only. No connection-tracking
table dumps, no firewall *rules* — just the high-level status that matters
when a customer reports the dashboard or zentryc.com is unreachable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import CollectorContext, CollectorResult


COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("network/ss-listen.txt", ["ss", "-ltnp"]),
    ("network/ip-route.txt", ["ip", "route"]),
    ("network/ip-addr.txt", ["ip", "-br", "addr"]),
    ("network/ip-link.txt", ["ip", "-br", "link"]),
    ("network/ufw-status.txt", ["ufw", "status", "verbose"]),
    ("network/iptables-policy.txt", ["iptables", "-L", "-n", "--policy"]),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="network")

    for arcname, cmd in COMMANDS:
        if not shutil.which(cmd[0]):
            result.notes.append(f"{cmd[0]} not in PATH; skipping {arcname}")
            continue
        result.files[arcname] = _run(cmd)

    # /etc/resolv.conf is small and the only DNS info we want.
    resolv = Path("/etc/resolv.conf")
    if resolv.exists():
        try:
            result.files["network/resolv.conf"] = resolv.read_bytes()
        except OSError as exc:
            result.warn(f"resolv.conf: {exc}")

    if not result.files:
        result.fail("no network commands ran")
    return result


def _run(cmd: list[str]) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10)
        out = proc.stdout
        if proc.returncode != 0:
            out += b"\n[support-bundle: exit=" + str(proc.returncode).encode() + b"]\n" + proc.stderr
        return out
    except subprocess.TimeoutExpired:
        return f"[support-bundle: {' '.join(cmd)} timed out]\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return f"[support-bundle: {' '.join(cmd)} failed: {exc!r}]\n".encode("utf-8")
