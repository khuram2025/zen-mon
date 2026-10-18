"""Escalation policies: staged routing per priority (migration 164).

A policy is a reusable escalation matrix row: an ordered list of steps
(``delay_minutes`` after the incident's announcement, each with targets — an
on-call schedule, a user or a notification channel), plus

* ``repeat_minutes`` / ``max_repeats`` — re-notify the current step while the
  incident stays unacknowledged (``max_repeats`` 0 = no repeats; the count
  restarts at every new step);
* ``review_minutes`` — an "Unresolved update" to the owner and the last
  notified step while the incident is open, acknowledged or not;
* ``ack_expiry_minutes`` — an acknowledgement older than this re-opens
  escalation from the current step;
* ``fallback_targets`` — where a step goes when it resolves to nobody or every
  delivery of the step failed;
* ``ack_target_minutes`` — the policy's stated acknowledgement target (shown
  in the matrix; the incident's ack due time uses the response targets);
* ``default_priorities`` — the priorities this policy routes when a rule names
  no policy (unique across enabled policies).

Route precedence (:func:`resolve_route`): the rule's own enabled policy, then
the enabled policy that is the default for the alert's priority, then the
rule's legacy inline ``escalation_levels`` (alert_escalation_service, behaviour
unchanged), else no escalation.

The planner (:func:`plan_policy`) is pure — state in, one action out, with the
clock passed in — so every timing rule is unit-tested without a database.
Escalation state for a policy-routed incident is ``metadata->'escalation'``
with ``"mode": "policy"`` (see :func:`new_state`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_priority as prio
from app.services import oncall_service as oc

MAX_STEPS = 10
MAX_TARGETS = 20
MAX_FALLBACK_TARGETS = 10
MAX_REPEATS = 20
TARGET_TYPES = ("schedule", "user", "channel")
# Unacknowledged: a review due within this long of the last step page is
# covered by that page (an escalation is itself an update).
REVIEW_COVER_S = 60
STATE_MODE = "policy"


# ── Model ────────────────────────────────────────────────────────────────────

@dataclass
class Step:
    position: int
    delay_minutes: int
    name: Optional[str] = None
    targets: list[dict] = field(default_factory=list)


@dataclass
class Policy:
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    default_priorities: list[str] = field(default_factory=list)
    ack_target_minutes: Optional[int] = None
    repeat_minutes: Optional[int] = None
    max_repeats: int = 3
    review_minutes: Optional[int] = None
    ack_expiry_minutes: Optional[int] = None
    fallback_targets: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def step(self, index: int) -> Optional[Step]:
        return self.steps[index] if 0 <= index < len(self.steps) else None


def _int_or_none(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def policy_from_dict(d: dict) -> Policy:
    steps = sorted((Step(position=int(s.get("position") or 0),
                         delay_minutes=int(s.get("delay_minutes") or 0),
                         name=s.get("name"), targets=list(s.get("targets") or []))
                    for s in (d.get("steps") or [])), key=lambda s: (s.delay_minutes, s.position))
    return Policy(
        id=str(d.get("id") or ""), name=d.get("name") or "", description=d.get("description"),
        enabled=bool(d.get("enabled", True)),
        default_priorities=[p for p in (d.get("default_priorities") or []) if prio.is_priority(p)],
        ack_target_minutes=_int_or_none(d.get("ack_target_minutes")),
        repeat_minutes=_int_or_none(d.get("repeat_minutes")),
        max_repeats=_int_or_none(d.get("max_repeats")) or 0,
        review_minutes=_int_or_none(d.get("review_minutes")),
        ack_expiry_minutes=_int_or_none(d.get("ack_expiry_minutes")),
        fallback_targets=list(d.get("fallback_targets") or []), steps=steps)


def policy_dict(p: Policy, *, used_by_rules: int = 0) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description, "enabled": p.enabled,
        "default_priorities": list(p.default_priorities),
        "ack_target_minutes": p.ack_target_minutes, "repeat_minutes": p.repeat_minutes,
        "max_repeats": p.max_repeats, "review_minutes": p.review_minutes,
        "ack_expiry_minutes": p.ack_expiry_minutes,
        "fallback_targets": list(p.fallback_targets),
        "steps": [{"position": i, "delay_minutes": s.delay_minutes, "name": s.name,
                   "targets": list(s.targets)} for i, s in enumerate(p.steps)],
        "used_by_rules": used_by_rules,
    }


# ── Validation ───────────────────────────────────────────────────────────────

class PolicyError(ValueError):
    """A policy payload that cannot be saved (message is user-facing)."""


def _bounded(name: str, v, lo: int, hi: int, *, nullable: bool = True) -> Optional[int]:
    if v is None:
        if nullable:
            return None
        raise PolicyError(f"{name} is required")
    if isinstance(v, bool) or not isinstance(v, int):
        raise PolicyError(f"{name} must be a whole number")
    if v < lo or v > hi:
        raise PolicyError(f"{name} must be between {lo} and {hi}")
    return v


def clean_targets(raw: Any, *, label: str, limit: int = MAX_TARGETS,
                  allow_empty: bool = False) -> list[dict]:
    if raw in (None, ""):
        raw = []
    if not isinstance(raw, list):
        raise PolicyError(f"{label} targets must be a list")
    if not raw and not allow_empty:
        raise PolicyError(f"{label} needs at least one target")
    if len(raw) > limit:
        raise PolicyError(f"{label} has more than {limit} targets")
    out, seen = [], set()
    for t in raw:
        if not isinstance(t, dict) or t.get("type") not in TARGET_TYPES:
            raise PolicyError(f"{label}: each target is {{type: schedule|user|channel, id}}")
        try:
            tid = str(uuid.UUID(str(t.get("id"))))
        except (TypeError, ValueError):
            raise PolicyError(f"{label}: target id must be a UUID") from None
        key = (t["type"], tid)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": t["type"], "id": tid})
    return out


def clean_policy(payload: dict) -> dict:
    """Validated, normalised policy fields (PolicyError on a bad payload).

    Steps are taken in ``position`` order (list order when absent) and their
    delays must strictly increase (two steps due at the same minute are one
    step with more targets); positions are renumbered 0..n-1.
    """
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 255:
        raise PolicyError("Name is required (at most 255 characters)")
    pr_raw = payload.get("default_priorities") or []
    if not isinstance(pr_raw, list):
        raise PolicyError("default_priorities must be a list")
    priorities = []
    for p in pr_raw:
        n = prio.normalize_priority(p)
        if not n:
            raise PolicyError(f"Unknown priority {p!r}; use P1, P2, P3 or P4")
        if n not in priorities:
            priorities.append(n)
    steps_raw = payload.get("steps") or []
    if not isinstance(steps_raw, list):
        raise PolicyError("steps must be a list")
    if len(steps_raw) > MAX_STEPS:
        raise PolicyError(f"At most {MAX_STEPS} escalation steps")
    steps = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            raise PolicyError("Each step is {delay_minutes, name, targets}")
        delay = _bounded(f"Step {i + 1} delay_minutes", s.get("delay_minutes", 0), 0, 10080,
                         nullable=False)
        sname = (str(s.get("name")).strip()[:100] or None) if s.get("name") else None
        pos = s.get("position", i)
        pos = pos if isinstance(pos, int) and not isinstance(pos, bool) else i
        steps.append({"position": (pos, i), "delay_minutes": delay, "name": sname,
                      "targets": clean_targets(s.get("targets"), label=f"Step {i + 1}")})
    steps.sort(key=lambda s: s["position"])
    delays = [s["delay_minutes"] for s in steps]
    if any(b <= a for a, b in zip(delays, delays[1:])):
        raise PolicyError("Step delays must increase from one step to the next")
    for i, s in enumerate(steps):
        s["position"] = i
    repeat = _bounded("repeat_minutes", payload.get("repeat_minutes"), 1, 1440)
    mr = payload.get("max_repeats")
    max_repeats = _bounded("max_repeats", 3 if mr is None else mr, 0, MAX_REPEATS, nullable=False)
    return {
        "name": name,
        "description": (str(payload.get("description")).strip() or None)
        if payload.get("description") else None,
        "enabled": bool(payload.get("enabled", True)),
        "default_priorities": priorities,
        "ack_target_minutes": _bounded("ack_target_minutes", payload.get("ack_target_minutes"), 1, 10080),
        "repeat_minutes": repeat,
        "max_repeats": max_repeats,
        "review_minutes": _bounded("review_minutes", payload.get("review_minutes"), 5, 1440),
        "ack_expiry_minutes": _bounded("ack_expiry_minutes", payload.get("ack_expiry_minutes"), 5, 10080),
        "fallback_targets": clean_targets(payload.get("fallback_targets"), label="Fallback",
                                          limit=MAX_FALLBACK_TARGETS, allow_empty=True),
        "steps": steps,
    }


def all_targets(cleaned: dict) -> list[dict]:
    out = list(cleaned.get("fallback_targets") or [])
    for s in cleaned.get("steps") or []:
        out += s.get("targets") or []
    return out


async def missing_targets(db: AsyncSession, targets: Iterable[dict]) -> list[str]:
    """Human-readable list of targets that do not exist."""
    by_type: dict[str, set[str]] = {t: set() for t in TARGET_TYPES}
    for t in targets:
        by_type[t["type"]].add(t["id"])
    tables = {"schedule": "oncall_schedules", "user": "users", "channel": "notification_channels"}
    missing = []
    for kind, ids in by_type.items():
        if not ids:
            continue
        found = {str(r[0]) for r in (await db.execute(text(
            f"SELECT id FROM {tables[kind]} WHERE id = ANY(CAST(:ids AS uuid[]))"
        ), {"ids": sorted(ids)})).fetchall()}
        missing += [f"{kind} {i}" for i in sorted(ids - found)]
    return missing


async def priority_conflicts(db: AsyncSession, priorities: list[str], *,
                             exclude_id: Optional[str] = None) -> dict[str, str]:
    """{priority: other policy name} for priorities already defaulted by
    another enabled policy."""
    if not priorities:
        return {}
    rows = (await db.execute(text(
        "SELECT id, name, default_priorities FROM escalation_policies"
        " WHERE enabled = true AND default_priorities && CAST(:p AS varchar[])"
        + (" AND id <> CAST(:x AS uuid)" if exclude_id else "")
    ), {"p": priorities, **({"x": exclude_id} if exclude_id else {})})).fetchall()
    out = {}
    for r in rows:
        for p in r.default_priorities or []:
            if p in priorities:
                out[p] = r.name
    return out


# ── Planner (pure) ───────────────────────────────────────────────────────────

def _ts(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def is_policy_state(state: Any) -> bool:
    return isinstance(state, dict) and state.get("mode") == STATE_MODE


def new_state(policy_id: str, *, carry: Optional[dict] = None) -> dict:
    """Empty policy state. ``carry`` (a legacy state being taken over) keeps
    the channels it paged so they still get the all-clear."""
    return {
        "mode": STATE_MODE, "policy_id": str(policy_id),
        "step": -1, "repeats": 0, "last_at": None,
        "reviews": 0, "last_review_at": None,
        "expiries": 0, "anchor_at": None,
        "history": {},                    # step index -> first notified at
        "channels": [str(c) for c in ((carry or {}).get("channels") or [])],
        "people": [],                     # recipients ever paged (all-clear)
        "last_channels": [], "last_people": [],
        "closed": False,
    }


@dataclass
class Plan:
    kind: str                     # escalate | repeat | review | ack_expired | close
    step: int = -1                # step index the action pages (-1 = none)
    repeat: int = 0               # repeat number for "repeat"
    review: int = 0               # review number for "review"
    expiry: int = 0               # expiry number for "ack_expired"
    skipped: list[int] = field(default_factory=list)
    covered: bool = False         # review satisfied by a step page (record only)


def escalation_anchor(state: Optional[dict], anchor: Optional[datetime]) -> Optional[datetime]:
    """Step deadlines count from the announcement, or from the re-anchor set
    when an expired acknowledgement re-opened escalation."""
    return _ts((state or {}).get("anchor_at")) or _ts(anchor)


def _review_due(policy: Policy, state: dict, anchor: datetime, now: datetime) -> Optional[int]:
    """The review number due now (missed ones are skipped), or None."""
    every = policy.review_minutes
    if not every:
        return None
    elapsed = (now - anchor).total_seconds() / 60.0
    due_n = int(elapsed // every)
    done = int(state.get("reviews") or 0)
    return due_n if due_n > done and due_n >= 1 else None


def plan_policy(policy: Policy, state: Optional[dict], *, status: str, anchor,
                acknowledged_at=None, now: datetime, suppressed: bool = False) -> Optional[Plan]:
    """What, if anything, a sweep should do for one policy-routed incident.

    ``anchor`` is ``COALESCE(notified_at, triggered_at)``; ``now`` is the
    injected clock. Order of precedence within one sweep: closure, an expired
    acknowledgement, a new step, a repeat of the current step, a review.
    """
    st = state if is_policy_state(state) else None
    if status == "resolved":
        return Plan("close") if st is not None and not st.get("closed") else None
    if suppressed or status not in ("active", "acknowledged"):
        return None
    now = _ts(now)
    base = _ts(anchor)
    if base is None or now is None:
        return None
    st = st or new_state(policy.id)
    current = int(st.get("step") if st.get("step") is not None else -1)

    if status == "acknowledged":
        ack = _ts(acknowledged_at)
        if policy.ack_expiry_minutes and ack and now >= ack + timedelta(minutes=policy.ack_expiry_minutes):
            return Plan("ack_expired", step=max(current, 0) if policy.steps else -1,
                        expiry=int(st.get("expiries") or 0) + 1)
        n = _review_due(policy, st, base, now)
        return Plan("review", review=n) if n else None

    # Active and unacknowledged.
    esc_anchor = escalation_anchor(st, base)
    elapsed = (now - esc_anchor).total_seconds() / 60.0
    if elapsed >= 0 and policy.steps:
        due = [i for i, s in enumerate(policy.steps) if s.delay_minutes <= elapsed]
        highest = max(due) if due else -1
        if highest > current:
            # Several steps due at once (sweeper downtime, a late discovery):
            # only the highest pages; the skipped ones are recorded.
            return Plan("escalate", step=highest,
                        skipped=[i for i in due if current < i < highest])
    last_at = _ts(st.get("last_at"))
    if (current >= 0 and policy.repeat_minutes and last_at is not None
            and int(st.get("repeats") or 0) < int(policy.max_repeats or 0)
            and now - last_at >= timedelta(minutes=policy.repeat_minutes)):
        return Plan("repeat", step=current, repeat=int(st.get("repeats") or 0) + 1)
    n = _review_due(policy, st, base, now)
    if n:
        due_at = base + timedelta(minutes=policy.review_minutes * n)
        covered = last_at is not None and last_at >= due_at - timedelta(seconds=REVIEW_COVER_S)
        return Plan("review", review=n, covered=covered)
    return None


def apply_plan(state: Optional[dict], plan: Plan, policy: Policy, now: datetime) -> dict:
    """The state after ``plan`` is claimed (recipients are added by the sweep)."""
    st = dict(state) if is_policy_state(state) else new_state(policy.id, carry=state)
    st["policy_id"] = policy.id
    iso = _ts(now).isoformat()
    if plan.kind == "close":
        st["closed"] = True
        st["closed_at"] = iso
    elif plan.kind == "escalate":
        st.update(step=plan.step, repeats=0, last_at=iso)
        hist = dict(st.get("history") or {})
        hist.setdefault(str(plan.step), iso)
        st["history"] = hist
    elif plan.kind == "repeat":
        st.update(repeats=plan.repeat, last_at=iso)
    elif plan.kind == "review":
        st.update(reviews=plan.review, last_review_at=iso)
    elif plan.kind == "ack_expired":
        step = plan.step
        delay = policy.steps[step].delay_minutes if 0 <= step < len(policy.steps) else 0
        # Re-anchor so the next step comes (its delay - this step's delay)
        # after the re-opening instead of being instantly overdue.
        st.update(expiries=plan.expiry, step=step, repeats=0, last_at=iso,
                  anchor_at=(_ts(now) - timedelta(minutes=delay)).isoformat())
        if step >= 0:
            hist = dict(st.get("history") or {})
            hist.setdefault(str(step), iso)
            st["history"] = hist
    return st


def delivery_event(plan: Plan) -> str:
    """Durable delivery key for a planned send (contract)."""
    if plan.kind == "escalate":
        return f"escalation:S{plan.step}:r0"
    if plan.kind == "repeat":
        return f"escalation:S{plan.step}:r{plan.repeat}"
    if plan.kind == "review":
        return f"review:{plan.review}"
    if plan.kind == "ack_expired":
        return f"ack_expired:{plan.expiry}"
    raise ValueError(f"no delivery event for {plan.kind}")


def fallback_event(step: int) -> str:
    return f"fallback:S{step}"


def coverage_problem(recipients: list, channels: list) -> Optional[str]:
    """'coverage_gap' when a step resolved to nobody and no channel."""
    return None if (recipients or channels) else "coverage_gap"


def delivery_problem(delivered: int, attempted: bool) -> Optional[str]:
    """'route_failed' when something was attempted and nothing was delivered."""
    return "route_failed" if attempted and not delivered else None


def next_action(policy: Policy, state: Optional[dict], *, status: str, anchor,
                acknowledged_at=None, now: datetime, suppressed: bool = False) -> tuple[Optional[datetime], Optional[str]]:
    """(when, what) the next timed action is due — for the route explanation."""
    if status not in ("active", "acknowledged") or suppressed:
        return None, None
    base = _ts(anchor)
    now = _ts(now)
    if base is None:
        return None, None
    st = state if is_policy_state(state) else new_state(policy.id)
    current = int(st.get("step") if st.get("step") is not None else -1)
    options: list[tuple[datetime, str]] = []
    if status == "acknowledged":
        ack = _ts(acknowledged_at)
        if policy.ack_expiry_minutes and ack:
            options.append((ack + timedelta(minutes=policy.ack_expiry_minutes), "ack_expiry"))
    else:
        esc_anchor = escalation_anchor(st, base)
        nxt = policy.step(current + 1)
        if nxt is not None:
            options.append((esc_anchor + timedelta(minutes=nxt.delay_minutes), f"step:{current + 1}"))
        last_at = _ts(st.get("last_at"))
        if (current >= 0 and policy.repeat_minutes and last_at
                and int(st.get("repeats") or 0) < int(policy.max_repeats or 0)):
            options.append((last_at + timedelta(minutes=policy.repeat_minutes), f"repeat:{current}"))
    if policy.review_minutes:
        n = int(st.get("reviews") or 0) + 1
        options.append((base + timedelta(minutes=policy.review_minutes * n), f"review:{n}"))
    if not options:
        return None, None
    at, what = min(options, key=lambda o: o[0])
    return max(at, now) if now else at, what


# ── Route resolution ─────────────────────────────────────────────────────────

@dataclass
class Route:
    source: str                    # rule | priority_default | legacy_levels | none
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def resolve_route(*, priority: str, rule_policy: Optional[dict] = None,
                  defaults: Optional[dict[str, dict]] = None,
                  legacy_levels: Any = None) -> Route:
    """Which escalation applies (precedence: rule policy > the rule's own
    legacy levels > priority default). A rule's explicit tiers are more
    specific than a priority-wide default, so creating a default policy (or
    the recommended matrix) never silently takes over a rule that already
    escalates. ``rule_policy`` is ``{id, name, enabled}`` or None;
    ``defaults`` maps priority -> ``{id, name}`` of the enabled default."""
    notes = []
    if rule_policy:
        if rule_policy.get("enabled"):
            return Route("rule", str(rule_policy["id"]), rule_policy.get("name"))
        notes.append(f"The rule's policy {rule_policy.get('name') or rule_policy.get('id')} is disabled.")
    if isinstance(legacy_levels, list) and any(isinstance(lv, dict) for lv in legacy_levels):
        d = (defaults or {}).get(priority)
        if d:
            notes.append(f"The rule's own escalation tiers take precedence over the {priority} "
                         f"default policy {d.get('name') or d.get('id')}.")
        return Route("legacy_levels", notes=notes)
    d = (defaults or {}).get(priority)
    if d:
        return Route("priority_default", str(d["id"]), d.get("name"), notes)
    return Route("none", notes=notes)


def route_policy_sql(a: str = "a", r: str = "r") -> str:
    """SQL: the policy id routing alert ``a`` of rule ``r`` (NULL = legacy/none).
    Same precedence as :func:`resolve_route`: a rule with its own tiers is
    never routed by a priority default."""
    return (
        f"COALESCE((SELECT _ep.id FROM escalation_policies _ep WHERE _ep.id = {r}.escalation_policy_id"
        f" AND _ep.enabled = true),"
        f" CASE WHEN jsonb_typeof({r}.escalation_levels) = 'array'"
        f" AND jsonb_array_length({r}.escalation_levels) > 0 THEN NULL ELSE"
        f" (SELECT _ep.id FROM escalation_policies _ep WHERE _ep.enabled = true"
        f" AND {prio.priority_sql(a, r)} = ANY(_ep.default_priorities)"
        f" ORDER BY _ep.created_at, _ep.id LIMIT 1) END)"
    )


# ── Database ─────────────────────────────────────────────────────────────────

_SUPPORTED: Optional[bool] = None


async def supported(db: AsyncSession) -> bool:
    """Whether migration 164 has run (cached once positive)."""
    global _SUPPORTED
    if _SUPPORTED:
        return True
    try:
        _SUPPORTED = bool((await db.execute(text(
            "SELECT to_regclass('public.escalation_policies') IS NOT NULL"
            " AND to_regclass('public.oncall_schedules') IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alerts'"
            " AND column_name = 'priority')"
        ))).scalar())
    except Exception:
        await db.rollback()
        _SUPPORTED = False
    return bool(_SUPPORTED)


async def load_policies(db: AsyncSession, ids: Optional[Iterable] = None) -> dict[str, Policy]:
    id_list = None if ids is None else [str(i) for i in ids if i]
    if id_list is not None and not id_list:
        return {}
    where = "" if id_list is None else " WHERE id = ANY(CAST(:ids AS uuid[]))"
    rows = (await db.execute(text(
        "SELECT id, name, description, enabled, default_priorities, ack_target_minutes,"
        " repeat_minutes, max_repeats, review_minutes, ack_expiry_minutes, fallback_targets"
        f" FROM escalation_policies{where} ORDER BY lower(name)"
    ), {} if id_list is None else {"ids": id_list})).fetchall()
    out = {str(r.id): Policy(
        id=str(r.id), name=r.name, description=r.description, enabled=bool(r.enabled),
        default_priorities=list(r.default_priorities or []),
        ack_target_minutes=r.ack_target_minutes, repeat_minutes=r.repeat_minutes,
        max_repeats=int(r.max_repeats or 0), review_minutes=r.review_minutes,
        ack_expiry_minutes=r.ack_expiry_minutes,
        fallback_targets=list(r.fallback_targets or [])) for r in rows}
    if out:
        for s in (await db.execute(text(
            "SELECT policy_id, position, delay_minutes, name, targets FROM escalation_steps"
            " WHERE policy_id = ANY(CAST(:ids AS uuid[])) ORDER BY policy_id, delay_minutes, position"
        ), {"ids": list(out)})).fetchall():
            out[str(s.policy_id)].steps.append(Step(position=s.position, delay_minutes=s.delay_minutes,
                                                    name=s.name, targets=list(s.targets or [])))
    return out


async def default_policies(db: AsyncSession) -> dict[str, dict]:
    """{priority: {id, name}} for enabled policies' default priorities
    (oldest policy wins should two ever overlap)."""
    out: dict[str, dict] = {}
    for r in (await db.execute(text(
        "SELECT id, name, default_priorities FROM escalation_policies WHERE enabled = true"
        " ORDER BY created_at, id"
    ))).fetchall():
        for p in r.default_priorities or []:
            out.setdefault(p, {"id": str(r.id), "name": r.name})
    return out


async def save_policy(db: AsyncSession, cleaned: dict, *, policy_id: Optional[str] = None,
                      user_id=None) -> str:
    """Insert or replace a policy and its steps (caller commits)."""
    import json
    params = {k: cleaned[k] for k in ("name", "description", "enabled", "ack_target_minutes",
                                      "repeat_minutes", "max_repeats", "review_minutes",
                                      "ack_expiry_minutes")}
    params.update(prio=cleaned["default_priorities"], fb=json.dumps(cleaned["fallback_targets"]))
    if policy_id:
        row = (await db.execute(text(
            "UPDATE escalation_policies SET name = :name, description = :description,"
            " enabled = :enabled, default_priorities = CAST(:prio AS varchar[]),"
            " ack_target_minutes = :ack_target_minutes, repeat_minutes = :repeat_minutes,"
            " max_repeats = :max_repeats, review_minutes = :review_minutes,"
            " ack_expiry_minutes = :ack_expiry_minutes, fallback_targets = CAST(:fb AS jsonb),"
            " updated_at = NOW() WHERE id = :id RETURNING id"
        ), {**params, "id": policy_id})).first()
        if not row:
            raise LookupError("Escalation policy not found")
        await db.execute(text("DELETE FROM escalation_steps WHERE policy_id = :id"), {"id": policy_id})
        pid = str(row.id)
    else:
        pid = str((await db.execute(text(
            "INSERT INTO escalation_policies (name, description, enabled, default_priorities,"
            " ack_target_minutes, repeat_minutes, max_repeats, review_minutes, ack_expiry_minutes,"
            " fallback_targets, created_by) VALUES (:name, :description, :enabled,"
            " CAST(:prio AS varchar[]), :ack_target_minutes, :repeat_minutes, :max_repeats,"
            " :review_minutes, :ack_expiry_minutes, CAST(:fb AS jsonb), :uid) RETURNING id"
        ), {**params, "uid": user_id})).scalar())
    for s in cleaned["steps"]:
        await db.execute(text(
            "INSERT INTO escalation_steps (policy_id, position, delay_minutes, name, targets)"
            " VALUES (:pid, :pos, :delay, :name, CAST(:t AS jsonb))"
        ), {"pid": pid, "pos": s["position"], "delay": s["delay_minutes"], "name": s["name"],
            "t": json.dumps(s["targets"])})
    return pid


async def rule_counts(db: AsyncSession) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in (await db.execute(text(
        "SELECT escalation_policy_id, count(*) FROM alert_rules"
        " WHERE escalation_policy_id IS NOT NULL GROUP BY escalation_policy_id"
    ))).fetchall()}


# ── Target resolution (shared by the sweep and the dry run) ─────────────────

@dataclass
class Resolved:
    recipients: list[dict] = field(default_factory=list)   # people (deduped)
    channels: list[dict] = field(default_factory=list)     # {id, name, type}
    gaps: list[str] = field(default_factory=list)

    @property
    def channel_ids(self) -> list[str]:
        return [c["id"] for c in self.channels]

    @property
    def empty(self) -> bool:
        return not self.recipients and not self.channels


class Directory:
    """Per-sweep cache of schedules, people and channels."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.schedules: dict[str, oc.Schedule] = {}
        self.people: dict[str, oc.Person] = {}
        self.channels: dict[str, Any] = {}
        self._missing: set[tuple[str, str]] = set()

    async def prime(self, targets: Iterable[dict], at: datetime) -> None:
        sched_ids = {t["id"] for t in targets if t.get("type") == "schedule"} - set(self.schedules)
        sched_ids -= {i for k, i in self._missing if k == "schedule"}
        if sched_ids:
            found = await oc.load_schedules(self.db, sched_ids,
                                            overrides_from=at - timedelta(days=1))
            self.schedules.update(found)
            self._missing |= {("schedule", i) for i in sched_ids - set(found)}
        user_ids = {t["id"] for t in targets if t.get("type") == "user"}
        for s in self.schedules.values():
            for lv in s.layers:
                user_ids |= set(lv.members)
            user_ids |= {o.user_id for o in s.overrides}
        await self.prime_people(user_ids)
        chan_ids = ({t["id"] for t in targets if t.get("type") == "channel"} - set(self.channels)
                    - {i for k, i in self._missing if k == "channel"})
        if chan_ids:
            rows = (await self.db.execute(text(
                "SELECT id, name, type, enabled FROM notification_channels"
                " WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": sorted(chan_ids)})).fetchall()
            for r in rows:
                self.channels[str(r.id)] = r
            self._missing |= {("channel", i) for i in chan_ids - {str(r.id) for r in rows}}

    async def prime_people(self, user_ids: Iterable) -> None:
        need = {str(u) for u in user_ids if u} - set(self.people) \
            - {i for k, i in self._missing if k == "user"}
        if need:
            found = await oc.load_people(self.db, need)
            self.people.update(found)
            self._missing |= {("user", i) for i in need - set(found)}

    def person_recipients(self, user_id: str, via: str, gaps: list[str]) -> list[dict]:
        p = self.people.get(str(user_id))
        if p is None:
            gaps.append(f"User {user_id} no longer exists.")
            return []
        if not p.active:
            gaps.append(f"{p.name} is not an active user.")
            return []
        rec = p.recipients(via)
        if not rec:
            gaps.append(f"{p.name} has no email or SMS contact method.")
        return rec

    def resolve(self, targets: Iterable[dict], at: datetime) -> Resolved:
        """People and channels the targets reach at instant ``at`` (call
        :meth:`prime` first). Recipients are de-duplicated across targets."""
        out = Resolved()
        people: list[dict] = []
        seen_ch: set[str] = set()
        for t in targets:
            kind, tid = t.get("type"), str(t.get("id"))
            if kind == "schedule":
                s = self.schedules.get(tid)
                if s is None:
                    out.gaps.append("An on-call schedule in this step no longer exists.")
                    continue
                if not s.enabled:
                    out.gaps.append(f"On-call schedule {s.name} is disabled.")
                    continue
                who = oc.on_call_at(s, at)
                if not who.users:
                    out.gaps.append(f"Nobody is on call in {s.name} at "
                                    f"{at.astimezone(s.zone).strftime('%Y-%m-%d %H:%M %Z')}.")
                    continue
                for uid in who.users:
                    people += self.person_recipients(uid, f"schedule:{s.name}", out.gaps)
            elif kind == "user":
                people += self.person_recipients(tid, "user", out.gaps)
            elif kind == "channel":
                ch = self.channels.get(tid)
                if ch is None:
                    out.gaps.append("A notification channel in this step no longer exists.")
                elif not ch.enabled:
                    out.gaps.append(f"Notification channel {ch.name} is disabled.")
                elif tid not in seen_ch:
                    seen_ch.add(tid)
                    out.channels.append({"id": tid, "name": ch.name, "type": ch.type})
        out.recipients = oc.dedupe_recipients(people)
        return out


def mask_address(kind: str, address: str) -> str:
    """Partially hidden contact address for viewers without user management."""
    a = str(address or "")
    if kind == "email" and "@" in a:
        local, _, domain = a.partition("@")
        return (local[:1] + "***@" + domain) if local else "***@" + domain
    digits = [c for c in a if c.isdigit()]
    return ("•••" + "".join(digits[-3:])) if digits else "•••"


def recipient_view(r: dict, *, reveal: bool) -> dict:
    return {"kind": r.get("kind"), "address": r.get("address") if reveal else
            mask_address(r.get("kind"), r.get("address")),
            "user_id": r.get("user_id"), "user_name": r.get("user_name"), "via": r.get("via")}


def channel_view(c: dict) -> dict:
    return {"kind": c.get("type"), "address": None, "user_id": None, "user_name": None,
            "via": f"channel:{c.get('name')}", "channel_id": c.get("id")}


async def preview(db: AsyncSession, policy: Policy, *, priority: str, at: datetime,
                  reveal: bool = False) -> dict:
    """Dry run of ``policy`` for an incident announced at ``at`` — who each
    step would reach at its due time, the coverage gaps and where the
    fallback would go. Never sends anything."""
    at = _ts(at)
    d = Directory(db)
    targets = list(policy.fallback_targets)
    for s in policy.steps:
        targets += s.targets
    await d.prime(targets, at)
    fallback_cache: dict[datetime, Resolved] = {}

    def fallback_at(when: datetime) -> Resolved:
        if when not in fallback_cache:
            fallback_cache[when] = d.resolve(policy.fallback_targets, when)
        return fallback_cache[when]

    warnings: list[str] = []
    if not policy.enabled:
        warnings.append("This policy is disabled; it routes nothing until it is enabled.")
    if not policy.steps:
        warnings.append("This policy has no steps.")
    steps = []
    for i, s in enumerate(policy.steps):
        when = at + timedelta(minutes=s.delay_minutes)
        res = d.resolve(s.targets, when)
        uses_fallback = res.empty
        item = {
            "position": i, "name": s.name, "at_offset_minutes": s.delay_minutes, "at": when,
            "recipients": [recipient_view(r, reveal=reveal) for r in res.recipients]
                          + [channel_view(c) for c in res.channels],
            "gaps": res.gaps, "uses_fallback": uses_fallback,
        }
        if uses_fallback:
            fb = fallback_at(when)
            item["fallback_recipients"] = ([recipient_view(r, reveal=reveal) for r in fb.recipients]
                                           + [channel_view(c) for c in fb.channels])
            if fb.empty:
                warnings.append(f"Step {i + 1} reaches nobody at "
                                f"{when.strftime('%Y-%m-%d %H:%M UTC')} and the fallback reaches nobody either.")
            else:
                warnings.append(f"Step {i + 1} reaches nobody at {when.strftime('%Y-%m-%d %H:%M UTC')};"
                                f" the fallback route would be used.")
        steps.append(item)
    fb_now = fallback_at(at)
    if not policy.fallback_targets:
        warnings.append("No fallback route: a step that reaches nobody, or whose deliveries all fail,"
                        " is recorded as a coverage gap with no alternate route.")
    if priority and policy.default_priorities and priority not in policy.default_priorities:
        warnings.append(f"{priority} is not one of this policy's default priorities; it applies only"
                        f" to rules that name it.")
    return {
        "policy": {"id": policy.id, "name": policy.name, "enabled": policy.enabled},
        "priority": priority,
        "at": at,
        "ack_target_minutes": policy.ack_target_minutes,
        "steps": steps,
        "repeat": ({"every_minutes": policy.repeat_minutes, "max_repeats": policy.max_repeats}
                   if policy.repeat_minutes and policy.max_repeats else None),
        "review": ({"every_minutes": policy.review_minutes,
                    "recipients": "The incident owner and the last notified step",
                    "continues_after_acknowledgement": True}
                   if policy.review_minutes else None),
        "ack_expiry_minutes": policy.ack_expiry_minutes,
        "fallback": [recipient_view(r, reveal=reveal) for r in fb_now.recipients]
                    + [channel_view(c) for c in fb_now.channels],
        "fallback_gaps": fb_now.gaps,
        "warnings": warnings,
    }
