"""Escalation policies API (1.24.17 escalation matrix).

Mounted with the ``escalation`` module policy: reads need ``alerts.view``,
changes need ``alerts.manage``. The dry runs are POSTs that only read and
declare their own ``alerts.view`` guard. Contact addresses in a dry run are
masked unless the caller can manage alerts or users.

Policy JSON::

    {id, name, description, enabled, default_priorities: ["P1"...],
     ack_target_minutes, repeat_minutes, max_repeats, review_minutes,
     ack_expiry_minutes, fallback_targets: [target],
     steps: [{position, delay_minutes, name, targets: [target]}], used_by_rules}

    target = {"type": "schedule" | "user" | "channel", "id": "<uuid>"}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.permissions import has_permission
from app.core.security import get_current_user, get_role_permissions, require_permission
from app.models.user import User
from app.services import alert_priority as prio
from app.services import escalation_policy_service as eps
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/escalation-policies", tags=["Escalation policies"])

can_view = require_permission("alerts.view")
can_manage = require_permission("alerts.manage")


class PolicyBody(BaseModel):
    """Validated by ``escalation_policy_service.clean_policy`` (one source of
    truth for the API and the recommended-policy builder)."""
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    enabled: bool = True
    default_priorities: list[str] = Field(default_factory=list)
    ack_target_minutes: Optional[int] = None
    repeat_minutes: Optional[int] = None
    max_repeats: Optional[int] = None
    review_minutes: Optional[int] = None
    ack_expiry_minutes: Optional[int] = None
    fallback_targets: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class PreviewBody(BaseModel):
    priority: Optional[str] = None
    at: Optional[datetime] = None


class DryRunBody(PreviewBody):
    policy: PolicyBody


class RecommendedBody(BaseModel):
    primary_schedule_id: Optional[UUID] = None
    backup_schedule_id: Optional[UUID] = None
    lead_user_id: Optional[UUID] = None
    noc_channel_ids: list[UUID] = Field(default_factory=list, max_length=10)
    team_channel_ids: list[UUID] = Field(default_factory=list, max_length=10)
    # Move P1..P4 defaults away from existing policies instead of refusing.
    replace_defaults: bool = False


async def _require_supported(db: AsyncSession) -> None:
    if not await eps.supported(db):
        raise HTTPException(503, "Escalation policies need database migration 164.")


async def _reveal(db: AsyncSession, user: User) -> bool:
    perms = await get_role_permissions(db, user.role)
    return has_permission(perms, "alerts.manage") or has_permission(perms, "users.manage")


async def _get(db: AsyncSession, policy_id) -> eps.Policy:
    found = await eps.load_policies(db, [policy_id])
    policy = found.get(str(policy_id))
    if not policy:
        raise HTTPException(404, "Escalation policy not found")
    return policy


async def _validated(db: AsyncSession, body: dict, *, exclude_id: Optional[str] = None) -> dict:
    try:
        cleaned = eps.clean_policy(body)
    except eps.PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    missing = await eps.missing_targets(db, eps.all_targets(cleaned))
    if missing:
        raise HTTPException(422, "Unknown escalation targets: " + ", ".join(missing))
    if cleaned["enabled"]:
        clash = await eps.priority_conflicts(db, cleaned["default_priorities"], exclude_id=exclude_id)
        if clash:
            raise HTTPException(409, "Each priority can have one default policy: " + "; ".join(
                f"{p} is already the default of {name}" for p, name in sorted(clash.items())))
    return cleaned


async def _payload(db: AsyncSession, policy_id) -> dict:
    policy = await _get(db, policy_id)
    counts = await eps.rule_counts(db)
    return eps.policy_dict(policy, used_by_rules=counts.get(policy.id, 0))


def _name_conflict(exc: IntegrityError) -> HTTPException:
    if "uq_escalation_policies_name" in str(exc.orig if hasattr(exc, "orig") else exc):
        return HTTPException(409, "An escalation policy with this name already exists")
    return HTTPException(409, "The escalation policy could not be saved")


@router.get("")
async def list_policies(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_supported(db)
    policies = await eps.load_policies(db)
    counts = await eps.rule_counts(db)
    return {"items": [eps.policy_dict(p, used_by_rules=counts.get(p.id, 0)) for p in policies.values()]}


@router.post("", status_code=201)
async def create_policy(body: PolicyBody, db: AsyncSession = Depends(get_db),
                        user: User = Depends(can_manage)):
    await _require_supported(db)
    cleaned = await _validated(db, body.model_dump())
    try:
        pid = await eps.save_policy(db, cleaned, user_id=user.id)
        await write_audit_log(db, actor=user, action="escalation_policy.create",
                              resource_type="escalation_policy", resource_id=pid,
                              metadata={"name": cleaned["name"], "policy": cleaned})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_conflict(exc) from exc
    return await _payload(db, pid)


# Fixed paths precede /{policy_id}.

def _recommended(body: RecommendedBody) -> list[dict]:
    """The field-report matrix as four policies (P1..P4) from the caller's
    schedules, lead and channels. Steps without a target are left out."""
    def sched(i):
        return [{"type": "schedule", "id": str(i)}] if i else []

    def chans(ids):
        return [{"type": "channel", "id": str(i)} for i in ids]

    primary = sched(body.primary_schedule_id)
    backup = sched(body.backup_schedule_id)
    lead = [{"type": "user", "id": str(body.lead_user_id)}] if body.lead_user_id else []
    noc, team = chans(body.noc_channel_ids), chans(body.team_channel_ids)
    fallback = noc + lead or team

    def steps(*items):
        return [{"delay_minutes": d, "name": n, "targets": t} for d, n, t in items if t]

    return [
        {"name": "P1 Critical — major incident", "default_priorities": ["P1"],
         "description": "Confirmed major outage or severe service impact. Primary on-call plus NOC at once; "
                        "backup at 5 min, incident lead at 15 min, major-incident escalation at 30 min; "
                        "updates every 15 min while unresolved.",
         "ack_target_minutes": 5, "repeat_minutes": 5, "max_repeats": 2, "review_minutes": 15,
         "ack_expiry_minutes": 60, "fallback_targets": fallback,
         "steps": steps((0, "Primary on-call and NOC", primary + noc), (5, "Backup on-call", backup),
                        (15, "Incident lead", lead),
                        (30, "Major-incident policy", noc + team + lead))},
        {"name": "P2 High — service degradation", "default_priorities": ["P2"],
         "description": "Persistent degradation or loss of redundancy. Owning team and NOC; backup at 15 min, "
                        "team lead at 30 min; unresolved updates every 30 min.",
         "ack_target_minutes": 15, "repeat_minutes": 15, "max_repeats": 1, "review_minutes": 30,
         "ack_expiry_minutes": 240, "fallback_targets": fallback,
         "steps": steps((0, "Owning team and NOC", primary + team + noc), (15, "Backup on-call", backup),
                        (30, "Team lead", lead))},
        {"name": "P3 Warning — team queue", "default_priorities": ["P3"],
         "description": "Sustained threshold or localised non-critical errors. Owning team queue (email or "
                        "grouped digest); team lead if still unowned after 4 hours.",
         "ack_target_minutes": 60, "repeat_minutes": None, "max_repeats": 0, "review_minutes": None,
         "ack_expiry_minutes": None, "fallback_targets": team or noc,
         "steps": steps((0, "Owning team queue", team or primary), (240, "Team lead", lead))},
        {"name": "P4 Information — audit only", "default_priorities": ["P4"],
         "description": "Recovery, expected reboot or routine change: recorded on the timeline and in "
                        "digests; no paging escalation.",
         "ack_target_minutes": None, "repeat_minutes": None, "max_repeats": 0, "review_minutes": None,
         "ack_expiry_minutes": None, "fallback_targets": [], "steps": []},
    ]


@router.post("/recommended", status_code=201)
async def create_recommended(body: RecommendedBody, db: AsyncSession = Depends(get_db),
                             user: User = Depends(can_manage)):
    """Create the four report-matrix policies as the P1..P4 defaults."""
    await _require_supported(db)
    if not (body.primary_schedule_id or body.noc_channel_ids or body.team_channel_ids):
        raise HTTPException(422, "Choose a primary on-call schedule or at least one NOC or team channel.")
    drafts = _recommended(body)
    cleaned_all = []
    for d in drafts:
        try:
            cleaned = eps.clean_policy(d)
        except eps.PolicyError as exc:
            raise HTTPException(422, f"{d['name']}: {exc}") from exc
        cleaned_all.append(cleaned)
    targets = [t for c in cleaned_all for t in eps.all_targets(c)]
    missing = await eps.missing_targets(db, targets)
    if missing:
        raise HTTPException(422, "Unknown escalation targets: " + ", ".join(missing))
    clash = await eps.priority_conflicts(db, list(prio.PRIORITIES))
    if clash and not body.replace_defaults:
        raise HTTPException(409, "Existing policies are already the default for: " + "; ".join(
            f"{p} ({name})" for p, name in sorted(clash.items()))
            + ". Send replace_defaults=true to move those defaults to the new policies.")
    taken = {r[0].lower() for r in (await db.execute(text(
        "SELECT name FROM escalation_policies"))).fetchall()}
    ids = []
    try:
        if clash:
            await db.execute(text(
                "UPDATE escalation_policies SET default_priorities = ARRAY(SELECT x FROM"
                " unnest(default_priorities) x WHERE x <> ALL(CAST(:p AS varchar[]))), updated_at = NOW()"
                " WHERE default_priorities && CAST(:p AS varchar[])"), {"p": list(prio.PRIORITIES)})
        for cleaned in cleaned_all:
            name, n = cleaned["name"], 2
            while name.lower() in taken:
                name = f"{cleaned['name']} ({n})"
                n += 1
            taken.add(name.lower())
            cleaned["name"] = name
            ids.append(await eps.save_policy(db, cleaned, user_id=user.id))
        await write_audit_log(db, actor=user, action="escalation_policy.recommended",
                              resource_type="escalation_policy", resource_id=",".join(ids),
                              metadata={"policies": [c["name"] for c in cleaned_all],
                                        "replaced_defaults": clash})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_conflict(exc) from exc
    counts = await eps.rule_counts(db)
    policies = await eps.load_policies(db, ids)
    return {"items": [eps.policy_dict(policies[i], used_by_rules=counts.get(i, 0)) for i in ids]}


@router.post("/dry-run")
async def dry_run_draft(body: DryRunBody, db: AsyncSession = Depends(get_db),
                        user: User = Depends(can_view)):
    """Preview an unsaved policy (the editor's live panel). Never sends."""
    await _require_supported(db)
    try:
        cleaned = eps.clean_policy(body.policy.model_dump())
    except eps.PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    policy = eps.policy_from_dict({**cleaned, "id": "draft"})
    return await _preview(db, user, policy, body)


async def _preview(db: AsyncSession, user: User, policy: eps.Policy, body: PreviewBody) -> dict:
    priority = prio.normalize_priority(body.priority) if body.priority else None
    if body.priority and not priority:
        raise HTTPException(422, "priority must be P1, P2, P3 or P4")
    if body.at is not None and body.at.tzinfo is None:
        raise HTTPException(422, "'at' must include a timezone offset")
    priority = priority or (policy.default_priorities[0] if policy.default_priorities else "P3")
    at = body.at or datetime.now(timezone.utc)
    return await eps.preview(db, policy, priority=priority, at=at, reveal=await _reveal(db, user))


@router.get("/{policy_id}")
async def get_policy(policy_id: UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    await _require_supported(db)
    return await _payload(db, policy_id)


@router.put("/{policy_id}")
async def update_policy(policy_id: UUID, body: PolicyBody, db: AsyncSession = Depends(get_db),
                        user: User = Depends(can_manage)):
    await _require_supported(db)
    before = eps.policy_dict(await _get(db, policy_id))
    cleaned = await _validated(db, body.model_dump(), exclude_id=str(policy_id))
    try:
        await eps.save_policy(db, cleaned, policy_id=str(policy_id))
        await write_audit_log(db, actor=user, action="escalation_policy.update",
                              resource_type="escalation_policy", resource_id=str(policy_id),
                              metadata={"name": cleaned["name"], "before": before, "after": cleaned})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_conflict(exc) from exc
    except LookupError:
        await db.rollback()
        raise HTTPException(404, "Escalation policy not found")
    return await _payload(db, policy_id)


@router.delete("/{policy_id}")
async def delete_policy(policy_id: UUID, db: AsyncSession = Depends(get_db),
                        user: User = Depends(can_manage)):
    """Delete a policy. Rules that named it fall back to the priority default
    (or their legacy levels); open incidents it was escalating stop at their
    current step and still get the all-clear on resolution."""
    await _require_supported(db)
    policy = await _get(db, policy_id)
    used = (await eps.rule_counts(db)).get(policy.id, 0)
    await db.execute(text("DELETE FROM escalation_policies WHERE id = :id"), {"id": policy_id})
    await write_audit_log(db, actor=user, action="escalation_policy.delete",
                          resource_type="escalation_policy", resource_id=str(policy_id),
                          metadata={"name": policy.name, "rules_unlinked": used})
    await db.commit()
    return {"deleted": True, "rules_unlinked": used}


@router.post("/{policy_id}/preview")
async def preview_policy(policy_id: UUID, body: Optional[PreviewBody] = None,
                         db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    """Dry run of a saved policy: who each step reaches at its due time,
    coverage gaps and the fallback. Never sends."""
    await _require_supported(db)
    return await _preview(db, user, await _get(db, policy_id), body or PreviewBody())
