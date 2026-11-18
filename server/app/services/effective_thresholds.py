"""Effective alert thresholds of one device or server (migrate-193).

``for_asset`` lists every rule that covers the asset with what is in force for
it and where it comes from: the rule itself, an override (which row), an
exclusion, or the rule being off. Overrides limited to some instances
(interfaces, components) are listed with their pattern.

``display_thresholds`` gives the device page the warn/crit levels of accepted
template-metric rules, so a reading's colour and the alarm for it agree.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_rule_exclusions as rx
from app.services import alert_rule_overrides as ovr


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _view(rule: dict, o: Optional[ovr.Override]) -> dict:
    from types import SimpleNamespace
    base = SimpleNamespace(**{**rule, "conditions": _json(rule.get("conditions"))})
    eff = ovr.apply(base, o)
    return {
        "conditions": ovr.effective_conditions(base, o),
        "min_duration": getattr(eff, "min_duration", None),
        "clear_duration": getattr(eff, "clear_duration", None),
        "priority": getattr(eff, "priority", None),
    }


async def for_asset(db: AsyncSession, kind: str, asset: dict, overrides: Optional[ovr.Overrides] = None) -> list[dict]:
    rules = await rx.rules_for_asset(db, kind, asset)
    overrides = overrides if overrides is not None else await ovr.load(db)
    groups = asset.get("_groups")
    from sqlalchemy import text
    full = {}
    if rules:
        rows = (await db.execute(text(
            "SELECT id::text, metric, operator, threshold, conditions, min_duration, clear_duration, priority, "
            "severity, profile_id::text, if_role, pack_key FROM alert_rules WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [r["id"] for r in rules]})).mappings().all()
        full = {r["id"]: dict(r) for r in rows}
    out = []
    for r in rules:
        rule = full.get(r["id"]) or {}
        lookup = dict(device_id=asset["id"]) if kind == "device" else dict(server_id=asset["id"])
        o = overrides.find(r["id"], groups=groups, primary_group=asset.get("group_id"), instance=None, **lookup)
        partial = [x for x in overrides.for_rule(r["id"]) if x.instance_pattern and _targets(x, kind, asset, groups)]
        if r.get("excluded"):
            source = "excluded"
        elif not r.get("enabled"):
            source = "disabled"
        elif o is not None:
            source = "override"
        else:
            source = "rule"
        out.append({
            **{k: r.get(k) for k in ("id", "name", "enabled", "metric", "severity", "family", "scope",
                                     "shared", "excluded", "active", "target")},
            "if_role": rule.get("if_role"), "pack_key": rule.get("pack_key"),
            "profile_id": rule.get("profile_id"),
            "rule": _view(rule, None) if rule else None,
            "effective": _view(rule, o) if rule else None,
            "source": source,
            "override": o.summary() if o is not None else None,
            "instance_overrides": [{**x.summary(), **_view(rule, x)} for x in partial] if rule else [],
        })
    return out


def _targets(o: ovr.Override, kind: str, asset: dict, groups) -> bool:
    if o.device_id:
        return kind == "device" and o.device_id == str(asset["id"])
    if o.server_id:
        return kind == "server" and o.server_id == str(asset["id"])
    member = set(groups or ())
    if asset.get("group_id"):
        member.add(str(asset["group_id"]))
    return o.group_id in member


async def display_thresholds(db: AsyncSession, device_id) -> dict[str, dict]:
    """{template metric key: {"warn", "crit", "op"}} from enabled, not excluded
    template-metric rules covering the device (overrides applied, device level)."""
    try:
        asset = await rx.asset_row(db, "device", device_id)
        if not asset:
            return {}
        items = await for_asset(db, "device", asset)
    except Exception:
        # The device page shows the template's colours rather than fail.
        rollback = getattr(db, "rollback", None)
        if rollback:
            try:
                await rollback()
            except Exception:
                pass
        return {}
    out: dict[str, dict] = {}
    for it in items:
        if not it.get("active") or not it.get("effective"):
            continue
        conds = it["effective"]["conditions"] or []
        if len(conds) != 1:
            continue
        c = conds[0]
        metric = str(c.get("metric") or "")
        if not metric.startswith("tpl_") or c.get("threshold") is None:
            continue
        op = c.get("operator")
        if op not in (">", ">=", "<", "<=", "gt", "gte", "lt", "lte"):
            continue
        low = op in ("<", "<=", "lt", "lte")
        level = "crit" if it.get("severity") == "critical" else "warn" if it.get("severity") == "warning" else None
        if level is None:
            continue
        key = metric[4:]
        entry = out.setdefault(key, {"op": "<=" if low else ">="})
        value = float(c["threshold"])
        prev = entry.get(level)
        # Several rules on one metric and severity: the first to alarm decides the colour.
        if prev is None or (value > prev if low else value < prev):
            entry[level] = value
    return out


def apply_display(groups: list[dict], levels: dict[str, dict]) -> list[dict]:
    """oid_groups with each covered metric's thresholds replaced by the rule levels
    (a level no rule sets keeps the template's value)."""
    if not levels:
        return groups
    out = []
    for g in groups or []:
        metrics = []
        for m in g.get("metrics") or []:
            lv = levels.get(m.get("key"))
            if lv:
                th = dict(m.get("thresholds") or {})
                if any(k in lv for k in ("warn", "crit")):
                    th["op"] = lv["op"]
                for k in ("warn", "crit"):
                    if k in lv:
                        th[k] = lv[k]
                m = {**m, "thresholds": th, "thresholds_source": "alert_rule"}
            metrics.append(m)
        out.append({**g, "metrics": metrics})
    return out
