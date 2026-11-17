"""Device Templates: publish, drafts, pilots, history, assignment and resync.

``device_profiles.oid_groups`` is always the effective, published document
the poller and the device page read. Everything that changes it goes through
:func:`publish` (or the resync for changes made outside the API), which bumps
``revision``, records a ``device_profile_versions`` row, removes readings of
groups that no longer exist and re-resolves templates customised from it.

All writers take one transaction-scoped advisory lock, so two API workers
never interleave a publish, a rollback and a resync.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.device_templates import overlay as ov

logger = logging.getLogger(__name__)

LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('device-templates'))"
HASH_SQL = "md5(COALESCE(match_rules, '{}'::jsonb)::text || '|' || COALESCE(oid_groups, '[]'::jsonb)::text)"
CONTENT_FIELDS = ("name", "vendor", "description", "device_type", "icon", "match_priority", "match_rules", "oid_groups")
_CHILD = "(d.managed_by_device_id IS NOT NULL OR d.poll_mode = 'via_controller')"

ROW_SQL = f"""
    SELECT p.id, p.name, p.vendor, p.version, p.builtin, p.description, p.match_rules, p.oid_groups,
           p.created_at, p.updated_at, p.builtin_key, p.parent_id, pp.name AS parent_name,
           p.overlay, p.parent_hash, p.device_type, p.icon, p.match_priority, p.revision, p.content_hash,
           p.draft, p.draft_updated_at, p.pilot_device_ids, p.pilot_oid_groups, p.pilot_started_at,
           (p.draft IS NOT NULL) AS has_draft,
           COALESCE(cardinality(p.pilot_device_ids), 0) AS pilot_device_count,
           (SELECT count(*) FROM devices d WHERE d.profile_id = p.id) AS device_count,
           (SELECT count(*) FROM devices d WHERE d.profile_suggested_id IS NOT NULL
               AND (d.profile_id = p.id OR d.profile_suggested_id = p.id)) AS suggestion_count,
           (SELECT count(*) FROM device_profiles c WHERE c.parent_id = p.id) AS customized_count
      FROM device_profiles p
      LEFT JOIN device_profiles pp ON pp.id = p.parent_id
"""


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _row(r) -> dict:
    d = dict(r)
    for k in ("match_rules", "oid_groups", "overlay", "draft", "pilot_oid_groups"):
        if k in d:
            d[k] = _json(d[k])
    d["match_rules"] = d.get("match_rules") or {}
    d["oid_groups"] = d.get("oid_groups") or []
    return d


async def lock(db: AsyncSession) -> None:
    await db.execute(text(LOCK_SQL))


async def get(db: AsyncSession, pid, *, for_update: bool = False) -> Optional[dict]:
    sql = ROW_SQL + " WHERE p.id = :id" + (" FOR UPDATE OF p" if for_update else "")
    r = (await db.execute(text(sql), {"id": pid})).mappings().first()
    return _row(r) if r else None


async def get_or_404(db: AsyncSession, pid, **kw) -> dict:
    row = await get(db, pid, **kw)
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return row


async def list_all(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text(ROW_SQL + " ORDER BY p.name"))).mappings().all()
    return [_row(r) for r in rows]


def snapshot(row: dict) -> dict:
    """What a version row keeps: enough to restore the template exactly."""
    return {
        "name": row.get("name"), "vendor": row.get("vendor"), "description": row.get("description"),
        "device_type": row.get("device_type"), "icon": row.get("icon"),
        "match_priority": row.get("match_priority", 100), "match_rules": row.get("match_rules") or {},
        "oid_groups": row.get("oid_groups") or [], "overlay": row.get("overlay"),
        "parent_id": str(row["parent_id"]) if row.get("parent_id") else None,
    }


def actor_name(user: Optional[User]) -> Optional[str]:
    return getattr(user, "username", None) if user is not None else None


async def record_version(db: AsyncSession, pid, revision: int, action: str, snap: dict,
                         user: Optional[User], reason: Optional[str]) -> None:
    await db.execute(text("""
        INSERT INTO device_profile_versions (profile_id, revision, action, snapshot, reason, changed_by, changed_by_name)
        VALUES (:pid, :rev, :action, CAST(:snap AS jsonb), :reason,
                (SELECT id FROM users WHERE id = :uid), :uname)
        ON CONFLICT (profile_id, revision) DO NOTHING
    """), {"pid": pid, "rev": revision, "action": action, "snap": json.dumps(snap, default=str),
           "reason": (reason or None) and reason[:500], "uid": getattr(user, "id", None),
           "uname": actor_name(user)})


def group_keys(groups: Iterable[dict]) -> set[str]:
    return {g.get("key") for g in groups or [] if g.get("key")}


async def purge_groups(db: AsyncSession, device_ids: list, keys: Iterable[str]) -> int:
    """Delete current readings of ``keys`` for ``device_ids`` (groups that stopped existing)."""
    keys = [k for k in keys if k]
    if not device_ids or not keys:
        return 0
    res = await db.execute(text("""
        DELETE FROM device_template_values
         WHERE device_id = ANY(CAST(:ids AS uuid[])) AND group_key = ANY(CAST(:keys AS text[]))
    """), {"ids": [str(i) for i in device_ids], "keys": keys})
    return res.rowcount or 0


async def device_ids_on(db: AsyncSession, pid) -> list[str]:
    return [str(r[0]) for r in (await db.execute(
        text("SELECT id FROM devices WHERE profile_id = :id"), {"id": pid})).all()]


async def check_device_type(db: AsyncSession, key: Optional[str], *, field: str = "device_type") -> None:
    from app.schemas.snmp import BUILTIN_DEVICE_TYPES
    if not key or key in BUILTIN_DEVICE_TYPES:
        return
    ok = (await db.execute(text("SELECT 1 FROM device_types WHERE key = :k"), {"k": key})).first()
    if not ok:
        raise HTTPException(status_code=422, detail=f"{field}: unknown device type '{key}'")


async def validate_content(db: AsyncSession, content: dict, *, parent: Optional[dict] = None) -> list[str]:
    """Raise 422 for content that cannot be published; return non-fatal warnings."""
    await check_device_type(db, content.get("device_type"))
    groups = content.get("oid_groups") or []
    errs: list[str] = []
    seen: set[str] = set()
    metric_keys: dict[str, str] = {}
    for g in groups:
        gk = g.get("key")
        if gk in seen:
            errs.append(f"group key '{gk}' is used twice")
        seen.add(gk)
        if g.get("kind") == "table" and not (g.get("metrics") or g.get("children")):
            errs.append(f"table group '{gk}' has no metrics")
        children = g.get("children") or {}
        if children.get("device_type"):
            exists = (await db.execute(text("SELECT 1 FROM device_types WHERE key = :k"),
                                       {"k": children["device_type"]})).first()
            if not exists:
                errs.append(f"group '{gk}': children device_type '{children['device_type']}' is not a known device type")
        for m in g.get("metrics") or []:
            mk = m.get("key")
            if mk in metric_keys and metric_keys[mk] != gk:
                errs.append(f"metric key '{mk}' is used in groups '{metric_keys[mk]}' and '{gk}' "
                            "(metric keys name the stored series and must be unique in a template)")
            metric_keys.setdefault(mk, gk)
    if parent is not None:
        errs += ov.problems_from_edit(canonical(parent.get("oid_groups") or []), groups)
    if errs:
        raise HTTPException(status_code=422, detail={"message": "Template cannot be saved", "errors": errs})
    name = (content.get("name") or "").strip()
    return [] if name else ["name is empty"]


async def name_taken(db: AsyncSession, name: str, exclude=None) -> bool:
    row = (await db.execute(text("SELECT 1 FROM device_profiles WHERE name = :n AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))"),
                            {"n": name, "x": str(exclude) if exclude else None})).first()
    return row is not None


async def unique_name(db: AsyncSession, base: str) -> str:
    base = base[:240]
    name = base
    n = 2
    while await name_taken(db, name):
        name = f"{base} {n}"
        n += 1
    return name


def canonical(groups: list[dict]) -> list[dict]:
    """Groups as the API's schema writes them (defaults filled, unknown keys dropped),
    so an operator's edited copy compares equal to an unchanged parent group."""
    from app.schemas.snmp import OidGroup
    out = []
    for g in groups or []:
        try:
            out.append(OidGroup.model_validate(g).model_dump(exclude_none=True))
        except Exception:
            out.append(g)
    return out


def stored_content(row: dict, content: dict) -> tuple[dict, list[str]]:
    """Content as stored: for a customised template, effective groups become an overlay
    (computed against the parent in schema form, resolved against the parent as stored)."""
    parent_groups = row.get("_parent_groups")
    if row.get("parent_id") and parent_groups is not None:
        overlay_doc = ov.diff_to_overlay(canonical(parent_groups), content.get("oid_groups") or [])
        groups, warnings = ov.resolve(parent_groups, overlay_doc)
        return {**content, "overlay": overlay_doc, "oid_groups": groups}, warnings
    return {**content, "overlay": None}, []


async def with_parent(db: AsyncSession, row: dict) -> dict:
    if row.get("parent_id"):
        parent = await get(db, row["parent_id"])
        row["_parent"] = parent
        row["_parent_groups"] = (parent or {}).get("oid_groups") or []
    return row


async def write_published(db: AsyncSession, row: dict, content: dict, *, action: str,
                          user: Optional[User], reason: Optional[str]) -> dict:
    """Make ``content`` the published state of ``row``: bump revision, record history,
    drop readings of removed groups, re-resolve customised children."""
    old_keys = group_keys(row.get("oid_groups") or [])
    new_groups = content.get("oid_groups") or []
    parent = row.get("_parent")
    revision = int(row.get("revision") or 1) + 1
    await db.execute(text(f"""
        UPDATE device_profiles SET
            name = :name, vendor = :vendor, description = :description,
            device_type = :device_type, icon = :icon, match_priority = :prio,
            match_rules = CAST(:mr AS jsonb), oid_groups = CAST(:groups AS jsonb),
            overlay = CAST(:overlay AS jsonb),
            parent_hash = CASE WHEN parent_id IS NULL THEN NULL ELSE :phash END,
            revision = :rev, draft = NULL, draft_updated_at = NULL, draft_updated_by = NULL,
            pilot_device_ids = NULL, pilot_oid_groups = NULL, pilot_started_at = NULL,
            updated_at = now(), updated_by = (SELECT id FROM users WHERE id = :uid)
         WHERE id = :id
    """), {"id": row["id"], "name": content["name"].strip(), "vendor": content.get("vendor"),
           "description": content.get("description"), "device_type": content.get("device_type"),
           "icon": content.get("icon"), "prio": int(content.get("match_priority") or 100),
           "mr": json.dumps(content.get("match_rules") or {}), "groups": json.dumps(new_groups),
           "overlay": json.dumps(content["overlay"]) if content.get("overlay") is not None else None,
           "phash": (parent or {}).get("content_hash"), "rev": revision, "uid": getattr(user, "id", None)})
    await db.execute(text(f"UPDATE device_profiles SET content_hash = {HASH_SQL} WHERE id = :id"), {"id": row["id"]})
    removed = old_keys - group_keys(new_groups)
    devices = await device_ids_on(db, row["id"])
    purged = await purge_groups(db, devices, removed)
    fresh = await get(db, row["id"])
    await record_version(db, row["id"], revision, action, snapshot(fresh), user, reason)
    await resolve_descendants(db, row["id"], user=user)
    fresh = await get(db, row["id"])
    fresh["_purged_readings"] = purged
    return fresh


async def resolve_descendants(db: AsyncSession, pid, *, user: Optional[User] = None, depth: int = 0) -> int:
    """Re-resolve templates customised from ``pid`` whose parent content changed."""
    if depth > ov.MAX_DEPTH:
        return 0
    parent = await get(db, pid)
    if parent is None:
        return 0
    children = (await db.execute(text("SELECT id FROM device_profiles WHERE parent_id = :id"), {"id": pid})).all()
    changed = 0
    for (cid,) in children:
        child = await get(db, cid)
        if child is None or child.get("parent_hash") == parent.get("content_hash"):
            continue
        groups, warnings = ov.resolve(parent["oid_groups"], child.get("overlay") or {})
        removed = group_keys(child["oid_groups"]) - group_keys(groups)
        revision = int(child.get("revision") or 1) + 1
        await db.execute(text(f"""
            UPDATE device_profiles SET oid_groups = CAST(:g AS jsonb), parent_hash = :ph, revision = :rev,
                   pilot_device_ids = NULL, pilot_oid_groups = NULL, pilot_started_at = NULL, updated_at = now()
             WHERE id = :id
        """), {"id": cid, "g": json.dumps(groups), "ph": parent.get("content_hash"), "rev": revision})
        await db.execute(text(f"UPDATE device_profiles SET content_hash = {HASH_SQL} WHERE id = :id"), {"id": cid})
        await purge_groups(db, await device_ids_on(db, cid), removed)
        snap = snapshot(await get(db, cid))
        snap["warnings"] = warnings
        await record_version(db, cid, revision, "parent_update", snap, user,
                             f"Parent template '{parent['name']}' changed to revision {parent['revision']}")
        changed += 1 + await resolve_descendants(db, cid, user=user, depth=depth + 1)
    return changed


async def resync(db: AsyncSession) -> dict:
    """Record templates changed outside the API (a release migration updating a
    built-in) and re-resolve customised templates whose parent changed."""
    await lock(db)
    external = (await db.execute(text(f"""
        SELECT id FROM device_profiles WHERE content_hash IS DISTINCT FROM {HASH_SQL}
    """))).all()
    for (pid,) in external:
        row = await get(db, pid)
        revision = int(row.get("revision") or 1) + 1
        await db.execute(text(f"UPDATE device_profiles SET revision = :rev, content_hash = {HASH_SQL} WHERE id = :id"),
                         {"id": pid, "rev": revision})
        await record_version(db, pid, revision, "external_update", snapshot(await get(db, pid)), None,
                             "Changed outside Device Templates (release update)")
    roots = (await db.execute(text("""
        SELECT DISTINCT c.parent_id FROM device_profiles c JOIN device_profiles p ON p.id = c.parent_id
         WHERE c.parent_hash IS DISTINCT FROM p.content_hash
    """))).all()
    reresolved = 0
    for (pid,) in roots:
        reresolved += await resolve_descendants(db, pid)
    await db.commit()
    return {"external_updates": len(external), "reresolved": reresolved}


async def save_draft(db: AsyncSession, row: dict, content: dict, user: User) -> dict:
    await db.execute(text("""
        UPDATE device_profiles SET draft = CAST(:d AS jsonb), draft_updated_at = now(),
               draft_updated_by = (SELECT id FROM users WHERE id = :uid)
         WHERE id = :id
    """), {"id": row["id"], "d": json.dumps(content), "uid": getattr(user, "id", None)})
    return await get(db, row["id"])


def content_of(row: dict) -> dict:
    return {f: row.get(f) for f in CONTENT_FIELDS}


async def audit(db: AsyncSession, user: Optional[User], action: str, row: dict | None, **meta) -> None:
    await write_audit_log(db, actor=user, action=f"device_template.{action}", resource_type="device_template",
                          resource_id=str(row["id"]) if row else None,
                          metadata={"name": (row or {}).get("name"), **meta})


# ── Assignment ─────────────────────────────────────────────────────────────

async def assign(db: AsyncSession, template: Optional[dict], device_ids: list, user: User, *,
                 source: str = "manual", set_device_type: bool = False) -> dict:
    """Put devices on ``template`` (None = no template). Controller-managed children
    are skipped; readings of the previous template are removed."""
    ids = [str(i) for i in dict.fromkeys(device_ids)]
    if not ids:
        return {"assigned": 0, "skipped": []}
    rows = (await db.execute(text(f"""
        SELECT d.id::text AS id, d.hostname, d.profile_id, {_CHILD} AS is_child
          FROM devices d WHERE d.id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": ids})).mappings().all()
    found = {r["id"]: r for r in rows}
    skipped = [{"id": i, "reason": "not found"} for i in ids if i not in found]
    skipped += [{"id": r["id"], "hostname": r["hostname"],
                 "reason": "controller-managed child: read through its controller's template"}
                for r in rows if r["is_child"]]
    target = str(template["id"]) if template else None
    moving = [r["id"] for r in rows if not r["is_child"] and (str(r["profile_id"]) if r["profile_id"] else None) != target]
    if moving:
        await db.execute(text("DELETE FROM device_template_values WHERE device_id = ANY(CAST(:ids AS uuid[]))"),
                         {"ids": moving})
        await db.execute(text("""
            UPDATE devices SET profile_id = CAST(:pid AS uuid),
                   profile_source = CASE WHEN CAST(:pid AS uuid) IS NULL THEN NULL ELSE :src END,
                   profile_assigned_at = CASE WHEN CAST(:pid AS uuid) IS NULL THEN NULL ELSE now() END,
                   profile_suggested_id = NULL, profile_suggested_at = NULL
             WHERE id = ANY(CAST(:ids AS uuid[]))
        """), {"pid": target, "src": source, "ids": moving})
        await db.execute(text("""
            UPDATE device_profiles
               SET pilot_device_ids = NULLIF(ARRAY(SELECT x FROM unnest(pilot_device_ids) x
                                                   WHERE x <> ALL(CAST(:ids AS uuid[]))), '{}')
             WHERE pilot_device_ids && CAST(:ids AS uuid[])
        """), {"ids": moving})
        await db.execute(text("""
            UPDATE device_profiles SET pilot_oid_groups = NULL, pilot_started_at = NULL
             WHERE pilot_device_ids IS NULL AND pilot_oid_groups IS NOT NULL
        """))
    typed = 0
    if template and set_device_type and template.get("device_type"):
        eligible = [r["id"] for r in rows if not r["is_child"]]
        res = await db.execute(text("""
            UPDATE devices SET device_type = :t, device_type_source = 'template'
             WHERE id = ANY(CAST(:ids AS uuid[])) AND device_type IS DISTINCT FROM :t
        """), {"t": template["device_type"], "ids": eligible})
        typed = res.rowcount or 0
    await audit(db, user, "assign" if template else "unassign", template,
                device_ids=moving, skipped=len(skipped), device_type_set=typed, source=source)
    return {"assigned" if template else "unassigned": len(moving), "unchanged": len(rows) - len(moving) - len(
        [r for r in rows if r["is_child"]]), "device_type_set": typed, "skipped": skipped}


# ── Coverage ───────────────────────────────────────────────────────────────

def defined_metrics(groups: list[dict]) -> set[tuple[str, str]]:
    return {(g.get("key"), m.get("key")) for g in groups or []
            if g.get("display") != "children" for m in g.get("metrics") or []}


async def coverage(db: AsyncSession, row: dict) -> dict:
    """Which defined metrics answered on each device (current readings, last 24 h)."""
    defined = defined_metrics(row.get("oid_groups") or [])
    devices = (await db.execute(text("""
        SELECT d.id::text AS id, d.hostname, host(d.ip_address) AS ip, d.status, d.profile_source,
               d.profile_assigned_at, d.profile_suggested_id::text AS suggested_id, s.name AS suggested_name,
               COALESCE(d.id = ANY(p.pilot_device_ids), FALSE) AS in_pilot
          FROM devices d JOIN device_profiles p ON p.id = d.profile_id
          LEFT JOIN device_profiles s ON s.id = d.profile_suggested_id
         WHERE d.profile_id = :id ORDER BY d.hostname
    """), {"id": row["id"]})).mappings().all()
    answered: dict[str, set] = {}
    if devices:
        vals = (await db.execute(text("""
            SELECT DISTINCT device_id::text, group_key, metric_key FROM device_template_values
             WHERE device_id = ANY(CAST(:ids AS uuid[])) AND updated_at > now() - interval '24 hours'
        """), {"ids": [d["id"] for d in devices]})).all()
        for dev, g, m in vals:
            answered.setdefault(dev, set()).add((g, m))
    out = []
    for d in devices:
        got = answered.get(d["id"], set()) & defined
        out.append({**dict(d), "answered": len(got), "defined": len(defined),
                    "coverage_pct": round(100.0 * len(got) / len(defined), 1) if defined else None,
                    "missing": sorted(f"{g}.{m}" for g, m in defined - got)[:50]})
    pcts = [d["coverage_pct"] for d in out if d["coverage_pct"] is not None]
    return {"defined": len(defined), "devices": out,
            "average_pct": round(sum(pcts) / len(pcts), 1) if pcts else None}


# ── Impact preview ─────────────────────────────────────────────────────────

def oid_count(groups: list[dict]) -> int:
    n = 0
    for g in groups or []:
        n += len(g.get("metrics") or [])
        if g.get("kind") == "table" and (g.get("table") or {}).get("label_oid"):
            n += 1
    return n


def _metric_map(groups: list[dict]) -> dict[str, dict]:
    return {f"{g.get('key')}.{m.get('key')}": m for g in groups or [] for m in g.get("metrics") or []}


def compare(before: list[dict], after: list[dict]) -> dict:
    bg = {g.get("key"): g for g in before or []}
    ag = {g.get("key"): g for g in after or []}
    bm, am = _metric_map(before), _metric_map(after)
    changed_groups = sorted(k for k in set(bg) & set(ag)
                            if json.dumps(bg[k], sort_keys=True) != json.dumps(ag[k], sort_keys=True))
    return {
        "groups_added": sorted(set(ag) - set(bg)), "groups_removed": sorted(set(bg) - set(ag)),
        "groups_changed": changed_groups,
        "metrics_added": sorted(set(am) - set(bm)), "metrics_removed": sorted(set(bm) - set(am)),
        "metrics_changed": sorted(k for k in set(am) & set(bm)
                                  if json.dumps(am[k], sort_keys=True) != json.dumps(bm[k], sort_keys=True)),
        "oids_before": oid_count(before), "oids_after": oid_count(after),
    }


async def impact(db: AsyncSession, row: dict, content: dict) -> dict:
    stored, warnings = stored_content(row, content)
    diff = compare(row.get("oid_groups") or [], stored["oid_groups"])
    removed_metric_keys = sorted({k.split(".", 1)[1] for k in diff["metrics_removed"]}
                                 - {k.split(".", 1)[1] for k in _metric_map(stored["oid_groups"])})
    series = [f"tpl_{k}" for k in removed_metric_keys]
    rules = []
    if series:
        rules = [dict(r) for r in (await db.execute(text("""
            SELECT id::text, name, metric, enabled FROM alert_rules
             WHERE metric = ANY(CAST(:s AS text[]))
                OR EXISTS (SELECT 1 FROM unnest(CAST(:s AS text[])) x WHERE COALESCE(conditions::text, '') LIKE '%"' || x || '"%')
             ORDER BY name
        """), {"s": series})).mappings().all()]
    devices = await device_ids_on(db, row["id"])
    children = (await db.execute(text("""
        SELECT c.name, count(d.id) AS devices FROM device_profiles c
          LEFT JOIN devices d ON d.profile_id = c.id WHERE c.parent_id = :id GROUP BY c.name ORDER BY c.name
    """), {"id": row["id"]})).mappings().all()
    match_changed = (json.dumps(row.get("match_rules") or {}, sort_keys=True)
                     != json.dumps(stored.get("match_rules") or {}, sort_keys=True)
                     or int(row.get("match_priority") or 100) != int(stored.get("match_priority") or 100))
    oid_growth = (diff["oids_after"] / diff["oids_before"]) if diff["oids_before"] else None
    return {
        **diff, "devices": len(devices), "customized_templates": [dict(c) for c in children],
        "alert_rules_on_removed_metrics": rules, "match_rules_changed": match_changed,
        "device_type_changed": row.get("device_type") != stored.get("device_type"),
        "warnings": warnings,
        "needs_confirmation": bool(oid_growth and oid_growth > 2.0 and diff["oids_after"] - diff["oids_before"] > 20),
    }


RESYNC_INTERVAL_S = 300


async def resync_loop() -> None:
    """Background pass: first shortly after startup (a release may have updated
    built-ins), then every five minutes. Every worker may run it; the advisory
    lock serialises them and an unchanged pass writes nothing."""
    from app.core.database import AsyncSessionLocal
    await asyncio.sleep(20)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                summary = await resync(db)
            if summary["external_updates"] or summary["reresolved"]:
                logger.info("device templates resync: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device templates resync failed")
        await asyncio.sleep(RESYNC_INTERVAL_S)
