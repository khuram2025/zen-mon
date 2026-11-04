#!/usr/bin/python3
"""Verify the NCCM (configuration backup) systemd units after install or OTA.

Fresh installs used to leave the NCCM worker and its timers uninstalled, so
the NCCM page reported the worker and schedule timer inactive and no backup
ever ran. Both install paths now install the units; this check proves it.

For every unit the appliance needs it reports, distinctly:
  not installed   the unit file is absent (systemd LoadState=not-found)
  not enabled     the unit will not start at boot
  inactive/failed the unit is not running now
and exits non-zero, naming each problem, when any required unit is not
installed and enabled; a unit that is installed and enabled but not active
is a warning unless ``--require-active`` is given. ``--wait`` (default 30 s)
gives a unit that was just started time to settle (the worker restarts on
failure).

Used by install.sh (verify_installation, ``--require-active``: an operator is
there to fix it) and as an OTA ``run_hook`` after start_services, where a
non-zero exit fails the update. The OTA hook fails only on what the update
itself installs (unit files, enablement): a worker that crashes for another
reason is reported loudly in the update log and on the NCCM page, but does not
roll back an otherwise good appliance update, which would not fix it either. Stdlib only; runs
directly (``#!/usr/bin/python3``) under the 1.23.9+ updater executor.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# unit -> (role, must be enabled, must be active). The *.service units behind
# the timers are oneshot runs started by their timer: installed, never enabled.
NCM_UNITS = {
    "zenplus-ncm-worker.service": ("backup worker", True, True),
    "zenplus-ncm-backup.timer": ("schedule timer", True, True),
    "zenplus-ncm-delivery.timer": ("delivery timer", True, True),
    "zenplus-ncm-backup.service": ("scheduled backup run", False, False),
    "zenplus-ncm-delivery.service": ("notification delivery run", False, False),
}


def unit_state(unit: str) -> dict:
    """LoadState/ActiveState/SubState/UnitFileState of one unit (never raises)."""
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return {
        "load": values.get("LoadState") or "unknown",
        "active": values.get("ActiveState") or "unknown",
        "sub": values.get("SubState") or "",
        "unit_file": values.get("UnitFileState") or "",
    }


def problems_for(unit: str, state: dict) -> list[str]:
    role, need_enabled, need_active = NCM_UNITS[unit]
    if "error" in state:
        return [f"{unit} ({role}): state could not be read ({state['error']})"]
    if state["load"] == "not-found":
        return [f"{unit} ({role}): NOT INSTALLED (no unit file in /etc/systemd/system)"]
    if state["load"] != "loaded":
        return [f"{unit} ({role}): unit failed to load (LoadState={state['load']})"]
    out = []
    if need_enabled and state["unit_file"] not in ("enabled", "enabled-runtime", "static", "alias"):
        out.append(f"{unit} ({role}): installed but NOT ENABLED (UnitFileState={state['unit_file'] or 'unknown'})")
    if need_active and state["active"] != "active":
        out.append(f"{unit} ({role}): installed but {state['active'].upper()} "
                   f"(SubState={state['sub'] or 'unknown'}); see journalctl -u {unit}")
    return out


def check(wait: float = 30.0, *, poll=unit_state, sleep=time.sleep, clock=time.monotonic) -> tuple[dict, list[str]]:
    deadline = clock() + max(0.0, wait)
    while True:
        states = {unit: poll(unit) for unit in NCM_UNITS}
        problems = [p for unit, state in states.items() for p in problems_for(unit, state)]
        # Missing or disabled units never fix themselves; only wait on activity.
        settling = all("installed but" in p and "NOT ENABLED" not in p for p in problems)
        if not problems or not settling or clock() >= deadline:
            return states, problems
        sleep(2)


def is_inactive_only(problem: str) -> bool:
    return "installed but" in problem and "NOT ENABLED" not in problem


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait", type=float, default=float(os.environ.get("ZENPLUS_NCM_UNIT_WAIT", "30")),
                        help="seconds to let just-started units become active (default 30)")
    parser.add_argument("--json", action="store_true", help="print unit states as JSON")
    parser.add_argument("--require-active", action="store_true",
                        help="fail (not only warn) when an installed, enabled unit is not active")
    args = parser.parse_args(argv)
    states, problems = check(args.wait)
    if args.json:
        print(json.dumps({"units": states, "problems": problems}, indent=1))
    for unit, state in states.items():
        if "error" not in state:
            print(f"[ncm-units] {unit}: load={state['load']} active={state['active']} "
                  f"enabled={state['unit_file'] or '-'}", flush=True)
    hard = [p for p in problems if args.require_active or not is_inactive_only(p)]
    for problem in problems:
        if problem not in hard:
            print(f"[ncm-units] WARN {problem}", file=sys.stderr, flush=True)
    if hard:
        for problem in hard:
            print(f"[ncm-units] FAIL {problem}", file=sys.stderr, flush=True)
        print("[ncm-units] NCCM backups cannot run until these units are installed, enabled and "
              "active (install from /opt/zenplus/scripts/systemd, then systemctl daemon-reload and "
              "systemctl enable --now zenplus-ncm-worker.service zenplus-ncm-backup.timer "
              "zenplus-ncm-delivery.timer)", file=sys.stderr, flush=True)
        return 1
    if problems:
        print("[ncm-units] NCCM units installed and enabled; not all are active yet (see WARN above)", flush=True)
        return 0
    print("[ncm-units] NCCM worker and timers installed, enabled and active", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
