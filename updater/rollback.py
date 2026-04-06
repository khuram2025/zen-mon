"""Backup and restore logic for update rollback."""

import logging
import os
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")

# Directories to back up (code only, not data)
BACKUP_TARGETS = [
    "server",
    "poller",
    "dashboard/dist",
    "dashboard/src",
    "dashboard/package.json",
    "dashboard/package-lock.json",
    "scripts",
    "bin",
    ".version",
]


def create_backup(backup_dir: str, version: str, include_db: bool = True) -> str:
    """Create a pre-update backup.

    Returns the backup directory path.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup_path = Path(backup_dir) / f"pre-{version}-{timestamp}"
    backup_path.mkdir(parents=True, exist_ok=True)

    # Save current version
    version_file = ZENPLUS_DIR / ".version"
    if version_file.exists():
        shutil.copy2(version_file, backup_path / "version.txt")

    # Create code archive
    logger.info("Creating code backup ...")
    code_archive = backup_path / "code.tar.gz"
    with tarfile.open(code_archive, "w:gz") as tar:
        for target in BACKUP_TARGETS:
            target_path = ZENPLUS_DIR / target
            if target_path.exists():
                tar.add(str(target_path), arcname=target)

    logger.info("Code backup: %s (%.1f MB)", code_archive, code_archive.stat().st_size / 1024 / 1024)

    # Database backup
    if include_db:
        _backup_postgres(backup_path)

    return str(backup_path)


def _backup_postgres(backup_path: Path) -> None:
    """Create a PostgreSQL dump."""
    dump_file = backup_path / "pg_dump.sql.gz"
    logger.info("Creating PostgreSQL backup ...")

    try:
        # Use pg_dump piped to gzip
        with open(dump_file, "wb") as f:
            pg_dump = subprocess.Popen(
                ["sudo", "-u", "postgres", "pg_dump", "zenplus"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            gzip = subprocess.Popen(
                ["gzip"],
                stdin=pg_dump.stdout,
                stdout=f,
                stderr=subprocess.PIPE,
            )
            pg_dump.stdout.close()
            gzip.communicate(timeout=300)

            if pg_dump.wait() != 0:
                stderr = pg_dump.stderr.read().decode()
                logger.warning("pg_dump warnings: %s", stderr)

        logger.info(
            "PostgreSQL backup: %s (%.1f MB)",
            dump_file,
            dump_file.stat().st_size / 1024 / 1024,
        )
    except Exception as e:
        logger.error("PostgreSQL backup failed: %s", e)
        dump_file.unlink(missing_ok=True)


def restore_backup(backup_dir: str) -> None:
    """Restore from the most recent backup."""
    backup_base = Path(backup_dir)

    if not backup_base.exists():
        logger.error("No backups found at %s", backup_dir)
        return

    # Find most recent backup
    backups = sorted(backup_base.iterdir(), reverse=True)
    if not backups:
        logger.error("No backup directories found")
        return

    backup_path = backups[0]
    logger.info("Restoring from backup: %s", backup_path)

    # Restore code
    code_archive = backup_path / "code.tar.gz"
    if code_archive.exists():
        logger.info("Restoring code ...")
        with tarfile.open(code_archive, "r:gz") as tar:
            # Security check
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    logger.error("Dangerous path in backup: %s", member.name)
                    return
            tar.extractall(str(ZENPLUS_DIR))
        logger.info("Code restored")

    # Restore version
    version_file = backup_path / "version.txt"
    if version_file.exists():
        shutil.copy2(version_file, ZENPLUS_DIR / ".version")

    # Restore database
    pg_dump = backup_path / "pg_dump.sql.gz"
    if pg_dump.exists():
        _restore_postgres(pg_dump)

    logger.info("Backup restored from %s", backup_path.name)


def _restore_postgres(dump_file: Path) -> None:
    """Restore PostgreSQL from dump."""
    logger.info("Restoring PostgreSQL ...")
    try:
        gunzip = subprocess.Popen(
            ["gunzip", "-c", str(dump_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        psql = subprocess.Popen(
            ["sudo", "-u", "postgres", "psql", "zenplus"],
            stdin=gunzip.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        gunzip.stdout.close()
        _, stderr = psql.communicate(timeout=300)

        if psql.returncode != 0:
            logger.warning("psql restore warnings: %s", stderr.decode())
        else:
            logger.info("PostgreSQL restored")
    except Exception as e:
        logger.error("PostgreSQL restore failed: %s", e)


def cleanup_old_backups(backup_dir: str, max_backups: int = 3) -> None:
    """Remove old backups, keeping the most recent max_backups."""
    backup_base = Path(backup_dir)
    if not backup_base.exists():
        return

    backups = sorted(backup_base.iterdir(), reverse=True)
    for old_backup in backups[max_backups:]:
        if old_backup.is_dir():
            logger.info("Removing old backup: %s", old_backup.name)
            shutil.rmtree(old_backup, ignore_errors=True)
