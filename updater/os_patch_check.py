"""Read-only assessment of the qualified Noble/amd64 security package baseline.

Run the probe with Ubuntu's Python so python-apt is independent of the
application virtual environment. Never refresh apt lists or apply a transaction.
"""
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

BASELINE = "noble-amd64-security-2026-11-26"
TARGETS = {
    "krb5-locales": "1.20.1-6ubuntu2.12",
    "libgssapi-krb5-2": "1.20.1-6ubuntu2.12",
    "libk5crypto3": "1.20.1-6ubuntu2.12",
    "libkrb5-3": "1.20.1-6ubuntu2.12",
    "libkrb5support0": "1.20.1-6ubuntu2.12",
    "libasound2-data": "1.2.11-1ubuntu0.5",
    "libasound2t64": "1.2.11-1ubuntu0.5",
    "libdw1t64": "0.190-1.1ubuntu0.2",
    "libelf1t64": "0.190-1.1ubuntu0.2",
    "libtiff6": "4.5.1+git230720-4ubuntu2.6",
}


def result(status, reason, packages=()):
    return {"baseline": BASELINE, "status": status, "reason": reason,
            "packages": list(packages)}


def simulate(packages):
    pins = [f"{p['name']}={p['required']}" for p in packages]
    proc = subprocess.run(
        ["apt-get", "--simulate", "--no-download", "--only-upgrade",
         "--no-install-recommends", "install", *pins],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "LC_ALL": "C"},
    )
    expected = {(p["name"], p["required"]) for p in packages}
    planned = set()
    configured = set()
    for line in proc.stdout.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            return False
        if line.startswith("Conf "):
            configured.add(line.split()[1].removesuffix(":amd64"))
        if line.startswith("Inst "):
            match = re.fullmatch(r"Inst ([^ ]+) \[[^\]]+\] \(([^ )]+).*", line)
            if not match:
                return False
            planned.add((match[1].removesuffix(":amd64"), match[2]))
    return (proc.returncode == 0 and expected == planned
            and configured <= {name for name, version in expected})


def assess():
    system = platform.freedesktop_os_release()
    if (system.get("ID"), system.get("VERSION_CODENAME"), platform.machine()) != (
        "ubuntu", "noble", "x86_64"
    ):
        return result("unsupported", "No qualified baseline for this OS or architecture")

    import apt
    import apt_pkg
    cache = apt.Cache(memonly=True)
    if "dpkg" not in cache or not cache["dpkg"].is_installed:
        return result("unknown", "Installed package database unavailable")
    if cache.broken_count:
        return result("blocked", "APT reports broken dependencies")
    needs = []
    problems = []
    for pkg in cache:
        name = pkg.shortname
        if name not in TARGETS or not pkg.is_installed:
            continue
        installed = pkg.installed
        target = TARGETS[name]
        if apt_pkg.version_compare(installed.version, target) >= 0:
            continue
        needs.append({"name": name, "architecture": installed.architecture,
                      "installed": installed.version, "required": target})
        if installed.architecture not in ("amd64", "all"):
            problems.append("Unqualified package architecture")
        if pkg._pkg.selected_state == apt_pkg.SELSTATE_HOLD:
            problems.append("Package held")
        version = next((v for v in pkg.versions if v.version == target), None)
        if not version or not any(
            o.trusted and o.origin == "Ubuntu" and o.archive == "noble-security"
            for o in version.origins
        ):
            problems.append("Qualified security version absent from trusted local APT indexes")
    needs.sort(key=lambda p: (p["name"], p["architecture"]))
    if problems:
        return result("blocked", ". ".join(sorted(set(problems))), needs)
    if not needs:
        return result("meets_baseline", "Installed packages meet this baseline", needs)
    if not simulate(needs):
        return result("blocked", "APT simulation differs from the qualified transaction", needs)
    return result("updates_needed", "Qualified package updates available", needs)


def collect_os_patch_check():
    try:
        completed = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve())],
            capture_output=True, text=True, check=True, timeout=35,
        )
        return json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return result("unknown", "OS package assessment unavailable")


if __name__ == "__main__":
    print(json.dumps(assess()))
