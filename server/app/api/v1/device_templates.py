"""Device Templates API (``/api/v1/snmp/profiles``; docs/device-templates.md).

Reading needs the ``snmp`` module's view permission (devices.view or
discovery.view). Changing a template needs ``devices.manage_templates`` and an
unscoped user: templates are fleet-wide configuration, like fleet-wide alert
rules. Putting devices on a template is a device change: ``devices.manage``
and every device in the caller's scope.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.snmp import (
    MatchRules,
    OidGroup,
    ProfileResponse,
    ProfileUpdate,
    TemplateAssignRequest,
    TemplateClassifyRequest,
    TemplateContent,
    TemplateCustomizeRequest,
    TemplatePublishRequest,
    TemplateRollbackRequest,
    TemplateTestRequest,
)
from app.services.device_templates import classify, probe
from app.services.device_templates import overlay as ov
from app.services.device_templates import service as svc

router = APIRouter(prefix="/snmp", tags=["Device Templates"])

MANAGE = "devices.manage_templates"
EXPORT_FORMAT = 2


async def template_manager(user: User = Depends(require_permission(MANAGE)),
                           db: AsyncSession = Depends(get_db)) -> User:
    if await scoping.visible_tags(db, user) is not None:
        raise HTTPException(status_code=403, detail="Templates apply to every device; a user limited to some "
                                                    "devices cannot change them")
    return user


async def devices_in_scope(db: AsyncSession, user: User, ids) -> None:
    scope = await scoping.visible_tags(db, user)
    if scope is None or not ids:
        return
    rows = (await db.execute(text("SELECT id::text, tags FROM devices WHERE id = ANY(CAST(:ids AS uuid[]))"),
                             {"ids": [str(i) for i in ids]})).all()
    seen = {r[0]: r[1] for r in rows}
    if any(str(i) not in seen or not scoping.entity_visible(seen[str(i)], scope) for i in ids):
        raise HTTPException(status_code=404, detail="Device not found")


def _response(row: dict) -> ProfileResponse:
    return ProfileResponse(**{k: v for k, v in row.items() if not k.startswith("_")})


def _detail(row: dict) -> dict:
    out = _response(row).model_dump(mode="json")
    out.update({"overlay": row.get("overlay"), "draft": row.get("draft"),
                "pilot_device_ids": [str(i) for i in row.get("pilot_device_ids") or []],
                "editable": not row.get("builtin")})
    return out


def _content(data: TemplateContent) -> dict:
    d = data.model_dump(exclude_none=True)
    d["match_rules"] = data.match_rules.model_dump(exclude_none=True, exclude_defaults=False)
    if not d["match_rules"].get("require_sys_descr"):
        d["match_rules"].pop("require_sys_descr", None)
    d["oid_groups"] = [g.model_dump(exclude_none=True) for g in data.oid_groups]
    d.setdefault("match_priority", 100)
    for f in ("vendor", "description", "device_type", "icon", "interface_roles", "rule_pack"):
        d.setdefault(f, None)
    return d


def _refuse_builtin(row: dict) -> None:
    if row.get("builtin"):
        raise HTTPException(status_code=400, detail="Built-in templates are read-only: use Customise to keep "
                                                    "receiving updates, or Clone for an independent copy")


async def _publishable(db: AsyncSession, row: dict, content: dict) -> tuple[dict, list[str]]:
    row = await svc.with_parent(db, row)
    if content.get("name") and await svc.name_taken(db, content["name"].strip(), exclude=row["id"]):
        raise HTTPException(status_code=409, detail="A template with this name already exists")
    await svc.validate_content(db, content, parent=row.get("_parent"))
    stored, warnings = svc.stored_content(row, content)
    if row.get("parent_id"):
        errs = ov.problems(stored["overlay"] or {}, row["_parent_groups"])
        if errs:
            raise HTTPException(status_code=422, detail={"message": "Template cannot be saved", "errors": errs})
    return stored, warnings


# ── Read ───────────────────────────────────────────────────────────────────

@router.get("/profiles", response_model=list[ProfileResponse])
async def list_profiles(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return [_response(r) for r in await svc.list_all(db)]


@router.get("/profiles/unmatched")
async def unmatched_devices(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """SNMP devices no template matched, with what the current templates would pick now."""
    scope = await scoping.visible_tags(db, user)
    rows = (await db.execute(text("""
        SELECT d.id::text AS id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor, d.model,
               d.sys_object_id, d.sys_descr, d.status, d.tags, d.identity_updated_at
          FROM devices d
         WHERE d.snmp_enabled AND d.profile_id IS NULL
           AND d.managed_by_device_id IS NULL AND d.poll_mode IS DISTINCT FROM 'via_controller'
         ORDER BY d.hostname
    """))).mappings().all()
    templates = await svc.list_all(db)
    out = []
    for r in rows:
        if scope is not None and not scoping.entity_visible(r["tags"], scope):
            continue
        best = classify.match(templates, r["sys_object_id"] or "", r["sys_descr"] or "")
        item = {k: v for k, v in r.items() if k != "tags"}
        item["would_match"] = {"id": str(best["id"]), "name": best["name"]} if best else None
        out.append(item)
    return {"data": out, "total": len(out)}


@router.get("/profiles/suggestions")
async def suggestions(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Devices on one template that another template now matches better."""
    scope = await scoping.visible_tags(db, user)
    rows = (await db.execute(text("""
        SELECT d.id::text AS id, d.hostname, host(d.ip_address) AS ip, d.tags, d.profile_source,
               d.profile_id::text AS current_id, c.name AS current_name,
               d.profile_suggested_id::text AS suggested_id, s.name AS suggested_name, d.profile_suggested_at,
               d.sys_object_id
          FROM devices d
          JOIN device_profiles s ON s.id = d.profile_suggested_id
          LEFT JOIN device_profiles c ON c.id = d.profile_id
         ORDER BY d.hostname
    """))).mappings().all()
    data = [{k: v for k, v in r.items() if k != "tags"} for r in rows
            if scope is None or scoping.entity_visible(r["tags"], scope)]
    return {"data": data, "total": len(data)}


@router.post("/profiles/classify")
async def classify_device(data: TemplateClassifyRequest, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Every template with whether it matches a device (or given identity) and why."""
    oid, descr = data.sys_object_id or "", data.sys_descr or ""
    current = None
    if data.device_id:
        await devices_in_scope(db, user, [data.device_id])
        r = (await db.execute(text("""
            SELECT d.sys_object_id, d.sys_descr, d.profile_id::text AS profile_id, d.profile_source, p.name
              FROM devices d LEFT JOIN device_profiles p ON p.id = d.profile_id WHERE d.id = :id
        """), {"id": data.device_id})).mappings().first()
        if r is None:
            raise HTTPException(status_code=404, detail="Device not found")
        oid, descr = r["sys_object_id"] or "", r["sys_descr"] or ""
        current = {"id": r["profile_id"], "name": r["name"], "source": r["profile_source"]}
    result = classify.explain(await svc.list_all(db), oid, descr)
    result["current"] = current
    return result


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    row = await svc.get_or_404(db, profile_id)
    out = _detail(row)
    if row.get("parent_id"):
        parent = await svc.get(db, row["parent_id"])
        _, warnings = ov.resolve((parent or {}).get("oid_groups") or [], row.get("overlay") or {})
        out["overlay_warnings"] = warnings
        out["parent_groups"] = (parent or {}).get("oid_groups") or []
        out["parent_revision"] = (parent or {}).get("revision")
    children = (await db.execute(text("SELECT id::text, name FROM device_profiles WHERE parent_id = :id ORDER BY name"),
                                 {"id": profile_id})).mappings().all()
    out["customized_by"] = [dict(c) for c in children]
    return out


@router.get("/profiles/{profile_id}/devices")
async def list_profile_devices(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """Devices on the template with assignment source, suggestion and metric coverage."""
    row = await svc.get_or_404(db, profile_id)
    cov = await svc.coverage(db, row)
    scope = await scoping.visible_tags(db, user)
    devices = cov["devices"]
    if scope is not None:
        tags = dict((await db.execute(text("SELECT id::text, tags FROM devices WHERE profile_id = :id"),
                                      {"id": profile_id})).all())
        devices = [d for d in devices if scoping.entity_visible(tags.get(d["id"]), scope)]
    extra = dict((r[0], r[1:]) for r in (await db.execute(text("""
        SELECT id::text, device_type, vendor, model FROM devices WHERE profile_id = :id
    """), {"id": profile_id})).all())
    for d in devices:
        t, v, m = extra.get(d["id"], (None, None, None))
        d.update({"ip_address": d.pop("ip"), "device_type": t, "vendor": v, "model": m})
    return {"data": devices, "defined": cov["defined"], "average_pct": cov["average_pct"]}


@router.get("/profiles/{profile_id}/versions")
async def list_versions(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    await svc.get_or_404(db, profile_id)
    rows = (await db.execute(text("""
        SELECT revision, action, reason, changed_by_name, changed_at,
               jsonb_array_length(COALESCE(snapshot->'oid_groups', '[]'::jsonb)) AS groups
          FROM device_profile_versions WHERE profile_id = :id ORDER BY revision DESC
    """), {"id": profile_id})).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.get("/profiles/{profile_id}/versions/{revision}")
async def get_version(profile_id: uuid.UUID, revision: int, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    row = await svc.get_or_404(db, profile_id)
    v = (await db.execute(text("""
        SELECT revision, action, reason, changed_by_name, changed_at, snapshot
          FROM device_profile_versions WHERE profile_id = :id AND revision = :rev
    """), {"id": profile_id, "rev": revision})).mappings().first()
    if v is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    out = dict(v)
    snap = svc._json(out["snapshot"]) or {}
    out["snapshot"] = snap
    out["compare_to_current"] = svc.compare(snap.get("oid_groups") or [], row.get("oid_groups") or [])
    return out


@router.get("/profiles/{profile_id}/export")
async def export_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    row = await svc.get_or_404(db, profile_id)
    body = {f: row.get(f) for f in svc.CONTENT_FIELDS}
    body["source_builtin_key"] = row.get("builtin_key")
    body["source_revision"] = row.get("revision")
    if row.get("parent_id"):
        parent = await svc.get(db, row["parent_id"])
        body["parent"] = {"builtin_key": (parent or {}).get("builtin_key"), "name": (parent or {}).get("name")}
        body["overlay"] = row.get("overlay") or {}
    return {"format_version": EXPORT_FORMAT, "profile": body}


# ── Change templates (devices.manage_templates, unscoped) ──────────────────

@router.post("/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(data: TemplateContent, db: AsyncSession = Depends(get_db),
                         user: User = Depends(template_manager)):
    content = _content(data)
    return await _create(db, content, user, action="created")


async def _create(db: AsyncSession, content: dict, user: User, *, action: str,
                  parent: Optional[dict] = None, overlay_doc: Optional[dict] = None,
                  reason: Optional[str] = None) -> ProfileResponse:
    await svc.lock(db)
    if await svc.name_taken(db, content["name"].strip()):
        raise HTTPException(status_code=409, detail="A template with this name already exists")
    if parent is None:
        await svc.validate_content(db, content)
    else:
        await svc.check_device_type(db, content.get("device_type"))
    row = (await db.execute(text(f"""
        INSERT INTO device_profiles (name, vendor, description, match_rules, oid_groups, builtin, device_type, icon,
                                     match_priority, parent_id, overlay, parent_hash, updated_by,
                                     interface_roles, rule_pack)
        VALUES (:name, :vendor, :desc, CAST(:mr AS jsonb), CAST(:g AS jsonb), FALSE, :dt, :icon, :prio,
                CAST(:parent AS uuid), CAST(:ov AS jsonb), :ph, (SELECT id FROM users WHERE id = :uid),
                CAST(:roles AS jsonb), CAST(:pack AS jsonb))
        RETURNING id
    """), {"name": content["name"].strip(), "vendor": content.get("vendor"), "desc": content.get("description"),
           "mr": json.dumps(content.get("match_rules") or {}), "g": json.dumps(content.get("oid_groups") or []),
           "dt": content.get("device_type"), "icon": content.get("icon"),
           "prio": int(content.get("match_priority") or 100),
           "parent": str(parent["id"]) if parent else None,
           "ov": json.dumps(overlay_doc) if parent else None,
           "ph": parent.get("content_hash") if parent else None, "uid": getattr(user, "id", None),
           "roles": json.dumps(content["interface_roles"]) if content.get("interface_roles") is not None else None,
           "pack": json.dumps(content["rule_pack"]) if content.get("rule_pack") is not None else None})).first()
    pid = row[0]
    await db.execute(text(f"UPDATE device_profiles SET content_hash = {svc.HASH_SQL} WHERE id = :id"), {"id": pid})
    fresh = await svc.get(db, pid)
    await svc.record_version(db, pid, 1, action, svc.snapshot(fresh), user, reason)
    await svc.audit(db, user, action, fresh, parent=parent["name"] if parent else None)
    await db.commit()
    return _response(fresh)


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(profile_id: uuid.UUID, data: ProfileUpdate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(template_manager)):
    """Change and publish at once (no draft). Kept for API clients; the UI uses draft + publish."""
    await svc.lock(db)
    row = await svc.get_or_404(db, profile_id, for_update=True)
    _refuse_builtin(row)
    content = svc.content_of(row)
    patch = data.model_dump(exclude_unset=True)
    if "match_rules" in patch and data.match_rules is not None:
        patch["match_rules"] = data.match_rules.model_dump(exclude_none=True)
    if "oid_groups" in patch and data.oid_groups is not None:
        patch["oid_groups"] = [g.model_dump(exclude_none=True) for g in data.oid_groups]
    content.update({k: v for k, v in patch.items() if v is not None})
    stored, _ = await _publishable(db, row, content)
    fresh = await svc.write_published(db, row, stored, action="published", user=user, reason="Saved through the API")
    await svc.audit(db, user, "publish", fresh, revision=fresh["revision"])
    await db.commit()
    return _response(fresh)


@router.put("/profiles/{profile_id}/draft")
async def save_draft(profile_id: uuid.UUID, data: TemplateContent, db: AsyncSession = Depends(get_db),
                     user: User = Depends(template_manager)):
    """Save unpublished changes. Devices keep polling the published revision."""
    row = await svc.get_or_404(db, profile_id)
    _refuse_builtin(row)
    content = _content(data)
    stored, warnings = await _publishable(db, row, content)
    fresh = await svc.save_draft(db, row, content, user)
    await db.commit()
    out = _detail(fresh)
    out["draft_warnings"] = warnings
    out["draft_resolved_groups"] = stored["oid_groups"] if row.get("parent_id") else None
    return out


@router.delete("/profiles/{profile_id}/draft", status_code=204)
async def discard_draft(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        user: User = Depends(template_manager)):
    row = await svc.get_or_404(db, profile_id)
    if row.get("pilot_device_ids"):
        raise HTTPException(status_code=409, detail="Cancel the pilot before discarding the draft")
    await db.execute(text("UPDATE device_profiles SET draft = NULL, draft_updated_at = NULL, draft_updated_by = NULL "
                          "WHERE id = :id"), {"id": profile_id})
    await db.commit()


class ImpactRequest(BaseModel):
    content: Optional[TemplateContent] = None


@router.post("/profiles/{profile_id}/impact")
async def impact(profile_id: uuid.UUID, data: ImpactRequest, db: AsyncSession = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """What publishing the draft (or the given content) would change."""
    row = await svc.with_parent(db, await svc.get_or_404(db, profile_id))
    if data.content is not None:
        content = _content(data.content)
    elif row.get("draft"):
        content = row["draft"]
    else:
        raise HTTPException(status_code=409, detail="No draft to compare")
    return await svc.impact(db, row, content)


@router.post("/profiles/{profile_id}/publish")
async def publish(profile_id: uuid.UUID, data: TemplatePublishRequest, db: AsyncSession = Depends(get_db),
                  user: User = Depends(template_manager)):
    """Publish the draft to every device, or to ``pilot_device_ids`` only (a pilot)."""
    await svc.lock(db)
    row = await svc.get_or_404(db, profile_id, for_update=True)
    _refuse_builtin(row)
    if not row.get("draft"):
        raise HTTPException(status_code=409, detail="Nothing to publish: save a draft first")
    content = TemplateContent(**row["draft"])
    stored, warnings = await _publishable(db, row, _content(content))
    if data.pilot_device_ids:
        pilot = [str(i) for i in dict.fromkeys(data.pilot_device_ids)]
        on = set(await svc.device_ids_on(db, profile_id))
        outside = [i for i in pilot if i not in on]
        if outside:
            raise HTTPException(status_code=422, detail="Pilot devices must already use this template")
        old_keys = svc.group_keys(row.get("pilot_oid_groups") or row.get("oid_groups") or [])
        await db.execute(text("""
            UPDATE device_profiles SET pilot_device_ids = CAST(:ids AS uuid[]), pilot_oid_groups = CAST(:g AS jsonb),
                   pilot_started_at = COALESCE(pilot_started_at, now()) WHERE id = :id
        """), {"id": profile_id, "ids": pilot, "g": json.dumps(stored["oid_groups"])})
        await svc.purge_groups(db, pilot, old_keys - svc.group_keys(stored["oid_groups"]))
        fresh = await svc.get(db, profile_id)
        await svc.audit(db, user, "pilot", fresh, devices=pilot, reason=data.reason)
        await db.commit()
        out = _detail(fresh)
        out["warnings"] = warnings
        return out
    fresh = await svc.write_published(db, await svc.with_parent(db, row), stored, action="published",
                                      user=user, reason=data.reason)
    await svc.audit(db, user, "publish", fresh, revision=fresh["revision"], reason=data.reason)
    await db.commit()
    out = _detail(fresh)
    out["warnings"] = warnings
    return out


@router.post("/profiles/{profile_id}/pilot/cancel")
async def cancel_pilot(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(template_manager)):
    """Put the pilot devices back on the published revision (the draft is kept)."""
    await svc.lock(db)
    row = await svc.get_or_404(db, profile_id, for_update=True)
    pilot = [str(i) for i in row.get("pilot_device_ids") or []]
    if not pilot:
        raise HTTPException(status_code=409, detail="No pilot is running")
    extra = svc.group_keys(row.get("pilot_oid_groups") or []) - svc.group_keys(row.get("oid_groups") or [])
    await db.execute(text("UPDATE device_profiles SET pilot_device_ids = NULL, pilot_oid_groups = NULL, "
                          "pilot_started_at = NULL WHERE id = :id"), {"id": profile_id})
    await svc.purge_groups(db, pilot, extra)
    fresh = await svc.get(db, profile_id)
    await svc.audit(db, user, "pilot_cancel", fresh, devices=pilot)
    await db.commit()
    return _detail(fresh)


@router.post("/profiles/{profile_id}/rollback")
async def rollback(profile_id: uuid.UUID, data: TemplateRollbackRequest, db: AsyncSession = Depends(get_db),
                   user: User = Depends(template_manager)):
    """Publish an earlier revision again (as a new revision; history is kept)."""
    await svc.lock(db)
    row = await svc.get_or_404(db, profile_id, for_update=True)
    _refuse_builtin(row)
    v = (await db.execute(text("SELECT snapshot FROM device_profile_versions WHERE profile_id = :id AND revision = :r"),
                          {"id": profile_id, "r": data.revision})).first()
    if v is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    snap = svc._json(v[0]) or {}
    row = await svc.with_parent(db, row)
    if row.get("parent_id"):
        if str(snap.get("parent_id") or "") != str(row["parent_id"]):
            raise HTTPException(status_code=409, detail="That revision belongs to a different parent template")
        groups, _ = ov.resolve(row["_parent_groups"], snap.get("overlay") or {})
        content = {**{f: snap.get(f) for f in svc.CONTENT_FIELDS}, "overlay": snap.get("overlay") or {},
                   "oid_groups": groups}
    else:
        content = {**{f: snap.get(f) for f in svc.CONTENT_FIELDS}, "overlay": None}
    content["name"] = row["name"] if await svc.name_taken(db, (content.get("name") or "").strip(),
                                                          exclude=profile_id) else content.get("name") or row["name"]
    reason = data.reason or f"Rolled back to revision {data.revision}"
    fresh = await svc.write_published(db, row, content, action="rollback", user=user, reason=reason)
    await svc.audit(db, user, "rollback", fresh, to_revision=data.revision, revision=fresh["revision"])
    await db.commit()
    return _detail(fresh)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         user: User = Depends(template_manager)):
    await svc.lock(db)
    row = await svc.get_or_404(db, profile_id, for_update=True)
    if row.get("builtin"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in templates")
    children = (await db.execute(text("SELECT name FROM device_profiles WHERE parent_id = :id ORDER BY name"),
                                 {"id": profile_id})).scalars().all()
    if children:
        raise HTTPException(status_code=409, detail=f"Templates customised from this one must be deleted first: "
                                                    f"{', '.join(children)}")
    rules = (await db.execute(text("SELECT name FROM alert_rules WHERE profile_id = :id ORDER BY name"),
                              {"id": profile_id})).scalars().all()
    if rules:
        raise HTTPException(status_code=409, detail="Alert rules are scoped to this template; delete them or change "
                                                    f"their scope first: {', '.join(rules[:10])}")
    devices = await svc.device_ids_on(db, profile_id)
    if devices:
        await db.execute(text("DELETE FROM device_template_values WHERE device_id = ANY(CAST(:ids AS uuid[]))"),
                         {"ids": devices})
        await db.execute(text("UPDATE devices SET profile_id = NULL, profile_source = NULL, profile_assigned_at = NULL "
                              "WHERE profile_id = :id"), {"id": profile_id})
    await db.execute(text("UPDATE devices SET profile_suggested_id = NULL, profile_suggested_at = NULL "
                          "WHERE profile_suggested_id = :id"), {"id": profile_id})
    await svc.audit(db, user, "delete", row, devices_unassigned=len(devices))
    await db.execute(text("DELETE FROM device_profiles WHERE id = :id"), {"id": profile_id})
    await db.commit()


@router.post("/profiles/{profile_id}/clone", response_model=ProfileResponse, status_code=201)
async def clone_profile(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                        user: User = Depends(template_manager)):
    """An independent copy of the effective template. It does not receive updates of the source."""
    src = await svc.get_or_404(db, profile_id)
    content = svc.content_of(src)
    content["name"] = await svc.unique_name(db, f"{src['name']} (copy)")
    content["match_priority"] = int(src.get("match_priority") or 100)
    from app.services.device_templates import family as tfamily
    fam = await tfamily.load(db)
    content["interface_roles"] = fam.roles(src["id"])
    content["rule_pack"] = fam.pack(src["id"])
    return await _create(db, content, user, action="cloned", reason=f"Copied from '{src['name']}' revision {src['revision']}")


@router.post("/profiles/{profile_id}/customize", response_model=ProfileResponse, status_code=201)
async def customize_profile(profile_id: uuid.UUID, data: TemplateCustomizeRequest,
                            db: AsyncSession = Depends(get_db), user: User = Depends(template_manager)):
    """A customised template: inherits the parent's groups (and its future updates) and stores
    only the differences. It matches the same devices at a higher priority."""
    parent = await svc.get_or_404(db, profile_id)
    depth, cursor = 1, parent
    while cursor.get("parent_id"):
        depth += 1
        cursor = await svc.get(db, cursor["parent_id"]) or {}
    if depth >= ov.MAX_DEPTH:
        raise HTTPException(status_code=422, detail=f"Templates can be customised at most {ov.MAX_DEPTH} levels deep")
    content = svc.content_of(parent)
    content["name"] = (data.name or "").strip() or await svc.unique_name(db, f"{parent['name']} (customised)")
    content["match_priority"] = min(int(parent.get("match_priority") or 100) + 10, 1000)
    # Interface roles and recommended rules stay the parent's (and follow its updates).
    content["interface_roles"] = content["rule_pack"] = None
    resp = await _create(db, content, user, action="customized", parent=parent, overlay_doc={},
                         reason=f"Customised from '{parent['name']}' revision {parent['revision']}")
    if data.move_devices:
        moved = (await db.execute(text("""
            UPDATE devices SET profile_id = :new, profile_assigned_at = now(),
                   profile_suggested_id = NULL, profile_suggested_at = NULL
             WHERE profile_id = :old RETURNING id
        """), {"new": resp.id, "old": profile_id})).all()
        await svc.audit(db, user, "assign", {"id": resp.id, "name": resp.name},
                        device_ids=[str(r[0]) for r in moved], source="customize")
        await db.commit()
        return _response(await svc.get(db, resp.id))
    return resp


class ProfileBundle(BaseModel):
    format_version: int = 1
    profile: dict


@router.post("/profiles/import", response_model=ProfileResponse, status_code=201)
async def import_profile(bundle: ProfileBundle, db: AsyncSession = Depends(get_db),
                         user: User = Depends(template_manager)):
    """Import an exported template (format 1 or 2) as a new custom template."""
    if bundle.format_version not in (1, 2):
        raise HTTPException(422, "Unsupported template format version")
    body = dict(bundle.profile)
    parent_ref = body.pop("parent", None)
    overlay_doc = body.pop("overlay", None)
    for k in ("source_builtin_key", "source_revision"):
        body.pop(k, None)
    content = _content(TemplateContent(**{k: v for k, v in body.items() if k in TemplateContent.model_fields}))
    content["name"] = await svc.unique_name(db, content["name"]) if await svc.name_taken(db, content["name"]) \
        else content["name"]
    if parent_ref:
        parent = None
        if parent_ref.get("builtin_key"):
            r = (await db.execute(text("SELECT id FROM device_profiles WHERE builtin_key = :k"),
                                  {"k": parent_ref["builtin_key"]})).first()
            parent = await svc.get(db, r[0]) if r else None
        if parent is None and parent_ref.get("name"):
            r = (await db.execute(text("SELECT id FROM device_profiles WHERE name = :n"), {"n": parent_ref["name"]})).first()
            parent = await svc.get(db, r[0]) if r else None
        if parent is None:
            raise HTTPException(422, f"The parent template '{parent_ref.get('name') or parent_ref.get('builtin_key')}' "
                                     "of this customised template does not exist here")
        doc = ov.normalise(overlay_doc or {})
        errs = ov.problems(doc, parent["oid_groups"])
        if errs:
            raise HTTPException(422, {"message": "Template cannot be imported", "errors": errs})
        content["oid_groups"], _ = ov.resolve(parent["oid_groups"], doc)
        return await _create(db, content, user, action="imported", parent=parent, overlay_doc=doc)
    return await _create(db, content, user, action="imported")


@router.post("/profiles/resync")
async def resync(db: AsyncSession = Depends(get_db), user: User = Depends(template_manager)):
    """Record built-in updates and re-resolve customised templates now (also runs in the background)."""
    return await svc.resync(db)


# ── Test, coverage ─────────────────────────────────────────────────────────

@router.post("/profiles/{profile_id}/test")
async def test_on_device(profile_id: uuid.UUID, data: TemplateTestRequest, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission(MANAGE, "devices.manage"))):
    """Run the draft (or published) template against one device. Nothing is stored."""
    row = await svc.with_parent(db, await svc.get_or_404(db, profile_id))
    await devices_in_scope(db, user, [data.device_id])
    if data.source == "draft" and row.get("draft"):
        stored, _ = svc.stored_content(row, _content(TemplateContent(**row["draft"])))
        groups = stored["oid_groups"]
    else:
        groups = row.get("oid_groups") or []
    groups = [g for g in groups if g.get("display") != "children"]
    if data.group_keys:
        wanted = set(data.group_keys)
        groups = [g for g in groups if g.get("key") in wanted]
    if not groups:
        raise HTTPException(status_code=422, detail="No metric groups to test")
    result = await probe.run(db, data.device_id, groups)
    result["source"] = data.source if (data.source == "published" or row.get("draft")) else "published"
    return result


# ── Assignment (devices.manage + device scope) ─────────────────────────────

@router.post("/profiles/{profile_id}/assign")
async def assign_profile(profile_id: uuid.UUID, data: TemplateAssignRequest, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("devices.manage"))):
    """Put devices on this template (an operator choice: the classifier never overrides it)."""
    row = await svc.get_or_404(db, profile_id)
    await devices_in_scope(db, user, data.device_ids)
    result = await svc.assign(db, row, data.device_ids, user, set_device_type=data.set_device_type)
    await db.commit()
    return result


class UnassignRequest(BaseModel):
    device_ids: list[uuid.UUID] = Field(..., max_length=5000)


@router.post("/profiles/unassign")
async def unassign_profile(data: UnassignRequest, db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_permission("devices.manage"))):
    """Take devices off any template (standard monitoring only); their template readings are removed."""
    await devices_in_scope(db, user, data.device_ids)
    result = await svc.assign(db, None, data.device_ids, user)
    await db.commit()
    return result


@router.post("/profiles/suggestions/accept")
async def accept_suggestions(data: UnassignRequest, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_permission("devices.manage"))):
    """Move each device to the template the classifier suggests for it."""
    await devices_in_scope(db, user, data.device_ids)
    rows = (await db.execute(text("""
        SELECT id::text, profile_suggested_id FROM devices
         WHERE id = ANY(CAST(:ids AS uuid[])) AND profile_suggested_id IS NOT NULL
    """), {"ids": [str(i) for i in data.device_ids]})).all()
    by_template: dict = {}
    for did, sid in rows:
        by_template.setdefault(sid, []).append(did)
    moved = 0
    for sid, ids in by_template.items():
        tpl = await svc.get(db, sid)
        if tpl:
            moved += (await svc.assign(db, tpl, ids, user))["assigned"]
    await db.commit()
    return {"moved": moved}


def _unused() -> tuple:  # keep imports used for type checkers of re-exported schemas
    return (MatchRules, OidGroup)


# ── Recommended alert rules (migrate-193, app.services.rule_packs) ─────────

class PackAcceptBody(BaseModel):
    enabled: bool = True
    notify_channels: list[str] = Field(default_factory=list, max_length=50)
    # Narrow the accepted rule to one group (with the template scope).
    group_id: Optional[uuid.UUID] = None


async def _pack_item(db: AsyncSession, profile_id, key: str) -> tuple[dict, dict, list[dict]]:
    from app.services import rule_packs
    from app.services.device_templates import family as tfamily
    row = await svc.get_or_404(db, profile_id)
    items = await rule_packs.state(db, row, await tfamily.load(db))
    item = rule_packs.find(items, key)
    if item is None:
        raise HTTPException(status_code=404, detail="This template has no such recommendation")
    return row, item, items


@router.get("/profiles/{profile_id}/recommended")
async def recommended_rules(profile_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """The template's recommended alert rules: available, accepted (with the rule) or
    changed since accepted (Apply / Keep). Nothing is created until accepted."""
    from app.services import rule_packs
    from app.services.device_templates import family as tfamily
    row = await svc.get_or_404(db, profile_id)
    return {"items": await rule_packs.state(db, row, await tfamily.load(db))}


@router.post("/profiles/{profile_id}/recommended/{key}/accept", status_code=201)
async def accept_recommended_rule(profile_id: uuid.UUID, key: str, data: PackAcceptBody,
                                  db: AsyncSession = Depends(get_db),
                                  user: User = Depends(require_permission("alerts.manage"))):
    """Create one ordinary alert rule from a recommendation, scoped to this template."""
    from app.api.v1 import alert_rules as rules_api
    from app.services import rule_packs
    row, item, _ = await _pack_item(db, profile_id, key)
    if item["accepted"]:
        raise HTTPException(status_code=409, detail="This recommendation is already accepted"
                            + ("" if item["accepted"]["on_this_template"]
                               else f" on {item['accepted']['template_name']}, which covers this template"))
    f = rule_packs.rule_fields(item)
    body = rules_api.AlertRuleCreate(
        name=f"{row['name']}: {item['name']}"[:255],
        description=(item.get("description") or "") + " Accepted from the template's recommended rules.",
        enabled=data.enabled, metric=f["metric"], operator=f["operator"], threshold=f["threshold"],
        conditions=[rules_api.ConditionItem(**c) for c in f["conditions"]],
        min_duration=f["min_duration"], clear_duration=f["clear_duration"], severity=f["severity"],
        priority=f["priority"], if_role=f["if_role"], profile_id=profile_id, group_id=data.group_id,
        notify_channels=data.notify_channels, recovery_alert=True,
    )
    created = await rules_api.create_alert_rule(body, db, user)
    try:
        await db.execute(text("UPDATE alert_rules SET pack_key = :k, pack_accepted = CAST(:acc AS jsonb) WHERE id = :id"),
                         {"k": key, "acc": rule_packs.accepted_record(item), "id": created["id"]})
        await db.commit()
    except Exception:
        # Accepted at the same moment by someone else: keep theirs.
        await db.rollback()
        await db.execute(text("DELETE FROM alert_rules WHERE id = :id"), {"id": created["id"]})
        await db.commit()
        raise HTTPException(status_code=409, detail="This recommendation was just accepted") from None
    return {**created, "pack_key": key}


async def _accepted_rule(item: dict) -> str:
    acc = item.get("accepted")
    if not acc:
        raise HTTPException(status_code=409, detail="This recommendation is not accepted")
    if not acc["on_this_template"]:
        raise HTTPException(status_code=409, detail=f"The rule belongs to {acc['template_name']}; change it there")
    return acc["rule_id"]


@router.post("/profiles/{profile_id}/recommended/{key}/apply")
async def apply_recommended_change(profile_id: uuid.UUID, key: str, db: AsyncSession = Depends(get_db),
                                   user: User = Depends(require_permission("alerts.manage"))):
    """Update the accepted rule to the current recommendation (threshold, holds, severity, priority)."""
    from app.api.v1 import alert_rules as rules_api
    from app.services import rule_packs
    _, item, _ = await _pack_item(db, profile_id, key)
    rule_id = await _accepted_rule(item)
    f = rule_packs.rule_fields(item)
    upd = rules_api.AlertRuleUpdate(
        operator=f["operator"], threshold=f["threshold"],
        conditions=[rules_api.ConditionItem(**c) for c in f["conditions"]],
        min_duration=f["min_duration"], clear_duration=f["clear_duration"], severity=f["severity"],
        priority=f["priority"], if_role=f["if_role"])
    out = await rules_api.update_alert_rule(uuid.UUID(rule_id), upd, db, user)
    await db.execute(text("UPDATE alert_rules SET pack_accepted = CAST(:acc AS jsonb) WHERE id = :id"),
                     {"acc": rule_packs.accepted_record(item), "id": rule_id})
    await db.commit()
    return out


@router.post("/profiles/{profile_id}/recommended/{key}/keep")
async def keep_accepted_rule(profile_id: uuid.UUID, key: str, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_permission("alerts.manage"))):
    """Keep the accepted rule as it is and stop showing the changed recommendation."""
    from app.services import alert_rule_scope as rs
    from app.services import rule_packs
    from app.services.audit_service import write_audit_log
    _, item, _ = await _pack_item(db, profile_id, key)
    rule_id = await _accepted_rule(item)
    rule = await rs.rule_row(db, rule_id)
    await rs.guard_rule_change(db, user, rule)
    await db.execute(text("UPDATE alert_rules SET pack_accepted = CAST(:acc AS jsonb) WHERE id = :id"),
                     {"acc": rule_packs.accepted_record(item), "id": rule_id})
    await write_audit_log(db, actor=user, action="alert_rule.recommendation_kept", resource_type="alert_rule",
                          resource_id=rule_id, metadata={"name": rule.get("name"), "pack_key": key,
                                                         "changes": item.get("changes")})
    await db.commit()
    return {"rule_id": rule_id, "status": "accepted"}
