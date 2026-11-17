"""Discovery rules phase 3: re-evaluation when assets or rules change.

Rules with the ``changed`` trigger keep matching devices, servers and
controller-managed children in line continuously. Once a minute (with the
"when added" sweep, one API worker at a time) ``reconcile_changed``:

1. fingerprints every asset's rule-relevant facts in SQL (md5; status is left
   out, so up/down flips never cause a re-evaluation; map placements and
   dependency parents are in, so a hand edit is noticed);
2. picks the assets whose fingerprint or whose rules revision differs from the
   stored one (``discovery_engine_asset_state``), in bounded batches;
3. evaluates the active ``changed`` rules with ``inventory.evaluate_assets``
   (the same planner as manual runs, including revert on un-match) and
   applies the changes, re-checked value by value;
4. holds the run for approval instead when it would change more than
   ``max_changes_per_run`` values: the whole planned set is stored on the run
   (``plan``) and nothing is applied until someone approves; a rejected plan
   marks those assets evaluated, so it is not proposed again until an asset or
   a rule changes. While a run waits, the pass does nothing; a rule edit makes
   the waiting plan stale and it is set aside (``rejected``, superseded).

Rules in Simulating mode with the ``changed`` trigger record what they would
do and write nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_rules import DiscoveryEngineRun

from . import engine, inventory

logger = logging.getLogger("zenplus.discovery_rules")

BATCH = 500

_MAPS = ("LEFT JOIN (SELECT {col} AS aid, string_agg(map_id::text, ',' ORDER BY map_id) AS v"
         " FROM manual_map_nodes WHERE {col} IS NOT NULL GROUP BY {col}) m ON m.aid = {alias}.id")
_PARENTS = ("LEFT JOIN (SELECT child_device_id AS aid, string_agg(parent_device_id::text, ',' ORDER BY"
            " parent_device_id) AS v FROM topology_dependencies GROUP BY child_device_id) p ON p.aid = d.id")

HASH_SQL = {
    "device": f"""
SELECT 'device' AS kind, d.id::text AS id, md5(concat_ws('|', d.hostname, host(d.ip_address), d.sys_object_id,
       d.sys_descr, d.vendor, d.model, d.device_type, d.os_version, d.serial_number, d.snmp_credential_id::text,
       d.tags::text, d.group_id::text, d.profile_id::text, d.location, d.owner, d.ping_enabled::text,
       d.snmp_enabled::text, d.ping_interval::text, d.snmp_poll_interval::text, m.v, p.v)) AS hash
FROM devices d {_MAPS.format(col='device_id', alias='d')} {_PARENTS}
WHERE d.poll_mode IS DISTINCT FROM 'via_controller'""",
    "child": f"""
SELECT 'child' AS kind, d.id::text AS id, md5(concat_ws('|', d.hostname, host(d.ip_address), d.vendor, d.model,
       d.device_type, d.os_version, d.serial_number, d.tags::text, d.group_id::text, d.location, d.owner,
       d.managed_source, c.hostname, m.v)) AS hash
FROM devices d LEFT JOIN devices c ON c.id = d.managed_by_device_id {_MAPS.format(col='device_id', alias='d')}
WHERE d.poll_mode = 'via_controller'""",
    "server": f"""
SELECT 'server' AS kind, s.id::text AS id, md5(concat_ws('|', s.display_name, s.hostname, s.fqdn, host(s.primary_ip),
       s.os_name, s.os_version, s.os_type, s.collection_mode, s.environment, s.owner, s.tags::text, m.v)) AS hash
FROM servers s {_MAPS.format(col='server_id', alias='s')}""",
}


async def candidates(db: AsyncSession, kinds: set[str], revision: str, *, limit: int) -> list[tuple[str, str, str]]:
    """[(kind, id, hash)] whose facts or rules revision changed since the last evaluation."""
    parts = [HASH_SQL[k] for k in ("device", "child", "server") if k in kinds]
    if not parts:
        return []
    sql = ("WITH h AS (" + " UNION ALL ".join(parts) + ")"
           " SELECT h.kind, h.id, h.hash FROM h LEFT JOIN discovery_engine_asset_state st"
           " ON st.asset_kind = h.kind AND st.asset_id = CAST(h.id AS uuid)"
           " WHERE st.facts_hash IS DISTINCT FROM h.hash OR st.rules_revision IS DISTINCT FROM :rev"
           " ORDER BY h.kind, h.id LIMIT :lim")
    return [tuple(r) for r in (await db.execute(text(sql), {"rev": revision, "lim": limit})).all()]


async def _load(db: AsyncSession, picked: list[tuple[str, str, str]]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for k in sorted({k for k, _, _ in picked}):
        ids = [i for kk, i, _ in picked if kk == k]
        out.extend(await inventory.load_assets(db, {"kinds": [k], f"{k}_ids": ids}, limit=len(ids)))
    return out


async def _mark(db: AsyncSession, picked: list[tuple[str, str, str]], revision: str, plans: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    for kind, aid, h in picked:
        matched = [m["rule_id"] for m in (plans or {}).get((kind, aid), {}).get("matched", [])]
        await db.execute(text(
            "INSERT INTO discovery_engine_asset_state (asset_kind, asset_id, facts_hash, rules_revision, evaluated_at,"
            " matched_rule_ids) VALUES (:k, CAST(:id AS uuid), :h, :rev, :now, CAST(:m AS uuid[]))"
            " ON CONFLICT (asset_kind, asset_id) DO UPDATE SET facts_hash = EXCLUDED.facts_hash,"
            " rules_revision = EXCLUDED.rules_revision, evaluated_at = EXCLUDED.evaluated_at,"
            " matched_rule_ids = COALESCE(EXCLUDED.matched_rule_ids, discovery_engine_asset_state.matched_rule_ids)"),
            {"k": kind, "id": aid, "h": h, "rev": revision, "now": now, "m": matched or None})


async def changed_rules(db: AsyncSession) -> tuple[list[dict], list[dict], str]:
    from .runner import load_rules
    every = await load_rules(db, modes=("active", "simulate"))
    rules = [r for r in every if "changed" in (r.get("triggers") or [])]
    active = [r for r in rules if r["mode"] == "active"]
    simulating = [r for r in rules if r["mode"] == "simulate"]
    return active, simulating, engine.rules_revision(rules)


async def reconcile_changed(db: AsyncSession, *, batch: int = BATCH, apply_credential=None) -> dict:
    """One pass (see the module docstring). Commits."""
    from .runner import load_settings

    settings = await load_settings(db)
    if not settings["enabled"]:
        return {"skipped": "engine_off"}
    active, simulating, revision = await changed_rules(db)
    if not active and not simulating:
        return {"skipped": "no_rules"}
    await db.execute(text(inventory.APPLY_LOCK))
    waiting = (await db.execute(text(
        "SELECT id, rules_revision FROM discovery_engine_runs WHERE status = 'needs_approval'"
        " AND trigger = 'changed' ORDER BY started_at LIMIT 1"))).first()
    if waiting is not None:
        if waiting[1] == revision:
            await db.rollback()
            return {"skipped": "waiting_for_approval", "engine_run_id": str(waiting[0])}
        await db.execute(text(
            "UPDATE discovery_engine_runs SET status = 'rejected', finished_at = now(),"
            " detail = 'Superseded: the rules changed while it waited; planned again' WHERE id = :id"),
            {"id": waiting[0]})
    kinds = {k for r in active + simulating for k in (r.get("asset_kinds") or ["device"])}
    found = await candidates(db, kinds, revision, limit=inventory.MAX_ASSETS)
    if not found:
        await db.commit()
        return {"assets": 0}
    picked = found[:batch]
    assets = await _load(db, picked)
    owned = await inventory.load_owned(db, assets)
    changes, matched, plans = inventory.evaluate_assets(assets, active, owned, trigger="changed")
    writes = [c for c in changes if c["outcome"] in ("apply", "revert")]
    cap = int(settings.get("max_changes_per_run") or 100)
    out: dict[str, Any] = {"assets": len(picked), "matched": matched}

    if len(writes) > cap:
        # Plan everything that changed, so one approval covers it.
        if len(found) > len(picked):
            picked = found
            assets = await _load(db, picked)
            owned = await inventory.load_owned(db, assets)
            changes, matched, plans = inventory.evaluate_assets(assets, active, owned, trigger="changed")
            writes = [c for c in changes if c["outcome"] in ("apply", "revert")]
        names = {(k, str(r["id"])): (r.get("hostname") or r.get("display_name"),
                                     r.get("ip_address") or r.get("primary_ip")) for k, r in assets}
        for c in changes:
            c["asset_name"], c["ip_address"] = names.get((c["asset_kind"], c["asset_id"]), (None, None))
        run = DiscoveryEngineRun(
            trigger="changed", mode="apply", status="needs_approval", rules_revision=revision,
            plan={"changes": inventory._jsonable(changes), "assets": [list(p) for p in picked]},
            counts={**inventory.summarise(changes, len(picked), matched), "cap": cap},
            detail=f"{len(writes)} changes exceed the limit of {cap} per automatic run; waiting for approval")
        db.add(run)
        await db.commit()
        logger.warning("discovery rules: %d changes held for approval (run %s)", len(writes), run.id)
        return {**out, "needs_approval": str(run.id), "changes": len(writes)}

    if changes:
        run = DiscoveryEngineRun(trigger="changed", mode="apply", rules_revision=revision, status="running",
                                 targets={"assets": len(picked)})
        db.add(run)
        await db.flush()
        counts = await inventory.apply_changes(db, changes, run, apply_credential=apply_credential)
        run.counts = {**inventory.summarise(changes, len(picked), matched), **counts}
        run.status = "partial" if counts["skipped_changed"] else "completed"
        run.finished_at = datetime.now(timezone.utc)
        out.update(counts, engine_run_id=str(run.id))
    out["simulated"] = await inventory.record_simulation(db, assets, active, simulating, owned, trigger="changed")
    await _mark(db, picked, revision, plans)
    await db.commit()
    return out


class ApprovalRefused(Exception):
    pass


async def decide(db: AsyncSession, run_id: uuid.UUID, *, approve: bool, actor_id, apply_credential=None) -> dict:
    """Approve (apply the stored plan, re-checked value by value) or reject a
    run held for approval. Either way its assets are marked evaluated at the
    rules revision it was planned for. Commits."""
    await db.execute(text(inventory.APPLY_LOCK))
    run = await db.get(DiscoveryEngineRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("Run not found")
    if run.status != "needs_approval":
        raise ApprovalRefused("This run is not waiting for approval")
    _, _, revision = await changed_rules(db)
    plan = run.plan or {}
    picked = [tuple(p) for p in plan.get("assets") or []]
    now = datetime.now(timezone.utc)
    if approve and run.rules_revision != revision:
        run.status, run.finished_at = "rejected", now
        run.detail = "Superseded: the rules changed since this plan was made; it will be planned again"
        await db.commit()
        raise ApprovalRefused(run.detail)
    run.approved_by, run.approved_at = actor_id, now
    if approve:
        counts = await inventory.apply_changes(db, list(plan.get("changes") or []), run,
                                               apply_credential=apply_credential)
        run.counts = {**(run.counts or {}), **counts}
        run.status = "partial" if counts["skipped_changed"] else "completed"
        run.detail = "Approved"
    else:
        run.status = "rejected"
        run.detail = "Rejected; not proposed again until an asset or a rule changes"
        counts = {}
    run.finished_at = now
    await _mark(db, picked, run.rules_revision)
    await db.commit()
    return {"engine_run_id": str(run.id), "status": run.status, **counts}


async def rules_pass(db: AsyncSession) -> dict:
    """The once-a-minute background pass: new assets, then changed ones."""
    out = {"created": await inventory.sweep_created(db)}
    out["changed"] = await reconcile_changed(db)
    return out


def pending_summary(run: DiscoveryEngineRun) -> dict:
    plan = run.plan or {}
    return {"changes": plan.get("changes") or [], "assets": len(plan.get("assets") or [])}

