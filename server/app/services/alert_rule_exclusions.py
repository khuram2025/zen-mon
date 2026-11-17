"""Per-asset exclusions from shared alert rules (migrate-187).

A shared rule ("Server restart" on every server, "Device Down" on every
device, a group/tag/type rule) can be turned off for one device or server
without disabling it for everyone else. Turning a shared rule off from an
asset's page used to flip ``alert_rules.enabled`` and so silenced the rule
fleet-wide.

Contract (``docs/alert-rule-exclusions.md``):

* A row ``(rule, asset)`` means the rule does not evaluate that asset. Every
  evaluator that matches rules to devices or servers loads the exclusions once
  per pass (:func:`load`) and skips excluded pairs: SNMP/template metric rules
  (``network_alert_service``, including controller-managed children, which are
  devices), device status transitions, flap notices and SNMP traps
  (``alert_engine``), syslog rules (``network_events``), Device Tracker rules
  for the reporting switch (``udt_alert_service``), and server rules — host
  metrics, services/processes, restart/shutdown and availability, including
  the Device Down rule's server coverage (``host_alert_service``).
* Only a rule that is not pinned to the asset itself can be excluded for it,
  and only when the rule currently applies to it. A rule pinned to one asset
  is simply enabled, disabled or deleted.
* Excluding resolves the rule's open incidents on that asset at once (status
  ``resolved``, ``metadata.resolved_by = 'rule_excluded'``, an alert event and
  no recovery notification: the condition did not recover, nobody wants to be
  told it did). Their escalations stop with them. Re-enabling deletes the row;
  evaluation resumes on the next pass (a status rule on the next transition)
  and a condition still breaching opens a new incident after its usual hold.
* An excluded availability rule still counts as covering the server for the
  staleness sweep, so excluding "Device Down" or "Agent offline" does not make
  the built-in fallback *agent offline* alert appear instead.
* Who: ``alerts.manage`` plus visibility of the asset. A tag-scoped manager may
  exclude a fleet rule for an asset inside their scope (it narrows alerting
  only for what they can see); changing the rule itself still needs the rule
  in scope (``alert_rule_scope.guard_rule_change``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.services import alert_rule_scope as rs
from app.services.tag_service import tag_set

logger = logging.getLogger(__name__)

ASSET_KINDS = ("device", "server")
OPEN_STATUSES = ("pending", "active", "acknowledged")
REASON_MAX = 500


class ExclusionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _get(row: Any, key: str):
    if isinstance(row, Mapping):          # dict or a SQLAlchemy RowMapping
        return row.get(key)
    return getattr(row, key, None)


# ── Evaluator side ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Exclusions:
    """The (kind, rule id, asset id) pairs no evaluator may alert on."""

    pairs: frozenset = field(default_factory=frozenset)

    def device(self, rule_id, device_id) -> bool:
        return bool(self.pairs) and ("device", str(rule_id), str(device_id)) in self.pairs

    def server(self, rule_id, server_id) -> bool:
        return bool(self.pairs) and ("server", str(rule_id), str(server_id)) in self.pairs

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)


EMPTY = Exclusions()


async def load(db: AsyncSession) -> Exclusions:
    """Every exclusion, once per evaluator pass. Never raises: before
    migrate-187 (or on any read error) nothing is excluded, and a savepoint
    keeps a failed read from aborting the evaluator's transaction."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT asset_kind, rule_id::text, COALESCE(device_id, server_id)::text "
                "FROM alert_rule_exclusions"))).all()
    except Exception:
        logger.debug("alert rule exclusions unavailable; evaluating every asset", exc_info=True)
        return EMPTY
    pairs = set()
    for row in rows or ():
        try:
            kind, rid, aid = row[0], row[1], row[2]
        except (TypeError, IndexError, KeyError):
            continue
        if kind in ASSET_KINDS and rid and aid:
            pairs.add((str(kind), str(rid), str(aid)))
    return Exclusions(frozenset(pairs)) if pairs else EMPTY


def drop_device(rules: Iterable, exclusions: Exclusions, device_id) -> list:
    """The rules that still evaluate this device."""
    rules = list(rules)
    if not exclusions or not device_id:
        return rules
    return [r for r in rules if not exclusions.device(_get(r, "id"), device_id)]


# ── Applicability ───────────────────────────────────────────────────────────

def kinds_for_rule(rule) -> set[str]:
    """The asset kinds a rule can be excluded for."""
    fam = rs.family(rule)
    if fam == "server":
        return {"server"}
    if fam in ("device", "udt"):
        from app.services.server_availability import is_down_rule
        return {"device", "server"} if is_down_rule(rule) else {"device"}
    return set()


def pinned_to(rule, kind: str, asset_id) -> bool:
    col = "device_id" if kind == "device" else "server_id"
    value = _get(rule, col)
    return bool(value) and str(value) == str(asset_id)


def device_rule_covers(rule, device: dict) -> bool:
    """A device-family rule's device / group / type / location / tag scope
    (the matching the evaluators apply)."""
    if _get(rule, "device_id") and str(_get(rule, "device_id")) != str(device["id"]):
        return False
    if _get(rule, "group_id"):
        # Group membership with subgroups (migrate-192) when the caller loaded it
        # ("_groups"); the primary group otherwise.
        from app.services.device_groups.membership import in_group
        if not in_group(_get(rule, "group_id"), device.get("_groups"), device.get("group_id")):
            return False
    if _get(rule, "profile_id"):
        # Template scope (migrate-193): the template or one customised from it.
        fam = device.get("_family")
        if fam is not None:
            if not fam.covers(_get(rule, "profile_id"), device.get("profile_id")):
                return False
        elif str(_get(rule, "profile_id")) != str(device.get("profile_id") or ""):
            return False
    if _get(rule, "device_type") and _get(rule, "device_type") != device.get("device_type"):
        return False
    loc = (_get(rule, "location") or "").strip().lower()
    if loc and loc not in (device.get("location") or "").lower():
        return False
    tag = (_get(rule, "scope_tag") or "").strip().lower()
    if tag and tag not in tag_set(device.get("tags")):
        return False
    return True


def server_rule_covers(rule, server: dict) -> bool:
    """A server rule (host_*) or the Device Down rule's server coverage."""
    if _get(rule, "server_id") and str(_get(rule, "server_id")) != str(server["id"]):
        return False
    if rs.family(rule) == "server":
        return True
    from app.services.server_availability import is_down_rule
    if not is_down_rule(rule):
        return False
    # Device Down pages agent servers only (server_availability).
    if server.get("collection_mode") != "agent" or server.get("status") == "disabled":
        return False
    tag = (_get(rule, "scope_tag") or "").strip().lower()
    return not tag or tag in tag_set(server.get("tags"))


def covers(rule, kind: str, asset: dict) -> bool:
    if kind not in kinds_for_rule(rule):
        return False
    return device_rule_covers(rule, asset) if kind == "device" else server_rule_covers(rule, asset)


# ── Assets ──────────────────────────────────────────────────────────────────

async def asset_row(db: AsyncSession, kind: str, asset_id) -> Optional[dict]:
    if kind == "device":
        row = (await db.execute(text(
            "SELECT id, hostname AS name, host(ip_address) AS ip, group_id, device_type, location, tags,"
            " managed_by_device_id, profile_id FROM devices WHERE id = CAST(:id AS uuid)"), {"id": str(asset_id)})).mappings().first()
        if row:
            from app.services.device_groups import membership
            from app.services.device_templates import family
            out = dict(row)
            out["_groups"] = await membership.groups_of(db, "device", out["id"])
            out["_family"] = await family.load(db)
            return out
    elif kind == "server":
        row = (await db.execute(text(
            "SELECT id, COALESCE(display_name, hostname) AS name, host(primary_ip) AS ip, tags,"
            " collection_mode, status FROM servers WHERE id = CAST(:id AS uuid)"), {"id": str(asset_id)})).mappings().first()
    else:
        return None
    return dict(row) if row else None


def asset_path(kind: str, asset_id) -> str:
    return f"/devices/{asset_id}" if kind == "device" else f"/servers/{asset_id}"


def _item(row) -> dict:
    kind = row["asset_kind"]
    aid = str(row["device_id"] or row["server_id"])
    return {
        "id": str(row["id"]),
        "rule_id": str(row["rule_id"]),
        "asset_kind": kind,
        "asset_id": aid,
        "asset_name": row.get("asset_name"),
        "asset_ip": row.get("asset_ip"),
        "asset_path": asset_path(kind, aid),
        "reason": row.get("reason"),
        "created_by": str(row["created_by"]) if row.get("created_by") else None,
        "created_by_name": row.get("created_by_name"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


_LIST_SQL = """
SELECT x.id, x.rule_id, x.asset_kind, x.device_id, x.server_id, x.reason, x.created_by,
       COALESCE(u.full_name, u.username, x.created_by_name) AS created_by_name, x.created_at,
       COALESCE(d.hostname, COALESCE(s.display_name, s.hostname)) AS asset_name,
       COALESCE(host(d.ip_address), host(s.primary_ip)) AS asset_ip,
       COALESCE(d.tags, s.tags) AS asset_tags
  FROM alert_rule_exclusions x
  LEFT JOIN devices d ON d.id = x.device_id
  LEFT JOIN servers s ON s.id = x.server_id
  LEFT JOIN users u ON u.id = x.created_by
"""


async def for_rule(db: AsyncSession, rule_id, scope: list[str] | None) -> dict:
    """{items, hidden}: the assets a rule is excluded for that the caller can
    see, newest first, and how many more exist outside their scope."""
    rows = (await db.execute(text(
        _LIST_SQL + " WHERE x.rule_id = CAST(:rid AS uuid) ORDER BY x.created_at DESC"),
        {"rid": str(rule_id)})).mappings().all()
    items, hidden = [], 0
    for row in rows:
        if not scoping.entity_visible(row["asset_tags"], scope):
            hidden += 1
            continue
        items.append(_item(row))
    return {"items": items, "hidden": hidden}


async def for_asset(db: AsyncSession, kind: str, asset_id) -> dict[str, dict]:
    """{rule id: exclusion} for one asset."""
    col = "device_id" if kind == "device" else "server_id"
    rows = (await db.execute(text(
        _LIST_SQL + f" WHERE x.{col} = CAST(:aid AS uuid)"), {"aid": str(asset_id)})).mappings().all()
    return {str(r["rule_id"]): _item(r) for r in rows}


async def counts(db: AsyncSession) -> dict[str, int]:
    """{rule id: number of excluded assets} (all of them, for the rule list)."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT rule_id::text, count(*) FROM alert_rule_exclusions GROUP BY rule_id"))).all()
    except Exception:
        return {}
    return {str(r[0]): int(r[1]) for r in rows}


# ── Rules that apply to one asset ───────────────────────────────────────────

_RULE_FIELDS = (
    "id, name, description, enabled, metric, operator, threshold, severity, trigger_on, "
    "device_id, group_id, device_type, location, scope_tag, server_id, target, trap_oid, "
    "service_check_id, service_check_group_id, conditions, priority, profile_id, if_role, "
    "min_duration, clear_duration"
)


async def rules_for_asset(db: AsyncSession, kind: str, asset: dict) -> list[dict]:
    """Every rule (enabled or not) that applies to this asset, with its
    exclusion for it. Shared rules first, then the asset's own."""
    rows = (await db.execute(text(
        f"SELECT {_RULE_FIELDS} FROM alert_rules ORDER BY name"))).mappings().all()
    excluded = await for_asset(db, kind, asset["id"])
    out = []
    for r in rows:
        rule = dict(r)
        if not covers(rule, kind, asset):
            continue
        rid = str(rule["id"])
        pinned = pinned_to(rule, kind, asset["id"])
        scope = rs.describe_scope(rule)
        out.append({
            "id": rid,
            "name": rule["name"],
            "description": rule.get("description"),
            "enabled": bool(rule["enabled"]),
            "metric": rule.get("metric"),
            "operator": rule.get("operator"),
            "threshold": float(rule["threshold"]) if rule.get("threshold") is not None else None,
            "severity": rule.get("severity"),
            "trigger_on": rule.get("trigger_on"),
            "target": rule.get("target"),
            "family": rs.family(rule),
            "scope": scope,
            "shared": not pinned,
            "excluded": None if pinned else excluded.get(rid),
            # Effective for this asset: on globally and not excluded here.
            "active": bool(rule["enabled"]) and (pinned or rid not in excluded),
        })
    out.sort(key=lambda x: (not x["shared"], (x["name"] or "").lower()))
    return out


# ── Changes ─────────────────────────────────────────────────────────────────

async def _check_asset(db: AsyncSession, user, kind: str, asset_id) -> dict:
    if kind not in ASSET_KINDS:
        raise ExclusionError(422, "asset_kind must be 'device' or 'server'")
    asset = await asset_row(db, kind, asset_id)
    scope = await scoping.visible_tags(db, user)
    if asset is None or not scoping.entity_visible(asset.get("tags"), scope):
        raise ExclusionError(404, f"{kind.capitalize()} not found")
    return asset


async def exclude(db: AsyncSession, user, rule: dict, kind: str, asset_id,
                  reason: str | None = None) -> dict:
    """Turn ``rule`` off for one asset. Idempotent. Resolves the rule's open
    incidents on that asset. The caller commits."""
    from app.services import alert_workflow as wf
    from app.services.audit_service import write_audit_log

    asset = await _check_asset(db, user, kind, asset_id)
    if pinned_to(rule, kind, asset["id"]):
        raise ExclusionError(422, f"This rule only watches this {kind}: disable or delete the rule instead.")
    if not covers(rule, kind, asset):
        raise ExclusionError(422, f"This rule does not apply to this {kind}.")
    reason = (reason or "").strip()[:REASON_MAX] or None
    col = "device_id" if kind == "device" else "server_id"
    inserted = (await db.execute(text(
        f"INSERT INTO alert_rule_exclusions (rule_id, asset_kind, {col}, reason, created_by, created_by_name) "
        f"VALUES (CAST(:rid AS uuid), :kind, CAST(:aid AS uuid), :reason, :uid, :uname) "
        f"ON CONFLICT (rule_id, {col}) WHERE {col} IS NOT NULL DO NOTHING RETURNING id"
    ), {"rid": str(rule["id"]), "kind": kind, "aid": str(asset["id"]), "reason": reason,
        "uid": getattr(user, "id", None), "uname": wf.actor_name(user)})).first()

    resolved = 0
    if inserted:
        note = f"Rule “{rule.get('name')}” disabled for this {kind}"
        open_rows = (await db.execute(text(
            f"SELECT id, status, severity FROM alerts WHERE {col} = CAST(:aid AS uuid)"
            " AND status IN ('pending','active','acknowledged')"
            " AND (rule_id = CAST(:rid AS uuid) OR (rule_id IS NULL AND metadata->>'rule_id' = :srid))"
            " FOR UPDATE"), {"aid": str(asset["id"]), "rid": str(rule["id"]), "srid": str(rule["id"])})).all()
        for row in open_rows:
            await db.execute(text(
                "UPDATE alerts SET status = 'resolved', resolved_at = NOW(),"
                " metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb) WHERE id = :id"
            ), {"id": row.id, "m": json.dumps({
                "resolved_by": "rule_excluded",
                "resolved_by_user": str(getattr(user, "id", "") or "") or None,
                "resolved_by_name": wf.actor_name(user),
                "resolution_note": note,
            })})
            await wf.record_event(db, row.id, "resolved", actor=user, source="rule_exclusion",
                                  reason=note, old_status=row.status, new_status="resolved",
                                  detail={"rule_id": str(rule["id"]), "asset_kind": kind,
                                          "asset_id": str(asset["id"])})
            resolved += 1
        await write_audit_log(db, actor=user, action="alert_rule.exclude", resource_type="alert_rule",
                              resource_id=str(rule["id"]),
                              metadata={"name": rule.get("name"), "asset_kind": kind,
                                        "asset_id": str(asset["id"]), "asset_name": asset.get("name"),
                                        "reason": reason, "resolved_alerts": resolved})
    current = (await for_asset(db, kind, asset["id"])).get(str(rule["id"]))
    return {"exclusion": current, "created": bool(inserted), "resolved_alerts": resolved}


async def include(db: AsyncSession, user, rule: dict, kind: str, asset_id) -> bool:
    """Re-enable ``rule`` for one asset. Returns whether an exclusion was
    removed. The caller commits."""
    from app.services.audit_service import write_audit_log

    asset = await _check_asset(db, user, kind, asset_id)
    col = "device_id" if kind == "device" else "server_id"
    removed = (await db.execute(text(
        f"DELETE FROM alert_rule_exclusions WHERE rule_id = CAST(:rid AS uuid) AND {col} = CAST(:aid AS uuid)"
        " RETURNING id, reason, created_by_name, created_at"
    ), {"rid": str(rule["id"]), "aid": str(asset["id"])})).first()
    if removed:
        await write_audit_log(db, actor=user, action="alert_rule.include", resource_type="alert_rule",
                              resource_id=str(rule["id"]),
                              metadata={"name": rule.get("name"), "asset_kind": kind,
                                        "asset_id": str(asset["id"]), "asset_name": asset.get("name"),
                                        "excluded_by": removed.created_by_name,
                                        "excluded_at": removed.created_at.isoformat() if removed.created_at else None})
    return bool(removed)
