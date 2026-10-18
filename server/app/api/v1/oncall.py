"""On-call schedules API (1.24.17).

Mounted with the ``oncall`` module policy: reads need ``alerts.view``,
changes need ``alerts.manage``.

Schedule JSON::

    {id, name, description, timezone, enabled,
     layers: [{id, name, position, shift_hours, handoff_at, members: [user_id],
               member_names, restrict_to: [{days: [0..6], start: "HH:MM", end: "HH:MM"}]}],
     overrides: [{id, user_id, user_name, starts_at, ends_at, reason}],
     now: {users: [{id, name}], source, layer, until}, used_by: [policy names]}

Days are 0 = Monday ... 6 = Sunday in the schedule's time zone; a window with
``end <= start`` runs past midnight. See app.services.oncall_service.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.services import alert_priority as prio
from app.services import escalation_policy_service as eps
from app.services import oncall_service as oc
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/oncall", tags=["On-call"])

can_manage = require_permission("alerts.manage")


class LayerBody(BaseModel):
    name: str = Field(default="Layer", min_length=1, max_length=100)
    position: Optional[int] = Field(default=None, ge=0, le=100)
    shift_hours: int = Field(default=168, ge=1, le=8760)
    handoff_at: datetime
    members: list[UUID] = Field(default_factory=list, max_length=oc.MAX_MEMBERS)
    restrict_to: list[dict[str, Any]] = Field(default_factory=list)


class ScheduleBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True
    layers: list[LayerBody] = Field(default_factory=list, max_length=oc.MAX_LAYERS)


class OverrideBody(BaseModel):
    user_id: UUID
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = Field(default=None, max_length=500)


async def _require_supported(db: AsyncSession) -> None:
    if not await eps.supported(db):
        raise HTTPException(503, "On-call schedules need database migration 164.")


def _aware(name: str, ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise HTTPException(422, f"'{name}' must include a timezone offset")
    return ts.astimezone(timezone.utc)


async def _clean(db: AsyncSession, body: ScheduleBody) -> dict:
    tz = body.timezone.strip()
    if not oc.valid_timezone(tz):
        raise HTTPException(422, f"Unknown time zone {body.timezone!r}; use an IANA name such as Asia/Riyadh")
    layers = []
    for i, lv in enumerate(body.layers):
        try:
            restrict = oc.clean_restrictions(lv.restrict_to)
        except ValueError as exc:
            raise HTTPException(422, f"Layer {lv.name}: {exc}") from exc
        layers.append({"name": lv.name.strip() or "Layer",
                       "position": lv.position if lv.position is not None else i,
                       "shift_hours": lv.shift_hours,
                       "handoff_at": _aware("handoff_at", lv.handoff_at),
                       "members": [str(m) for m in dict.fromkeys(lv.members)],
                       "restrict_to": restrict})
    member_ids = {m for lv in layers for m in lv["members"]}
    if member_ids:
        found = {str(r[0]) for r in (await db.execute(text(
            "SELECT id FROM users WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": sorted(member_ids)})).fetchall()}
        unknown = sorted(member_ids - found)
        if unknown:
            raise HTTPException(422, "Unknown schedule members: " + ", ".join(unknown))
    return {"name": body.name.strip(), "description": (body.description or "").strip() or None,
            "timezone": tz, "enabled": body.enabled, "layers": layers}


async def _save_layers(db: AsyncSession, schedule_id: str, layers: list[dict]) -> None:
    import json
    await db.execute(text("DELETE FROM oncall_layers WHERE schedule_id = :id"), {"id": schedule_id})
    for lv in layers:
        await db.execute(text(
            "INSERT INTO oncall_layers (schedule_id, name, position, shift_hours, handoff_at, members, restrict_to)"
            " VALUES (:sid, :name, :pos, :shift, :handoff, CAST(:members AS uuid[]), CAST(:r AS jsonb))"
        ), {"sid": schedule_id, "name": lv["name"], "pos": lv["position"], "shift": lv["shift_hours"],
            "handoff": lv["handoff_at"], "members": lv["members"], "r": json.dumps(lv["restrict_to"])})


async def _referencing_policies(db: AsyncSession, kind: str, target_id: str) -> list[str]:
    """Names of policies whose steps or fallback name this target."""
    probe = [{"type": kind, "id": target_id}]
    import json
    rows = (await db.execute(text(
        "SELECT DISTINCT p.name FROM escalation_policies p"
        " LEFT JOIN escalation_steps s ON s.policy_id = p.id"
        " WHERE p.fallback_targets @> CAST(:t AS jsonb) OR s.targets @> CAST(:t AS jsonb)"
        " ORDER BY p.name"), {"t": json.dumps(probe)})).fetchall()
    return [r[0] for r in rows]


async def _schedule_payload(db: AsyncSession, schedule: oc.Schedule, *, now: datetime,
                            description: Optional[str] = None) -> dict:
    user_ids = {m for lv in schedule.layers for m in lv.members} | {o.user_id for o in schedule.overrides}
    names = await oc.user_names(db, user_ids)
    out = oc.schedule_dict(schedule, names=names)
    who = oc.on_call_at(schedule, now)
    out["description"] = description
    out["overrides"] = [{"id": o.id, "user_id": o.user_id, "user_name": names.get(o.user_id),
                         "starts_at": o.starts_at, "ends_at": o.ends_at, "reason": o.reason}
                        for o in schedule.overrides if o.ends_at > now]
    out["now"] = {"users": [{"id": u, "name": names.get(u)} for u in who.users],
                  "source": who.source, "layer": who.layer_name,
                  "until": oc.on_call_until(schedule, now)}
    out["used_by"] = await _referencing_policies(db, "schedule", schedule.id)
    return out


async def _load_one(db: AsyncSession, schedule_id) -> tuple[oc.Schedule, Optional[str]]:
    found = await oc.load_schedules(db, [schedule_id])
    s = found.get(str(schedule_id))
    if not s:
        raise HTTPException(404, "On-call schedule not found")
    desc = (await db.execute(text("SELECT description FROM oncall_schedules WHERE id = :id"),
                             {"id": schedule_id})).scalar()
    return s, desc


@router.get("/schedules")
async def list_schedules(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_supported(db)
    now = datetime.now(timezone.utc)
    schedules = await oc.load_schedules(db)
    descs = {str(r.id): r.description for r in (await db.execute(text(
        "SELECT id, description FROM oncall_schedules"))).fetchall()}
    return {"items": [await _schedule_payload(db, s, now=now, description=descs.get(s.id))
                      for s in schedules.values()]}


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleBody, db: AsyncSession = Depends(get_db),
                          user: User = Depends(can_manage)):
    await _require_supported(db)
    clean = await _clean(db, body)
    try:
        sid = str((await db.execute(text(
            "INSERT INTO oncall_schedules (name, description, timezone, enabled, created_by)"
            " VALUES (:name, :description, :timezone, :enabled, :uid) RETURNING id"
        ), {**{k: clean[k] for k in ("name", "description", "timezone", "enabled")},
            "uid": user.id})).scalar())
        await _save_layers(db, sid, clean["layers"])
        await write_audit_log(db, actor=user, action="oncall_schedule.create",
                              resource_type="oncall_schedule", resource_id=sid,
                              metadata={"name": clean["name"], "timezone": clean["timezone"],
                                        "layers": len(clean["layers"])})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "An on-call schedule with this name already exists") from exc
    s, desc = await _load_one(db, sid)
    return await _schedule_payload(db, s, now=datetime.now(timezone.utc), description=desc)


@router.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _require_supported(db)
    s, desc = await _load_one(db, schedule_id)
    return await _schedule_payload(db, s, now=datetime.now(timezone.utc), description=desc)


@router.put("/schedules/{schedule_id}")
async def update_schedule(schedule_id: UUID, body: ScheduleBody, db: AsyncSession = Depends(get_db),
                          user: User = Depends(can_manage)):
    await _require_supported(db)
    before, _ = await _load_one(db, schedule_id)
    clean = await _clean(db, body)
    try:
        await db.execute(text(
            "UPDATE oncall_schedules SET name = :name, description = :description, timezone = :timezone,"
            " enabled = :enabled, updated_at = NOW() WHERE id = :id"
        ), {**{k: clean[k] for k in ("name", "description", "timezone", "enabled")}, "id": schedule_id})
        await _save_layers(db, str(schedule_id), clean["layers"])
        await write_audit_log(db, actor=user, action="oncall_schedule.update",
                              resource_type="oncall_schedule", resource_id=str(schedule_id),
                              metadata={"before": oc.schedule_dict(before),
                                        "after": {k: clean[k] for k in ("name", "timezone", "enabled")},
                                        "layers": len(clean["layers"])})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "An on-call schedule with this name already exists") from exc
    s, desc = await _load_one(db, schedule_id)
    return await _schedule_payload(db, s, now=datetime.now(timezone.utc), description=desc)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(can_manage)):
    await _require_supported(db)
    s, _ = await _load_one(db, schedule_id)
    used = await _referencing_policies(db, "schedule", s.id)
    if used:
        raise HTTPException(409, "This schedule is used by escalation policies: " + ", ".join(used)
                            + ". Remove it from them first.")
    await db.execute(text("DELETE FROM oncall_schedules WHERE id = :id"), {"id": schedule_id})
    await write_audit_log(db, actor=user, action="oncall_schedule.delete", resource_type="oncall_schedule",
                          resource_id=str(schedule_id), metadata={"name": s.name})
    await db.commit()
    return {"deleted": True}


@router.get("/schedules/{schedule_id}/timeline")
async def schedule_timeline(
    schedule_id: UUID,
    from_time: Optional[datetime] = Query(default=None, alias="from"),
    to_time: Optional[datetime] = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Shifts between from and to (default: now to 14 days ahead; at most 62 days)."""
    await _require_supported(db)
    start = _aware("from", from_time) if from_time else datetime.now(timezone.utc)
    end = _aware("to", to_time) if to_time else start + timedelta(days=14)
    if end <= start:
        raise HTTPException(422, "'to' must be after 'from'")
    if end - start > timedelta(days=oc.MAX_TIMELINE_DAYS):
        raise HTTPException(422, f"The timeline covers at most {oc.MAX_TIMELINE_DAYS} days")
    found = await oc.load_schedules(db, [schedule_id], overrides_from=start)
    s = found.get(str(schedule_id))
    if not s:
        raise HTTPException(404, "On-call schedule not found")
    tl = oc.timeline(s, start, end)
    names = await oc.user_names(db, {sh["user_id"] for sh in tl["shifts"]})
    for sh in tl["shifts"]:
        sh["user_name"] = names.get(sh["user_id"])
    return {"schedule_id": s.id, "timezone": s.timezone, "from": start, "to": end, **tl}


@router.get("/schedules/{schedule_id}/now")
async def schedule_now(schedule_id: UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    await _require_supported(db)
    s, _ = await _load_one(db, schedule_id)
    now = datetime.now(timezone.utc)
    who = oc.on_call_at(s, now)
    names = await oc.user_names(db, who.users)
    return {"users": [{"id": u, "name": names.get(u)} for u in who.users], "source": who.source,
            "layer": who.layer_name, "until": oc.on_call_until(s, now), "at": now}


@router.post("/schedules/{schedule_id}/overrides", status_code=201)
async def create_override(schedule_id: UUID, body: OverrideBody, db: AsyncSession = Depends(get_db),
                          user: User = Depends(can_manage)):
    await _require_supported(db)
    s, _ = await _load_one(db, schedule_id)
    starts, ends = _aware("starts_at", body.starts_at), _aware("ends_at", body.ends_at)
    if ends <= starts:
        raise HTTPException(422, "ends_at must be after starts_at")
    if ends - starts > timedelta(days=90):
        raise HTTPException(422, "An override covers at most 90 days")
    target = (await db.execute(text("SELECT id, is_active FROM users WHERE id = :id"),
                               {"id": body.user_id})).first()
    if not target or not target.is_active:
        raise HTTPException(422, "The override must name an active user")
    oid = str((await db.execute(text(
        "INSERT INTO oncall_overrides (schedule_id, user_id, starts_at, ends_at, reason, created_by)"
        " VALUES (:sid, :uid, :s, :e, :reason, :by) RETURNING id"
    ), {"sid": schedule_id, "uid": body.user_id, "s": starts, "e": ends,
        "reason": (body.reason or "").strip() or None, "by": user.id})).scalar())
    await write_audit_log(db, actor=user, action="oncall_override.create", resource_type="oncall_schedule",
                          resource_id=str(schedule_id),
                          metadata={"override_id": oid, "schedule": s.name, "user_id": str(body.user_id),
                                    "starts_at": starts.isoformat(), "ends_at": ends.isoformat(),
                                    "reason": body.reason})
    await db.commit()
    return {"id": oid, "schedule_id": str(schedule_id), "user_id": str(body.user_id),
            "starts_at": starts, "ends_at": ends, "reason": (body.reason or "").strip() or None}


@router.delete("/schedules/{schedule_id}/overrides/{override_id}")
async def delete_override(schedule_id: UUID, override_id: UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(can_manage)):
    await _require_supported(db)
    row = (await db.execute(text(
        "DELETE FROM oncall_overrides WHERE id = :id AND schedule_id = :sid"
        " RETURNING user_id, starts_at, ends_at"), {"id": override_id, "sid": schedule_id})).first()
    if not row:
        raise HTTPException(404, "Override not found")
    await write_audit_log(db, actor=user, action="oncall_override.delete", resource_type="oncall_schedule",
                          resource_id=str(schedule_id),
                          metadata={"override_id": str(override_id), "user_id": str(row.user_id),
                                    "starts_at": row.starts_at.isoformat(), "ends_at": row.ends_at.isoformat()})
    await db.commit()
    return {"deleted": True}


@router.get("/coverage")
async def coverage(days: int = Query(default=7, ge=1, le=31), db: AsyncSession = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Who covers each priority, and where the matrix has holes: unrouted
    priorities, steps that reach nobody now, schedules with nobody on call in
    the next ``days`` days, members without a contact method."""
    await _require_supported(db)
    now = datetime.now(timezone.utc)
    defaults = await eps.default_policies(db)
    policies = await eps.load_policies(db)
    schedules = await oc.load_schedules(db)
    directory = eps.Directory(db)
    directory.schedules.update(schedules)
    all_targets = [t for p in policies.values() for st in p.steps for t in st.targets]
    all_targets += [t for p in policies.values() for t in p.fallback_targets]
    await directory.prime(all_targets, now)

    out_prio: dict[str, Any] = {}
    unrouted = []
    for p in prio.PRIORITIES:
        d = defaults.get(p)
        if not d:
            unrouted.append(p)
            out_prio[p] = {"policy_id": None, "policy_name": None, "steps_ok": False,
                           "gaps": [f"No escalation policy is the default for {prio.priority_label(p)};"
                                    f" only rules that name a policy or legacy levels escalate."]}
            continue
        policy = policies.get(d["id"])
        gaps, ok = [], True
        if policy:
            fb = directory.resolve(policy.fallback_targets, now)
            for i, st in enumerate(policy.steps):
                res = directory.resolve(st.targets, now)
                gaps += [f"Step {i + 1}: {g}" for g in res.gaps]
                if res.empty:
                    ok = False if fb.empty else ok
                    gaps.append(f"Step {i + 1} reaches nobody now"
                                + ("; the fallback route would be used." if not fb.empty
                                   else " and there is no working fallback."))
        out_prio[p] = {"policy_id": d["id"], "policy_name": d["name"], "steps_ok": ok,
                       "steps": len(policy.steps) if policy else 0, "gaps": gaps}

    without_members = [{"id": s.id, "name": s.name} for s in schedules.values()
                       if not any(lv.members for lv in s.layers)]
    schedule_gaps = []
    for s in schedules.values():
        if not s.enabled or not any(lv.members for lv in s.layers):
            continue
        for g in oc.timeline(s, now, now + timedelta(days=days))["gaps"][:20]:
            schedule_gaps.append({"schedule_id": s.id, "schedule_name": s.name,
                                  "start": g["start"], "end": g["end"]})
    referenced = {m for s in schedules.values() for lv in s.layers for m in lv.members}
    referenced |= {t["id"] for t in all_targets if t.get("type") == "user"}
    await directory.prime_people(referenced)
    no_contacts = []
    for uid in sorted(referenced):
        person = directory.people.get(uid)
        if person is None:
            no_contacts.append({"id": uid, "name": None, "reason": "User no longer exists"})
        elif not person.active:
            no_contacts.append({"id": uid, "name": person.name, "reason": "Inactive user"})
        elif not person.recipients("coverage"):
            no_contacts.append({"id": uid, "name": person.name, "reason": "No email or SMS contact method"})
    return {"at": now, "priorities": out_prio, "unrouted_priorities": unrouted,
            "schedules_without_members": without_members, "schedule_gaps": schedule_gaps,
            "users_without_contacts": no_contacts}
