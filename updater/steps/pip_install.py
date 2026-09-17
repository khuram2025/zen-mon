"""Install Python dependencies."""

import logging
import os
import subprocess

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

VENV_PIP = "/opt/zenplus/venv/bin/pip"


@step_handler("pip_install")
def pip_install(step: dict, extract_dir: str, cfg) -> None:
    req_file = step.get("requirements", "requirements.txt")
    req_path = os.path.join(extract_dir, req_file)

    if not os.path.exists(req_path):
        raise FileNotFoundError(f"Requirements file not found: {req_path}")

    logger.info("Installing Python dependencies from %s", req_file)
    result = subprocess.run(
        [VENV_PIP, "install", "-r", req_path, "--quiet"],
        capture_output=True,
        text=True,
        timeout=300,
        # The updater may protect its own backups with umask 077. Package
        # files must remain readable by the non-root API service account.
        **({"umask": 0o022} if os.name == "posix" else {}),
    )

    if result.returncode != 0:
        raise RuntimeError(f"pip install failed: {result.stderr}")

    if result.stdout.strip():
        logger.info("pip output: %s", result.stdout.strip())
