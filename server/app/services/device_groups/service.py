"""Device groups: tree, kinds, what uses a group, static members.

A group is ``static`` (members chosen: devices whose primary group it is, plus
devices or servers added explicitly) or ``dynamic`` (members computed from a
condition). The kind never changes after creation. Groups nest up to
``MAX_DEPTH`` levels; a group's members include its subgroups' members.

A group in use (alert rules, maintenance windows, sensor assignments,
discovery profiles or rules, subgroups, devices whose primary group it is) is
never deleted: the database would cascade-delete its alert rules and
maintenance windows, so the API refuses and lists what uses it.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.device_groups import facts as gfacts
from app.services.discovery_rules import conditions

MAX_DEPTH = 5

GROUP_SQL = """
    SELECT g.id::text AS id, g.name, g.description, g.color, g.parent_id::text AS parent_id, g.kind,
           g.condition, g.asset_kinds, g.member_count, g.evaluated_at, g.eval_error, g.created_at, g.updated_at,
           (SELECT count(*) FROM devices d WHERE d.group_id = g.id) AS primary_count,
           (SELECT count(*) FROM device_group_members m WHERE m.group_id = g.id AND m.source = 'static') AS static_count,
           (SELECT count(*) FROM device_groups c WHERE c.parent_id = g.id) AS child_count
      FROM device_groups g
"""


def _row(r) -> dict:
    d = dict(r)
    if isinstance(d.get("condition"), str):
        d["condition"] = json.loads(d["condition"])
    d["asset_kinds"] = list(d.get("asset_kinds") or ["device"])
    return d


async def get(db: AsyncSession, gid) -> Optional[dict]:
    r = (await db.execute(text(GROUP_SQL + " WHERE g.id = CAST(:id AS uuid)"), {"id": str(gid)})).mappings().first()
    return _row(r) if r else None


async def get_or_404(db: AsyncSession, gid) -> dict:
    g = await get(db, gid)
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return g


async def list_all(db: AsyncSession) -> list[dict]:
    return [_row(r) for r in (await db.execute(text(GROUP_SQL + " ORDER BY g.name"))).mappings().all()]


async def validate_condition(db: AsyncSession, condition: Any) -> dict:
    defs = await gfacts.custom_defs(db)
    fields, enums = gfacts.catalog(defs)
    try:
        return conditions.validate(condition, fields=fields, enums=enums)
    except conditions.ConditionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def check_parent(db: AsyncSession, gid: Optional[str], parent_id: Optional[str]) -> None:
    """Parent exists, is not the group or one of its descendants, and depth stays within MAX_DEPTH."""
    if not parent_id:
        return
    if gid and str(parent_id) == str(gid):
        raise HTTPException(status_code=422, detail="A group cannot be its own parent")
    parent = await get(db, parent_id)
    if parent is None:
        raise HTTPException(status_code=422, detail="Parent group not found")
    if gid:
        descendants = set((await db.execute(text(
            "SELECT group_id::text FROM device_group_ancestry WHERE ancestor_id = CAST(:g AS uuid)"),
            {"g": str(gid)})).scalars().all())
        if str(parent_id) in descendants:
            raise HTTPException(status_code=422, detail="A group cannot be moved under one of its own subgroups")
    depth = (await db.execute(text(
        "SELECT count(*) FROM device_group_ancestry WHERE group_id = CAST(:p AS uuid)"), {"p": str(parent_id)})).scalar() or 1
    below = 0
    if gid:
        below = (await db.execute(text("""
            WITH RECURSIVE d(id, depth) AS (
                SELECT id, 0 FROM device_groups WHERE id = CAST(:g AS uuid)
                UNION ALL SELECT c.id, d.depth + 1 FROM device_groups c JOIN d ON c.parent_id = d.id WHERE d.depth < 8)
            SELECT max(depth) FROM d"""), {"g": str(gid)})).scalar() or 0
    if depth + 1 + below > MAX_DEPTH:
        raise HTTPException(status_code=422, detail=f"Groups nest at most {MAX_DEPTH} levels deep")


async def used_by(db: AsyncSession, gid: str) -> dict:
    """Everything that refers to the group (the delete guard and the group page's "Used by")."""
    p = {"g": str(gid)}

    async def rows(sql: str) -> list[dict]:
        try:
            async with db.begin_nested():
                return [dict(r) for r in (await db.execute(text(sql), p)).mappings().all()]
        except Exception:          # a table from a later migration may be absent
            return []

    return {
        "alert_rules": await rows("SELECT id::text, name, enabled FROM alert_rules WHERE group_id = CAST(:g AS uuid) ORDER BY name"),
        "maintenance": await rows("""SELECT id::text, reason, starts_at, ends_at FROM device_maintenance
                                     WHERE scope_type = 'group' AND scope_group_id = CAST(:g AS uuid) ORDER BY starts_at DESC"""),
        "sensor_assignments": await rows("""SELECT a.sensor_id::text AS id, s.name FROM sensor_assignments a
                                            LEFT JOIN sensors s ON s.id = a.sensor_id
                                            WHERE a.target_type = 'group' AND a.target_id = CAST(:g AS uuid)"""),
        "discovery_profiles": await rows("SELECT id::text, name FROM discovery_profiles WHERE default_group_id = CAST(:g AS uuid)"),
        "discovery_rules": await rows("""SELECT id::text, name FROM discovery_engine_rules
                                         WHERE actions::text LIKE '%' || :g || '%' ORDER BY name"""),
        "subgroups": await rows("SELECT id::text, name FROM device_groups WHERE parent_id = CAST(:g AS uuid) ORDER BY name"),
        # Rule overrides for the group's members (migrate-193).
        "alert_overrides": await rows("""SELECT o.id::text, r.id::text AS rule_id, r.name FROM alert_rule_overrides o
                                         JOIN alert_rules r ON r.id = o.rule_id
                                         WHERE o.group_id = CAST(:g AS uuid) ORDER BY r.name"""),
        "primary_devices": (await db.execute(text("SELECT count(*) FROM devices WHERE group_id = CAST(:g AS uuid)"), p)).scalar() or 0,
    }


def in_use(u: dict) -> list[str]:
    reasons = []
    for key, label in (("alert_rules", "alert rule"), ("maintenance", "maintenance window"),
                       ("sensor_assignments", "sensor assignment"), ("discovery_profiles", "discovery profile"),
                       ("discovery_rules", "discovery rule"), ("subgroups", "subgroup"),
                       ("alert_overrides", "alert rule override")):
        if u.get(key):
            reasons.append(f"{len(u[key])} {label}(s)")
    if u.get("primary_devices"):
        reasons.append(f"{u['primary_devices']} device(s) whose group it is")
    return reasons


async def assert_static(db: AsyncSession, group_id: Any, *, what: str = "A device's group") -> None:
    """A device's primary group (devices.group_id) must be a static group."""
    if not group_id:
        return
    kind = (await db.execute(text("SELECT kind FROM device_groups WHERE id = CAST(:g AS uuid)"),
                             {"g": str(group_id)})).scalar()
    if kind is None:
        raise HTTPException(status_code=422, detail="Group not found")
    if kind != "static":
        raise HTTPException(status_code=422, detail=f"{what} must be a static group: members of a dynamic group "
                                                    "come from its condition")


async def add_static(db: AsyncSession, group: dict, kind: str, ids: list[str]) -> int:
    if group["kind"] != "static":
        raise HTTPException(status_code=422, detail="Members of a dynamic group come from its condition")
    if kind not in group["asset_kinds"]:
        raise HTTPException(status_code=422, detail=f"This group holds {', '.join(group['asset_kinds'])} only")
    table = "devices" if kind == "device" else "servers"
    found = set((await db.execute(text(f"SELECT id::text FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                                  {"ids": ids})).scalars().all())
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status_code=404, detail=f"{kind.title()} not found: {missing[0]}")
    res = await db.execute(text("""
        INSERT INTO device_group_members (group_id, asset_kind, asset_id, source)
        SELECT CAST(:g AS uuid), :k, x, 'static' FROM unnest(CAST(:ids AS uuid[])) x ON CONFLICT DO NOTHING
    """), {"g": group["id"], "k": kind, "ids": ids})
    return res.rowcount or 0


async def remove_static(db: AsyncSession, group: dict, kind: str, ids: list[str]) -> int:
    res = await db.execute(text("""
        DELETE FROM device_group_members
         WHERE group_id = CAST(:g AS uuid) AND asset_kind = :k AND source = 'static' AND asset_id = ANY(CAST(:ids AS uuid[]))
    """), {"g": group["id"], "k": kind, "ids": ids})
    return res.rowcount or 0


async def members(db: AsyncSession, gid: str, *, limit: int = 500) -> list[dict]:
    """Members including subgroups', with how each one is a member and its name."""
    rows = (await db.execute(text("""
        WITH c AS (
            SELECT m.asset_kind, m.asset_id, m.source, m.group_id
              FROM device_group_members m
              JOIN device_group_ancestry ga ON ga.group_id = m.group_id
             WHERE ga.ancestor_id = CAST(:g AS uuid))
        SELECT c.asset_kind, c.asset_id::text AS id,
               array_agg(DISTINCT c.source) AS sources,
               bool_or(c.group_id <> CAST(:g AS uuid)) AS via_subgroup,
               COALESCE(max(d.hostname), max(s.display_name), max(s.hostname)) AS name,
               COALESCE(max(host(d.ip_address)), max(host(s.primary_ip))) AS ip_address,
               COALESCE(max(d.status), max(s.status)) AS status,
               COALESCE(max(d.tags::text), max(s.tags::text)) AS tags
          FROM c
          LEFT JOIN devices d ON c.asset_kind = 'device' AND d.id = c.asset_id
          LEFT JOIN servers s ON c.asset_kind = 'server' AND s.id = c.asset_id
         GROUP BY c.asset_kind, c.asset_id
         ORDER BY name NULLS LAST
         LIMIT :lim
    """), {"g": str(gid), "lim": limit})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        out.append(d)
    return out
