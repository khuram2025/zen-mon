"""Install or update systemd service units."""

import logging
import os
import shutil
import subprocess

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

SYSTEMD_DIR = "/etc/systemd/system"


@step_handler("install_systemd")
def install_systemd(step: dict, extract_dir: str, cfg) -> None:
    source = step.get("source", "")
    if not source:
        raise ValueError("install_systemd requires 'source'")

    src_path = os.path.join(extract_dir, source)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Systemd unit not found: {src_path}")

    unit_name = os.path.basename(src_path)
    dest_path = os.path.join(SYSTEMD_DIR, unit_name)

    shutil.copy2(src_path, dest_path)
    os.chmod(dest_path, 0o644)

    # Reload systemd to pick up changes
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)

    # Enable the service
    enable = step.get("enable", True)
    if enable:
        subprocess.run(
            ["systemctl", "enable", unit_name], check=True, timeout=30
        )

    logger.info("Installed systemd unit: %s", unit_name)
