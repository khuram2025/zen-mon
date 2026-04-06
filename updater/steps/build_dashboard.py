"""Build or deploy the React dashboard."""

import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

DASHBOARD_DIR = Path("/opt/zenplus/dashboard")
DIST_DIR = DASHBOARD_DIR / "dist"


@step_handler("build_dashboard")
def build_dashboard(step: dict, extract_dir: str, cfg) -> None:
    prebuilt = step.get("prebuilt", True)

    if prebuilt:
        _deploy_prebuilt(step, extract_dir)
    else:
        _build_from_source()


def _deploy_prebuilt(step: dict, extract_dir: str) -> None:
    """Extract pre-built dashboard dist."""
    source = step.get("source", "dashboard-dist.tar.gz")
    archive_path = os.path.join(extract_dir, source)

    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Dashboard archive not found: {archive_path}")

    # Remove old dist
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                raise RuntimeError(f"Dangerous path in archive: {member.name}")
        tar.extractall(str(DIST_DIR))

    logger.info("Pre-built dashboard deployed to %s", DIST_DIR)


def _build_from_source() -> None:
    """Build dashboard from source (npm install + build)."""
    logger.info("Building dashboard from source ...")

    result = subprocess.run(
        ["npm", "install", "--production=false"],
        capture_output=True,
        text=True,
        cwd=str(DASHBOARD_DIR),
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"npm install failed: {result.stderr}")

    result = subprocess.run(
        ["npm", "run", "build"],
        capture_output=True,
        text=True,
        cwd=str(DASHBOARD_DIR),
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"npm build failed: {result.stderr}")

    logger.info("Dashboard built successfully")
