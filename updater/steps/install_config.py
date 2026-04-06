"""Install configuration files."""

import logging
import os
import shutil

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


@step_handler("install_config")
def install_config(step: dict, extract_dir: str, cfg) -> None:
    source = step.get("source", "")
    dest = step.get("dest", "")

    if not source or not dest:
        raise ValueError("install_config requires 'source' and 'dest'")

    src_path = os.path.join(extract_dir, source)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Config file not found: {src_path}")

    # Backup existing config
    if os.path.exists(dest):
        backup = dest + ".pre-update"
        shutil.copy2(dest, backup)
        logger.info("Backed up config: %s → %s", dest, backup)

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src_path, dest)
    logger.info("Installed config: %s → %s", source, dest)
