"""Engine settings, rule loading, preview, and evaluation of a discovery scan.

``evaluate_discovery_run`` runs at the end of every scan while the engine is
on (``system_settings['discovery_rules'].enabled``, off by default) and when
an operator re-evaluates a past scan. ``preview`` computes the same plans and
dispositions without writing anything; both go through
``engine.evaluate`` / ``engine.disposition`` / ``assign_capacity``.

Auto-import goes through ``app.services.discovery_import`` under the
``discovery-import`` lock and the license budget (``license_capacity.reserve``):
at most ``max_auto_import_per_run`` results per evaluation, and
``license_reserve_nodes`` nodes are always left free for manual adds. Results
that do not fit stay in review with the reason. Nothing that already exists
is changed: existing devices are only annotated.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_rules import DiscoveryEngineRule, DiscoveryEngineRun, DiscoveryEngineRunItem
from app.models.discovery_v2 import (
    DiscoveryIgnoredDevice,
    DiscoveryProfile,
    DiscoveryResultV2,
    DiscoveryRun,
)
from app.schemas.discovery_v2 import ImportRequest
from app.services import license_capacity

from . import engine
from .facts import facts_from_result, sample_facts

logger = logging.getLogger("zenplus.discovery_rules")

SETTINGS_KEY = "discovery_rules"
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "max_auto_import_per_run": 50,
    "license_reserve_nodes": 0,
    "history_days": 180,
    # Automatic re-evaluation ("changed" trigger): a pass that would change
    # more values than this waits for approval instead (phase 3).
    "max_changes_per_run": 100,
}
# Result statuses the rules look at; failed (no answer) and already-ignored
# addresses carry no facts worth classifying.
EVALUATED_STATUSES = ("new", "existing", "changed", "unknown")
ACTIONABLE = ("auto_import", "ignore")


# ── settings ────────────────────────────────────────────────────────────────

async def load_settings(db: AsyncSession) -> dict:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": SETTINGS_KEY})).first()
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    out = dict(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        out.update({k: value[k] for k in DEFAULT_SETTINGS if k in value})
    return out


async def save_settings(db: AsyncSession, values: dict, user_id) -> dict:
    current = await load_settings(db)
    current.update({k: values[k] for k in DEFAULT_SETTINGS if k in values})
    await db.execute(text(
        "INSERT INTO system_settings (key, value, updated_at, updated_by)"
        " VALUES (:k, CAST(:v AS jsonb), now(), :u)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),"
        " updated_by = EXCLUDED.updated_by"),
        {"k": SETTINGS_KEY, "v": json.dumps(current), "u": user_id})
    return current


# ── rules ───────────────────────────────────────────────────────────────────

def rule_dict(row: DiscoveryEngineRule) -> dict:
    return {
        "id": str(row.id), "name": row.name, "description": row.description, "priority": row.priority,
        "revision": row.revision,
        "mode": row.mode, "asset_kinds": list(row.asset_kinds or ["device"]),
        "triggers": list(row.triggers or ["discovery"]),
        "profile_ids": [str(p) for p in row.profile_ids] if row.profile_ids else None,
        "condition": row.condition, "actions": list(row.actions or []),
        "stop_processing": bool(row.stop_processing),
        "enforcement": row.enforcement or "set_once",
        "on_unmatch": row.on_unmatch or "keep",
    }


async def load_rules(db: AsyncSession, modes: Iterable[str] = ("active",)) -> list[dict]:
    rows = (await db.execute(
        select(DiscoveryEngineRule).where(DiscoveryEngineRule.mode.in_(tuple(modes)))
        .order_by(DiscoveryEngineRule.priority)
    )).scalars().all()
    return [rule_dict(r) for r in rows]


# ── shared disposition logic (preview == apply) ─────────────────────────────

def plan_result(result: Any, rules: list[dict], *, import_mode: str | None) -> tuple[dict, str, str | None]:
    """(plan, disposition, reason) for one scan result, before capacity."""
    plan = engine.evaluate(facts_from_result(result), rules, trigger="discovery")
    disp, reason = engine.disposition(plan, import_mode=import_mode,
                                      result_status=getattr(result, "status", None) if not isinstance(result, dict)
                                      else result.get("status"),
                                      import_ready=bool(getattr(result, "import_ready", False)
                                                        if not isinstance(result, dict)
                                                        else result.get("import_ready")))
    return plan, disp, reason


def assign_capacity(planned: list[list], *, cap: int, room: int | None) -> None:
    """Hold back auto-imports beyond the per-run cap or the license room.

    ``planned`` rows are ``[result_id, plan, disposition, reason]`` in result
    order; rows are changed in place. ``room`` None means unlimited. The
    estimate counts one node per result (a server linked to a new device
    shares its node, so this never under-counts)."""
    taken = 0
    for row in planned:
        if row[2] != "auto_import":
            continue
        if taken >= cap:
            row[2], row[3] = "review", "cap"
            continue
        if room is not None and taken >= room:
            row[2], row[3] = "review", "license_capacity_reached"
            continue
        taken += 1


async def license_room(db: AsyncSession, reserve_nodes: int) -> int | None:
    cap = await license_capacity.get_capacity(db)
    if not cap.enforced:
        return None
    available = cap.available or 0 if cap.can_add else 0
    return max(0, available - max(0, int(reserve_nodes or 0)))


def _suggestions(plan: dict, profile: DiscoveryProfile | None) -> tuple[Any, Any, list]:
    group = engine.setting(plan, "group") or (profile.default_group_id if profile else None)
    template = engine.setting(plan, "template") or (profile.default_template_id if profile else None)
    tags: list[str] = []
    for t in list((profile.default_tags if profile else None) or []) + engine.tag_values(plan):
        if t and str(t).lower() not in {x.lower() for x in tags}:
            tags.append(str(t))
    return (uuid.UUID(str(group)) if group else None,
            uuid.UUID(str(template)) if template else None, tags)


# ── preview ─────────────────────────────────────────────────────────────────

async def preview(db: AsyncSession, rules: list[dict], *, discovery_run_id: uuid.UUID | None = None,
                  facts: dict | None = None, import_mode: str | None = None, limit: int = 1000) -> dict:
    """Plans and dispositions for a past scan (or hand-typed facts) under
    ``rules``. Writes nothing."""
    settings = await load_settings(db)
    if facts is not None:
        f = sample_facts(facts)
        plan = engine.evaluate(f, rules, trigger="discovery")
        disp, reason = engine.disposition(plan, import_mode=import_mode or "auto_match",
                                          result_status=f.get("result_status") or "new", import_ready=True)
        return {"rows": [{"result_id": None, "facts": f, "plan": plan, "disposition": disp, "reason": reason}],
                "summary": _summary([disp]), "import_mode": import_mode or "auto_match",
                "license_room": None, "max_auto_import_per_run": settings["max_auto_import_per_run"]}

    run = await db.get(DiscoveryRun, discovery_run_id)
    if not run:
        raise LookupError("Scan not found")
    profile = await db.get(DiscoveryProfile, run.profile_id) if run.profile_id else None
    mode = import_mode or (profile.import_mode if profile else "review")
    results = (await db.execute(
        select(DiscoveryResultV2).where(DiscoveryResultV2.run_id == run.id,
                                        DiscoveryResultV2.status.in_(EVALUATED_STATUSES),
                                        DiscoveryResultV2.imported.is_(False))
        .order_by(DiscoveryResultV2.id).limit(limit)
    )).scalars().all()
    planned = [[r.id, *plan_result(r, rules, import_mode=mode)] for r in results]
    room = await license_room(db, settings["license_reserve_nodes"])
    assign_capacity(planned, cap=int(settings["max_auto_import_per_run"]), room=room)
    by_id = {r.id: r for r in results}
    rows = []
    for rid, plan, disp, reason in planned:
        r = by_id[rid]
        rows.append({
            "result_id": rid, "ip_address": str(r.ip_address), "hostname": r.hostname or r.sys_name,
            "vendor": r.vendor, "model": r.model, "device_type": r.device_type,
            "sys_object_id": r.sys_object_id, "status": r.status,
            "plan": plan, "disposition": disp, "reason": reason,
        })
    return {"rows": rows, "summary": _summary([p[2] for p in planned]), "import_mode": mode,
            "license_room": room, "max_auto_import_per_run": settings["max_auto_import_per_run"],
            "discovery_run_id": str(run.id), "profile_name": profile.name if profile else None}


def _summary(dispositions: list[str]) -> dict:
    out: dict[str, int] = {"evaluated": len(dispositions)}
    for d in dispositions:
        out[d] = out.get(d, 0) + 1
    out["matched"] = len(dispositions) - out.get("no_match", 0)
    return out


# ── evaluation of a scan ────────────────────────────────────────────────────

def _item(run_id, result: DiscoveryResultV2, **kw) -> DiscoveryEngineRunItem:
    return DiscoveryEngineRunItem(run_id=run_id, asset_kind=kw.pop("asset_kind", "discovery_result"),
                                  discovery_result_id=result.id, ip_address=str(result.ip_address), **kw)


async def evaluate_discovery_run(db: AsyncSession, discovery_run_id: uuid.UUID, *, trigger: str,
                                 actor_id: uuid.UUID | None = None, apply: bool = True,
                                 rules: list[dict] | None = None) -> dict:
    """Evaluate the active rules on one scan's results, store the plans and,
    with ``apply``, act on the decisions the profile's import mode allows.

    Commits. Returns the engine run's counts."""
    from app.services import discovery_import  # avoid an import cycle at module load

    settings = await load_settings(db)
    run = await db.get(DiscoveryRun, discovery_run_id)
    if not run:
        raise LookupError("Scan not found")
    profile = await db.get(DiscoveryProfile, run.profile_id) if run.profile_id else None
    mode = profile.import_mode if profile else "review"
    rules = rules if rules is not None else await load_rules(db)
    rule_by_id = {r["id"]: r for r in rules}

    erun = DiscoveryEngineRun(trigger=trigger, mode="apply" if apply else "annotate",
                              discovery_run_id=run.id, rules_revision=engine.rules_revision(rules),
                              started_by=actor_id, status="running")
    db.add(erun)
    await db.flush()

    if apply:
        # Serialize with manual imports before deciding anything, so the
        # capacity estimate and the import see the same inventory.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('discovery-import', 0))"))

    results = (await db.execute(
        select(DiscoveryResultV2).where(DiscoveryResultV2.run_id == run.id,
                                        DiscoveryResultV2.status.in_(EVALUATED_STATUSES),
                                        DiscoveryResultV2.imported.is_(False),
                                        DiscoveryResultV2.ignored.is_(False))
        .order_by(DiscoveryResultV2.id)
    )).scalars().all()
    planned = [[r.id, *plan_result(r, rules, import_mode=mode)] for r in results]

    budget = None
    if apply and any(p[2] == "auto_import" for p in planned):
        budget = await license_capacity.reserve(db)
        if budget.remaining is not None:
            budget.remaining = max(0, budget.remaining - max(0, int(settings["license_reserve_nodes"] or 0)))
        assign_capacity(planned, cap=int(settings["max_auto_import_per_run"]), room=budget.remaining)
    elif not apply:
        room = await license_room(db, settings["license_reserve_nodes"])
        assign_capacity(planned, cap=int(settings["max_auto_import_per_run"]), room=room)

    by_id = {r.id: r for r in results}
    final: dict[int, tuple[str, str | None]] = {}

    # Store plans and suggestions first; the import reads them from the row.
    for rid, plan, disp, reason in planned:
        r = by_id[rid]
        group, template, tags = _suggestions(plan, profile)
        r.suggested_group_id, r.matched_template_id, r.suggested_tags = group, template, tags
        r.matched_rule_ids = [uuid.UUID(m["rule_id"]) for m in plan["matched"]] or None
        final[rid] = (disp if apply or disp not in ACTIONABLE else f"would_{disp.replace('auto_', '')}",
                      reason)
        r.rule_plan = {**engine.compact(plan), "reason": reason}
    await db.flush()

    if apply:
        # Ignore: the address joins the Ignored list with the rule as reason;
        # later scans skip it like a manual ignore.
        for rid, plan, disp, _ in planned:
            if disp != "ignore":
                continue
            r = by_id[rid]
            decision = engine.decision_of(plan)
            exists = (await db.execute(select(DiscoveryIgnoredDevice.id).where(
                DiscoveryIgnoredDevice.ip_address == r.ip_address))).first()
            if not exists:
                db.add(DiscoveryIgnoredDevice(ip_address=str(r.ip_address), mac_address=r.mac_address,
                                              hostname=r.hostname,
                                              reason=f"Discovery rule: {decision['rule_name']}"[:500],
                                              ignored_by=actor_id))
            r.ignored = True
            r.ignored_at = datetime.now(timezone.utc)
            r.status = "ignored"
            final[rid] = ("ignored", None)

        to_import = [p for p in planned if p[2] == "auto_import"]
        if to_import and budget is not None:
            route = {p[0]: engine.decision_of(p[1]).get("import_as") or "device" for p in to_import}
            payload = ImportRequest(result_ids=[p[0] for p in to_import],
                                    enable_monitoring=bool(profile.enable_monitoring) if profile else True,
                                    conflict_strategy="skip")
            outcome = await discovery_import.import_results(db, run, payload, actor_id=actor_id,
                                                            budget=budget, route=route)
            for rep in outcome.items:
                rid = rep["result_id"]
                status = rep.get("status")
                if status == "imported":
                    final[rid] = ("imported", None)
                elif rep.get("license_capacity_reached"):
                    final[rid] = ("review", "license_capacity_reached")
                elif status == "conflict":
                    final[rid] = ("review", "conflict")
                elif status == "skipped":
                    final[rid] = ("imported", None)
                else:
                    final[rid] = ("review", "error")

    counts: dict[str, int] = {"evaluated": len(planned), "matched": 0, "imported": 0, "ignored": 0,
                              "would_import": 0, "would_ignore": 0, "review": 0, "existing": 0,
                              "no_match": 0, "blocked_license": 0, "blocked_cap": 0, "errors": 0}
    match_counts: dict[str, int] = {}
    for rid, plan, _, _ in planned:
        r = by_id[rid]
        disp, reason = final[rid]
        r.disposition = disp
        r.rule_plan = {**(r.rule_plan or {}), "reason": reason}
        counts[disp] = counts.get(disp, 0) + 1
        if plan["matched"]:
            counts["matched"] += 1
        if reason == "license_capacity_reached":
            counts["blocked_license"] += 1
        elif reason == "cap":
            counts["blocked_cap"] += 1
        elif reason in ("error", "conflict"):
            counts["errors"] += 1
        for m in plan["matched"]:
            match_counts[m["rule_id"]] = match_counts.get(m["rule_id"], 0) + 1
        _record_items(db, erun.id, r, plan, disp, reason, rule_by_id)

    counts["simulated"] = await _record_scan_simulation(db, run, results, rules, mode)
    erun.counts = counts
    erun.status = "partial" if (counts["blocked_license"] or counts["blocked_cap"] or counts["errors"]) \
        else "completed"
    erun.finished_at = datetime.now(timezone.utc)
    if apply:
        now = datetime.now(timezone.utc)
        for rid_str in rule_by_id:
            await db.execute(text(
                "UPDATE discovery_engine_rules SET last_evaluated_at = :now, last_match_count = :n"
                " WHERE id = CAST(:id AS uuid)"), {"now": now, "n": match_counts.get(rid_str, 0), "id": rid_str})
    await _prune(db, int(settings.get("history_days") or 180))
    await db.commit()
    return {"engine_run_id": str(erun.id), "status": erun.status, **counts}


_DECISION_OUTCOME = {
    "imported": "applied", "ignored": "applied", "review": "applied",
    "would_import": "simulated", "would_ignore": "simulated",
}
_REASON_OUTCOME = {"license_capacity_reached": "blocked_license", "cap": "blocked_cap",
                   "not_ready": "not_ready", "error": "error", "conflict": "error"}


def _record_items(db, run_id, r: DiscoveryResultV2, plan: dict, disp: str, reason: str | None,
                  rule_by_id: dict) -> None:
    if not plan["matched"]:
        return
    imported = disp == "imported"

    def rev(rule_id):
        return (rule_by_id.get(rule_id) or {}).get("revision")

    for key, entry in plan["settings"].items():
        if key == "decision":
            outcome = _REASON_OUTCOME.get(reason or "", _DECISION_OUTCOME.get(disp, "simulated"))
            after = {"decision": entry["value"], "import_as": entry.get("import_as"), "result": disp}
        else:
            outcome = "applied" if imported else "simulated"
            after = entry["value"]
        db.add(_item(run_id, r, rule_id=uuid.UUID(entry["rule_id"]), rule_name=entry["rule_name"],
                     rule_revision=rev(entry["rule_id"]), action_key=key, after=after, outcome=outcome,
                     detail=reason, asset_id=r.imported_device_id if imported else None,
                     asset_kind="device" if imported and r.imported_device_id else "discovery_result"))
    by_rule: dict[str, list[str]] = {}
    for t in plan["tags"]:
        by_rule.setdefault(t["rule_id"], []).append(t["value"])
    for rule_id, values in by_rule.items():
        name = next((t["rule_name"] for t in plan["tags"] if t["rule_id"] == rule_id), None)
        db.add(_item(run_id, r, rule_id=uuid.UUID(rule_id), rule_name=name, rule_revision=rev(rule_id),
                     action_key="tags", after=values, outcome="applied" if imported else "simulated"))
    for s in plan["shadowed"]:
        db.add(_item(run_id, r, rule_id=uuid.UUID(s["rule_id"]), rule_name=s["rule_name"],
                     rule_revision=rev(s["rule_id"]), action_key=s["key"], after=s["value"],
                     outcome="shadowed", detail=f"Set by higher-priority rule {s['winner_rule_name']}"))


async def _record_scan_simulation(db: AsyncSession, run: DiscoveryRun, results: list, active: list[dict],
                                  import_mode: str | None) -> int:
    """Rules in Simulating mode: what they would decide for each scan result,
    recorded as ``simulated`` items of a run in mode ``simulate``. Nothing on
    the result or in inventory changes."""
    simulating = await load_rules(db, modes=("simulate",))
    if not simulating or not results:
        return 0
    sim_ids = {r["id"] for r in simulating}
    srun = DiscoveryEngineRun(trigger="discovery", mode="simulate", discovery_run_id=run.id, status="completed",
                              rules_revision=engine.rules_revision(active + simulating))
    db.add(srun)
    await db.flush()
    n = 0
    for r in results:
        plan = engine.evaluate(facts_from_result(r), active + simulating, trigger="discovery")
        entries = [(k, e) for k, e in plan["settings"].items() if e["rule_id"] in sim_ids]
        for key, e in entries:
            after = e["value"]
            if key == "decision":
                disp, reason = engine.disposition(plan, import_mode=import_mode, result_status=r.status,
                                                  import_ready=bool(r.import_ready))
                after = {"decision": e["value"], "import_as": e.get("import_as"), "result": disp}
            db.add(_item(srun.id, r, rule_id=uuid.UUID(e["rule_id"]), rule_name=e["rule_name"], action_key=key,
                         after=after, outcome="simulated"))
            n += 1
        tags: dict[str, list] = {}
        for t in plan["tags"]:
            if t["rule_id"] in sim_ids:
                tags.setdefault(t["rule_id"], []).append(t)
        for rid, ts in tags.items():
            db.add(_item(srun.id, r, rule_id=uuid.UUID(rid), rule_name=ts[0]["rule_name"], action_key="tags",
                         after=[t["value"] for t in ts], outcome="simulated"))
            n += 1
    srun.counts = {"simulated": n, "evaluated": len(results)}
    srun.finished_at = datetime.now(timezone.utc)
    if not n:
        await db.delete(srun)
    return n


async def _prune(db: AsyncSession, days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(7, days))
    await db.execute(text("DELETE FROM discovery_engine_runs WHERE started_at < :c"), {"c": cutoff})
