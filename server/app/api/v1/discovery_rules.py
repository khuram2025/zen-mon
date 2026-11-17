"""Discovery rules API (``/api/v1/discovery-v2/rules``).

Reading rules, the catalog, previews and history needs ``discovery.view``.
Creating, editing, ordering and changing rules or engine settings needs
``discovery.manage_rules``; activating a rule also needs every permission its
actions use (``devices.manage`` for device settings, ``servers.manage`` to
import servers). Rules are fleet-wide configuration: a user whose view is
limited by tags cannot change them (the stance fleet-wide alert rules take).
Re-evaluating a past scan needs ``discovery.run`` (it can import).

Every change is audit logged. See ``docs/discovery-rule-engine.md``.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import SUPERUSER_PERMISSION
from app.core.security import get_role_permissions, require_permission
from app.models.discovery_rules import (
    DiscoveryEngineRule,
    DiscoveryEngineRuleVersion,
    DiscoveryEngineRun,
    DiscoveryEngineRunItem,
)
from app.models.discovery_v2 import DiscoveryProfile, DiscoveryRun
from app.models.user import User
from app.schemas.discovery_rules import (
    EngineSettings,
    EvaluateScanRequest,
    ImportRequest,
    StarterRequest,
    InventoryApplyRequest,
    InventoryRunRequest,
    PreviewRequest,
    RuleCreate,
    RuleModeRequest,
    RuleOut,
    RuleReorderRequest,
    RuleUpdate,
    RuleVersionOut,
)
from app.services import tag_service
from app.services.audit_service import write_audit_log
from app.services.discovery_rules import actions as rule_actions
from app.services.discovery_rules import conditions, inventory, portable, reconcile, runner
from app.services.discovery_rules.facts import ENUM_VALUES, FIELD_SOURCES, FIELDS, REFERENCE_SOURCES

router = APIRouter(prefix="/discovery-v2/rules", tags=["Discovery rules"])

FLEET_DETAIL = ("Discovery rules apply to the whole network; users whose view is limited by tags "
                "cannot change them.")

_REF_TABLES = {
    "device_groups": "device_groups",
    "device_profiles": "device_profiles",
    "snmp_credentials": "snmp_credentials",
    "discovery_profiles": "discovery_profiles",
    "devices": "devices",
    "manual_maps": "manual_maps",
}


async def _fleet_guard(db: AsyncSession, user: User) -> None:
    if await scoping.visible_tags(db, user) is not None:
        raise HTTPException(403, FLEET_DETAIL)


def _out(row: DiscoveryEngineRule) -> RuleOut:
    return RuleOut.model_validate(row, from_attributes=True)


def _snapshot(row: DiscoveryEngineRule) -> dict:
    return {
        "name": row.name, "description": row.description, "priority": row.priority, "mode": row.mode,
        "asset_kinds": list(row.asset_kinds or []), "profile_ids": [str(p) for p in row.profile_ids or []] or None,
        "condition": row.condition, "actions": row.actions, "stop_processing": row.stop_processing,
        "triggers": list(row.triggers or []), "enforcement": row.enforcement, "on_unmatch": row.on_unmatch,
    }


async def _exists(db: AsyncSession, source: str, ids: list[str]) -> list[str]:
    """Ids among ``ids`` that do not exist in the source table."""
    if not ids:
        return []
    table = _REF_TABLES[source]
    # A rule sets a device's own group, which must be a static group (migrate-192).
    extra = " AND kind = 'static'" if source == "device_groups" else ""
    found = (await db.execute(text(f"SELECT id::text FROM {table} WHERE id = ANY(CAST(:ids AS uuid[])){extra}"),
                              {"ids": ids})).scalars().all()
    return [i for i in ids if i not in set(found)]


def _leaf_refs(node: dict, out: list) -> None:
    if "op" in node:
        for c in node.get("conditions") or []:
            _leaf_refs(c, out)
        return
    src = REFERENCE_SOURCES.get(node["field"])
    if src and node.get("value") is not None:
        vals = node["value"] if isinstance(node["value"], list) else [node["value"]]
        out.extend((src, str(v)) for v in vals)


async def _normalise(db: AsyncSession, payload) -> tuple[dict, list[dict], list[str] | None]:
    """Validated condition, actions and profile ids, or 422 naming the problem."""
    try:
        condition = conditions.validate(payload.condition)
        acts = rule_actions.validate_actions(payload.actions, list(payload.asset_kinds))
    except (conditions.ConditionError, rule_actions.ActionError) as e:
        raise HTTPException(422, str(e)) from None
    refs: list[tuple[str, str]] = []
    _leaf_refs(condition, refs)
    for a in acts:
        spec = rule_actions.ACTIONS[a["key"]]
        if spec.value == "ref":
            refs.append((spec.source, a["value"]))
    profile_ids = [str(p) for p in payload.profile_ids] if payload.profile_ids else None
    for pid in profile_ids or []:
        refs.append(("discovery_profiles", pid))
    by_source: dict[str, list[str]] = {}
    for src, rid in refs:
        by_source.setdefault(src, []).append(rid)
    for src, ids in by_source.items():
        try:
            missing = await _exists(db, src, sorted(set(ids)))
        except Exception:
            missing = ids
        if missing:
            raise HTTPException(422, f"Unknown {src.replace('_', ' ')[:-1]}: {missing[0]}")
    for a in acts:
        if a["key"] == "tags":
            a["value"] = await tag_service.canonicalize_tags(db, a["value"])
        if a["key"] == "device_type":
            from app.schemas.snmp import BUILTIN_DEVICE_TYPES
            known = a["value"] in BUILTIN_DEVICE_TYPES or (await db.execute(
                text("SELECT 1 FROM device_types WHERE key = :k"), {"k": a["value"]})).first() is not None
            if not known:
                raise HTTPException(422, f"Unknown device type '{a['value']}'")
    return condition, acts, profile_ids


async def _check_activation(db: AsyncSession, user: User, acts: list[dict], kinds: list[str],
                            verb: str = "Activating this rule") -> None:
    perms = set(await get_role_permissions(db, user.role))
    if SUPERUSER_PERMISSION in perms:
        return
    missing = sorted(rule_actions.permissions_needed(acts, kinds) - perms)
    if missing:
        raise HTTPException(403, f"{verb} needs the permissions its actions use: " + ", ".join(missing))


async def _record_version(db: AsyncSession, row: DiscoveryEngineRule, user: User, reason: str) -> None:
    db.add(DiscoveryEngineRuleVersion(rule_id=row.id, revision=row.revision, snapshot=_snapshot(row),
                                      changed_by=user.id, reason=reason))


async def _get_rule(db: AsyncSession, rule_id: uuid.UUID) -> DiscoveryEngineRule:
    row = await db.get(DiscoveryEngineRule, rule_id)
    if not row:
        raise HTTPException(404, "Rule not found")
    return row


async def _name_taken(db: AsyncSession, name: str, exclude: uuid.UUID | None = None) -> bool:
    q = select(DiscoveryEngineRule.id).where(func.lower(DiscoveryEngineRule.name) == name.strip().lower())
    if exclude:
        q = q.where(DiscoveryEngineRule.id != exclude)
    return (await db.execute(q)).first() is not None


# ── catalog and settings ────────────────────────────────────────────────────

@router.get("/catalog")
async def catalog(_user: User = Depends(require_permission("discovery.view"))):
    """Fields with their operators, actions and limits; the rule builder is drawn from it."""
    fields = []
    for key, (ftype, label, help_) in FIELDS.items():
        fields.append({
            "key": key, "type": ftype, "label": label, "help": help_,
            "operators": [{"key": op, "label": lbl, "value": kind}
                          for op, (lbl, kind) in conditions.OPERATORS[ftype].items()],
            "values": ENUM_VALUES.get(key), "source": REFERENCE_SOURCES.get(key),
            "applies_to": FIELD_SOURCES.get(key, []),
        })
    return {
        "fields": fields,
        "groups": [{"key": "all", "label": "All of"}, {"key": "any", "label": "Any of"},
                   {"key": "none", "label": "None of"}],
        "actions": rule_actions.catalog(),
        "decisions": list(rule_actions.DECISIONS),
        "import_as": list(rule_actions.IMPORT_AS),
        "device_types": list(rule_actions.DEVICE_TYPES),
        "modes": [{"key": "draft", "label": "Draft"}, {"key": "simulate", "label": "Simulating"},
                  {"key": "active", "label": "Active"}, {"key": "disabled", "label": "Disabled"}],
        "triggers": [{"key": "discovery", "label": "Discovery scan results"},
                     {"key": "created", "label": "Devices and servers when they are added"},
                     {"key": "changed", "label": "Devices and servers whenever they change (and when the rule changes)"}],
        "on_unmatch": [{"key": "keep", "label": "Keep the values the rule set"},
                       {"key": "revert", "label": "Revert them to what they were (if unchanged since)"}],
        "asset_kinds": [{"key": "device", "label": "Network devices"}, {"key": "server", "label": "Servers"},
                        {"key": "child", "label": "Controller-managed switches and access points"}],
        "enforcement": [{"key": "set_once", "label": "Set once (keep later hand edits)"},
                        {"key": "enforce", "label": "Enforce (put the rule's value back)"}],
        "limits": {"max_depth": conditions.MAX_DEPTH, "max_leaves": conditions.MAX_LEAVES,
                   "max_regex": conditions.MAX_REGEX},
    }


@router.get("/settings", response_model=EngineSettings)
async def get_settings(db: AsyncSession = Depends(get_db),
                       _user: User = Depends(require_permission("discovery.view"))):
    return EngineSettings(**await runner.load_settings(db))


@router.put("/settings", response_model=EngineSettings)
async def put_settings(payload: EngineSettings, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("discovery.manage_rules"))):
    await _fleet_guard(db, user)
    before = await runner.load_settings(db)
    after = await runner.save_settings(db, payload.model_dump(), user.id)
    await write_audit_log(db, actor=user, action="discovery.rules.settings", resource_type="discovery_rules",
                          resource_id=None, metadata={"before": before, "after": after})
    await db.commit()
    return EngineSettings(**after)


# ── list / create / reorder ─────────────────────────────────────────────────

@router.get("", response_model=list[RuleOut])
async def list_rules(db: AsyncSession = Depends(get_db),
                     _user: User = Depends(require_permission("discovery.view"))):
    rows = (await db.execute(select(DiscoveryEngineRule).order_by(DiscoveryEngineRule.priority))).scalars().all()
    return [_out(r) for r in rows]


async def _create(db: AsyncSession, payload: RuleCreate, user: User, *, reason: str = "created",
                  builtin_key: str | None = None) -> DiscoveryEngineRule:
    """Validate and insert one rule with its first version (no commit)."""
    condition, acts, profile_ids = await _normalise(db, payload)
    if payload.mode == "active":
        await _check_activation(db, user, acts, list(payload.asset_kinds))
    edge = (await db.execute(select(func.min(DiscoveryEngineRule.priority) if payload.position == "first"
                                    else func.max(DiscoveryEngineRule.priority)))).scalar()
    priority = 10 if edge is None else (edge - 10 if payload.position == "first" else edge + 10)
    now = datetime.now(timezone.utc)
    row = DiscoveryEngineRule(
        name=payload.name.strip(), description=payload.description, priority=priority, mode=payload.mode,
        asset_kinds=list(payload.asset_kinds), triggers=list(payload.triggers), profile_ids=profile_ids,
        condition=condition, actions=acts, stop_processing=payload.stop_processing, revision=1,
        enforcement=payload.enforcement, on_unmatch=payload.on_unmatch, builtin_key=builtin_key,
        created_by=user.id, updated_by=user.id, created_at=now, updated_at=now,
        activated_by=user.id if payload.mode == "active" else None,
        activated_at=now if payload.mode == "active" else None,
    )
    db.add(row)
    await db.flush()
    await _record_version(db, row, user, reason)
    return row


@router.post("", response_model=RuleOut, status_code=201)
async def create_rule(payload: RuleCreate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("discovery.manage_rules"))):
    await _fleet_guard(db, user)
    if await _name_taken(db, payload.name):
        raise HTTPException(409, "A rule with this name already exists")
    row = await _create(db, payload, user)
    await write_audit_log(db, actor=user, action="discovery.rules.create", resource_type="discovery_rule",
                          resource_id=str(row.id), metadata={"name": row.name, "mode": row.mode,
                                                             "priority": row.priority})
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/reorder", response_model=list[RuleOut])
async def reorder_rules(payload: RuleReorderRequest, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("discovery.manage_rules"))):
    """Set the evaluation order: ``ids`` must list every rule exactly once."""
    await _fleet_guard(db, user)
    rows = (await db.execute(select(DiscoveryEngineRule).with_for_update())).scalars().all()
    by_id = {r.id: r for r in rows}
    if len(payload.ids) != len(set(payload.ids)) or set(payload.ids) != set(by_id):
        raise HTTPException(422, "List every rule exactly once")
    await db.execute(text("SET CONSTRAINTS discovery_engine_rules_priority_key DEFERRED"))
    before = [str(r.id) for r in sorted(rows, key=lambda r: r.priority)]
    for i, rid in enumerate(payload.ids):
        by_id[rid].priority = (i + 1) * 10
    await write_audit_log(db, actor=user, action="discovery.rules.reorder", resource_type="discovery_rules",
                          resource_id=None, metadata={"before": before, "after": [str(i) for i in payload.ids]})
    await db.commit()
    rows = (await db.execute(select(DiscoveryEngineRule).order_by(DiscoveryEngineRule.priority))).scalars().all()
    return [_out(r) for r in rows]


# ── preview, re-evaluation, history ─────────────────────────────────────────

@router.post("/preview")
async def preview(payload: PreviewRequest, db: AsyncSession = Depends(get_db),
                  _user: User = Depends(require_permission("discovery.view"))):
    """What the rules would do to a past scan (or to hand-typed facts). Writes
    nothing. An unsaved rule from the editor takes its place among the active
    rules (``scope=with_active``) or is evaluated alone (``only``)."""
    if payload.discovery_run_id is None and payload.facts is None:
        raise HTTPException(422, "Choose a scan or enter sample facts")
    rules: list[dict] = []
    if payload.scope == "with_active" or payload.rule is None:
        rules = await runner.load_rules(db)
    if payload.rule is not None:
        draft = payload.rule
        try:
            condition = conditions.validate(draft.condition)
            acts = rule_actions.validate_actions(draft.actions, list(draft.asset_kinds))
        except (conditions.ConditionError, rule_actions.ActionError) as e:
            raise HTTPException(422, str(e)) from None
        draft_id = str(draft.id) if draft.id else "00000000-0000-0000-0000-000000000000"
        priority = draft.priority
        if priority is None and draft.id:
            saved = await db.get(DiscoveryEngineRule, draft.id)
            priority = saved.priority if saved else None
        rules = [r for r in rules if r["id"] != draft_id]
        if priority is None:
            priority = max([r["priority"] for r in rules], default=0) + 10
        elif any(r["priority"] == priority for r in rules):
            priority = priority - 0.5  # just above the rule that holds this place
        rules.append({
            "id": draft_id, "name": draft.name, "priority": priority, "revision": None, "mode": "draft",
            "asset_kinds": list(draft.asset_kinds), "triggers": ["discovery"],
            "profile_ids": [str(p) for p in draft.profile_ids] if draft.profile_ids else None,
            "condition": condition, "actions": acts, "stop_processing": draft.stop_processing,
            "enforcement": draft.enforcement,
        })
    try:
        return await runner.preview(db, rules, discovery_run_id=payload.discovery_run_id, facts=payload.facts,
                                    import_mode=payload.import_mode, limit=payload.limit)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.post("/evaluate-scan/{discovery_run_id}")
async def evaluate_scan(discovery_run_id: uuid.UUID, payload: EvaluateScanRequest,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("discovery.run"))):
    """Run the active rules on a finished scan again: store each result's plan
    and, with ``apply``, auto-import / ignore as the profile allows."""
    settings = await runner.load_settings(db)
    if not settings["enabled"]:
        raise HTTPException(409, "Discovery rules are turned off (Discovery → Rules → Settings)")
    run = await db.get(DiscoveryRun, discovery_run_id)
    if not run:
        raise HTTPException(404, "Scan not found")
    if run.status in ("queued", "running"):
        raise HTTPException(409, "The scan is still running")
    summary = await runner.evaluate_discovery_run(db, discovery_run_id, trigger="manual", actor_id=user.id,
                                                  apply=payload.apply)
    await write_audit_log(db, actor=user, action="discovery.rules.evaluate", resource_type="discovery_run",
                          resource_id=str(discovery_run_id), metadata={"apply": payload.apply, **summary})
    await db.commit()
    return summary


@router.get("/history")
async def history(limit: int = Query(default=50, ge=1, le=500), discovery_run_id: Optional[uuid.UUID] = None,
                  db: AsyncSession = Depends(get_db),
                  _user: User = Depends(require_permission("discovery.view"))):
    q = select(DiscoveryEngineRun).order_by(DiscoveryEngineRun.started_at.desc()).limit(limit)
    if discovery_run_id:
        q = q.where(DiscoveryEngineRun.discovery_run_id == discovery_run_id)
    rows = (await db.execute(q)).scalars().all()
    names: dict = {}
    run_ids = [r.discovery_run_id for r in rows if r.discovery_run_id]
    if run_ids:
        for rid, pname in (await db.execute(
                select(DiscoveryRun.id, DiscoveryProfile.name)
                .join(DiscoveryProfile, DiscoveryProfile.id == DiscoveryRun.profile_id)
                .where(DiscoveryRun.id.in_(run_ids)))).all():
            names[rid] = pname
    return [{
        "id": str(r.id), "trigger": r.trigger, "mode": r.mode, "status": r.status,
        "discovery_run_id": str(r.discovery_run_id) if r.discovery_run_id else None,
        "profile_name": names.get(r.discovery_run_id), "rules_revision": r.rules_revision,
        "started_by": str(r.started_by) if r.started_by else None,
        "started_at": r.started_at, "finished_at": r.finished_at, "counts": r.counts or {},
        "detail": r.detail, "approved_by": str(r.approved_by) if r.approved_by else None,
        "approved_at": r.approved_at,
    } for r in rows]


@router.get("/history/{engine_run_id}/items")
async def history_items(engine_run_id: uuid.UUID, outcome: Optional[str] = None,
                        rule_id: Optional[uuid.UUID] = None,
                        limit: int = Query(default=500, ge=1, le=5000), offset: int = Query(default=0, ge=0),
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("discovery.view"))):
    if not await db.get(DiscoveryEngineRun, engine_run_id):
        raise HTTPException(404, "Run not found")
    q = (select(DiscoveryEngineRunItem).where(DiscoveryEngineRunItem.run_id == engine_run_id)
         .order_by(DiscoveryEngineRunItem.id).limit(limit).offset(offset))
    if outcome:
        q = q.where(DiscoveryEngineRunItem.outcome == outcome)
    if rule_id:
        q = q.where(DiscoveryEngineRunItem.rule_id == rule_id)
    rows = (await db.execute(q)).scalars().all()
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        rows = await _visible_items(db, rows, scope)
    return [{
        "id": r.id, "asset_kind": r.asset_kind, "asset_id": str(r.asset_id) if r.asset_id else None,
        "discovery_result_id": r.discovery_result_id,
        "ip_address": str(r.ip_address) if r.ip_address else None,
        "rule_id": str(r.rule_id) if r.rule_id else None, "rule_name": r.rule_name,
        "rule_revision": r.rule_revision, "action_key": r.action_key, "before": r.before, "after": r.after,
        "outcome": r.outcome, "detail": r.detail, "created_at": r.created_at,
    } for r in rows]


async def _visible_items(db: AsyncSession, rows, scope: list[str]):
    """Items about devices/servers the viewer cannot see are left out; scan
    results (discovery is not tag-scoped) stay."""
    out, tags = [], {}
    for kind, table in (("device", "devices"), ("server", "servers")):
        ids = [str(r.asset_id) for r in rows if r.asset_kind == kind and r.asset_id]
        if ids:
            for aid, t in (await db.execute(text(
                    f"SELECT id::text, tags FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})).all():
                tags[(kind, aid)] = t
    for r in rows:
        if r.asset_kind in ("device", "server") and r.asset_id:
            if not scoping.entity_visible(tags.get((r.asset_kind, str(r.asset_id))), scope):
                continue
        out.append(r)
    return out


# ── inventory: manual runs, undo, explain, conflicts (phase 2) ──────────────

async def _run_rules(db: AsyncSession, rule_ids) -> list[dict]:
    rules = await runner.load_rules(db)
    if rule_ids:
        wanted = {str(i) for i in rule_ids}
        missing = wanted - {r["id"] for r in rules}
        if missing:
            raise HTTPException(422, "Only active rules can run on inventory; not active: " + ", ".join(sorted(missing)))
        rules = [r for r in rules if r["id"] in wanted]
    if not rules:
        raise HTTPException(422, "There are no active rules to run")
    return rules


def _targets(payload) -> dict:
    t = payload.targets.model_dump()
    if t.get("subnet"):
        try:
            ipaddress.ip_network(t["subnet"], strict=False)
        except ValueError:
            raise HTTPException(422, f"Not a subnet: {t['subnet']}") from None
    return t


async def _check_run(db: AsyncSession, user: User, rules: list[dict], targets: dict) -> None:
    kinds = list(targets.get("kinds") or ["device", "server"])
    acts = [a for r in rules for a in r["actions"] if a["key"] != "decision"]
    await _check_activation(db, user, acts, kinds, verb="Running these rules on inventory")


@router.post("/inventory/preview")
async def inventory_preview(payload: InventoryRunRequest, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("discovery.manage_rules"))):
    """What the active rules (or the chosen ones) would change on the devices
    and servers the filter selects. Writes nothing; ``digest`` identifies this
    exact set of changes for ``/inventory/apply``."""
    await _fleet_guard(db, user)
    rules = await _run_rules(db, payload.rule_ids)
    return await inventory.preview_inventory(db, rules, _targets(payload))


@router.post("/inventory/apply")
async def inventory_apply(payload: InventoryApplyRequest, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("discovery.manage_rules"))):
    """Apply exactly the previewed changes. 409 with the new preview when the
    inventory or the rules changed since (preview again)."""
    await _fleet_guard(db, user)
    rules = await _run_rules(db, payload.rule_ids)
    targets = _targets(payload)
    await _check_run(db, user, rules, targets)
    try:
        out = await inventory.apply_inventory(db, rules, targets, expected_digest=payload.digest, actor_id=user.id)
    except inventory.PreviewStale as e:
        await db.rollback()
        raise HTTPException(409, {"code": "preview_stale",
                                  "message": "The inventory or the rules changed since the preview; preview again",
                                  "summary": e.preview["summary"], "digest": e.preview["digest"]}) from None
    await write_audit_log(db, actor=user, action="discovery.rules.run", resource_type="discovery_rules",
                          resource_id=out["engine_run_id"],
                          metadata={"rule_ids": [r["id"] for r in rules], "targets": inventory._jsonable(targets),
                                    "applied": out.get("applied"), "kept_manual_edit": out.get("kept_manual_edit"),
                                    "skipped_changed": out.get("skipped_changed")})
    await db.commit()
    return out


@router.post("/history/{engine_run_id}/undo")
async def undo(engine_run_id: uuid.UUID, db: AsyncSession = Depends(get_db),
               user: User = Depends(require_permission("discovery.manage_rules"))):
    """Restore every value the run set that still equals what it set; values
    changed since are listed and left alone. Devices a scan imported are
    listed for a deliberate delete and never deleted here."""
    await _fleet_guard(db, user)
    kinds = [k for (k,) in (await db.execute(text(
        "SELECT DISTINCT asset_kind FROM discovery_engine_run_items WHERE run_id = :r AND outcome = 'applied'"
        " AND asset_kind IN ('device', 'server', 'child')"), {"r": engine_run_id})).all()]
    perms = set(await get_role_permissions(db, user.role))
    need = {"device": "devices.manage", "child": "devices.manage", "server": "servers.manage"}
    missing = sorted(need[k] for k in kinds if need[k] not in perms)
    if missing and SUPERUSER_PERMISSION not in perms:
        raise HTTPException(403, "Undoing this run needs: " + ", ".join(missing))
    try:
        out = await inventory.undo_run(db, engine_run_id, actor_id=user.id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except inventory.UndoRefused as e:
        raise HTTPException(409, str(e)) from None
    await write_audit_log(db, actor=user, action="discovery.rules.undo", resource_type="discovery_engine_run",
                          resource_id=str(engine_run_id),
                          metadata={k: v for k, v in out.items() if k != "imported_devices"}
                          | {"imported_devices": len(out["imported_devices"])})
    await db.commit()
    return out


@router.get("/assets/{kind}/{asset_id}")
async def explain_asset(kind: str, asset_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("discovery.view", "devices.view", "servers.view"))):
    """Which rules set what on this device or server, hand edits the rules
    kept, rules that match it now and what they would still change."""
    if kind not in ("device", "server", "child"):
        raise HTTPException(404, "Unknown asset kind")
    table = "servers" if kind == "server" else "devices"
    row = (await db.execute(text(f"SELECT tags FROM {table} WHERE id = :id"), {"id": asset_id})).first()
    if row is None or not scoping.entity_visible(row[0], await scoping.visible_tags(db, user)):
        raise HTTPException(404, "Not found")
    out = await inventory.explain(db, kind, asset_id, await runner.load_rules(db))
    if out is None:
        raise HTTPException(404, "Not found")
    out["engine_enabled"] = (await runner.load_settings(db))["enabled"]
    return out


@router.get("/conflicts")
async def rule_conflicts(db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("discovery.view"))):
    """Over the current inventory: matches and wins per active rule, which
    rules override which, rules that match nothing or never win."""
    rules = await runner.load_rules(db)
    if not rules:
        return {"assets": 0, "truncated": False, "rules": [], "overridden": [], "never_match": [], "never_win": []}
    return await inventory.conflicts(db, rules)


# ── phase 3: approval, export/import, starter rules ─────────────────────────

async def _check_kinds(db: AsyncSession, user: User, kinds, verb: str) -> None:
    perms = set(await get_role_permissions(db, user.role))
    if SUPERUSER_PERMISSION in perms:
        return
    need = {"device": "devices.manage", "child": "devices.manage", "server": "servers.manage"}
    missing = sorted({need[k] for k in kinds if k in need and need[k] not in perms})
    if missing:
        raise HTTPException(403, f"{verb} needs: " + ", ".join(missing))


@router.get("/history/{engine_run_id}/pending")
async def pending_changes(engine_run_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("discovery.view"))):
    """The changes an automatic run is holding for approval."""
    run = await db.get(DiscoveryEngineRun, engine_run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    plan = run.plan or {}
    changes = list(plan.get("changes") or [])
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        ids = {(c["asset_kind"], c["asset_id"]) for c in changes}
        visible = set()
        for kind in ("device", "child", "server"):
            want = [i for k, i in ids if k == kind]
            if want:
                table = "servers" if kind == "server" else "devices"
                for aid, tags in (await db.execute(text(
                        f"SELECT id::text, tags FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                        {"ids": want})).all():
                    if scoping.entity_visible(tags, scope):
                        visible.add((kind, aid))
        changes = [c for c in changes if (c["asset_kind"], c["asset_id"]) in visible]
    return {"engine_run_id": str(run.id), "status": run.status, "detail": run.detail, "counts": run.counts or {},
            "assets": len(plan.get("assets") or []), "changes": changes}


async def _decide(engine_run_id, approve: bool, db: AsyncSession, user: User) -> dict:
    await _fleet_guard(db, user)
    run = await db.get(DiscoveryEngineRun, engine_run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    kinds = {c.get("asset_kind") for c in (run.plan or {}).get("changes") or []}
    await _check_kinds(db, user, kinds, "Approving these changes" if approve else "Rejecting these changes")
    try:
        out = await reconcile.decide(db, engine_run_id, approve=approve, actor_id=user.id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except reconcile.ApprovalRefused as e:
        raise HTTPException(409, str(e)) from None
    await write_audit_log(db, actor=user, action="discovery.rules.approve" if approve else "discovery.rules.reject",
                          resource_type="discovery_engine_run", resource_id=str(engine_run_id), metadata=out)
    await db.commit()
    return out


@router.post("/history/{engine_run_id}/approve")
async def approve_run(engine_run_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("discovery.manage_rules"))):
    """Apply the changes an automatic run held back (each value re-checked first)."""
    return await _decide(engine_run_id, True, db, user)


@router.post("/history/{engine_run_id}/reject")
async def reject_run(engine_run_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("discovery.manage_rules"))):
    """Discard the held changes; they are not proposed again until an asset or a rule changes."""
    return await _decide(engine_run_id, False, db, user)


@router.get("/export")
async def export_rules(db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("discovery.view"))):
    """Every rule, in order, with references by name (no ids, no secrets)."""
    rules = await runner.load_rules(db, modes=("draft", "simulate", "active", "disabled"))
    doc = await portable.export_rules(db, rules)
    await write_audit_log(db, actor=user, action="discovery.rules.export", resource_type="discovery_rules",
                          resource_id=None, metadata={"rules": len(rules)})
    await db.commit()
    return doc


@router.post("/import")
async def import_rules(payload: ImportRequest, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("discovery.manage_rules"))):
    """Create the file's rules as Drafts, last in the order. A rule whose
    references do not resolve here, or that is invalid, is skipped and
    reported; nothing is guessed."""
    await _fleet_guard(db, user)
    doc = payload.document
    if doc.get("format") != portable.FORMAT or not isinstance(doc.get("rules"), list):
        raise HTTPException(422, "Not a ZenPlus discovery rules export")
    created, skipped = [], []
    for raw in doc["rules"][:500]:
        name = str((raw or {}).get("name") or "")[:150]
        try:
            resolved, notes = await portable.resolve_rule(db, raw)
            fields = {k: v for k, v in resolved.items() if k in RuleCreate.model_fields}
            body = RuleCreate(**{**fields, "mode": "draft", "position": "last"})
        except portable.Unresolved as e:
            skipped.append({"name": name, "reason": str(e)})
            continue
        except Exception as e:  # noqa: BLE001 - invalid shape: report, keep going
            skipped.append({"name": name, "reason": f"invalid rule: {str(e)[:200]}"})
            continue
        base, n = body.name.strip(), 1
        while await _name_taken(db, body.name):
            n += 1
            body.name = f"{base} (imported{'' if n == 2 else f' {n - 1}'})"[:150]
        try:
            async with db.begin_nested():
                out = await _create(db, body, user, reason="imported")
        except HTTPException as e:
            skipped.append({"name": name, "reason": str(e.detail)})
            continue
        created.append({"id": str(out.id), "name": out.name, "notes": notes})
    await write_audit_log(db, actor=user, action="discovery.rules.import", resource_type="discovery_rules",
                          resource_id=None, metadata={"created": [c["name"] for c in created], "skipped": skipped})
    await db.commit()
    return {"created": created, "skipped": skipped}


@router.get("/starter")
async def starter_rules(db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_permission("discovery.view"))):
    """Starter rules an administrator can add as Drafts."""
    have = {k for (k,) in (await db.execute(text(
        "SELECT builtin_key FROM discovery_engine_rules WHERE builtin_key IS NOT NULL"))).all()}
    return [{"key": s["key"], "name": s["name"], "description": s["description"], "added": s["key"] in have}
            for s in portable.STARTERS]


@router.post("/starter")
async def add_starter_rules(payload: StarterRequest, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("discovery.manage_rules"))):
    await _fleet_guard(db, user)
    unknown = [k for k in payload.keys if k not in portable.STARTER_BY_KEY]
    if unknown:
        raise HTTPException(422, "Unknown starter rules: " + ", ".join(unknown))
    have = {k for (k,) in (await db.execute(text(
        "SELECT builtin_key FROM discovery_engine_rules WHERE builtin_key IS NOT NULL"))).all()}
    created = []
    for key in dict.fromkeys(payload.keys):
        if key in have:
            continue
        st = portable.STARTER_BY_KEY[key]
        body = RuleCreate(name=st["name"], description=st["description"], mode="draft",
                          asset_kinds=st["asset_kinds"], triggers=st["triggers"], condition=st["condition"],
                          actions=st["actions"], position="last")
        base, n = body.name, 1
        while await _name_taken(db, body.name):
            n += 1
            body.name = f"{base} ({n})"
        out = await _create(db, body, user, reason=f"starter rule {key}", builtin_key=key)
        created.append({"id": str(out.id), "name": out.name, "key": key})
    await write_audit_log(db, actor=user, action="discovery.rules.starter", resource_type="discovery_rules",
                          resource_id=None, metadata={"created": created})
    await db.commit()
    return {"created": created}


# ── one rule ────────────────────────────────────────────────────────────────

@router.get("/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                   _user: User = Depends(require_permission("discovery.view"))):
    return _out(await _get_rule(db, rule_id))


@router.put("/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, payload: RuleUpdate, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("discovery.manage_rules"))):
    await _fleet_guard(db, user)
    row = await _get_rule(db, rule_id)
    if payload.expected_revision is not None and payload.expected_revision != row.revision:
        raise HTTPException(409, f"The rule was changed by someone else (now revision {row.revision}); "
                                 "reload it before saving")
    if await _name_taken(db, payload.name, exclude=row.id):
        raise HTTPException(409, "A rule with this name already exists")
    condition, acts, profile_ids = await _normalise(db, payload)
    if payload.mode == "active":
        await _check_activation(db, user, acts, list(payload.asset_kinds))
    before = _snapshot(row)
    now = datetime.now(timezone.utc)
    if payload.mode == "active" and (row.mode != "active" or acts != row.actions):
        row.activated_by, row.activated_at = user.id, now
    row.name = payload.name.strip()
    row.description = payload.description
    row.mode = payload.mode
    row.asset_kinds = list(payload.asset_kinds)
    row.profile_ids = profile_ids
    row.condition = condition
    row.actions = acts
    row.stop_processing = payload.stop_processing
    row.triggers = list(payload.triggers)
    row.enforcement = payload.enforcement
    row.on_unmatch = payload.on_unmatch
    row.revision = row.revision + 1
    row.updated_by = user.id
    row.updated_at = now
    await _record_version(db, row, user, "updated")
    await write_audit_log(db, actor=user, action="discovery.rules.update", resource_type="discovery_rule",
                          resource_id=str(row.id), metadata={"before": before, "after": _snapshot(row),
                                                             "revision": row.revision})
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{rule_id}/mode", response_model=RuleOut)
async def set_mode(rule_id: uuid.UUID, payload: RuleModeRequest, db: AsyncSession = Depends(get_db),
                   user: User = Depends(require_permission("discovery.manage_rules"))):
    await _fleet_guard(db, user)
    row = await _get_rule(db, rule_id)
    if row.mode == payload.mode:
        return _out(row)
    if payload.mode == "active":
        await _check_activation(db, user, list(row.actions or []), list(row.asset_kinds or ["device"]))
        row.activated_by, row.activated_at = user.id, datetime.now(timezone.utc)
    previous = row.mode
    row.mode = payload.mode
    row.revision += 1
    row.updated_by = user.id
    row.updated_at = datetime.now(timezone.utc)
    await _record_version(db, row, user, f"mode {previous} -> {payload.mode}")
    await write_audit_log(db, actor=user, action="discovery.rules.mode", resource_type="discovery_rule",
                          resource_id=str(row.id), metadata={"name": row.name, "from": previous,
                                                             "to": payload.mode})
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{rule_id}/clone", response_model=RuleOut, status_code=201)
async def clone_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("discovery.manage_rules"))):
    """A copy in Draft mode, placed last."""
    await _fleet_guard(db, user)
    src = await _get_rule(db, rule_id)
    name, n = f"{src.name} (copy)", 1
    while await _name_taken(db, name):
        n += 1
        name = f"{src.name} (copy {n})"
    edge = (await db.execute(select(func.max(DiscoveryEngineRule.priority)))).scalar() or 0
    now = datetime.now(timezone.utc)
    row = DiscoveryEngineRule(
        name=name[:150], description=src.description, priority=edge + 10, mode="draft",
        asset_kinds=list(src.asset_kinds or ["device"]), triggers=list(src.triggers or ["discovery"]),
        profile_ids=src.profile_ids, condition=src.condition, actions=src.actions,
        stop_processing=src.stop_processing, enforcement=src.enforcement, on_unmatch=src.on_unmatch,
        revision=1, created_by=user.id,
        updated_by=user.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    await db.flush()
    await _record_version(db, row, user, f"cloned from {src.name}")
    await write_audit_log(db, actor=user, action="discovery.rules.clone", resource_type="discovery_rule",
                          resource_id=str(row.id), metadata={"from": str(src.id), "name": row.name})
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_permission("discovery.manage_rules"))):
    """Deletes the rule and its versions. Run history keeps the rule's name;
    devices it imported or configured are not touched."""
    await _fleet_guard(db, user)
    row = await _get_rule(db, rule_id)
    snap = _snapshot(row)
    await db.delete(row)
    await write_audit_log(db, actor=user, action="discovery.rules.delete", resource_type="discovery_rule",
                          resource_id=str(rule_id), metadata=snap)
    await db.commit()


@router.get("/{rule_id}/versions", response_model=list[RuleVersionOut])
async def rule_versions(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        _user: User = Depends(require_permission("discovery.view"))):
    await _get_rule(db, rule_id)
    rows = (await db.execute(text(
        "SELECT v.revision, v.snapshot, v.changed_by, u.username AS changed_by_name, v.changed_at, v.reason"
        " FROM discovery_engine_rule_versions v LEFT JOIN users u ON u.id = v.changed_by"
        " WHERE v.rule_id = :id ORDER BY v.revision DESC"), {"id": rule_id})).mappings().all()
    return [RuleVersionOut(**dict(r)) for r in rows]
