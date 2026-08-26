"""Apply code changes — patch or full file replacement."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..code_inventory import reconcile_code_tree
from ..executor import step_handler

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")


@step_handler("apply_code")
def apply_code(step: dict, extract_dir: str, cfg) -> None:
    method = step.get("method", "replace")

    if method == "patch":
        _apply_patch(step, extract_dir)
    elif method == "replace":
        _apply_replace(step, extract_dir)
    else:
        raise ValueError(f"Unknown code apply method: {method}")


def _apply_patch(step: dict, extract_dir: str) -> None:
    """Apply a git-format patch."""
    patch_file = step.get("source", "code.patch")
    patch_path = os.path.join(extract_dir, patch_file)

    if not os.path.exists(patch_path):
        raise FileNotFoundError(f"Patch file not found: {patch_path}")

    result = subprocess.run(
        ["git", "apply", "--stat", patch_path],
        capture_output=True,
        text=True,
        cwd=str(ZENPLUS_DIR),
    )
    logger.info("Patch stats:\n%s", result.stdout)

    result = subprocess.run(
        ["git", "apply", "--check", patch_path],
        capture_output=True,
        text=True,
        cwd=str(ZENPLUS_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Patch cannot be applied cleanly: {result.stderr}")

    result = subprocess.run(
        ["git", "apply", patch_path],
        capture_output=True,
        text=True,
        cwd=str(ZENPLUS_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Patch apply failed: {result.stderr}")

    logger.info("Patch applied successfully")


def _apply_replace(step: dict, extract_dir: str) -> None:
    """Apply an exact signed code snapshot (or a legacy overlay payload)."""
    source_dir = step.get("source", "code/")
    code_path = Path(extract_dir) / source_dir

    if not code_path.exists():
        raise FileNotFoundError(f"Code directory not found: {code_path}")

    removed = reconcile_code_tree(code_path, ZENPLUS_DIR)
    count = 0
    for src_file in code_path.rglob("*"):
        if src_file.is_dir():
            continue
        rel_path = src_file.relative_to(code_path)
        dest_file = ZENPLUS_DIR / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        count += 1

    logger.info("Replaced %d files; removed %d stale files", count, removed)
