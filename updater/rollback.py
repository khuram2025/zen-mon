"""Backup and restore logic for update rollback."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .code_inventory import (
    DEFAULT_MANAGED_FILES,
    DEFAULT_MANAGED_ROOTS,
    DEFAULT_PRESERVE_PATHS,
    remove_stale_managed_files,
)

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")

# Back up every tree the release can mutate, plus built binaries.  Earlier
# backups omitted updater/, support/, docker/, most dashboard configuration and
# docker-compose.yml, so rollback could combine old API code with new support or
# container configuration.
BACKUP_MANAGED_ROOTS = (*DEFAULT_MANAGED_ROOTS, "bin")
BACKUP_MANAGED_FILES = DEFAULT_MANAGED_FILES
BACKUP_TARGETS = [*BACKUP_MANAGED_ROOTS, *BACKUP_MANAGED_FILES]

# These paths contain appliance-local state and are neither replaced nor rolled
# back. dashboard/dist is intentionally NOT excluded: build_dashboard replaces
# it and rollback must restore the prior assets exactly.
BACKUP_PRESERVE_PATHS = tuple(
    path for path in DEFAULT_PRESERVE_PATHS if path != "dashboard/dist"
)
BACKUP_MANIFEST = "code-backup-manifest.json"
BACKUP_MANIFEST_VERSION = 1


def _safe_relative(value: str) -> str:
    raw = str(value or "").replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise RuntimeError(f"unsafe backup path: {value!r}")
    return path.as_posix()


def _is_preserved(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in BACKUP_PRESERVE_PATHS
    )


def _backup_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    relative = _safe_relative(member.name)
    if _is_preserved(relative):
        return None
    # A code backup should never capture a link to data outside /opt/zenplus.
    if member.issym() or member.islnk():
        logger.warning("Skipping symlink from code backup: %s", relative)
        return None
    return member


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
                tar.add(str(target_path), arcname=target, filter=_backup_filter)
        archived_files = sorted(
            _safe_relative(member.name)
            for member in tar.getmembers()
            if member.isfile()
        )

    # The marker opts this backup into exact restore. Older archives have no
    # marker and retain legacy overlay behavior for backward compatibility.
    (backup_path / BACKUP_MANIFEST).write_text(
        json.dumps({
            "format_version": BACKUP_MANIFEST_VERSION,
            "managed_roots": list(BACKUP_MANAGED_ROOTS),
            "managed_files": list(BACKUP_MANAGED_FILES),
            "preserve_paths": list(BACKUP_PRESERVE_PATHS),
            "files": archived_files,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

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
    backups = sorted(
        (path for path in backup_base.iterdir() if path.is_dir()),
        reverse=True,
    )
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
            members = tar.getmembers()
            for member in members:
                _safe_relative(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise RuntimeError(f"unsupported entry in code backup: {member.name}")

            marker_path = backup_path / BACKUP_MANIFEST
            if marker_path.is_file():
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker.get("format_version") != BACKUP_MANIFEST_VERSION:
                    raise RuntimeError("unsupported code backup manifest version")
                archived_files = {
                    _safe_relative(member.name)
                    for member in members
                    if member.isfile()
                }
                recorded_files = {
                    _safe_relative(name) for name in marker.get("files") or ()
                }
                if archived_files != recorded_files:
                    raise RuntimeError("code backup archive does not match its manifest")
                removed = remove_stale_managed_files(
                    ZENPLUS_DIR,
                    incoming_files=recorded_files,
                    managed_roots=marker.get("managed_roots") or (),
                    managed_files=marker.get("managed_files") or (),
                    preserve_paths=marker.get("preserve_paths") or (),
                )
                logger.info("Removed %d post-backup code files", removed)

            tar.extractall(str(ZENPLUS_DIR), members=members)
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
