"""Create pre-update backup."""

import logging

from ..executor import step_handler
from ..rollback import cleanup_old_backups, create_backup, restore_backup

logger = logging.getLogger("zenplus.updater")


@step_handler("backup")
def backup_step(step: dict, extract_dir: str, cfg) -> None:
    targets = step.get("targets", ["code", "database"])
    include_db = "database" in targets
    version = "current"

    # Try to get version from parent manifest context
    backup_dir = cfg.update.backup_dir
    create_backup(backup_dir, version, include_db=include_db)
    cleanup_old_backups(backup_dir, cfg.update.max_backups)


@step_handler("restore_backup")
def restore_backup_step(step: dict, extract_dir: str, cfg) -> None:
    restore_backup(cfg.update.backup_dir)
