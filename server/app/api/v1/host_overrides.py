"""Manual host addresses for service checks (docs/service-check-connections.md).

Administrator only, like probe trust: a check that carries a credential sends
it to the address stored here. Every change is written to the audit log first;
a change whose audit entry cannot be stored is refused.
"""
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import AuditUnavailable, write_audit_log
from app.services.host_overrides import (
    MAX_ENTRIES, SCOPE_ALL, SCOPE_SENSOR, SETTINGS_KEY, HostOverrideError, build_entry,
    load_host_overrides, lock_host_overrides, put_entry, remove_entry, save_host_overrides,
    sensor_applies,
)

router = APIRouter(prefix="/system/monitoring/host-overrides", tags=["Monitoring Settings"])

_AUDIT_UNAVAILABLE = "The change was not saved because it could not be recorded in the audit log. Try again."
# Sensors that hold a configuration: one that never enrolled or was turned off runs no checks.
_RUNNING = ("online", "degraded", "offline")


class HostOverrideIn(BaseModel):
    host: str = Field(max_length=300)
    address: str = Field(max_length=64)
    scope: Literal["all", "central", "sensor"] = "all"
    sensor_id: Optional[str] = Field(default=None, max_length=64)
    note: str = Field(default="", max_length=400)


async def load_sensors(db) -> list[dict[str, Any]]:
    rows = (await db.execute(text(
        "SELECT id::text AS id, name, status, version, capabilities FROM sensors ORDER BY lower(name)"
    ))).mappings().all()
    return [{"id": str(row["id"]).lower(), "name": row["name"], "status": row["status"],
             "version": row["version"], "applies": sensor_applies(row["capabilities"])} for row in rows]


def public_status(document: dict[str, Any], sensors: list[dict[str, Any]]) -> dict[str, Any]:
    """The stored rows plus, for each, the sensors that will not apply it.

    A sensor too old to apply manual addresses keeps resolving the name
    itself; the row says so instead of looking as if it worked everywhere.
    """
    by_id = {sensor["id"]: sensor for sensor in sensors}
    behind = [sensor for sensor in sensors if not sensor["applies"] and sensor["status"] in _RUNNING]
    entries = []
    for entry in document.get("entries", []):
        row = {key: entry.get(key) for key in ("id", "host", "address", "scope", "sensor_id", "note", "updated_at", "updated_by")}
        sensor = by_id.get(str(entry.get("sensor_id") or "").lower())
        row["sensor_name"] = sensor["name"] if sensor else None
        row["sensor_missing"] = entry.get("scope") == SCOPE_SENSOR and sensor is None
        if entry.get("scope") == SCOPE_ALL:
            row["not_applied_by"] = [item["name"] for item in behind]
        elif entry.get("scope") == SCOPE_SENSOR and sensor and not sensor["applies"]:
            row["not_applied_by"] = [sensor["name"]]
        else:
            row["not_applied_by"] = []
        entries.append(row)
    entries.sort(key=lambda row: (row["host"] or "", row["scope"] or "", row["sensor_name"] or ""))
    return {"entries": entries, "sensors": sensors, "max_entries": MAX_ENTRIES}


def _refused(exc: HostOverrideError) -> HTTPException:
    return HTTPException(exc.status, {"message": str(exc), "field": exc.field})


def _audit_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in ("host", "address", "scope", "sensor_id", "note")}


async def _store(db, user: User, document: dict[str, Any], *, action: str, entry_id: str, metadata: dict[str, Any]) -> None:
    await save_host_overrides(db, document)
    try:
        await write_audit_log(db, actor=user, action=action, resource_type="host_override",
                              resource_id=entry_id, metadata={"setting": SETTINGS_KEY, **metadata}, required=True)
    except AuditUnavailable:
        await db.rollback()
        raise HTTPException(503, _AUDIT_UNAVAILABLE) from None
    await db.commit()


@router.get("")
async def list_host_overrides(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    return public_status(await load_host_overrides(db), await load_sensors(db))


@router.post("", status_code=201)
async def add_host_override(data: HostOverrideIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    sensors = await load_sensors(db)
    document = await lock_host_overrides(db)
    try:
        entry = build_entry(data.model_dump(), sensor_ids=[sensor["id"] for sensor in sensors], actor=user.username)
        put_entry(document, entry)
    except HostOverrideError as exc:
        raise _refused(exc) from None
    await _store(db, user, document, action="monitoring.host_overrides.add", entry_id=entry["id"], metadata=_audit_view(entry))
    return public_status(document, sensors)


@router.put("/{entry_id}")
async def change_host_override(entry_id: str, data: HostOverrideIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    sensors = await load_sensors(db)
    document = await lock_host_overrides(db)
    if not any(item.get("id") == entry_id for item in document["entries"]):
        raise HTTPException(404, "Manual address not found")
    try:
        entry = build_entry(data.model_dump(), sensor_ids=[sensor["id"] for sensor in sensors], actor=user.username, entry_id=entry_id)
        previous = put_entry(document, entry)
    except HostOverrideError as exc:
        raise _refused(exc) from None
    await _store(db, user, document, action="monitoring.host_overrides.update", entry_id=entry_id,
                 metadata={**_audit_view(entry), "previous": _audit_view(previous or {})})
    return public_status(document, sensors)


@router.delete("/{entry_id}")
async def remove_host_override(entry_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    sensors = await load_sensors(db)
    document = await lock_host_overrides(db)
    removed = remove_entry(document, entry_id)
    if removed is None:
        raise HTTPException(404, "Manual address not found")
    await _store(db, user, document, action="monitoring.host_overrides.remove", entry_id=entry_id, metadata=_audit_view(removed))
    return public_status(document, sensors)
