"""What an alert rule applies to, how many things that is, and who may change it.

An alert rule is shared policy: editing it from one incident changes alerting
for every entity it targets (Alerts review A05/A20/A22). This module gives
the Alert Center and the rule APIs one description of a rule's reach:

* ``describe_scope(rule)`` — the scope kind and a readable label
  ("All devices in group Core, tagged web").
* ``affected(db, rule)`` — how many devices/servers/checks it currently
  covers, using the same matching the evaluators apply (group, type,
  location substring, tag — ANDed).
* ``rule_in_user_scope(db, rule, scope)`` — governance for tag-scoped users:
  they may only create/change rules whose target sits inside what they can
  see. Fleet-wide policy needs an unrestricted user.

``rule`` is any mapping/row with the alert_rules column names.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.services.tag_service import tag_set


def _get(rule: Any, key: str):
    if isinstance(rule, dict):
        return rule.get(key)
    return getattr(rule, key, None)


def family(rule) -> str:
    metric = (_get(rule, "metric") or "").lower()
    if metric.startswith("host_"):
        return "server"
    if metric.startswith("apm_"):
        return "apm"
    if metric.startswith("netpath_"):
        return "netpath"
    if metric.startswith("udt_"):
        return "udt"
    if metric.startswith("netflow_"):
        return "netflow"
    if (_get(rule, "service_check_id") or _get(rule, "service_check_group_id")
            or metric == "service_status"):
        return "service"
    return "device"


def describe_scope(rule, names: dict | None = None) -> dict:
    """{kind, label, target}. ``names`` may carry resolved display names
    (device, group, server, service_check, service_group)."""
    n = names or {}
    fam = family(rule)
    target = _get(rule, "target")
    if fam == "server":
        if _get(rule, "server_id"):
            return {"kind": "server", "label": f"Server {n.get('server') or ''}".strip(),
                    "target": target}
        return {"kind": "global", "label": "All monitored servers", "target": target}
    if fam == "apm":
        return {"kind": "apm", "label": f"APM service {target}" if target else "All APM services",
                "target": target}
    if fam == "netpath":
        return {"kind": "netpath", "label": f"Path probe {target}" if target else "All path probes",
                "target": target}
    if fam == "udt":
        return {"kind": "udt", "label": "User device tracking", "target": target}
    if fam == "netflow":
        return {"kind": "netflow", "label": f"NetFlow — {_netflow_scope_label(rule)}", "target": target,
                "flow_scope": _netflow_scope(rule)}
    if fam == "service":
        if _get(rule, "service_check_id"):
            return {"kind": "service_check",
                    "label": f"Service check {n.get('service_check') or ''}".strip(),
                    "target": target}
        if _get(rule, "service_check_group_id"):
            return {"kind": "service_group",
                    "label": f"Service checks in group {n.get('service_group') or ''}".strip(),
                    "target": target}
        return {"kind": "global", "label": "All service checks", "target": target}
    # Device family: device, or a filter combination (ANDed by the engine).
    if _get(rule, "device_id"):
        label = f"Device {n.get('device') or ''}".strip()
        if target:
            label += f", interface {target}"
        return {"kind": "interface" if target else "device", "label": label, "target": target}
    parts = []
    kind = "global"
    if _get(rule, "group_id"):
        parts.append(f"in group {n.get('group') or 'selected'}")
        kind = "group"
    if _get(rule, "device_type"):
        parts.append(f"of type {_get(rule, 'device_type')}")
        kind = "device_type" if kind == "global" else kind
    if _get(rule, "location"):
        parts.append(f"at location matching “{_get(rule, 'location')}”")
        kind = "location" if kind == "global" else kind
    if _get(rule, "scope_tag"):
        parts.append(f"tagged {_get(rule, 'scope_tag')}")
        kind = "tag" if kind == "global" else kind
    label = "All devices" + ((" " + ", ".join(parts)) if parts else "")
    if target:
        label += f" — interface {target}"
    return {"kind": kind, "label": label, "target": target}


def _netflow_scope(rule) -> dict:
    scope = _get(rule, "flow_scope") or {}
    if isinstance(scope, str):
        import json

        try:
            scope = json.loads(scope)
        except ValueError:
            scope = {}
    return scope if isinstance(scope, dict) else {}


def _netflow_scope_label(rule) -> str:
    from app.services.netflow_alert_service import describe_scope as nf_describe

    metric = (_get(rule, "metric") or "").lower()
    label = nf_describe(_netflow_scope(rule))
    if metric == "netflow_finding":
        return f"detection findings ({label})" if _netflow_scope(rule) else "all detection findings"
    if metric == "netflow_exporter_silent" and not _netflow_scope(rule):
        return "every flow exporter"
    return label


def _netflow_exporter(rule) -> str | None:
    """The exporter a NetFlow rule is pinned to (exporter=, or iface=N@exporter)."""
    scope = _netflow_scope(rule)
    if scope.get("exporter"):
        return str(scope["exporter"])
    iface = str(scope.get("iface") or "")
    if "@" in iface:
        return iface.split("@", 1)[1]
    return None


async def scope_names(db: AsyncSession, rule) -> dict:
    """Display names for the ids a rule references."""
    out: dict = {}
    queries = [
        ("device", "device_id", "SELECT hostname FROM devices WHERE id = :id"),
        ("group", "group_id", "SELECT name FROM device_groups WHERE id = :id"),
        ("server", "server_id", "SELECT COALESCE(display_name, hostname) FROM servers WHERE id = :id"),
        ("service_check", "service_check_id", "SELECT name FROM service_checks WHERE id = :id"),
        ("service_group", "service_check_group_id", "SELECT name FROM service_check_groups WHERE id = :id"),
    ]
    for key, col, sql in queries:
        val = _get(rule, col)
        if val:
            try:
                out[key] = (await db.execute(text(sql), {"id": val})).scalar()
            except Exception:
                await db.rollback()
    return out


async def affected(db: AsyncSession, rule) -> dict:
    """{count, noun, shared}. count is None when it cannot be measured."""
    fam = family(rule)
    try:
        if fam == "server":
            if _get(rule, "server_id"):
                return {"count": 1, "noun": "server", "shared": False}
            n = (await db.execute(text("SELECT count(*) FROM servers"))).scalar() or 0
            return {"count": int(n), "noun": "servers", "shared": n != 1}
        if fam == "service":
            if _get(rule, "service_check_id"):
                return {"count": 1, "noun": "service check", "shared": False}
            if _get(rule, "service_check_group_id"):
                n = (await db.execute(text(
                    "SELECT count(*) FROM service_checks WHERE group_id = :g"
                ), {"g": _get(rule, "service_check_group_id")})).scalar() or 0
            else:
                n = (await db.execute(text("SELECT count(*) FROM service_checks"))).scalar() or 0
            return {"count": int(n), "noun": "service checks", "shared": n != 1}
        if fam == "netpath":
            if _get(rule, "target"):
                return {"count": 1, "noun": "path probe", "shared": False}
            n = (await db.execute(text("SELECT count(*) FROM netpath_probes"))).scalar() or 0
            return {"count": int(n), "noun": "path probes", "shared": n != 1}
        if fam == "netflow":
            if _netflow_exporter(rule):
                return {"count": 1, "noun": "flow exporter", "shared": False}
            return {"count": None, "noun": "flow exporters", "shared": True}
        if fam in ("apm", "udt"):
            return {"count": None, "noun": "APM services" if fam == "apm" else "tracked endpoints",
                    "shared": not bool(_get(rule, "target"))}
        # device family
        if _get(rule, "device_id"):
            return {"count": 1, "noun": "device", "shared": False}
        where, params = [], {}
        if _get(rule, "group_id"):
            where.append("d.group_id = :g")
            params["g"] = _get(rule, "group_id")
        if _get(rule, "device_type"):
            where.append("d.device_type = :dt")
            params["dt"] = _get(rule, "device_type")
        if _get(rule, "location"):
            where.append("d.location ILIKE '%' || :loc || '%'")
            params["loc"] = _get(rule, "location")
        if _get(rule, "scope_tag"):
            where.append("EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                         "COALESCE(d.tags, '[]'::jsonb)) _t WHERE LOWER(_t) = LOWER(:tag))")
            params["tag"] = str(_get(rule, "scope_tag")).strip()
        sql = "SELECT count(*) FROM devices d" + (" WHERE " + " AND ".join(where) if where else "")
        n = (await db.execute(text(sql), params)).scalar() or 0
        return {"count": int(n), "noun": "devices", "shared": n != 1}
    except Exception:
        await db.rollback()
        return {"count": None, "noun": "targets", "shared": True}


async def rule_in_user_scope(db: AsyncSession, rule, scope: list[str] | None) -> bool:
    """Whether a tag-scoped user may create/change this rule."""
    if scope is None:
        return True
    if _get(rule, "device_id"):
        tags = (await db.execute(text("SELECT tags FROM devices WHERE id = :id"),
                                 {"id": _get(rule, "device_id")})).scalar()
        return scoping.entity_visible(tags, scope)
    if _get(rule, "server_id"):
        tags = (await db.execute(text("SELECT tags FROM servers WHERE id = :id"),
                                 {"id": _get(rule, "server_id")})).scalar()
        return scoping.entity_visible(tags, scope)
    if _get(rule, "service_check_id"):
        tags = (await db.execute(text("SELECT tags FROM service_checks WHERE id = :id"),
                                 {"id": _get(rule, "service_check_id")})).scalar()
        return scoping.entity_visible(tags, scope)
    if family(rule) == "netflow":
        # A scoped user may only watch an exporter whose owner device they can
        # see; fleet-wide flow policy (no exporter) needs unrestricted access.
        exporter = _netflow_exporter(rule)
        if not exporter:
            return False
        from app.api.v1.netflow.access import canonical_ip, resolve_exporter_owners

        ip = canonical_ip(exporter)
        owner = (await resolve_exporter_owners(db, [ip])).get(ip) if ip else None
        return owner is not None and bool(set(owner.tags) & set(scope))
    tag = _get(rule, "scope_tag")
    if tag and family(rule) == "device":
        return bool(tag_set([tag]) & set(scope))
    return False


FLEET_RULE_DETAIL = ("This rule applies beyond your visibility scope (fleet-wide or to "
                     "entities you cannot see); changing it needs unrestricted access.")


_AUDIT_SKIP = {"updated_at", "created_at", "email_body", "recovery_email_body"}


def rule_changes(before: dict | None, after: dict | None) -> dict:
    """{field: [before, after]} for audit records (long bodies elided)."""
    b, a = dict(before or {}), dict(after or {})
    out = {}
    for key in sorted(set(b) | set(a)):
        if key in _AUDIT_SKIP:
            if b.get(key) != a.get(key):
                out[key] = ["…", "…"]
            continue
        if str(b.get(key)) != str(a.get(key)):
            out[key] = [_jsonable(b.get(key)), _jsonable(a.get(key))]
    return out


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)


async def guard_rule_change(db: AsyncSession, user, *rules) -> None:
    """403 unless every given rule version (before and/or after the change)
    sits inside the user's visibility scope."""
    from fastapi import HTTPException

    scope = await scoping.visible_tags(db, user)
    if scope is None:
        return
    for rule in rules:
        if rule is not None and not await rule_in_user_scope(db, rule, scope):
            raise HTTPException(403, FLEET_RULE_DETAIL)


async def rule_row(db: AsyncSession, rule_id) -> dict | None:
    row = (await db.execute(text("SELECT * FROM alert_rules WHERE id = :id"),
                            {"id": rule_id})).mappings().first()
    return dict(row) if row else None


async def impact(db: AsyncSession, rule: dict) -> dict:
    open_alerts = (await db.execute(text(
        "SELECT count(*) FROM alerts a WHERE a.status IN ('pending','active','acknowledged')"
        " AND (a.rule_id = :id OR (a.rule_id IS NULL AND a.metadata->>'rule_id' = :sid))"
    ), {"id": rule["id"], "sid": str(rule["id"])})).scalar() or 0
    return {
        "id": str(rule["id"]), "name": rule.get("name"), "enabled": rule.get("enabled"),
        "scope": describe_scope(rule, await scope_names(db, rule)),
        "affected": await affected(db, rule),
        "open_alerts": int(open_alerts),
        "notify_channels": len(rule.get("notify_channels") or []),
    }
