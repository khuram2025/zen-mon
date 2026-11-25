#!/usr/bin/python3
"""Wait for the restarted API to answer before the updater's health check.

The updater's own health check gives the API about 15 seconds (5 attempts,
3 seconds apart). On some appliances the API needs about that long before it
accepts connections, so 1.25.18 -> 1.25.25 and 1.25.26 rolled back on
10.51.2.12 while 1.25.24 passed on its last attempt. This hook runs from the
signed payload, so the updater already installed on the appliance (whatever
its version) waits here first; the health check that follows then passes at
once.

It waits up to WAIT_S for a 2xx answer, stops early when systemd has given up
on the unit, and on failure prints the unit state and the newest log lines so
the update log says why. Exit 0 = answered, 1 = did not answer in time.
"""
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/api/v1/system/health"
UNIT = "zenplus-api"
WAIT_S = 180
POLL_S = 2
REQUEST_TIMEOUT_S = 10
JOURNAL_LINES = 8
_SECRET = re.compile(r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)(\S*\s*[=:]\s*)\S+")


def answered(url=URL, timeout=REQUEST_TIMEOUT_S):
    """True on a 2xx answer; False while the API is not accepting or not ready."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def unit_state(unit=UNIT):
    """systemd's view of the unit: ActiveState, SubState, NRestarts, exit status."""
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState,SubState,NRestarts,ExecMainStatus"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def journal_tail(unit=UNIT, lines=JOURNAL_LINES):
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [_SECRET.sub(r"\1\2[redacted]", line)[:160] for line in out.splitlines()[-lines:]]


def describe(state):
    if not state:
        return "unit state unavailable"
    return (f"{state.get('ActiveState', '?')}/{state.get('SubState', '?')}, "
            f"{state.get('NRestarts', '?')} automatic restarts, "
            f"last exit status {state.get('ExecMainStatus', '?')}")


def wait(wait_s=WAIT_S, poll_s=POLL_S, *, probe=answered, state=unit_state,
         clock=time.monotonic, sleep=time.sleep):
    """Return (ok, seconds waited, final unit state)."""
    start = clock()
    while True:
        if probe():
            return True, clock() - start, None
        current = state()
        # systemd stopped restarting it (start limit hit or exited for good).
        if current.get("ActiveState") == "failed":
            return False, clock() - start, current
        if clock() - start >= wait_s:
            return False, clock() - start, current
        sleep(poll_s)


def main():
    ok, waited, state = wait()
    if ok:
        print(f"[api-wait] API answered after {waited:.0f} s")
        return 0
    for line in journal_tail():
        print(f"[api-wait] log: {line}")
    print(f"[api-wait] API did not answer within {waited:.0f} s ({describe(state)})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
