"""Install pre-compiled Go binaries."""

import logging
import os
import shutil
import stat

from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")


@step_handler("install_binary")
def install_binary(step: dict, extract_dir: str, cfg) -> None:
    source = step.get("source", "")
    dest = step.get("dest", "")

    if not source or not dest:
        raise ValueError("install_binary requires 'source' and 'dest'")

    src_path = os.path.join(extract_dir, source)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"Binary not found: {src_path}")

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # Backup existing binary
    if os.path.exists(dest):
        backup = dest + ".bak"
        shutil.copy2(dest, backup)
        logger.info("Backed up existing binary: %s → %s", dest, backup)

    shutil.copy2(src_path, dest)
    os.chmod(dest, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    logger.info("Installed binary: %s → %s", source, dest)
