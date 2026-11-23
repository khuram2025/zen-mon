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

Phase 4 adds, for both automatic passes:

* a per-rule change cap (``discovery_engine_rules.max_changes_per_run``) and
  the match-growth stop (``growth_breaker_pct`` / ``growth_breaker_min``):
  either holds the run for approval like the engine-wide cap;
* fingerprint parts for interfaces, LLDP/CDP evidence, monitoring sensors,
  service checks and software baselines, present only while a rule tests
  those facts or uses those actions;
* scheduled runs: rules with the ``scheduled`` trigger evaluate the whole
  inventory at the configured times (``scheduled_pass``);
* the "Unmatched" report of assets no rule matches (``unmatched``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
_SENSOR_IDS = ("LEFT JOIN (SELECT target_id AS aid, string_agg(sensor_id::text, ',' ORDER BY sensor_id) AS v"
               " FROM sensor_assignments WHERE target_type = 'device' GROUP BY target_id) sa ON sa.aid = d.id")

# Optional parts of a device's fingerprint, present only while a rule tests
# those facts or uses those actions (``inventory.needs``).
_DEVICE_EXTRAS = {
    "interfaces": ("i.v", "LEFT JOIN (SELECT device_id AS aid, md5(string_agg(concat_ws(':', if_index, if_name,"
                   " if_alias, if_descr, if_type, COALESCE(configured_speed_bps, if_speed), monitored), ','"
                   " ORDER BY if_index)) AS v FROM device_interfaces GROUP BY device_id) i ON i.aid = d.id"),
    # Current LLDP/CDP evidence: which neighbours, whether seen within 24 h and
    # the local port's state (from either end of the link row).
    "topology": ("t.v", "LEFT JOIN (SELECT aid, md5(string_agg(x, ',' ORDER BY x)) AS v FROM ("
                 " SELECT tl.local_device_id AS aid, concat_ws(':', tl.remote_device_id, tl.local_if_index,"
                 " tl.last_seen_at > now() - interval '24 hours', li.oper_status) AS x FROM topology_links tl"
                 " LEFT JOIN device_interfaces li ON li.device_id = tl.local_device_id"
                 " AND li.if_index = tl.local_if_index WHERE tl.remote_device_id IS NOT NULL"
                 " UNION ALL SELECT tl.remote_device_id, concat_ws(':', tl.local_device_id, tl.remote_port_id,"
                 " tl.last_seen_at > now() - interval '24 hours') FROM topology_links tl"
                 " WHERE tl.remote_device_id IS NOT NULL) e GROUP BY aid) t ON t.aid = d.id"),
    "sensors": ("sv.v", "LEFT JOIN (SELECT device_id AS aid, string_agg(poller_id, ',' ORDER BY poller_id) AS v"
                " FROM device_monitoring_vantages GROUP BY device_id) sv ON sv.aid = d.id"),
    "service_checks": ("sc.v", "LEFT JOIN (SELECT device_id AS aid, md5(string_agg(name, ',' ORDER BY name)) AS v"
                       " FROM service_checks WHERE device_id IS NOT NULL GROUP BY device_id) sc ON sc.aid = d.id"),
}


def _shared_parts(kind: str, alias: str, extras: frozenset | set) -> str:
    """Fingerprint parts every kind has while a rule needs them: custom field
    values ("custom_fields") and static group memberships ("groups")."""
    ak = "server" if kind == "server" else "device"
    out = ""
    if "custom_fields" in extras:
        out += (", (SELECT md5(string_agg(concat_ws(':', v.field_id, v.value_text, v.value_num, v.value_date,"
                f" v.value_bool), ',' ORDER BY v.field_id)) FROM custom_field_values v WHERE v.asset_kind = '{ak}'"
                f" AND v.asset_id = {alias}.id)")
    if "groups" in extras:
        out += (", (SELECT string_agg(g.group_id::text, ',' ORDER BY g.group_id) FROM device_group_members g"
                f" WHERE g.asset_kind = '{ak}' AND g.asset_id = {alias}.id AND g.source = 'static')")
    return out


def hash_sql(kind: str, extras: frozenset | set = frozenset()) -> str:
    """[(kind, id, md5 of the facts rules test)] for every asset of ``kind``."""
    shared = _shared_parts(kind, "s" if kind == "server" else "d", extras)
    if kind == "device":
        parts = [v for k, (v, _) in sorted(_DEVICE_EXTRAS.items()) if k in extras]
        joins = " ".join(j for k, (_, j) in sorted(_DEVICE_EXTRAS.items()) if k in extras)
        tail = "".join(", " + p for p in parts) + shared
        return f"""
SELECT 'device' AS kind, d.id::text AS id, md5(concat_ws('|', d.hostname, host(d.ip_address), d.sys_object_id,
       d.sys_descr, d.vendor, d.model, d.device_type, d.os_version, d.serial_number, d.snmp_credential_id::text,
       d.tags::text, d.group_id::text, d.profile_id::text, d.location, d.owner, d.ping_enabled::text,
       d.snmp_enabled::text, d.ping_interval::text, d.snmp_poll_interval::text, m.v, p.v, sa.v{tail})) AS hash
FROM devices d {_MAPS.format(col='device_id', alias='d')} {_PARENTS} {_SENSOR_IDS} {joins}
WHERE d.poll_mode IS DISTINCT FROM 'via_controller'"""
    if kind == "child":
        return f"""
SELECT 'child' AS kind, d.id::text AS id, md5(concat_ws('|', d.hostname, host(d.ip_address), d.vendor, d.model,
       d.device_type, d.os_version, d.serial_number, d.tags::text, d.group_id::text, d.location, d.owner,
       d.managed_source, c.hostname, m.v{shared})) AS hash
FROM devices d LEFT JOIN devices c ON c.id = d.managed_by_device_id {_MAPS.format(col='device_id', alias='d')}
WHERE d.poll_mode = 'via_controller'"""
    base = shared
    if "server_inventory" in extras:
        from app.services.server_inventory_facts import HASH_PARTS
        base += "".join(", " + HASH_PARTS[k] for k in ("services", "software", "processes"))
    if "baselines" in extras:
        # results of the server's evaluation, and every baseline's scope
        base += (", (SELECT md5(string_agg(concat_ws(':', r.rule_id, r.status), ',' ORDER BY r.rule_id))"
                " FROM server_baseline_results r WHERE r.server_id = s.id),"
                " (SELECT md5(string_agg(concat_ws(':', b.id, b.name, b.enabled, b.os_type, b.site_id,"
                " b.match_tags::text), ',' ORDER BY b.id)) FROM software_baselines b)")
    return f"""
SELECT 'server' AS kind, s.id::text AS id, md5(concat_ws('|', s.display_name, s.hostname, s.fqdn, host(s.primary_ip),
       s.os_name, s.os_version, s.os_type, s.collection_mode, s.environment, s.owner, s.tags::text, m.v,
       s.site_id::text, (SELECT string_agg(COALESCE(a.policy_id::text, '-'), ',' ORDER BY a.created_at, a.id)
       FROM agents a WHERE a.server_id = s.id){base})) AS hash
FROM servers s {_MAPS.format(col='server_id', alias='s')}"""


# The phase 3 name, kept for callers and tests: fingerprints without extras.
HASH_SQL = {k: hash_sql(k) for k in ("device", "child", "server")}


async def candidates(db: AsyncSession, kinds: set[str], revision: str, *, limit: int,
                     extras: frozenset | set = frozenset()) -> list[tuple[str, str, str]]:
    """[(kind, id, hash)] whose facts or rules revision changed since the last evaluation."""
    parts = [hash_sql(k, extras) for k in ("device", "child", "server") if k in kinds]
    if not parts:
        return []
    sql = ("WITH h AS (" + " UNION ALL ".join(parts) + ")"
           " SELECT h.kind, h.id, h.hash FROM h LEFT JOIN discovery_engine_asset_state st"
           " ON st.asset_kind = h.kind AND st.asset_id = CAST(h.id AS uuid)"
           " WHERE st.facts_hash IS DISTINCT FROM h.hash OR st.rules_revision IS DISTINCT FROM :rev"
           " ORDER BY h.kind, h.id LIMIT :lim")
    return [tuple(r) for r in (await db.execute(text(sql), {"rev": revision, "lim": limit})).all()]


async def _load(db: AsyncSession, picked: list[tuple[str, str, str]],
                extras: frozenset | set = frozenset(), needles: dict | None = None) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for k in sorted({k for k, _, _ in picked}):
        ids = [i for kk, i, _ in picked if kk == k]
        out.extend(await inventory.load_assets(db, {"kinds": [k], f"{k}_ids": ids}, limit=len(ids), extras=extras,
                                               needles=needles))
    return out


async def _mark(db: AsyncSession, picked: list[tuple[str, str, str]], revision: str, plans: dict | None = None) -> None:
    """Record the assets as evaluated. With ``plans`` their matching rules are
    recorded too (an asset that now matches nothing gets an empty list)."""
    now = datetime.now(timezone.utc)
    for kind, aid, h in picked:
        if plans is None:
            matched = None
        else:
            matched = [m["rule_id"] for m in plans.get((kind, aid), {}).get("matched", [])]
        await db.execute(text(
            "INSERT INTO discovery_engine_asset_state (asset_kind, asset_id, facts_hash, rules_revision, evaluated_at,"
            " matched_rule_ids) VALUES (:k, CAST(:id AS uuid), :h, :rev, :now, CAST(:m AS uuid[]))"
            " ON CONFLICT (asset_kind, asset_id) DO UPDATE SET facts_hash = EXCLUDED.facts_hash,"
            " rules_revision = EXCLUDED.rules_revision, evaluated_at = EXCLUDED.evaluated_at,"
            " matched_rule_ids = CASE WHEN :keep THEN discovery_engine_asset_state.matched_rule_ids"
            " ELSE EXCLUDED.matched_rule_ids END"),
            {"k": kind, "id": aid, "h": h, "rev": revision, "now": now, "m": matched, "keep": matched is None})


async def rules_for(db: AsyncSession, trigger: str) -> tuple[list[dict], list[dict], str]:
    """(active, simulating, revision) of the rules with ``trigger``."""
    from .runner import load_rules
    every = await load_rules(db, modes=("active", "simulate"))
    rules = [r for r in every if trigger in (r.get("triggers") or [])]
    active = [r for r in rules if r["mode"] == "active"]
    simulating = [r for r in rules if r["mode"] == "simulate"]
    return active, simulating, engine.rules_revision(rules)


async def changed_rules(db: AsyncSession) -> tuple[list[dict], list[dict], str]:
    return await rules_for(db, "changed")


def limit_reasons(changes: list[dict], rules: list[dict], settings: dict,
                  new_matches: dict[str, set] | None = None, growth: dict[str, int] | None = None) -> list[str]:
    """Why an automatic run must wait for approval (empty: it may apply).

    * more value changes than ``max_changes_per_run`` (engine-wide);
    * more value changes by one rule than its own ``max_changes_per_run``;
    * the match-growth stop: one rule would change more assets *it did not
      match before* than max(growth_breaker_min, growth_breaker_pct % of the
      assets it matched last time). ``new_matches`` maps a rule id to the
      (kind, id) of the assets it newly matches; a rule edit that only changes
      what a rule sets on the assets it already matched is covered by the caps.
      Rules never evaluated before are covered by the caps only. ``growth``
      caps the count per rule where only the growth of the match count is
      known (scheduled runs)."""
    writes = [c for c in changes if c["outcome"] in ("apply", "revert")]
    cap = int(settings.get("max_changes_per_run") or 100)
    out: list[str] = []
    if len(writes) > cap:
        out.append(f"{len(writes)} changes exceed the limit of {cap} per automatic run")
    by_rule: dict[str, int] = {}
    assets_by_rule: dict[str, set] = {}
    for c in writes:
        rid = c.get("rule_id")
        by_rule[rid] = by_rule.get(rid, 0) + 1
        assets_by_rule.setdefault(rid, set()).add((c["asset_kind"], c["asset_id"]))
    pct = int(settings.get("growth_breaker_pct") or 0)
    floor = int(settings.get("growth_breaker_min") or 0)
    for r in rules:
        n = by_rule.get(r["id"], 0)
        own = r.get("max_changes_per_run")
        if own and n > own:
            out.append(f"Rule \"{r['name']}\" would make {n} changes; its own limit is {own}")
        last = r.get("last_match_count")
        fresh = (new_matches or {}).get(r["id"], set())
        grown = len(assets_by_rule.get(r["id"], set()) & set(fresh))
        if growth and r["id"] in growth:
            grown = min(grown, growth[r["id"]])
        if pct and last is not None and grown > max(floor, last * pct / 100):
            out.append(f"Rule \"{r['name']}\" newly matches and would change {grown} assets; it matched {last} "
                       f"last time (growth stop at {pct} %)")
    return out


async def _new_matches(db: AsyncSession, plans: dict) -> dict[str, set]:
    """{rule id: {(kind, id)}}: assets a rule matches now that its last
    recorded evaluation (``discovery_engine_asset_state.matched_rule_ids``) did
    not. An asset never evaluated before counts as a new match."""
    before: dict = {}
    for kind in ("device", "child", "server"):
        ids = [a for (k, a) in plans if k == kind]
        if not ids:
            continue
        for aid, m in (await db.execute(text(
                "SELECT asset_id::text, matched_rule_ids FROM discovery_engine_asset_state"
                " WHERE asset_kind = :k AND asset_id = ANY(CAST(:ids AS uuid[]))"), {"k": kind, "ids": ids})).all():
            before[(kind, aid)] = {str(x) for x in (m or [])}
    out: dict[str, set] = {}
    for key, plan in plans.items():
        had = before.get(key, set())
        for m in plan.get("matched") or []:
            if m["rule_id"] not in had:
                out.setdefault(m["rule_id"], set()).add(key)
    return out


async def _update_match_counts(db: AsyncSession, rule_ids: list[str]) -> None:
    """last_match_count of ``changed`` rules = assets whose last evaluation matched them."""
    if not rule_ids:
        return
    await db.execute(text(
        "UPDATE discovery_engine_rules r SET last_evaluated_at = now(), last_match_count ="
        " (SELECT count(*) FROM discovery_engine_asset_state st WHERE r.id = ANY(st.matched_rule_ids))"
        " WHERE r.id = ANY(CAST(:ids AS uuid[]))"), {"ids": rule_ids})


async def _set_match_counts(db: AsyncSession, counts: dict[str, int]) -> None:
    for rid, n in counts.items():
        await db.execute(text("UPDATE discovery_engine_rules SET last_evaluated_at = now(), last_match_count = :n"
                              " WHERE id = CAST(:id AS uuid)"), {"id": rid, "n": n})


def _names(changes: list[dict], assets: list[tuple[str, dict]]) -> None:
    names = {(k, str(r["id"])): (r.get("hostname") or r.get("display_name"),
                                 r.get("ip_address") or r.get("primary_ip")) for k, r in assets}
    for c in changes:
        c["asset_name"], c["ip_address"] = names.get((c["asset_kind"], c["asset_id"]), (None, None))


async def _waiting(db: AsyncSession, trigger: str, revision: str) -> str | None:
    """The id of a run of ``trigger`` still waiting for approval at this
    revision; a waiting run of an older revision is set aside (superseded)."""
    waiting = (await db.execute(text(
        "SELECT id, rules_revision FROM discovery_engine_runs WHERE status = 'needs_approval'"
        " AND trigger = :t ORDER BY started_at LIMIT 1"), {"t": trigger})).first()
    if waiting is None:
        return None
    if waiting[1] == revision:
        return str(waiting[0])
    await db.execute(text(
        "UPDATE discovery_engine_runs SET status = 'rejected', finished_at = now(),"
        " detail = 'Superseded: the rules changed while it waited; planned again' WHERE id = :id"),
        {"id": waiting[0]})
    return None


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
    held = await _waiting(db, "changed", revision)
    if held:
        await db.rollback()
        return {"skipped": "waiting_for_approval", "engine_run_id": held}
    extras = frozenset(inventory.needs(active + simulating))
    nd = inventory.needles(active + simulating)
    kinds = {k for r in active + simulating for k in (r.get("asset_kinds") or ["device"])}
    found = await candidates(db, kinds, revision, limit=inventory.MAX_ASSETS, extras=extras)
    if not found:
        await db.commit()
        return {"assets": 0}
    picked = found[:batch]
    assets = await _load(db, picked, extras, nd)
    owned = await inventory.load_owned(db, assets)
    changes, matched, plans = inventory.evaluate_assets(assets, active, owned, trigger="changed")
    out: dict[str, Any] = {"assets": len(picked), "matched": matched}
    reasons = limit_reasons(changes, active, settings, await _new_matches(db, plans))

    if reasons:
        # Plan everything that changed, so one approval covers it.
        if len(found) > len(picked):
            picked = found
            assets = await _load(db, picked, extras, nd)
            owned = await inventory.load_owned(db, assets)
            changes, matched, plans = inventory.evaluate_assets(assets, active, owned, trigger="changed")
            reasons = limit_reasons(changes, active, settings, await _new_matches(db, plans)) or reasons
        writes = [c for c in changes if c["outcome"] in ("apply", "revert")]
        _names(changes, assets)
        cap = int(settings.get("max_changes_per_run") or 100)
        run = DiscoveryEngineRun(
            trigger="changed", mode="apply", status="needs_approval", rules_revision=revision,
            plan={"changes": inventory._jsonable(changes), "assets": [list(p) for p in picked],
                  "reasons": reasons,
                  "matched": {f"{k}:{a}": [m["rule_id"] for m in p["matched"]] for (k, a), p in plans.items()}},
            counts={**inventory.summarise(changes, len(picked), matched), "cap": cap},
            detail="; ".join(reasons) + "; waiting for approval")
        db.add(run)
        await db.commit()
        logger.warning("discovery rules: %d changes held for approval (run %s): %s", len(writes), run.id,
                       "; ".join(reasons))
        return {**out, "needs_approval": str(run.id), "changes": len(writes), "reasons": reasons}

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
    await _update_match_counts(db, [r["id"] for r in active + simulating])
    await db.commit()
    return out


# ── scheduled runs ──────────────────────────────────────────────────────────

SCHEDULE_HOURS = (1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168)
_REFERENCE_DAY = date(2026, 1, 5)   # a Monday: multi-day schedules count days from here


def last_slot(now: datetime, settings: dict) -> datetime:
    """The most recent scheduled time at or before ``now`` (aware, UTC).

    Hours that divide a day repeat from ``schedule_time`` each local day;
    longer intervals repeat every N days at ``schedule_time``, counted from a
    fixed Monday (168 h = weekly on Mondays)."""
    tz = ZoneInfo(str(settings.get("schedule_timezone") or "UTC"))
    every = int(settings.get("schedule_every_hours") or 24)
    hh, mm = (int(x) for x in str(settings.get("schedule_time") or "02:00").split(":"))
    local = now.astimezone(tz)
    if every < 24:
        anchor = datetime.combine(local.date(), time(hh, mm), tzinfo=tz)
        if anchor > local:   # before today's first slot: count from yesterday's
            anchor = datetime.combine(local.date() - timedelta(days=1), time(hh, mm), tzinfo=tz)
        steps = int((local - anchor).total_seconds() // (every * 3600))
        return (anchor + timedelta(hours=every * steps)).astimezone(timezone.utc)
    days = every // 24
    day = local.date()
    for _ in range(days + 1):
        if (day - _REFERENCE_DAY).days % days == 0:
            slot = datetime.combine(day, time(hh, mm), tzinfo=tz)
            if slot <= local:
                return slot.astimezone(timezone.utc)
        day = day - timedelta(days=1)
    while (day - _REFERENCE_DAY).days % days:
        day = day - timedelta(days=1)
    return datetime.combine(day, time(hh, mm), tzinfo=tz).astimezone(timezone.utc)


def next_slot(now: datetime, settings: dict) -> datetime:
    every = int(settings.get("schedule_every_hours") or 24)
    probe = last_slot(now, settings)
    # step forward until past now (DST-safe: recompute from a later instant)
    for h in range(1, every + 26):
        cand = last_slot(probe + timedelta(hours=h), settings)
        if cand > now:
            return cand
    return probe + timedelta(hours=every)


async def _state(db: AsyncSession) -> dict:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": inventory.STATE_KEY})).first()
    value = row[0] if row else None
    if isinstance(value, str):
        import json
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


async def _save_state(db: AsyncSession, **values) -> None:
    import json
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
        " ON CONFLICT (key) DO UPDATE SET value = COALESCE(system_settings.value, '{}'::jsonb) || EXCLUDED.value"),
        {"k": inventory.STATE_KEY, "v": json.dumps(values, default=str)})


async def scheduled_pass(db: AsyncSession, *, now: datetime | None = None, apply_credential=None,
                         force: bool = False) -> dict:
    """Run the rules with the ``scheduled`` trigger when a scheduled time has
    passed: rules without a schedule of their own on the shared engine
    schedule (once per slot; the first slot after the schedule is turned on
    only marks the start), rules with their own schedule at their own slots
    (``schedules``). Same caps and approval as the ``changed`` pass. ``force``
    runs every scheduled rule now regardless of the schedules. Commits."""
    from .runner import load_settings

    now = now or datetime.now(timezone.utc)
    settings = await load_settings(db)
    if not settings["enabled"]:
        return {"skipped": "engine_off"}
    out = await _shared_pass(db, settings, now, apply_credential, force)
    if not force:
        own = await _own_pass(db, settings, now, apply_credential)
        if own is not None:
            out["own_schedules"] = own
    return out


async def _shared_pass(db: AsyncSession, settings: dict, now: datetime, apply_credential, force: bool) -> dict:
    state = await _state(db)
    if not settings.get("schedule_enabled") and not force:
        if state.get("last_scheduled_slot"):
            # turned off: forget the last slot, so turning it on again starts
            # at the next slot instead of catching up a missed one
            await _save_state(db, last_scheduled_slot=None)
            await db.commit()
        return {"skipped": "schedule_off"}
    slot = last_slot(now, settings)
    done = state.get("last_scheduled_slot")
    if not force:
        if not done:
            await _save_state(db, last_scheduled_slot=slot.isoformat())
            await db.commit()
            return {"skipped": "schedule_started", "next": next_slot(now, settings).isoformat()}
        if datetime.fromisoformat(str(done)) >= slot:
            return {"skipped": "not_due", "next": next_slot(now, settings).isoformat()}
    active, simulating, revision = await rules_for(db, "scheduled")
    if not force:   # rules with a schedule of their own run at their own times
        active = [r for r in active if not r.get("schedule")]
        simulating = [r for r in simulating if not r.get("schedule")]
    await db.execute(text(inventory.APPLY_LOCK))
    # re-read under the lock: another worker may have run this slot
    state = await _state(db)
    if not force and state.get("last_scheduled_slot") and \
            datetime.fromisoformat(str(state["last_scheduled_slot"])) >= slot:
        await db.rollback()
        return {"skipped": "not_due"}
    await _save_state(db, last_scheduled_slot=slot.isoformat(), last_scheduled_run_at=now.isoformat())
    if not active and not simulating:
        await db.commit()
        return {"skipped": "no_rules"}
    return await _run_scheduled(db, active, simulating, revision, settings, now, {"slot": slot.isoformat()},
                                apply_credential)


def _as_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


async def _own_pass(db: AsyncSession, settings: dict, now: datetime, apply_credential) -> dict | None:
    """Rules with a schedule of their own whose slot is due run together, once
    per slot each. A recurring schedule with no starting point yet (saved
    before migrate-213's starting point, or reset) only gets one."""
    from . import schedules

    active, simulating, revision = await rules_for(db, "scheduled")
    own = [r for r in active + simulating if r.get("schedule")]
    if not own:
        return None
    started = 0
    for r in own:
        if r.get("schedule_last_slot") is None and r["schedule"]["kind"] != "once":
            point = schedules.last_slot(now, r["schedule"])
            if point is not None:
                await db.execute(text("UPDATE discovery_engine_rules SET schedule_last_slot = :s"
                                      " WHERE id = CAST(:id AS uuid) AND schedule_last_slot IS NULL"),
                                 {"s": point, "id": r["id"]})
                started += 1
    due = {r["id"] for r in own if schedules.due(r["schedule"], _as_dt(r.get("schedule_last_slot")), now)}
    if not due:
        if started:
            await db.commit()
        return {"due": 0, "started": started} if started else None
    await db.execute(text(inventory.APPLY_LOCK))
    # re-read under the lock: another worker may have run these slots
    rows = (await db.execute(text(
        "SELECT id::text, schedule, schedule_last_slot FROM discovery_engine_rules"
        " WHERE id = ANY(CAST(:ids AS uuid[])) FOR UPDATE"), {"ids": sorted(due)})).all()
    slots = {}
    for rid, sched, last in rows:
        if isinstance(sched, str):
            import json
            sched = json.loads(sched)
        slot = schedules.due(sched, last, now) if sched else None
        if slot is not None:
            slots[rid] = slot
    for rid, slot in slots.items():
        await db.execute(text("UPDATE discovery_engine_rules SET schedule_last_slot = :s WHERE id = CAST(:id AS uuid)"),
                         {"s": slot, "id": rid})
    if not slots:
        await db.commit()
        return {"due": 0, "started": started}
    names = [r["name"] for r in own if r["id"] in slots]
    res = await _run_scheduled(db, [r for r in active if r["id"] in slots],
                               [r for r in simulating if r["id"] in slots], revision, settings, now,
                               {"rules": names, "slots": {k: v.isoformat() for k, v in slots.items()}},
                               apply_credential)
    return {"due": len(slots), "started": started, "rules": names, **res}


async def _run_scheduled(db: AsyncSession, active: list[dict], simulating: list[dict], revision: str,
                         settings: dict, now: datetime, targets: dict, apply_credential) -> dict:
    """Evaluate ``active`` (and record ``simulating``) over the whole
    inventory; hold for approval when a cap or the growth stop says so.
    Caller holds APPLY_LOCK. ``revision`` is of every scheduled rule, as
    approval compares it. Commits."""
    held = await _waiting(db, "scheduled", revision)
    if held:
        await db.commit()
        return {"skipped": "waiting_for_approval", "engine_run_id": held}
    extras = frozenset(inventory.needs(active + simulating))
    kinds = sorted({k for r in active + simulating for k in (r.get("asset_kinds") or ["device"])})
    assets = await inventory.load_assets(db, {"kinds": kinds}, extras=extras,
                                         needles=inventory.needles(active + simulating))
    owned = await inventory.load_owned(db, assets)
    changes, matched, plans = inventory.evaluate_assets(assets, active, owned, trigger="scheduled")
    rule_matches = {r["id"]: 0 for r in active}
    for p in plans.values():
        for m in p["matched"]:
            if m["rule_id"] in rule_matches:
                rule_matches[m["rule_id"]] += 1
    out: dict[str, Any] = {"assets": len(assets), "matched": matched, **{k: v for k, v in targets.items()
                                                                          if k == "slot"}}
    # A full run has no per-asset history of scheduled matches: a rule whose
    # match count grew is treated as newly matching the assets it changes.
    grew = {r["id"] for r in active
            if r.get("last_match_count") is not None and rule_matches.get(r["id"], 0) > r["last_match_count"]}
    fresh = {rid: {(k, a) for (k, a), p in plans.items() if any(m["rule_id"] == rid for m in p["matched"])}
             for rid in grew}
    reasons = limit_reasons(changes, active, settings, fresh,
                            {rid: rule_matches[rid] - (next(r for r in active if r["id"] == rid)["last_match_count"])
                             for rid in grew})
    if reasons:
        _names(changes, assets)
        writes = [c for c in changes if c["outcome"] in ("apply", "revert")]
        run = DiscoveryEngineRun(
            trigger="scheduled", mode="apply", status="needs_approval", rules_revision=revision,
            targets={"assets": len(assets), **targets},
            plan={"changes": inventory._jsonable(changes), "assets": [], "reasons": reasons,
                  "rule_matches": rule_matches},
            counts={**inventory.summarise(changes, len(assets), matched),
                    "cap": int(settings.get("max_changes_per_run") or 100)},
            detail="; ".join(reasons) + "; waiting for approval")
        db.add(run)
        await db.commit()
        return {**out, "needs_approval": str(run.id), "changes": len(writes), "reasons": reasons}
    run = DiscoveryEngineRun(trigger="scheduled", mode="apply", rules_revision=revision, status="running",
                             targets={"assets": len(assets), **targets})
    db.add(run)
    await db.flush()
    counts = await inventory.apply_changes(db, changes, run, apply_credential=apply_credential)
    run.counts = {**inventory.summarise(changes, len(assets), matched), **counts}
    run.status = "partial" if counts["skipped_changed"] else "completed"
    run.finished_at = datetime.now(timezone.utc)
    out.update(counts, engine_run_id=str(run.id))
    out["simulated"] = await inventory.record_simulation(db, assets, active, simulating, owned, trigger="scheduled")
    await _set_match_counts(db, rule_matches)
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
    t_active, t_sim, revision = await rules_for(db, run.trigger)
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
    if run.trigger == "scheduled":
        if approve:
            await _set_match_counts(db, plan.get("rule_matches") or {})
    else:
        stored = plan.get("matched")
        plans = None if stored is None else {
            tuple(key.split(":", 1)): {"matched": [{"rule_id": r} for r in ids]} for key, ids in stored.items()}
        await _mark(db, picked, run.rules_revision, plans)
        if plans is not None:
            await _update_match_counts(db, [r["id"] for r in t_active + t_sim])
    await db.commit()
    return {"engine_run_id": str(run.id), "status": run.status, **counts}


async def rules_pass(db: AsyncSession) -> dict:
    """The once-a-minute background pass: new assets, changed ones, then a
    scheduled run when one is due."""
    out = {"created": await inventory.sweep_created(db)}
    out["changed"] = await reconcile_changed(db)
    out["scheduled"] = await scheduled_pass(db)
    return out


# ── assets no rule matches ──────────────────────────────────────────────────

async def unmatched(db: AsyncSession, rules: list[dict], *, kinds: list[str], limit: int = 1000) -> dict:
    """Assets no given rule matches (whatever the rules' triggers), for the
    "Unmatched" report: which devices and servers the rules leave alone."""
    extras = frozenset(inventory.needs(rules))
    assets = await inventory.load_assets(db, {"kinds": kinds}, extras=extras, needles=inventory.needles(rules))
    manual = [{**r, "triggers": ["manual"]} for r in rules]
    rows: list[dict] = []
    per_kind: dict[str, dict] = {k: {"assets": 0, "unmatched": 0} for k in kinds}
    for kind, row in assets:
        per_kind[kind]["assets"] += 1
        plan = engine.evaluate(inventory.facts_for(kind, row), manual, trigger="manual", kind=kind)
        if plan["matched"]:
            continue
        per_kind[kind]["unmatched"] += 1
        if len(rows) < limit:
            rows.append({"asset_kind": kind, "asset_id": str(row["id"]),
                         "name": row.get("hostname") or row.get("display_name"),
                         "ip_address": row.get("ip_address") or row.get("primary_ip"),
                         "vendor": row.get("vendor"), "model": row.get("model"),
                         "device_type": row.get("device_type") or ("server" if kind == "server" else None),
                         "os": row.get("os_name") or row.get("os_version"),
                         "tags": inventory._tags(row.get("tags"))})
    return {"kinds": per_kind, "assets": len(assets), "unmatched": sum(v["unmatched"] for v in per_kind.values()),
            "rows": rows, "truncated": len(assets) >= inventory.MAX_ASSETS or
            sum(v["unmatched"] for v in per_kind.values()) > len(rows)}


def pending_summary(run: DiscoveryEngineRun) -> dict:
    plan = run.plan or {}
    return {"changes": plan.get("changes") or [], "assets": len(plan.get("assets") or [])}

