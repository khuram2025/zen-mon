"""Settings -> Storage: retention/purge, OS cleanup, backup & restore.

Complements the existing GET /system/storage (disk/LVM status + expansion)
in system_updates.py. All endpoints are admin-only and audited.
"""

import asyncio
import json
import os
import re
import subprocess
import tarfile
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services import storage_service as svc

router = APIRouter(prefix="/system/storage", tags=["Storage"])


# ─── Overview / retention ───────────────────────────────────────────────────

@router.get("/overview")
async def storage_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """OS + data mounts, managed-table inventory, settings, recent events."""
    os_info, inventory = await asyncio.gather(svc.os_overview(), svc.ch_inventory())
    retention = await svc.get_retention_settings(db)
    schedule = await svc.get_backup_schedule(db)

    categories = []
    for cat_id, meta in svc.CATEGORIES.items():
        cat_tables = [t for t in inventory if t["category"] == cat_id]
        categories.append({
            "id": cat_id,
            **meta,
            "configured_days": retention["categories"].get(cat_id),
            "size_bytes": sum(t["size_bytes"] for t in cat_tables),
            "size_human": svc.human_size(sum(t["size_bytes"] for t in cat_tables)),
            "rows": sum(t["rows"] for t in cat_tables),
            "tables": cat_tables,
        })

    return {
        **os_info,
        "categories": categories,
        "auto_purge": retention["auto_purge"],
        "table_overrides": retention["table_overrides"],
        "backup_schedule": schedule,
    }


class AutoPurgeSettings(BaseModel):
    enabled: bool = True
    threshold_pct: int = Field(85, ge=50, le=98)
    target_pct: int = Field(75, ge=40, le=95)
    min_keep_days: int = Field(7, ge=1, le=365)


class RetentionUpdate(BaseModel):
    categories: dict[str, Optional[int]] = {}
    table_overrides: dict[str, int] = {}
    auto_purge: AutoPurgeSettings = AutoPurgeSettings()


@router.put("/retention")
async def update_retention(
    body: RetentionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    for cat, days in body.categories.items():
        if cat not in svc.CATEGORIES:
            raise HTTPException(400, f"Unknown category: {cat}")
        if days is not None and not (1 <= days <= 3650):
            raise HTTPException(400, f"Retention for {cat} must be 1..3650 days")
    for table, days in body.table_overrides.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", table):
            raise HTTPException(400, f"Invalid table name: {table}")
        if not (1 <= days <= 3650):
            raise HTTPException(400, f"Override for {table} must be 1..3650 days")
    if body.auto_purge.target_pct >= body.auto_purge.threshold_pct:
        raise HTTPException(400, "Auto-purge target must be below the threshold")

    value = body.model_dump()
    await svc._upsert_setting(db, svc.RETENTION_SETTINGS_KEY, value)
    changes = await svc.apply_retention(db, value, actor=user.username)
    await write_audit_log(
        db, actor=user, action="settings.storage.retention.update",
        resource_type="system_setting", resource_id=svc.RETENTION_SETTINGS_KEY,
        metadata={"changes": changes},
    )
    await db.commit()
    errors = [c for c in changes if "error" in c]
    return {
        "message": "Retention policy saved",
        "ttl_changes": changes,
        "errors": errors,
    }


# ─── Purge ──────────────────────────────────────────────────────────────────

class PurgeRequest(BaseModel):
    category: Optional[str] = None
    table: Optional[str] = None
    older_than_days: int = Field(ge=1, le=3650)
    mode: str = Field("partitions", pattern="^(partitions|precise)$")
    dry_run: bool = True


@router.post("/purge")
async def purge_data(
    body: PurgeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """Free space by dropping expired partitions (dry_run previews first)."""
    inventory = await svc.ch_inventory()
    meta = {t["table"]: t for t in inventory}
    if body.table:
        if body.table not in meta:
            raise HTTPException(404, f"Unknown or unmanaged table: {body.table}")
        tables = [body.table]
    elif body.category:
        if body.category not in svc.CATEGORIES:
            raise HTTPException(400, f"Unknown category: {body.category}")
        tables = [t["table"] for t in inventory if t["category"] == body.category]
    else:
        raise HTTPException(400, "Provide either 'category' or 'table'")

    if body.dry_run:
        plan = await svc.plan_purge(tables, body.older_than_days)
        return {
            "dry_run": True,
            "partitions": plan,
            "total_bytes": sum(p["size_bytes"] for p in plan),
            "total_human": svc.human_size(sum(p["size_bytes"] for p in plan)),
            "note": (
                "Partitions are dropped whole; rows newer than the oldest "
                "partition boundary are only removed in 'precise' mode."
            ),
        }

    result = await svc.execute_purge(
        db, meta, tables, body.older_than_days,
        mode=body.mode, actor=user.username,
    )
    await write_audit_log(
        db, actor=user, action="system.storage_purge",
        resource_type="storage", resource_id=body.table or body.category,
        metadata={"older_than_days": body.older_than_days,
                  "freed_bytes": result["freed_bytes"]},
    )
    await db.commit()
    return result


@router.get("/events")
async def storage_events(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    rows = (
        await db.execute(
            text(
                "SELECT id, created_at, event_type, actor, freed_bytes, details "
                "FROM storage_events ORDER BY created_at DESC LIMIT :n"
            ),
            {"n": min(max(limit, 1), 200)},
        )
    ).all()
    return [
        {
            "id": r[0],
            "created_at": r[1].isoformat(),
            "event_type": r[2],
            "actor": r[3],
            "freed_bytes": r[4],
            "freed_human": svc.human_size(r[4] or 0),
            "details": r[5],
        }
        for r in rows
    ]


# ─── OS cleanup ─────────────────────────────────────────────────────────────

class OsCleanupRequest(BaseModel):
    actions: list[str] = Field(min_length=1)
    journal_max_mb: int = Field(200, ge=50, le=5000)


@router.post("/os-cleanup")
async def os_cleanup(
    body: OsCleanupRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    allowed = {"vacuum_journal", "apt_clean"}
    bad = set(body.actions) - allowed
    if bad:
        raise HTTPException(400, f"Unknown actions: {sorted(bad)}")
    result = await svc.os_cleanup(db, body.actions, body.journal_max_mb, user.username)
    await write_audit_log(
        db, actor=user, action="system.storage_os_cleanup",
        resource_type="storage", resource_id="/",
        metadata={"actions": body.actions, "freed_bytes": result["freed_bytes"]},
    )
    await db.commit()
    return result


# ─── Backups ────────────────────────────────────────────────────────────────

class CreateBackupRequest(BaseModel):
    include_clickhouse: bool = False
    note: Optional[str] = Field(None, max_length=500)


class BackupScheduleUpdate(BaseModel):
    enabled: bool = False
    frequency: str = Field("weekly", pattern="^(daily|weekly)$")
    weekday: int = Field(6, ge=0, le=6)
    hour_utc: int = Field(2, ge=0, le=23)
    include_clickhouse: bool = False
    keep_last: int = Field(5, ge=1, le=30)


def _backup_row_to_dict(r) -> dict:
    return {
        "id": str(r[0]),
        "created_at": r[1].isoformat(),
        "created_by": r[2],
        "kind": r[3],
        "status": r[4],
        "include_clickhouse": r[5],
        "size_bytes": r[6],
        "size_human": svc.human_size(r[6] or 0),
        "note": r[7],
        "error": r[8],
        "finished_at": r[9].isoformat() if r[9] else None,
        "last_restore_at": r[10].isoformat() if r[10] else None,
        "last_restore_status": r[11],
        "last_restore_error": r[12],
    }


_BACKUP_SELECT = (
    "SELECT id, created_at, created_by, kind, status, include_clickhouse, "
    "size_bytes, note, error, finished_at, last_restore_at, "
    "last_restore_status, last_restore_error FROM storage_backups "
)


@router.get("/backups")
async def list_backups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    rows = (await db.execute(text(_BACKUP_SELECT + "ORDER BY created_at DESC"))).all()
    return [_backup_row_to_dict(r) for r in rows]


@router.post("/backups")
async def create_backup(
    body: CreateBackupRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    running = (
        await db.execute(
            text("SELECT 1 FROM storage_backups WHERE status='running' LIMIT 1")
        )
    ).first()
    if running:
        raise HTTPException(409, "A backup is already running")

    backup_id = str(uuid.uuid4())
    kind = "full" if body.include_clickhouse else "config"
    await db.execute(
        text(
            "INSERT INTO storage_backups (id, created_by, kind, status, "
            "include_clickhouse, note) VALUES (:id, :by, :kind, 'running', :ch, :note)"
        ),
        {"id": backup_id, "by": user.username, "kind": kind,
         "ch": body.include_clickhouse, "note": body.note},
    )
    await write_audit_log(
        db, actor=user, action="system.storage_backup_create",
        resource_type="storage_backup", resource_id=backup_id,
        metadata={"kind": kind},
    )
    await db.commit()
    asyncio.create_task(
        svc.run_backup(backup_id, kind, body.include_clickhouse, user.username)
    )
    return {"id": backup_id, "status": "running",
            "message": f"{'Full' if body.include_clickhouse else 'Configuration'} backup started"}


class RestoreRequest(BaseModel):
    components: list[str] = Field(min_length=1)


@router.post("/backups/{backup_id}/restore")
async def restore_backup(
    backup_id: uuid.UUID,
    body: RestoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    allowed = {"postgres", "clickhouse_schema", "clickhouse_data", "config"}
    bad = set(body.components) - allowed
    if bad:
        raise HTTPException(400, f"Unknown components: {sorted(bad)}")

    row = (
        await db.execute(
            text("SELECT status, include_clickhouse FROM storage_backups WHERE id=:id"),
            {"id": str(backup_id)},
        )
    ).first()
    if not row:
        raise HTTPException(404, "Backup not found")
    if row[0] != "completed":
        raise HTTPException(409, f"Backup is {row[0]}, not restorable")
    if "clickhouse_data" in body.components and not row[1]:
        raise HTTPException(400, "This backup does not include ClickHouse data")
    if not os.path.isfile(os.path.join(svc.BACKUP_ROOT, str(backup_id), "manifest.json")):
        raise HTTPException(410, "Backup files are missing on disk")

    await db.execute(
        text(
            "UPDATE storage_backups SET last_restore_at=NOW(), "
            "last_restore_status='running', last_restore_error=NULL WHERE id=:id"
        ),
        {"id": str(backup_id)},
    )
    await svc.write_storage_event(
        db, "restore_started", actor=user.username,
        details={"backup_id": str(backup_id), "components": body.components},
    )
    await write_audit_log(
        db, actor=user, action="system.storage_backup_restore",
        resource_type="storage_backup", resource_id=str(backup_id),
        metadata={"components": body.components},
    )
    await db.commit()
    asyncio.create_task(svc.run_restore(str(backup_id), body.components, user.username))
    return {"status": "running", "message": "Restore started",
            "components": body.components}


@router.post("/restart-services")
async def restart_services(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """Restart app services to pick up a restored configuration/database."""
    await write_audit_log(
        db, actor=user, action="system.storage_restart_services",
        resource_type="storage", resource_id="services",
    )
    await db.commit()
    r = await asyncio.to_thread(svc._run_helper, "restart-app-services")
    if r.returncode != 0:
        raise HTTPException(500, f"Restart failed: {(r.stderr or r.stdout).strip()}")
    return {"status": "ok", "message": "Services restarting"}


@router.get("/backups/{backup_id}/download")
async def download_backup(
    backup_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    backup_dir = os.path.join(svc.BACKUP_ROOT, str(backup_id))
    if not os.path.isdir(backup_dir):
        raise HTTPException(404, "Backup files not found")
    ch_dir = os.path.join(svc.CH_BACKUP_HOST_ROOT, "appliance", str(backup_id))

    # Stream tar czf - so multi-GB full backups never hit memory or temp disk.
    args = ["tar", "-cz", "-C", svc.BACKUP_ROOT, str(backup_id)]
    if os.path.isdir(ch_dir):
        args += ["-C", os.path.join(svc.CH_BACKUP_HOST_ROOT, "appliance"),
                 f"--transform=s|^{backup_id}|clickhouse-{backup_id}|",
                 str(backup_id)]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE)

    def _stream():
        try:
            while True:
                chunk = proc.stdout.read(1024 * 512)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition":
                f'attachment; filename="zenplus-backup-{backup_id}.tar.gz"'
        },
    )


@router.post("/backups/upload")
async def upload_backup(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """Import a previously downloaded backup archive."""
    tmp_path = os.path.join(svc.BACKUP_ROOT, f".upload-{uuid.uuid4()}.tar.gz")
    os.makedirs(svc.BACKUP_ROOT, exist_ok=True)
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)

        def _extract() -> dict:
            with tarfile.open(tmp_path, "r:gz") as tar:
                members = tar.getmembers()
                # Safety: no absolute paths / traversal, and locate manifest.
                manifest_member = None
                for m in members:
                    if m.name.startswith("/") or ".." in m.name:
                        raise ValueError(f"unsafe path in archive: {m.name}")
                    if m.name.count("/") == 1 and m.name.endswith("/manifest.json"):
                        manifest_member = m
                if manifest_member is None:
                    raise ValueError("archive does not contain a backup manifest")
                mf = json.load(tar.extractfile(manifest_member))
                backup_id = mf.get("id")
                uuid.UUID(str(backup_id))  # validates format
                for m in members:
                    if m.name.startswith(f"{backup_id}/"):
                        tar.extract(m, svc.BACKUP_ROOT)
                    elif m.name.startswith(f"clickhouse-{backup_id}/"):
                        m.name = m.name.replace(f"clickhouse-{backup_id}", str(backup_id), 1)
                        tar.extract(m, os.path.join(svc.CH_BACKUP_HOST_ROOT, "appliance"))
                return mf

        manifest = await asyncio.to_thread(_extract)
        backup_id = str(manifest["id"])
        backup_dir = os.path.join(svc.BACKUP_ROOT, backup_id)
        size = await asyncio.to_thread(svc._dir_size, backup_dir)
        has_ch = os.path.isdir(
            os.path.join(svc.CH_BACKUP_HOST_ROOT, "appliance", backup_id)
        )
        await db.execute(
            text(
                "INSERT INTO storage_backups (id, created_by, kind, status, "
                "include_clickhouse, size_bytes, path, note, finished_at) "
                "VALUES (:id, :by, :kind, 'completed', :ch, :size, :path, :note, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET status='completed', "
                "size_bytes=EXCLUDED.size_bytes, include_clickhouse=EXCLUDED.include_clickhouse"
            ),
            {"id": backup_id, "by": user.username, "kind": manifest.get("kind", "config"),
             "ch": has_ch, "size": size, "path": backup_dir,
             "note": f"Imported from upload ({file.filename})"},
        )
        await write_audit_log(
            db, actor=user, action="system.storage_backup_upload",
            resource_type="storage_backup", resource_id=backup_id,
        )
        await db.commit()
        return {"id": backup_id, "message": "Backup imported",
                "include_clickhouse": has_ch}
    except (ValueError, KeyError, json.JSONDecodeError, tarfile.TarError) as e:
        raise HTTPException(400, f"Invalid backup archive: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.delete("/backups/{backup_id}")
async def delete_backup(
    backup_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    row = (
        await db.execute(
            text("SELECT status FROM storage_backups WHERE id=:id"),
            {"id": str(backup_id)},
        )
    ).first()
    if not row:
        raise HTTPException(404, "Backup not found")
    if row[0] == "running":
        raise HTTPException(409, "Cannot delete a running backup")
    await svc.delete_backup_files(str(backup_id))
    await db.execute(
        text("DELETE FROM storage_backups WHERE id=:id"), {"id": str(backup_id)}
    )
    await svc.write_storage_event(
        db, "backup_deleted", actor=user.username, details={"backup_id": str(backup_id)}
    )
    await write_audit_log(
        db, actor=user, action="system.storage_backup_delete",
        resource_type="storage_backup", resource_id=str(backup_id),
    )
    await db.commit()
    return {"message": "Backup deleted"}


@router.put("/backups/schedule")
async def update_backup_schedule(
    body: BackupScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    await svc._upsert_setting(db, svc.BACKUP_SCHEDULE_KEY, body.model_dump())
    await write_audit_log(
        db, actor=user, action="settings.storage.backup_schedule.update",
        resource_type="system_setting", resource_id=svc.BACKUP_SCHEDULE_KEY,
    )
    await db.commit()
    return {"message": "Backup schedule saved"}
