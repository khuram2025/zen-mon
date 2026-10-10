#!/usr/bin/python3
"""Snapshot (and on rollback, restore) the systemd units an OTA package installs.

Manifest ``install_systemd`` steps and setup-storage.sh write unit files to
/etc/systemd/system, which the code backup does not cover. After a rollback to
an older version those units could point at a binary or module the restored
version does not have (e.g. 1.23.9 appliances never installed the NetFlow
collector), and the rollback ``start_services`` step would start them into a
restart loop or a failed unit.

Default mode (manifest hook right after ota-preflight.py, BEFORE
stop_services, so "active" is the real running state): record, for every unit
this package installs, whether it existed, its enablement, whether it was
active, and a copy of the file.

``--restore`` (called by ota-reconcile-storage-rollback.sh after the code is
restored): put every recorded unit back exactly as it was: restore the old
file and enablement, or disable/stop/remove a unit that did not exist before,
then daemon-reload and reset-failed. Without a snapshot (an update that failed
before it was taken) units whose executable or module is missing from the
restored tree are disabled and removed instead.

Runs as root from the signed payload; compatible with the 1.23.9 executor
(script executed directly, cwd /opt/zenplus, ZENPLUS_DIR set).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

ZENPLUS_DIR = os.environ.get("ZENPLUS_DIR", "/opt/zenplus")
SYSTEMD_DIR = os.environ.get("ZENPLUS_SYSTEMD_DIR", "/etc/systemd/system")
STATE_DIR = os.environ.get("ZENPLUS_OTA_STATE_DIR", "/var/lib/zenplus-ota/units")
PAYLOAD_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRA_UNITS = ("zenplus-storage-watchdog.service",)   # installed by setup-storage.sh
UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]+\.(service|timer|path|socket)$")


def log(msg: str) -> None:
    print(f"[ota-units] {msg}", flush=True)


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)


def package_version() -> str:
    try:
        with open(os.path.join(PAYLOAD_DIR, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "")
    except (OSError, ValueError):
        return ""


def package_units() -> list[str]:
    """Unit names this package installs (manifest first, payload scan as fallback)."""
    names: list[str] = []
    try:
        with open(os.path.join(PAYLOAD_DIR, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        names = [os.path.basename(s.get("source", "")) for s in manifest.get("steps", [])
                 if s.get("type") == "install_systemd"]
    except (OSError, ValueError):
        for sub in ("code/poller/systemd", "code/scripts/systemd"):
            d = os.path.join(PAYLOAD_DIR, sub)
            if os.path.isdir(d):
                names += sorted(os.listdir(d))
    names += list(EXTRA_UNITS)
    seen, out = set(), []
    for n in names:
        if UNIT_RE.match(n) and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def snapshot() -> int:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    files_dir = os.path.join(STATE_DIR, "files")
    shutil.rmtree(files_dir, ignore_errors=True)
    os.makedirs(files_dir, mode=0o700)
    record = {}
    for unit in package_units():
        path = os.path.join(SYSTEMD_DIR, unit)
        exists = os.path.isfile(path)
        record[unit] = {
            "existed": exists,
            "enabled": exists and systemctl("is-enabled", "--quiet", unit).returncode == 0,
            "active": exists and systemctl("is-active", "--quiet", unit).returncode == 0,
        }
        if exists:
            shutil.copy2(path, os.path.join(files_dir, unit))
    tmp = os.path.join(STATE_DIR, ".snapshot.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "package_version": package_version(), "units": record}, f, indent=1)
    os.replace(tmp, os.path.join(STATE_DIR, "snapshot.json"))
    log(f"recorded {len(record)} unit(s): " + ", ".join(
        f"{u}={'present' if r['existed'] else 'absent'}" for u, r in record.items()))
    return 0


def _exec_target(path: str) -> tuple[str | None, str | None]:
    """(program, python module) from a unit's first ExecStart line."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None, None
    m = re.search(r"^ExecStart=(.*)$", text, re.M)
    if not m:
        return None, None
    argv = m.group(1).strip().lstrip("-@:+!").split()
    if not argv:
        return None, None
    module = argv[argv.index("-m") + 1] if "-m" in argv and argv.index("-m") + 1 < len(argv) else None
    return argv[0], module


def _runnable(path: str) -> bool:
    program, module = _exec_target(path)
    # Judge against the restored tree (ZENPLUS_DIR), which units name as /opt/zenplus.
    if program and (program == "/opt/zenplus" or program.startswith("/opt/zenplus/")):
        if not os.path.exists(ZENPLUS_DIR + program[len("/opt/zenplus"):]):
            return False
    if module:
        rel = module.replace(".", "/")
        roots = [os.path.join(ZENPLUS_DIR, "server"), ZENPLUS_DIR]
        if not any(os.path.exists(os.path.join(r, rel + ".py")) or os.path.isdir(os.path.join(r, rel))
                   for r in roots):
            return False
    return True


def _remove(unit: str) -> None:
    systemctl("disable", "--now", unit)
    try:
        os.unlink(os.path.join(SYSTEMD_DIR, unit))
    except FileNotFoundError:
        pass
    log(f"{unit}: removed (not installed before this update / not runnable on the restored version)")


def restore() -> int:
    snap_path = os.path.join(STATE_DIR, "snapshot.json")
    changed: list[str] = []
    rc = 0
    try:
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        units = snap.get("units", {})
        # A snapshot left by another update (this one failed before taking
        # its own) describes the wrong baseline: do not apply it.
        if snap.get("package_version") != package_version():
            log(f"snapshot is for {snap.get('package_version')!r}, not this package; ignored")
            units = None
    except (OSError, ValueError):
        units = None
    if units is not None:
        for unit, rec in units.items():
            if not UNIT_RE.match(unit):
                continue
            dest = os.path.join(SYSTEMD_DIR, unit)
            if not rec.get("existed"):
                if os.path.exists(dest):
                    _remove(unit)
                    changed.append(unit)
                continue
            saved = os.path.join(STATE_DIR, "files", unit)
            try:
                with open(saved, "rb") as a:
                    old = a.read()
                cur = open(dest, "rb").read() if os.path.exists(dest) else None
                if cur != old:
                    tmp = dest + ".zenplus-restore"
                    with open(tmp, "wb") as w:
                        w.write(old)
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, dest)
                    changed.append(unit)
                    log(f"{unit}: previous unit file restored")
            except OSError as exc:
                log(f"{unit}: could not restore ({exc})")
                rc = 1
            want = "enable" if rec.get("enabled") else "disable"
            systemctl(want, unit)
    else:
        log("no unit snapshot; removing units that cannot run on the restored version")
        for unit in package_units():
            dest = os.path.join(SYSTEMD_DIR, unit)
            if unit.endswith(".service") and os.path.exists(dest) and not _runnable(dest):
                _remove(unit)
                changed.append(unit)
        # A timer whose service was removed would only fail.
        for unit in package_units():
            dest = os.path.join(SYSTEMD_DIR, unit)
            if unit.endswith(".timer") and os.path.exists(dest) and \
                    not os.path.exists(os.path.join(SYSTEMD_DIR, unit[:-6] + ".service")):
                _remove(unit)
                changed.append(unit)
    systemctl("daemon-reload")
    for unit in changed:
        systemctl("reset-failed", unit)
    if units is not None:
        # Units that were running before the update and still exist: start
        # them again (rollback start_services covers only the main services).
        for unit, rec in units.items():
            if rec.get("existed") and rec.get("active") and UNIT_RE.match(unit):
                systemctl("start", unit)
    log(f"reconciled {len(changed)} unit(s)")
    return rc


if __name__ == "__main__":
    sys.exit(restore() if "--restore" in sys.argv[1:] else snapshot())
