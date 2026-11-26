"""Receive the qualified read-only reporter from Zentryc on ZenPlus 1.25.1."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

import httpx

ENDPOINT = "https://zentryc.com/api/v1/appliances/components/os-patch-reporter"
FIRMWARE = "1.25.1"
DIGEST = "6e22f0692bb0d5c75f19e99422a1250ca4b30ab62532854a990ec366a462e44c"
MAX_BYTES = 256 * 1024


def fetch(client, headers, firmware):
    with client.stream("GET", ENDPOINT, headers={**headers, "X-ZenPlus-Firmware": firmware},
                       follow_redirects=False) as response:
        if response.status_code == 204:
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected component download response")
        data = bytearray()
        for chunk in response.iter_bytes(8192):
            if len(data) + len(chunk) > MAX_BYTES:
                raise ValueError("Oversized reporting component")
            data.extend(chunk)
        if hashlib.sha256(data).hexdigest() != DIGEST:
            raise ValueError("Unexpected reporting component checksum")
        return bytes(data)


def receive():
    from .agent import _api_headers
    from .config import load_config
    from .inventory import get_current_version
    from .package_trust import verify_and_extract

    cfg = load_config()
    version = get_current_version()
    if version != FIRMWARE:
        return False
    if (cfg.server.url.rstrip("/") != "https://zentryc.com" or not cfg.security.verify_tls
            or not cfg.appliance.id or not cfg.appliance.api_key):
        raise ValueError("Component delivery requires a verified Zentryc enrollment")
    with httpx.Client(timeout=httpx.Timeout(30, connect=10), verify=True) as client:
        data = fetch(client, _api_headers(cfg), version)
    if data is None:
        return False
    with tempfile.TemporaryDirectory(prefix="zenplus-os-report-") as scratch:
        package = Path(scratch) / "reporter.zup"
        package.write_bytes(data)
        verified = Path(scratch) / "verified"
        manifest = verify_and_extract(
            str(package), str(verified), public_key_paths=cfg.security.public_key_paths,
            revoked_fingerprints=cfg.security.revoked_key_fingerprints,
            max_age_days=cfg.security.max_manifest_age_days, allow_legacy=False,
            expected_version="1.0.0",
        )
        if (manifest.get("release_profile") != "zenplus-os-patch-reporter"
                or manifest.get("supported_firmware") != [version]):
            raise ValueError("Incompatible reporting component")
        # The signed installer rechecks the actual firmware and base files under the OTA lock.
        subprocess.run([sys.executable, "-I", "-B", str(verified / "os_report_component.py"),
                        "install", str(package)], check=True)
    subprocess.run([sys.executable, "-B", "-m", "updater", "--report-os-patches"],
                   cwd="/opt/zenplus", check=True)
    return True


if __name__ == "__main__":
    receive()
