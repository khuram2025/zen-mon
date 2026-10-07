"""Backup and restore logic for update rollback."""

from __future__ import annotations

import json
import gzip
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
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
            _, gzip_stderr = gzip.communicate(timeout=300)

            if pg_dump.wait() != 0:
                stderr = pg_dump.stderr.read().decode()
                raise RuntimeError(f"pg_dump exited unsuccessfully: {stderr.strip()}")
            if gzip.returncode != 0:
                detail = (gzip_stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(f"gzip exited unsuccessfully: {detail}")
            if not dump_file.is_file() or dump_file.stat().st_size == 0:
                raise RuntimeError("pg_dump produced an empty backup")

        logger.info(
            "PostgreSQL backup: %s (%.1f MB)",
            dump_file,
            dump_file.stat().st_size / 1024 / 1024,
        )
    except Exception as e:
        logger.error("PostgreSQL backup failed: %s", e)
        dump_file.unlink(missing_ok=True)
        raise RuntimeError(f"PostgreSQL backup failed: {e}") from e


def restore_backup(backup_dir: str, *, selected_backup: Path | None = None) -> None:
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

    backup_path = selected_backup or backups[0]
    if selected_backup is not None and (
        selected_backup.resolve().parent != backup_base.resolve()
        or not selected_backup.is_dir()
    ):
        raise RuntimeError("selected backup is outside the backup directory or missing")
    logger.info("Restoring from backup: %s", backup_path)

    # Restore code
    code_archive = backup_path / "code.tar.gz"
    if code_archive.exists():
        logger.info("Restoring code ...")
        with tarfile.open(code_archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                _safe_relative(member.name)
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError(f"unsupported entry in code backup: {member.name}")
                if not (ZENPLUS_DIR / member.name).resolve().is_relative_to(ZENPLUS_DIR.resolve()):
                    raise RuntimeError(f"unsafe restore destination: {member.name}")

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

            # Replacing an inode is safe even if an existing executable is
            # running. Direct extraction truncates it and fails with ETXTBSY.
            with tempfile.TemporaryDirectory(prefix=".restore-", dir=ZENPLUS_DIR) as stage:
                tar.extractall(stage, members=members)
                for member in members:
                    source = Path(stage) / member.name
                    target = ZENPLUS_DIR / member.name
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.copystat(source, target)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, target)
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
    """Restore legacy plain dumps atomically into a clean application schema.

    Loading a normal pg_dump over existing tables leaves changed rows intact:
    CREATE/COPY errors are ignored by psql unless ON_ERROR_STOP is enabled.
    Reset user schemas and replay the dump in one transaction. Invalid gzip is
    detected before opening the database; any SQL error rolls back the reset.
    Database ownership/settings and system schemas remain unchanged.
    """
    logger.info("Restoring PostgreSQL ...")
    try:
        with tempfile.TemporaryFile(mode="w+b") as sql:
            sql.write(b"""DO $restore$
DECLARE item record;
BEGIN
  FOR item IN SELECT nspname FROM pg_namespace
      WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
        AND nspname <> 'information_schema'
  LOOP
    EXECUTE format('DROP SCHEMA %I CASCADE', item.nspname);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pg_database_owner') THEN
    CREATE SCHEMA public AUTHORIZATION pg_database_owner;
  ELSE
    CREATE SCHEMA public AUTHORIZATION postgres;
  END IF;
  GRANT USAGE ON SCHEMA public TO PUBLIC;
  IF current_setting('server_version_num')::integer < 150000 THEN
    GRANT CREATE ON SCHEMA public TO PUBLIC;
  END IF;
END $restore$;
""")
            with gzip.open(dump_file, "rb") as source:
                shutil.copyfileobj(source, sql)
            sql.seek(0)
            result = subprocess.run(
                ["sudo", "-u", "postgres", "psql", "--single-transaction",
                 "-v", "ON_ERROR_STOP=1", "zenplus"],
                stdin=sql, capture_output=True, timeout=300,
            )
            if result.returncode != 0:
                # SQL error details can contain appliance data; never log them.
                raise RuntimeError(
                    f"psql exited {result.returncode}; restore transaction rolled back"
                )
        logger.info("PostgreSQL restored")
    except Exception as e:
        logger.error("PostgreSQL restore failed: %s", e)
        raise RuntimeError(f"PostgreSQL restore failed: {e}") from e


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
