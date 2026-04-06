"""Run pre/post update hook scripts."""

import logging
import os
import subprocess

from ..crypto import sha256_file
from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


@step_handler("run_hook")
def run_hook(step: dict, extract_dir: str, cfg) -> None:
    script = step.get("script", "")
    if not script:
        raise ValueError("run_hook requires 'script'")

    script_path = os.path.join(extract_dir, script)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Hook script not found: {script_path}")

    timeout = step.get("timeout", 120)

    # Make executable
    os.chmod(script_path, 0o755)

    logger.info("Running hook: %s", script)
    result = subprocess.run(
        [script_path],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/opt/zenplus",
        env={**os.environ, "ZENPLUS_DIR": "/opt/zenplus"},
    )

    if result.stdout.strip():
        logger.info("Hook stdout: %s", result.stdout.strip()[:1000])
    if result.stderr.strip():
        logger.warning("Hook stderr: %s", result.stderr.strip()[:1000])

    if result.returncode != 0:
        raise RuntimeError(
            f"Hook {script} exited with code {result.returncode}: "
            f"{result.stderr.strip()[:500]}"
        )
