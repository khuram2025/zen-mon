"""Install Python dependencies."""

import logging
import os
import subprocess

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

VENV_PIP = "/opt/zenplus/venv/bin/pip"
WHEELS_DIR = "wheels"


def _run_pip(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [VENV_PIP, "install", *args, "--quiet"],
        capture_output=True,
        text=True,
        timeout=300,
        # The updater may protect its own backups with umask 077. Package
        # files must remain readable by the non-root API service account.
        **({"umask": 0o022} if os.name == "posix" else {}),
    )


@step_handler("pip_install")
def pip_install(step: dict, extract_dir: str, cfg) -> None:
    req_file = step.get("requirements", "requirements.txt")
    req_path = os.path.join(extract_dir, req_file)

    if not os.path.exists(req_path):
        raise FileNotFoundError(f"Requirements file not found: {req_path}")

    logger.info("Installing Python dependencies from %s", req_file)
    wheels = os.path.join(os.path.dirname(req_path), WHEELS_DIR)
    result = None
    if os.path.isdir(wheels):
        # Packages bundle wheels for dependencies added since the oldest
        # supported upgrade source. Try those alone first so appliances
        # without access to the package index neither fail nor wait on
        # index retries.
        result = _run_pip(["--no-index", "--find-links", wheels, "-r", req_path])
        if result.returncode != 0:
            logger.warning("Offline install from bundled wheels incomplete, "
                           "retrying with the package index: %s", result.stderr.strip())
    if result is None or result.returncode != 0:
        result = _run_pip(["-r", req_path])

    if result.returncode != 0:
        raise RuntimeError(f"pip install failed: {result.stderr}")

    if result.stdout.strip():
        logger.info("pip output: %s", result.stdout.strip())
