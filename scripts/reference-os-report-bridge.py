#!/opt/zenplus/venv/bin/python -I
"""Run the newly fetched OS assessment inside the reference's signed OTA transaction."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

REFERENCE = "09933c9f-0e63-4bb1-8d2b-a51f7af53bf3"
BASE = "1.25.1"


def inventory():
    dpkg = subprocess.check_output(["/usr/bin/dpkg-query", "-W",
                                    "-f=${binary:Package}\\t${db:Status-Status}\\t${Version}\\n"], timeout=30)
    snaps = subprocess.check_output(["/usr/bin/snap", "list", "--all"], timeout=30)
    return [hashlib.sha256(item).hexdigest() for item in
            (dpkg, snaps, Path("/var/lib/dpkg/status").read_bytes())]


def main():
    sys.path.insert(0, "/opt/zenplus")
    from updater.config import load_config
    from updater.inventory import get_current_version
    from updater.agent import _api_headers
    import httpx

    cfg = load_config()
    if cfg.appliance.id != REFERENCE or get_current_version() != BASE:
        raise ValueError("Bridge requires the enrolled 1.25.1 reference")
    fetch = runpy.run_path(str(Path(__file__).with_name("report_component.py")))["verified_component"]
    before = inventory()
    with tempfile.TemporaryDirectory(prefix="zenplus-os-assessment-") as scratch:
        result = fetch(cfg, BASE, scratch)
        if result is None:
            raise ValueError("Zentryc did not issue a component for the reference")
        _, verified = result
        # Read the component from this HTTPS transaction. The installed reporter is not invoked.
        assessment = json.loads(subprocess.check_output(
            ["/usr/bin/python3", "-I", "-B", str(verified / "os_patch_check.py")], timeout=45))
    if inventory() != before:
        raise ValueError("OS package inventory changed during the assessment")
    if assessment.get("status") not in ("updates_needed", "blocked", "meets_baseline"):
        raise ValueError("Reference returned no usable assessment")
    with httpx.Client(verify=True, follow_redirects=False, timeout=25) as client:
        reply = client.post("https://zentryc.com/api/v1/appliances/checkin", headers=_api_headers(cfg),
                            json={"current_version": BASE, "os_patch_check": assessment})
        reply.raise_for_status()
    print(json.dumps({"firmware_assessed": BASE, "inventory_sha256": before,
                      "status": assessment["status"], "packages": len(assessment["packages"])}))


if __name__ == "__main__":
    main()
