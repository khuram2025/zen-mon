"""Per-asset overrides of a shared alert rule (migrate-193, docs/alert-rule-overrides.md).

A row changes, for one device, one server or the members of one group (and
optionally only for instances whose name matches a pattern), any of: the
threshold, the clear level (reset threshold), the breach hold
(``min_duration``), the stable-recovery hold (``clear_duration``) and the
incident priority. ``metric`` names the condition a threshold/clear level
applies to (NULL = the rule's first condition).

Order: device and server rows are tried before group rows; within each, by
``position`` then creation. The first row that matches and has not expired
applies; nothing is merged across rows. Turning a rule off for one asset stays
an exclusion (``alert_rule_exclusions``).

Evaluators load overrides once per pass (:func:`load`), as they load
exclusions, and evaluate an asset with :func:`apply`. An open incident whose
effective threshold changes follows the usual hold and stable-recovery rules;
missing samples still never resolve.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CHANGE_FIELDS = ("threshold", "reset_threshold", "min_duration", "clear_duration", "priority")
REASON_MAX = 500


@dataclass(frozen=True)
class Override:
    id: str
    rule_id: str
    position: int
    device_id: Optional[str]
    server_id: Optional[str]
    group_id: Optional[str]
    instance_pattern: Optional[str]
    metric: Optional[str]
    threshold: Optional[float]
    reset_threshold: Optional[float]
    min_duration: Optional[int]
    clear_duration: Optional[int]
    priority: Optional[str]
    reason: str
    expires_at: Optional[datetime]
    created_at: Optional[datetime] = None

    @property
    def target_kind(self) -> str:
        return "device" if self.device_id else "server" if self.server_id else "group"

    def instance_matches(self, instance: Optional[str]) -> bool:
        if not self.instance_pattern:
            return True
        if instance is None:
            return False
        return fnmatch.fnmatchcase(instance.lower(), self.instance_pattern.lower())

    def summary(self) -> dict:
        return {"id": self.id, "target_kind": self.target_kind, "reason": self.reason,
                "instance_pattern": self.instance_pattern,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None}


def _sort_key(o: Override):
    return (0 if (o.device_id or o.server_id) else 1, o.position,
            o.created_at or datetime.min.replace(tzinfo=timezone.utc), o.id)


@dataclass
class Overrides:
    by_rule: dict[str, list[Override]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_rule)

    def for_rule(self, rule_id) -> list[Override]:
        return self.by_rule.get(str(rule_id), [])

    def find(self, rule_id, *, device_id=None, server_id=None, groups: Optional[Iterable[str]] = None,
             primary_group=None, instance: Optional[str] = None) -> Optional[Override]:
        rows = self.for_rule(rule_id)
        if not rows:
            return None
        member = set(groups or ())
        if primary_group:
            member.add(str(primary_group))
        for o in rows:
            if o.device_id and (not device_id or o.device_id != str(device_id)):
                continue
            if o.server_id and (not server_id or o.server_id != str(server_id)):
                continue
            if o.group_id and o.group_id not in member:
                continue
            if not o.instance_matches(instance):
                continue
            return o
        return None

    def has_instance_rows(self, rule_id) -> bool:
        return any(o.instance_pattern for o in self.for_rule(rule_id))

    def longest_holds(self, rule_id) -> tuple[int, int]:
        """(longest min_duration, longest clear_duration) any row sets, for history lookback."""
        rows = self.for_rule(rule_id)
        return (max([o.min_duration or 0 for o in rows] or [0]),
                max([o.clear_duration or 0 for o in rows] or [0]))


EMPTY = Overrides()

_SELECT = ("SELECT id::text, rule_id::text, position, device_id::text, server_id::text, group_id::text, "
           "instance_pattern, metric, threshold, reset_threshold, min_duration, clear_duration, priority, "
           "reason, expires_at, created_at FROM alert_rule_overrides")


def _row(r) -> Override:
    return Override(id=r[0], rule_id=r[1], position=int(r[2] or 0), device_id=r[3], server_id=r[4],
                    group_id=r[5], instance_pattern=r[6], metric=r[7],
                    threshold=float(r[8]) if r[8] is not None else None,
                    reset_threshold=float(r[9]) if r[9] is not None else None,
                    min_duration=int(r[10]) if r[10] is not None else None,
                    clear_duration=int(r[11]) if r[11] is not None else None,
                    priority=r[12], reason=r[13], expires_at=r[14], created_at=r[15])


async def load(db: AsyncSession) -> Overrides:
    """Unexpired overrides of every rule, in evaluation order. Empty before migrate-193."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                _SELECT + " WHERE expires_at IS NULL OR expires_at > now()"))).all()
    except Exception:
        logger.debug("alert_rule_overrides unavailable", exc_info=True)
        return EMPTY
    out: dict[str, list[Override]] = {}
    for r in rows:
        o = _row(r)
        out.setdefault(o.rule_id, []).append(o)
    for rows_ in out.values():
        rows_.sort(key=_sort_key)
    return Overrides(out)


# ── Applying ────────────────────────────────────────────────────────────────

def _conditions(rule) -> list[dict]:
    conds = getattr(rule, "conditions", None)
    if isinstance(conds, str):
        conds = json.loads(conds)
    if conds:
        return [dict(c) for c in conds]
    return [dict(metric=rule.metric, operator=rule.operator,
                 threshold=float(rule.threshold) if rule.threshold is not None else 0.0)]


def effective_conditions(rule, override: Optional[Override]) -> list[dict]:
    conds = _conditions(rule)
    if override is None or (override.threshold is None and override.reset_threshold is None):
        return conds
    idx = 0
    if override.metric:
        idx = next((i for i, c in enumerate(conds) if c.get("metric") == override.metric), -1)
        if idx < 0:
            return conds
    c = dict(conds[idx])
    if override.threshold is not None:
        c["threshold"] = override.threshold
        if override.reset_threshold is None and c.get("reset_threshold") is not None:
            # Keep an explicit clear level on the healthy side of the new trigger.
            high = c.get("operator") in (">", ">=", "gt", "gte")
            if (high and c["reset_threshold"] >= override.threshold) or \
                    (not high and c["reset_threshold"] <= override.threshold):
                c["reset_threshold"] = None
    if override.reset_threshold is not None:
        c["reset_threshold"] = override.reset_threshold
    conds[idx] = c
    return conds


def _mapping(rule) -> dict:
    if hasattr(rule, "_mapping"):
        return dict(rule._mapping)
    if isinstance(rule, dict):
        return dict(rule)
    if isinstance(rule, SimpleNamespace):
        return dict(vars(rule))
    return {k: getattr(rule, k) for k in dir(rule) if not k.startswith("_") and not callable(getattr(rule, k))}


def apply(rule, override: Optional[Override]):
    """The rule as evaluated for one asset: unchanged without an override, else a
    copy whose conditions, threshold, holds and priority carry the override."""
    if override is None:
        return rule
    data = _mapping(rule)
    conds = effective_conditions(rule, override)
    data["conditions"] = conds
    data["metric"], data["operator"], data["threshold"] = (
        conds[0].get("metric"), conds[0].get("operator"), conds[0].get("threshold"))
    if override.min_duration is not None:
        data["min_duration"] = override.min_duration
    if override.clear_duration is not None:
        data["clear_duration"] = override.clear_duration
    if override.priority is not None:
        data["priority"] = override.priority
    data["override"] = override.summary()
    return SimpleNamespace(**data)


# ── Validation and storage ──────────────────────────────────────────────────

class OverrideError(ValueError):
    pass


def validate(body: dict, rule: dict) -> dict:
    """Normalised override fields for ``rule`` (a dict). Raises OverrideError."""
    targets = [k for k in ("device_id", "server_id", "group_id") if body.get(k)]
    if len(targets) != 1:
        raise OverrideError("Choose exactly one of a device, a server or a group")
    out: dict[str, Any] = {k: (str(body[k]) if k in targets else None)
                           for k in ("device_id", "server_id", "group_id")}
    reason = (body.get("reason") or "").strip()
    if not 1 <= len(reason) <= REASON_MAX:
        raise OverrideError(f"Give a reason (at most {REASON_MAX} characters)")
    out["reason"] = reason
    pattern = (body.get("instance_pattern") or "").strip() or None
    if pattern and len(pattern) > 200:
        raise OverrideError("Instance pattern: at most 200 characters")
    out["instance_pattern"] = pattern
    conds = _conditions(SimpleNamespace(**{"conditions": rule.get("conditions"), "metric": rule.get("metric"),
                                           "operator": rule.get("operator"), "threshold": rule.get("threshold")}))
    metric = (body.get("metric") or "").strip() or None
    if metric and metric not in {c.get("metric") for c in conds}:
        raise OverrideError(f"The rule has no condition on {metric}")
    out["metric"] = metric
    for k in ("threshold", "reset_threshold"):
        v = body.get(k)
        if v is not None and v != "":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise OverrideError(f"{k.replace('_', ' ')} must be a number") from None
            if v != v or v in (float("inf"), float("-inf")):
                raise OverrideError(f"{k.replace('_', ' ')} must be a finite number")
            out[k] = v
        else:
            out[k] = None
    for k in ("min_duration", "clear_duration"):
        v = body.get(k)
        if v is not None and v != "":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise OverrideError(f"{k.replace('_', ' ')} must be whole seconds") from None
            if not 0 <= v <= 86400:
                raise OverrideError(f"{k.replace('_', ' ')} must be between 0 and 86400 seconds")
            out[k] = v
        else:
            out[k] = None
    prio = (body.get("priority") or "").strip().upper() or None
    if prio and prio not in ("P1", "P2", "P3", "P4"):
        raise OverrideError("Priority must be P1, P2, P3 or P4")
    out["priority"] = prio
    if not any(out[k] is not None for k in CHANGE_FIELDS):
        raise OverrideError("An override must change the threshold, clear level, holds or priority")
    # The trigger and clear level must stay consistent for the condition they change.
    target = next((c for c in conds if c.get("metric") == metric), conds[0]) if metric else conds[0]
    trig = out["threshold"] if out["threshold"] is not None else target.get("threshold")
    reset = out["reset_threshold"]
    if reset is not None and trig is not None:
        high = target.get("operator") in (">", ">=", "gt", "gte")
        low = target.get("operator") in ("<", "<=", "lt", "lte")
        if (high and reset >= float(trig)) or (low and reset <= float(trig)):
            raise OverrideError("The clear level must be on the healthy side of the threshold")
    expires = body.get("expires_at")
    if expires:
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                raise OverrideError("expires_at must be an ISO date and time") from None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        out["expires_at"] = expires
    else:
        out["expires_at"] = None
    try:
        out["position"] = int(body.get("position") if body.get("position") is not None else 100)
    except (TypeError, ValueError):
        raise OverrideError("position must be a whole number") from None
    return out


LIST_SQL = """
    SELECT o.id::text, o.rule_id::text, o.position, o.device_id::text, o.server_id::text, o.group_id::text,
           o.instance_pattern, o.metric, o.threshold, o.reset_threshold, o.min_duration, o.clear_duration,
           o.priority, o.reason, o.expires_at, o.created_at, o.updated_at, o.created_by_name,
           COALESCE(d.hostname, COALESCE(s.display_name, s.hostname), g.name) AS target_name,
           COALESCE(d.tags, s.tags) AS target_tags
      FROM alert_rule_overrides o
      LEFT JOIN devices d ON d.id = o.device_id
      LEFT JOIN servers s ON s.id = o.server_id
      LEFT JOIN device_groups g ON g.id = o.group_id
"""


def item(r) -> dict:
    o = _row(r)
    now = datetime.now(timezone.utc)
    return {
        "id": o.id, "rule_id": o.rule_id, "position": o.position,
        "device_id": o.device_id, "server_id": o.server_id, "group_id": o.group_id,
        "target_kind": o.target_kind, "target_name": r[18],
        "instance_pattern": o.instance_pattern, "metric": o.metric,
        "threshold": o.threshold, "reset_threshold": o.reset_threshold,
        "min_duration": o.min_duration, "clear_duration": o.clear_duration, "priority": o.priority,
        "reason": o.reason,
        "expires_at": o.expires_at.isoformat() if o.expires_at else None,
        "expired": bool(o.expires_at and o.expires_at <= now),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": r[16].isoformat() if r[16] else None,
        "created_by": r[17],
    }


async def list_for_rule(db: AsyncSession, rule_id) -> list[tuple[dict, Any]]:
    """[(item, target tags)] in evaluation order, expired rows included (listed as expired)."""
    rows = (await db.execute(text(LIST_SQL + " WHERE o.rule_id = CAST(:rid AS uuid)"),
                             {"rid": str(rule_id)})).all()
    ordered = sorted(rows, key=lambda r: _sort_key(_row(r)))
    return [(item(r), r[19]) for r in ordered]
