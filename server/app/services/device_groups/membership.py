"""Group membership: what the alert evaluators ask, and the pass that keeps
dynamic groups current.

Membership lives in ``device_group_members`` (source ``primary`` from
``devices.group_id`` via trigger, ``static`` added by hand, ``rule`` from a
dynamic group's condition). ``device_group_member_closure`` rolls members up
to every ancestor group: a rule on a parent group covers its subgroups.

Evaluators call :func:`groups_of` (one asset) or :func:`load` (a whole pass)
and test a rule with :func:`in_group`. Both are tolerant: if the membership
table cannot be read (an API ahead of migration 192) they return ``None`` and
:func:`in_group` falls back to the device's primary group, which is exactly
the old behaviour.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.device_groups import facts as gfacts
from app.services.discovery_rules import conditions

logger = logging.getLogger(__name__)

LOCK_KEY = "device-groups-reconcile"
RECONCILE_INTERVAL_S = 60
SAMPLE = 25


# ── evaluator side ─────────────────────────────────────────────────────────

async def groups_of(db: AsyncSession, kind: str, asset_id: Any) -> Optional[set[str]]:
    """Ids of every group (with ancestors) the asset belongs to; None if unavailable."""
    if not asset_id:
        return set()
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT group_id::text FROM device_group_member_closure WHERE asset_kind = :k AND asset_id = :id"),
                {"k": kind, "id": str(asset_id)})).scalars().all()
        return set(rows)
    except Exception:
        logger.debug("group membership unavailable; using primary groups", exc_info=True)
        return None


async def load(db: AsyncSession, kind: str = "device") -> Optional[dict[str, set[str]]]:
    """{asset_id: {group ids}} for one evaluator pass; None if unavailable."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT asset_id::text, group_id::text FROM device_group_member_closure WHERE asset_kind = :k"),
                {"k": kind})).all()
    except Exception:
        logger.debug("group membership unavailable; using primary groups", exc_info=True)
        return None
    out: dict[str, set[str]] = {}
    for asset, group in rows:
        out.setdefault(asset, set()).add(group)
    return out


def in_group(rule_group_id: Any, asset_groups: Optional[Iterable[str]], primary_group_id: Any = None) -> bool:
    """Whether a rule scoped to ``rule_group_id`` covers the asset. No group scope: True."""
    if not rule_group_id:
        return True
    if asset_groups is None:
        return bool(primary_group_id) and str(primary_group_id) == str(rule_group_id)
    return str(rule_group_id) in asset_groups


# ── dynamic groups ─────────────────────────────────────────────────────────

def _matches(group: dict, assets: list[tuple[str, str, dict]], fields: dict) -> set[tuple[str, str]]:
    kinds = set(group.get("asset_kinds") or ["device"])
    cond = group["condition"]
    return {(k, i) for k, i, f in assets if k in kinds and conditions.evaluate(cond, f, fields=fields)}


async def preview(db: AsyncSession, condition: dict, asset_kinds: list[str], *, scope: Optional[list[str]] = None) -> dict:
    """Who a condition would include now (count by kind and a sample), without saving."""
    defs = await gfacts.custom_defs(db)
    fields, enums = gfacts.catalog(defs)
    cond = conditions.validate(condition, fields=fields, enums=enums)
    assets = await gfacts.load_assets(db, tuple(asset_kinds))
    hit = [(k, i, f) for k, i, f in assets if k in asset_kinds and conditions.evaluate(cond, f, fields=fields)]
    if scope is not None:
        from app.core import scoping
        hit = [x for x in hit if scoping.entity_visible(x[2].get("tags"), scope)]
    by_kind: dict[str, int] = {}
    for k, _, _ in hit:
        by_kind[k] = by_kind.get(k, 0) + 1
    sample = [{"asset_kind": k, "id": i, "name": f.get("name"), "ip_address": f.get("ip_address"),
               "vendor": f.get("vendor"), "device_type": f.get("device_type")} for k, i, f in hit[:SAMPLE]]
    return {"count": len(hit), "by_kind": by_kind, "sample": sample,
            "total_considered": sum(1 for k, _, _ in assets if k in asset_kinds)}


_last_fingerprint: dict[str, str] = {}


async def reconcile(db: AsyncSession, *, group_ids: Optional[list[str]] = None, force: bool = False) -> dict:
    """Recompute dynamic group members and every group's member count.

    One worker at a time (transaction advisory lock; a busy lock skips the pass).
    A pass whose facts and conditions are unchanged since the last one in this
    process writes nothing. A condition that fails to evaluate keeps the previous
    members and records ``eval_error``."""
    got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": LOCK_KEY})).scalar()
    if not got:
        return {"skipped": "busy"}
    sql = "SELECT id::text AS id, condition, asset_kinds FROM device_groups WHERE kind = 'dynamic'"
    params: dict = {}
    if group_ids:
        sql += " AND id = ANY(CAST(:ids AS uuid[]))"
        params["ids"] = [str(g) for g in group_ids]
    groups = [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]
    defs = await gfacts.custom_defs(db)
    fields, _ = gfacts.catalog(defs)
    assets = await gfacts.load_assets(db)

    if not group_ids and not force:
        fp = hashlib.sha256(json.dumps([groups, assets, defs], sort_keys=True, default=str).encode()).hexdigest()
        if _last_fingerprint.get("all") == fp:
            await db.rollback()
            return {"skipped": "unchanged"}
    else:
        fp = None

    added = removed = 0
    errors = 0
    for g in groups:
        try:
            cond = g["condition"] if isinstance(g["condition"], dict) else json.loads(g["condition"])
            want = _matches({**g, "condition": cond}, assets, fields)
            err = None
        except Exception as exc:          # a broken stored condition never empties the group
            errors += 1
            await db.execute(text("UPDATE device_groups SET eval_error = :e, evaluated_at = now() WHERE id = :id"),
                             {"e": str(exc)[:500], "id": g["id"]})
            continue
        have = {(k, i) for k, i in (await db.execute(text(
            "SELECT asset_kind, asset_id::text FROM device_group_members WHERE group_id = :g AND source = 'rule'"),
            {"g": g["id"]})).all()}
        add, drop = want - have, have - want
        for kind in ("device", "server"):
            ids = [i for k, i in add if k == kind]
            if ids:
                await db.execute(text("""
                    INSERT INTO device_group_members (group_id, asset_kind, asset_id, source)
                    SELECT :g, :k, x, 'rule' FROM unnest(CAST(:ids AS uuid[])) x ON CONFLICT DO NOTHING
                """), {"g": g["id"], "k": kind, "ids": ids})
            ids = [i for k, i in drop if k == kind]
            if ids:
                await db.execute(text("""
                    DELETE FROM device_group_members
                     WHERE group_id = :g AND asset_kind = :k AND source = 'rule' AND asset_id = ANY(CAST(:ids AS uuid[]))
                """), {"g": g["id"], "k": kind, "ids": ids})
        added += len(add)
        removed += len(drop)
        await db.execute(text("UPDATE device_groups SET eval_error = :e, evaluated_at = now() WHERE id = :id"),
                         {"e": err, "id": g["id"]})
    await refresh_counts(db)
    await db.commit()
    if fp:
        _last_fingerprint["all"] = fp
    return {"groups": len(groups), "added": added, "removed": removed, "errors": errors}


async def refresh_counts(db: AsyncSession) -> None:
    await db.execute(text("""
        UPDATE device_groups g SET member_count = c.n
          FROM (SELECT gg.id, (SELECT count(*) FROM device_group_member_closure m WHERE m.group_id = gg.id) AS n
                  FROM device_groups gg) c
         WHERE c.id = g.id AND g.member_count IS DISTINCT FROM c.n
    """))


async def reconcile_loop() -> None:
    """Background pass: every minute. Every worker may run it; the lock serialises them."""
    from app.core.database import AsyncSessionLocal
    await asyncio.sleep(30)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                summary = await reconcile(db)
            if summary.get("added") or summary.get("removed") or summary.get("errors"):
                logger.info("device groups reconcile: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device groups reconcile failed")
        await asyncio.sleep(RECONCILE_INTERVAL_S)
