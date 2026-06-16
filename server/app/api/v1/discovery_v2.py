"""Discovery v2 API — Profiles, Schedules, Runs, Results, Imports, Ignored.

All write endpoints require operator+ role. Run/import/ignore/delete
actions are audit logged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.device import Device
from app.models.discovery_v2 import (
    DiscoveryIgnoredDevice,
    DiscoveryImportBatch,
    DiscoveryImportItem,
    DiscoveryProfile,
    DiscoveryResultV2,
    DiscoveryRun,
    DiscoverySchedule,
)
from app.models.user import User
from app.services.discovery_scheduler import compute_next_run, run_scheduler_tick
from app.schemas.discovery_v2 import (
    DiscoveryProfileCreate,
    DiscoveryProfileResponse,
    DiscoveryProfileUpdate,
    DiscoveryResultResponse,
    DiscoveryRunResponse,
    DiscoveryRunStartRequest,
    DiscoveryScheduleCreate,
    DiscoveryScheduleResponse,
    IgnoredDeviceResponse,
    IgnoreRequest,
    ImportRequest,
    ImportResponse,
)
from app.services.audit_service import write_audit_log
from app.services.discovery_executor import (
    cancel_run,
    expand_targets,
    start_run_task,
)


router = APIRouter(prefix="/discovery-v2", tags=["Discovery v2"])


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────
def _summary_for_schedule(s: DiscoverySchedule | None) -> Optional[str]:
    if not s or not s.enabled:
        return None
    if s.schedule_type == "once_now":
        return "One-time (immediate)"
    if s.schedule_type == "once_future":
        return f"Once at {s.start_date.isoformat() if s.start_date else 'unscheduled'}"
    if s.schedule_type == "cron":
        return f"Cron: {s.cron_expression or '—'}"
    freq = s.frequency or "recurring"
    extras = []
    if s.time_of_day:
        extras.append(s.time_of_day.strftime("%H:%M"))
    if s.day_of_week is not None and freq == "weekly":
        wk = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        extras.append(wk[s.day_of_week % 7])
    if extras:
        return f"{freq.capitalize()} @ {' / '.join(extras)}"
    return freq.capitalize()


# Next-run calculation + tick logic live in the scheduler service so the
# background loop (main.py) and this API share one implementation.
_compute_next_run = compute_next_run


async def _load_profile_response(
    db: AsyncSession, profile: DiscoveryProfile
) -> DiscoveryProfileResponse:
    sched = (
        await db.execute(
            select(DiscoverySchedule).where(DiscoverySchedule.profile_id == profile.id)
        )
    ).scalar_one_or_none()
    last_run = None
    if profile.last_run_id:
        last_run = await db.get(DiscoveryRun, profile.last_run_id)

    return DiscoveryProfileResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        enabled=profile.enabled,
        scope_type=profile.scope_type,
        targets=profile.targets or [],
        exclusions=profile.exclusions or [],
        collector_id=profile.collector_id,
        protocols=profile.protocols or ["icmp"],
        custom_ports=profile.custom_ports or [],
        snmp_credential_ids=profile.snmp_credential_ids or [],
        windows_credential_ids=profile.windows_credential_ids or [],
        ssh_credential_ids=profile.ssh_credential_ids or [],
        detect_lldp=profile.detect_lldp,
        detect_mac=profile.detect_mac,
        detect_vendor=profile.detect_vendor,
        max_concurrency=profile.max_concurrency,
        scan_timeout_ms=profile.scan_timeout_ms,
        retry_count=profile.retry_count,
        rate_limit_pps=profile.rate_limit_pps,
        max_duration_sec=profile.max_duration_sec,
        import_mode=profile.import_mode,
        default_group_id=profile.default_group_id,
        default_tags=profile.default_tags or [],
        default_template_id=profile.default_template_id,
        default_location=profile.default_location,
        default_owner=profile.default_owner,
        enable_monitoring=profile.enable_monitoring,
        keep_disabled=profile.keep_disabled,
        notify_recipients=profile.notify_recipients or [],
        last_run_id=profile.last_run_id,
        created_by=profile.created_by,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        last_run_status=last_run.status if last_run else None,
        last_run_at=last_run.completed_at if last_run else None,
        total_devices_found=(last_run.responding_targets if last_run else 0),
        new_devices_found=(last_run.new_devices if last_run else 0),
        existing_devices_matched=(last_run.existing_devices if last_run else 0),
        failed_targets=(last_run.failed_targets if last_run else 0),
        schedule_id=sched.id if sched else None,
        schedule_summary=_summary_for_schedule(sched),
        next_run_at=sched.next_run_at if sched else None,
    )


# ───────────────────────────────────────────────────────────────────
# Profiles
# ───────────────────────────────────────────────────────────────────
@router.get("/profiles", response_model=list[DiscoveryProfileResponse])
async def list_profiles(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(select(DiscoveryProfile).order_by(DiscoveryProfile.updated_at.desc()))
    ).scalars().all()
    return [await _load_profile_response(db, p) for p in rows]


@router.post("/profiles", response_model=DiscoveryProfileResponse, status_code=201)
async def create_profile(
    payload: DiscoveryProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    existing = (
        await db.execute(select(DiscoveryProfile).where(DiscoveryProfile.name == payload.name))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Profile name already in use")

    p = DiscoveryProfile(
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        scope_type=payload.scope_type,
        targets=payload.targets,
        exclusions=payload.exclusions,
        collector_id=payload.collector_id,
        protocols=payload.protocols,
        custom_ports=payload.custom_ports,
        snmp_credential_ids=[str(c) for c in payload.snmp_credential_ids],
        windows_credential_ids=[str(c) for c in payload.windows_credential_ids],
        ssh_credential_ids=[str(c) for c in payload.ssh_credential_ids],
        detect_lldp=payload.detect_lldp,
        detect_mac=payload.detect_mac,
        detect_vendor=payload.detect_vendor,
        max_concurrency=payload.max_concurrency,
        scan_timeout_ms=payload.scan_timeout_ms,
        retry_count=payload.retry_count,
        rate_limit_pps=payload.rate_limit_pps,
        max_duration_sec=payload.max_duration_sec,
        import_mode=payload.import_mode,
        default_group_id=payload.default_group_id,
        default_tags=payload.default_tags,
        default_template_id=payload.default_template_id,
        default_location=payload.default_location,
        default_owner=payload.default_owner,
        enable_monitoring=payload.enable_monitoring,
        keep_disabled=payload.keep_disabled,
        notify_recipients=payload.notify_recipients,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(p)
    await db.flush()

    if payload.schedule:
        s = DiscoverySchedule(
            profile_id=p.id,
            enabled=payload.schedule.enabled,
            schedule_type=payload.schedule.schedule_type,
            frequency=payload.schedule.frequency,
            cron_expression=payload.schedule.cron_expression,
            interval_minutes=payload.schedule.interval_minutes,
            time_of_day=payload.schedule.time_of_day,
            day_of_week=payload.schedule.day_of_week,
            day_of_month=payload.schedule.day_of_month,
            timezone=payload.schedule.timezone,
            start_date=payload.schedule.start_date or datetime.now(timezone.utc),
            end_date=payload.schedule.end_date,
            maintenance_window=payload.schedule.maintenance_window,
            created_by=user.id,
        )
        s.next_run_at = _compute_next_run(s)
        db.add(s)

    await db.commit()
    await db.refresh(p)

    await write_audit_log(
        db, actor=user, action="discovery.profile.create",
        resource_type="discovery_profile", resource_id=str(p.id),
        metadata={"name": p.name, "scope_type": p.scope_type, "targets": p.targets},
    )
    await db.commit()
    return await _load_profile_response(db, p)


@router.get("/profiles/{profile_id}", response_model=DiscoveryProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    p = await db.get(DiscoveryProfile, profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")
    return await _load_profile_response(db, p)


@router.patch("/profiles/{profile_id}", response_model=DiscoveryProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    payload: DiscoveryProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    p = await db.get(DiscoveryProfile, profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")
    fields = payload.model_dump(exclude_unset=True)
    for k, v in fields.items():
        if k in ("snmp_credential_ids", "windows_credential_ids", "ssh_credential_ids") and v is not None:
            v = [str(x) for x in v]
        setattr(p, k, v)
    p.updated_by = user.id
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()

    await write_audit_log(
        db, actor=user, action="discovery.profile.update",
        resource_type="discovery_profile", resource_id=str(p.id),
        metadata={"fields": list(fields.keys())},
    )
    await db.commit()
    await db.refresh(p)
    return await _load_profile_response(db, p)


@router.post("/profiles/{profile_id}/clone", response_model=DiscoveryProfileResponse, status_code=201)
async def clone_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    src = await db.get(DiscoveryProfile, profile_id)
    if not src:
        raise HTTPException(404, "Profile not found")
    new_name = f"{src.name} (copy)"
    counter = 1
    while (
        await db.execute(select(DiscoveryProfile).where(DiscoveryProfile.name == new_name))
    ).scalar_one_or_none():
        counter += 1
        new_name = f"{src.name} (copy {counter})"

    p = DiscoveryProfile(
        name=new_name,
        description=src.description,
        enabled=src.enabled,
        scope_type=src.scope_type,
        targets=list(src.targets or []),
        exclusions=list(src.exclusions or []),
        collector_id=src.collector_id,
        protocols=list(src.protocols or []),
        custom_ports=list(src.custom_ports or []),
        snmp_credential_ids=list(src.snmp_credential_ids or []),
        windows_credential_ids=list(src.windows_credential_ids or []),
        ssh_credential_ids=list(src.ssh_credential_ids or []),
        detect_lldp=src.detect_lldp,
        detect_mac=src.detect_mac,
        detect_vendor=src.detect_vendor,
        max_concurrency=src.max_concurrency,
        scan_timeout_ms=src.scan_timeout_ms,
        retry_count=src.retry_count,
        rate_limit_pps=src.rate_limit_pps,
        max_duration_sec=src.max_duration_sec,
        import_mode=src.import_mode,
        default_group_id=src.default_group_id,
        default_tags=list(src.default_tags or []),
        default_template_id=src.default_template_id,
        default_location=src.default_location,
        default_owner=src.default_owner,
        enable_monitoring=src.enable_monitoring,
        keep_disabled=src.keep_disabled,
        notify_recipients=list(src.notify_recipients or []),
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)

    await write_audit_log(
        db, actor=user, action="discovery.profile.clone",
        resource_type="discovery_profile", resource_id=str(p.id),
        metadata={"cloned_from": str(src.id), "new_name": new_name},
    )
    await db.commit()
    return await _load_profile_response(db, p)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    p = await db.get(DiscoveryProfile, profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")
    name = p.name
    await db.delete(p)
    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.profile.delete",
        resource_type="discovery_profile", resource_id=str(profile_id),
        metadata={"name": name},
    )
    await db.commit()


# ───────────────────────────────────────────────────────────────────
# Estimate scope
# ───────────────────────────────────────────────────────────────────
@router.post("/estimate")
async def estimate_scope(
    payload: dict,
    _user: User = Depends(get_current_user),
):
    targets = payload.get("targets", []) or []
    exclusions = payload.get("exclusions", []) or []
    ips = expand_targets(targets, exclusions)
    return {
        "ip_count": len(ips),
        "preview": ips[:10],
        "truncated": len(ips) >= 4096,
        "warnings": (
            ["Large scan: consider rate-limiting or running off-hours."] if len(ips) > 256 else []
        ),
    }


# ───────────────────────────────────────────────────────────────────
# Schedules
# ───────────────────────────────────────────────────────────────────
@router.get("/schedules", response_model=list[DiscoveryScheduleResponse])
async def list_schedules(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(select(DiscoverySchedule).order_by(DiscoverySchedule.next_run_at.asc().nullslast()))
    ).scalars().all()
    return rows


@router.post("/profiles/{profile_id}/schedule", response_model=DiscoveryScheduleResponse, status_code=201)
async def upsert_schedule(
    profile_id: uuid.UUID,
    payload: DiscoveryScheduleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    p = await db.get(DiscoveryProfile, profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")
    existing = (
        await db.execute(select(DiscoverySchedule).where(DiscoverySchedule.profile_id == profile_id))
    ).scalar_one_or_none()
    if existing:
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(existing, k, v)
        existing.updated_at = datetime.now(timezone.utc)
        existing.next_run_at = _compute_next_run(existing)
        s = existing
    else:
        s = DiscoverySchedule(
            profile_id=profile_id,
            enabled=payload.enabled,
            schedule_type=payload.schedule_type,
            frequency=payload.frequency,
            cron_expression=payload.cron_expression,
            interval_minutes=payload.interval_minutes,
            time_of_day=payload.time_of_day,
            day_of_week=payload.day_of_week,
            day_of_month=payload.day_of_month,
            timezone=payload.timezone,
            start_date=payload.start_date or datetime.now(timezone.utc),
            end_date=payload.end_date,
            maintenance_window=payload.maintenance_window,
            created_by=user.id,
        )
        s.next_run_at = _compute_next_run(s)
        db.add(s)
    await db.commit()
    await db.refresh(s)
    await write_audit_log(
        db, actor=user, action="discovery.schedule.upsert",
        resource_type="discovery_schedule", resource_id=str(s.id),
        metadata={"profile_id": str(profile_id), "type": s.schedule_type},
    )
    await db.commit()
    return s


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    s = await db.get(DiscoverySchedule, schedule_id)
    if not s:
        raise HTTPException(404, "Schedule not found")
    profile_id = s.profile_id
    await db.delete(s)
    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.schedule.delete",
        resource_type="discovery_schedule", resource_id=str(schedule_id),
        metadata={"profile_id": str(profile_id)},
    )
    await db.commit()


@router.post("/schedules/{schedule_id}/pause", status_code=200)
async def pause_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    s = await db.get(DiscoverySchedule, schedule_id)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s.enabled = False
    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.schedule.pause",
        resource_type="discovery_schedule", resource_id=str(schedule_id),
    )
    await db.commit()
    return {"ok": True}


@router.post("/schedules/{schedule_id}/resume", status_code=200)
async def resume_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    s = await db.get(DiscoverySchedule, schedule_id)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s.enabled = True
    s.next_run_at = _compute_next_run(s)
    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.schedule.resume",
        resource_type="discovery_schedule", resource_id=str(schedule_id),
    )
    await db.commit()
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Runs
# ───────────────────────────────────────────────────────────────────
@router.post("/profiles/{profile_id}/run", response_model=DiscoveryRunResponse, status_code=201)
async def start_run(
    profile_id: uuid.UUID,
    payload: DiscoveryRunStartRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    p = await db.get(DiscoveryProfile, profile_id)
    if not p:
        raise HTTPException(404, "Profile not found")

    in_flight = (
        await db.execute(
            select(DiscoveryRun).where(
                DiscoveryRun.profile_id == profile_id,
                DiscoveryRun.status.in_(("queued", "running")),
            )
        )
    ).scalar_one_or_none()
    if in_flight:
        raise HTTPException(409, "A scan is already running for this profile")

    trigger = (payload.trigger_type if payload else "manual") or "manual"
    run = DiscoveryRun(
        profile_id=profile_id,
        trigger_type=trigger,
        status="queued",
        phase="preparing",
        progress_pct=0,
        started_by=user.id,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    await write_audit_log(
        db, actor=user, action="discovery.run.start",
        resource_type="discovery_run", resource_id=str(run.id),
        metadata={"profile_id": str(profile_id), "trigger": trigger},
    )
    await db.commit()

    start_run_task(run.id)
    return run


@router.get("/runs", response_model=list[DiscoveryRunResponse])
async def list_runs(
    profile_id: Optional[uuid.UUID] = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = select(DiscoveryRun).order_by(DiscoveryRun.created_at.desc()).limit(limit)
    if profile_id:
        q = q.where(DiscoveryRun.profile_id == profile_id)
    runs = (await db.execute(q)).scalars().all()

    profile_names: dict[uuid.UUID, str] = {}
    if runs:
        ids = list({r.profile_id for r in runs})
        rows = (
            await db.execute(
                select(DiscoveryProfile.id, DiscoveryProfile.name).where(DiscoveryProfile.id.in_(ids))
            )
        ).all()
        profile_names = {pid: name for pid, name in rows}

    out: list[DiscoveryRunResponse] = []
    for r in runs:
        item = DiscoveryRunResponse.model_validate(r, from_attributes=True)
        item.profile_name = profile_names.get(r.profile_id)
        out.append(item)
    return out


@router.get("/runs/{run_id}", response_model=DiscoveryRunResponse)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    r = await db.get(DiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    res = DiscoveryRunResponse.model_validate(r, from_attributes=True)
    p = await db.get(DiscoveryProfile, r.profile_id)
    res.profile_name = p.name if p else None
    return res


@router.post("/runs/{run_id}/cancel", status_code=200)
async def cancel_run_endpoint(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    r = await db.get(DiscoveryRun, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if r.status not in ("running", "queued"):
        raise HTTPException(400, f"Cannot cancel run in status {r.status}")
    cancelled = cancel_run(run_id)
    if not cancelled:
        r.status = "cancelled"
        r.phase = "done"
        r.completed_at = datetime.now(timezone.utc)
        await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.run.cancel",
        resource_type="discovery_run", resource_id=str(run_id),
    )
    await db.commit()
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Results
# ───────────────────────────────────────────────────────────────────
@router.get("/runs/{run_id}/results", response_model=list[DiscoveryResultResponse])
async def list_results(
    run_id: uuid.UUID,
    status: Optional[str] = None,
    limit: int = Query(default=500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = select(DiscoveryResultV2).where(DiscoveryResultV2.run_id == run_id)
    if status:
        q = q.where(DiscoveryResultV2.status == status)
    q = q.order_by(DiscoveryResultV2.id.asc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()

    out: list[DiscoveryResultResponse] = []
    for r in rows:
        out.append(DiscoveryResultResponse(
            id=r.id,
            run_id=r.run_id,
            profile_id=r.profile_id,
            ip_address=str(r.ip_address),
            mac_address=r.mac_address,
            hostname=r.hostname,
            fqdn=r.fqdn,
            sys_name=r.sys_name,
            sys_object_id=r.sys_object_id,
            serial_number=r.serial_number,
            vendor=r.vendor,
            device_type=r.device_type,
            model=r.model,
            os=r.os,
            os_version=r.os_version,
            protocols_detected=r.protocols_detected or [],
            protocol_status=(r.raw_data or {}).get("protocol_status") or {},
            open_ports=r.open_ports or [],
            response_time_ms=r.response_time_ms,
            credential_status=r.credential_status,
            credential_used=r.credential_used,
            status=r.status,
            matched_device_id=r.matched_device_id,
            matched_template_id=r.matched_template_id,
            suggested_group_id=r.suggested_group_id,
            suggested_tags=r.suggested_tags or [],
            confidence_score=r.confidence_score,
            conflict_type=r.conflict_type,
            conflict_with_id=r.conflict_with_id,
            import_ready=r.import_ready,
            imported=r.imported,
            imported_at=r.imported_at,
            imported_device_id=r.imported_device_id,
            ignored=r.ignored,
            error_message=r.error_message,
            scanned_at=r.scanned_at,
        ))
    return out


# ───────────────────────────────────────────────────────────────────
# Import
# ───────────────────────────────────────────────────────────────────
_SERVER_OS_HINTS = ("windows", "linux", "ubuntu", "debian", "centos", "rhel",
                    "red hat", "suse", "fedora", "esxi", "macos")


def _is_server_result(result: DiscoveryResultV2) -> bool:
    """Server-class host (vs network gear) — drives auto routing."""
    if (result.device_type or "").lower() == "server":
        return True
    os_l = (result.os or "").lower()
    return any(h in os_l for h in _SERVER_OS_HINTS)


def _server_os_type(result: DiscoveryResultV2) -> str:
    os_l = (result.os or "").lower()
    if "windows" in os_l:
        return "windows"
    if any(h in os_l for h in ("linux", "ubuntu", "debian", "centos", "rhel", "red hat", "suse", "fedora")):
        return "linux"
    if "macos" in os_l or "mac os" in os_l:
        return "macos"
    if "bsd" in os_l:
        return "bsd"
    if "esxi" in os_l:
        return "other"
    # No OS probe result — fall back to port heuristics.
    ports = _result_ports(result)
    if ports & {3389, 5985, 5986, 445}:
        return "windows"
    if 22 in ports:
        return "linux"
    return "unknown"


def _result_ports(result: DiscoveryResultV2) -> set[int]:
    out: set[int] = set()
    for p in result.open_ports or []:
        try:
            out.add(int(p))
        except (TypeError, ValueError):
            continue
    return out


def _server_collection_mode(result: DiscoveryResultV2) -> str:
    """Best agentless mode the scan proved (or hinted) for this host."""
    if result.windows_credential_used:
        return "agentless_winrm"
    if result.credential_used:
        return "snmp"
    ports = _result_ports(result)
    if 5985 in ports or 5986 in ports:
        return "agentless_winrm"
    if 22 in ports:
        return "ssh"
    if 161 in ports:
        return "snmp"
    return "none"


def _route_result(result: DiscoveryResultV2, payload: ImportRequest) -> tuple[bool, bool]:
    """(create_device, create_server) for one result under the chosen mode."""
    if payload.import_as == "device":
        return True, False
    if payload.import_as == "server":
        return False, True
    if payload.import_as == "both":
        return True, True
    # auto: server-class hosts become servers (+ a device for ping/SNMP
    # monitoring when enabled); everything else stays a network device.
    if _is_server_result(result):
        return payload.enable_monitoring, True
    return True, False


async def _create_server_from_result(
    db: AsyncSession,
    result: DiscoveryResultV2,
    payload: ImportRequest,
    user: User,
    device_id: uuid.UUID | None,
) -> uuid.UUID:
    display_name = (result.hostname or result.sys_name or result.fqdn
                    or str(result.ip_address))
    hw = " ".join(b for b in (result.vendor, result.model) if b)
    description = f"Imported from network discovery{f' — {hw}' if hw else ''}"
    row = (await db.execute(
        text("""INSERT INTO servers
                    (display_name, hostname, fqdn, primary_ip, device_id,
                     os_type, os_name, os_version, collection_mode,
                     environment, description, tags, status, created_by)
                VALUES (:dn, :hn, :fqdn, :ip, :dev,
                        :ost, :osn, :osv, :cm,
                        :env, :desc, CAST(:tags AS jsonb), 'unknown', :cb)
                RETURNING id"""),
        {
            "dn": display_name[:255],
            "hn": (result.hostname or result.sys_name or None),
            "fqdn": result.fqdn,
            "ip": str(result.ip_address),
            "dev": device_id,
            "ost": _server_os_type(result),
            "osn": result.os,
            "osv": result.os_version,
            "cm": _server_collection_mode(result),
            "env": payload.environment,
            "desc": description,
            "tags": json.dumps(payload.tags or result.suggested_tags or []),
            "cb": user.id,
        },
    )).first()
    return row[0]


@router.post("/runs/{run_id}/import", response_model=ImportResponse)
async def import_results(
    run_id: uuid.UUID,
    payload: ImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    run = await db.get(DiscoveryRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    batch = DiscoveryImportBatch(
        run_id=run_id,
        profile_id=run.profile_id,
        group_id=payload.group_id,
        template_id=payload.template_id,
        snmp_credential_id=payload.snmp_credential_id,
        tags=payload.tags,
        enable_monitoring=payload.enable_monitoring,
        started_by=user.id,
        status="running",
        total_items=len(payload.result_ids),
    )
    db.add(batch)
    await db.flush()

    successful = 0
    failed = 0
    skipped = 0
    conflicts = 0
    devices_created = 0
    servers_created = 0
    item_reports: list[dict] = []

    for rid in payload.result_ids:
        result = await db.get(DiscoveryResultV2, rid)
        if not result or result.run_id != run_id:
            item = DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="failed",
                                       error_message="Result not found")
            db.add(item)
            failed += 1
            item_reports.append({"result_id": rid, "status": "failed",
                                 "error": "result not found"})
            continue
        if result.imported:
            item = DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="skipped",
                                       error_message="Already imported")
            db.add(item)
            skipped += 1
            item_reports.append({"result_id": rid, "status": "skipped",
                                 "reason": "already imported"})
            continue

        want_device, want_server = _route_result(result, payload)
        report: dict = {"result_id": rid, "ip": str(result.ip_address)}
        conflict_types: list[str] = []
        created_device_id: uuid.UUID | None = None
        created_server_id: uuid.UUID | None = None
        linked_device_id: uuid.UUID | None = None
        error: str | None = None

        # Device leg (network monitoring)
        existing_dev = (
            await db.execute(
                text("SELECT id FROM devices WHERE ip_address = :ip"),
                {"ip": str(result.ip_address)},
            )
        ).first()
        if existing_dev:
            linked_device_id = existing_dev[0]
        if want_device:
            if existing_dev and payload.conflict_strategy == "skip":
                conflict_types.append("ip_exists")
            else:
                try:
                    hostname = result.hostname or result.sys_name or str(result.ip_address)
                    device = Device(
                        hostname=hostname,
                        ip_address=str(result.ip_address),
                        device_type=result.device_type or "other",
                        location=payload.location,
                        group_id=payload.group_id or result.suggested_group_id,
                        tags=payload.tags or result.suggested_tags or [],
                        ping_enabled=payload.enable_monitoring,
                        ping_interval=payload.ping_interval,
                        snmp_enabled=False,
                        snmp_credential_id=payload.snmp_credential_id,
                        sys_object_id=result.sys_object_id,
                        vendor=result.vendor,
                        model=result.model,
                        os_version=result.os_version,
                        profile_id=payload.template_id or result.matched_template_id,
                        status="unknown",
                        created_by=user.id,
                    )
                    db.add(device)
                    await db.flush()
                    created_device_id = device.id
                    linked_device_id = device.id
                    devices_created += 1
                except Exception as e:
                    error = f"device: {e}"

        # Server leg (server-monitoring inventory)
        if want_server and error is None:
            hn = result.hostname or result.sys_name
            existing_srv = (
                await db.execute(
                    text("""SELECT id FROM servers
                            WHERE primary_ip = :ip
                               OR (hostname IS NOT NULL AND CAST(:hn AS text) IS NOT NULL
                                   AND lower(hostname) = lower(CAST(:hn AS text)))
                            LIMIT 1"""),
                    {"ip": str(result.ip_address), "hn": hn},
                )
            ).first()
            if existing_srv and payload.conflict_strategy == "skip":
                conflict_types.append("server_exists")
            else:
                try:
                    created_server_id = await _create_server_from_result(
                        db, result, payload, user, linked_device_id,
                    )
                    servers_created += 1
                except Exception as e:
                    error = f"server: {e}"

        # Record what was created even on partial failure so retries don't duplicate.
        now = datetime.now(timezone.utc)
        if created_device_id or created_server_id:
            result.imported = True
            result.imported_at = now
            if created_device_id:
                result.imported_device_id = created_device_id
            if created_server_id:
                result.imported_server_id = created_server_id
            result.status = "imported"

        if error is not None:
            item = DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="failed",
                                       device_id=created_device_id,
                                       server_id=created_server_id,
                                       error_message=error)
            db.add(item)
            failed += 1
            report.update(status="failed", error=error)
        elif created_device_id or created_server_id:
            item = DiscoveryImportItem(
                batch_id=batch.id,
                result_id=rid,
                status="imported",
                device_id=created_device_id,
                server_id=created_server_id,
                conflict_type=",".join(conflict_types)[:40] or None,
                processed_at=now,
            )
            db.add(item)
            successful += 1
            report["status"] = "imported"
            if created_device_id:
                report["device_id"] = str(created_device_id)
            if created_server_id:
                report["server_id"] = str(created_server_id)
            if conflict_types:
                report["partial_conflicts"] = conflict_types
        else:
            item = DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="conflict",
                                       device_id=linked_device_id,
                                       conflict_type=",".join(conflict_types)[:40] or "exists")
            db.add(item)
            conflicts += 1
            report.update(status="conflict", conflicts=conflict_types or ["exists"])
        item_reports.append(report)

    batch.successful_items = successful
    batch.failed_items = failed
    batch.skipped_items = skipped
    batch.status = "completed" if failed == 0 else "partial"
    batch.completed_at = datetime.now(timezone.utc)
    await db.commit()

    await write_audit_log(
        db, actor=user, action="discovery.import.commit",
        resource_type="discovery_import_batch", resource_id=str(batch.id),
        metadata={
            "run_id": str(run_id), "total": len(payload.result_ids),
            "successful": successful, "failed": failed, "skipped": skipped,
            "conflicts": conflicts, "import_as": payload.import_as,
            "devices_created": devices_created, "servers_created": servers_created,
        },
    )
    await db.commit()

    return ImportResponse(
        batch_id=batch.id,
        total=len(payload.result_ids),
        successful=successful,
        failed=failed,
        skipped=skipped,
        conflicts=conflicts,
        devices_created=devices_created,
        servers_created=servers_created,
        items=item_reports,
    )


# ───────────────────────────────────────────────────────────────────
# Ignore
# ───────────────────────────────────────────────────────────────────
@router.post("/ignore")
async def ignore_devices(
    payload: IgnoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    ignored: list[str] = []
    if payload.result_ids:
        rows = (
            await db.execute(
                select(DiscoveryResultV2).where(DiscoveryResultV2.id.in_(payload.result_ids))
            )
        ).scalars().all()
        for r in rows:
            r.ignored = True
            r.status = "ignored"
            r.ignored_at = datetime.now(timezone.utc)
            exists = (
                await db.execute(
                    select(DiscoveryIgnoredDevice).where(
                        DiscoveryIgnoredDevice.ip_address == r.ip_address
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                ig = DiscoveryIgnoredDevice(
                    ip_address=str(r.ip_address),
                    mac_address=r.mac_address,
                    hostname=r.hostname,
                    reason=payload.reason,
                    ignored_by=user.id,
                )
                db.add(ig)
            ignored.append(str(r.ip_address))
    elif payload.ip_address:
        exists = (
            await db.execute(
                select(DiscoveryIgnoredDevice).where(
                    DiscoveryIgnoredDevice.ip_address == payload.ip_address
                )
            )
        ).scalar_one_or_none()
        if not exists:
            db.add(DiscoveryIgnoredDevice(
                ip_address=payload.ip_address,
                reason=payload.reason,
                ignored_by=user.id,
            ))
            ignored.append(payload.ip_address)

    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.ignore",
        resource_type="discovery_result", resource_id=None,
        metadata={"ips": ignored, "reason": payload.reason},
    )
    await db.commit()
    return {"ignored": ignored}


@router.get("/ignored", response_model=list[IgnoredDeviceResponse])
async def list_ignored(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(DiscoveryIgnoredDevice).order_by(DiscoveryIgnoredDevice.ignored_at.desc())
        )
    ).scalars().all()
    out = []
    for r in rows:
        out.append(IgnoredDeviceResponse(
            id=r.id,
            ip_address=str(r.ip_address) if r.ip_address else None,
            mac_address=r.mac_address,
            hostname=r.hostname,
            reason=r.reason,
            ignored_at=r.ignored_at,
        ))
    return out


@router.delete("/ignored/{ignored_id}", status_code=204)
async def remove_ignored(
    ignored_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    r = await db.get(DiscoveryIgnoredDevice, ignored_id)
    if not r:
        raise HTTPException(404, "Not found")
    ip = str(r.ip_address) if r.ip_address else None
    await db.delete(r)
    await db.commit()
    await write_audit_log(
        db, actor=user, action="discovery.ignore.remove",
        resource_type="discovery_ignored", resource_id=str(ignored_id),
        metadata={"ip": ip},
    )
    await db.commit()


# ───────────────────────────────────────────────────────────────────
# Import batches (history)
# ───────────────────────────────────────────────────────────────────
@router.get("/imports")
async def list_import_batches(
    run_id: Optional[uuid.UUID] = None,
    profile_id: Optional[uuid.UUID] = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = select(DiscoveryImportBatch).order_by(DiscoveryImportBatch.started_at.desc()).limit(limit)
    if run_id:
        q = q.where(DiscoveryImportBatch.run_id == run_id)
    if profile_id:
        q = q.where(DiscoveryImportBatch.profile_id == profile_id)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for r in rows:
        out.append({
            "id": str(r.id),
            "run_id": str(r.run_id),
            "profile_id": str(r.profile_id),
            "status": r.status,
            "total_items": r.total_items,
            "successful_items": r.successful_items,
            "failed_items": r.failed_items,
            "skipped_items": r.skipped_items,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        })
    return out


# ───────────────────────────────────────────────────────────────────
# Scheduler tick
# ───────────────────────────────────────────────────────────────────
@router.post("/scheduler/tick")
async def scheduler_tick(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Manual tick — the background loop runs the same logic every minute."""
    return await run_scheduler_tick(db)
