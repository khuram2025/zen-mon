"""On-call schedules: who is on duty at an instant, and how to reach them.

A schedule (migration 164: ``oncall_schedules`` / ``oncall_layers`` /
``oncall_overrides``) lives in one IANA time zone and answers "who is on call
at T" with this precedence:

1. **Overrides** — a time-boxed substitute (holiday cover, swap). Every
   override covering T is on call; they win over every layer.
2. **Layers** — each layer rotates its ordered ``members`` every
   ``shift_hours`` starting at ``handoff_at``. Optional ``restrict_to``
   windows limit when the layer is on duty. Of the layers on duty at T the
   one with the highest ``position`` wins (a "business hours" layer above a
   24x7 base layer, say).

Rotation is anchored to the schedule's local wall clock: a daily rotation
handed off at 09:00 Asia/Riyadh or 09:00 Europe/London hands off at 09:00
local on every day, including the days a DST change makes 23 or 25 hours
long. Shift boundaries are therefore ``handoff_local + n * shift_hours`` in
wall-clock time, converted to an instant in the zone. A wall time skipped by
a spring-forward gap resolves to the instant just after the gap; a repeated
fall-back hour uses its first occurrence.

``restrict_to`` is ``[{"days": [0..6], "start": "HH:MM", "end": "HH:MM"}]`` in
the schedule zone, 0 = Monday ... 6 = Sunday (Python weekday order; the rule
schedule's ISO 1..7 minus one). ``end <= start`` spans midnight and belongs
to the day it starts on (Fri 18:00 -> Sat 08:00 is ``days: [4]``);
``start == end`` is the whole day. No ``days`` means every day. An empty
``restrict_to`` means always on duty.

Contacts: a user's enabled ``user_contact_methods`` (email / sms). Their
account email is the implicit email contact when they have no email method
of their own. Inactive users are never paged. Recipient keys use the same
normalisation as ``host_alert_service._recipients`` (lower-case email, phone
without spaces or punctuation) so one person reached through a channel and a
schedule in the same step gets one message.

Everything above the "Database" section is pure and unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_TIMELINE_DAYS = 62
MAX_LAYERS = 10
MAX_MEMBERS = 50
MAX_RESTRICTIONS = 14

# Kept identical to host_alert_service._PHONE_NOISE (test-pinned): the
# delivery dedupe keys and these recipient keys must agree.
_PHONE_NOISE = re.compile(r"[\s().\-]")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().\-]{5,30}$")


# ── Model ────────────────────────────────────────────────────────────────────

@dataclass
class Layer:
    id: str
    name: str
    position: int
    shift_hours: int
    handoff_at: datetime
    members: list[str]
    restrict_to: list[dict] = field(default_factory=list)


@dataclass
class Override:
    id: str
    user_id: str
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None


@dataclass
class Schedule:
    id: str
    name: str
    timezone: str = "UTC"
    enabled: bool = True
    layers: list[Layer] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)

    @property
    def zone(self) -> ZoneInfo:
        return zone_or_utc(self.timezone)


@dataclass
class OnCall:
    """Who is on call at one instant."""
    users: list[str]
    source: str                  # "override" | "layer" | "none"
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    override_ids: list[str] = field(default_factory=list)


def zone_or_utc(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def valid_timezone(name: Any) -> bool:
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        return False
    try:
        ZoneInfo(name.strip())
    except Exception:
        return False
    return True


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ── Validation (shared by the API) ──────────────────────────────────────────

def parse_hhmm(value: Any) -> Optional[time]:
    if not isinstance(value, str):
        return None
    m = _HHMM.match(value.strip())
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def clean_restrictions(raw: Any) -> list[dict]:
    """Validated, normalised ``restrict_to``; ValueError with a reason."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("restrict_to must be a list of windows")
    if len(raw) > MAX_RESTRICTIONS:
        raise ValueError(f"At most {MAX_RESTRICTIONS} restriction windows per layer")
    out = []
    for w in raw:
        if not isinstance(w, dict):
            raise ValueError("Each restriction window is {days, start, end}")
        days = w.get("days") or []
        if not isinstance(days, list) or any(
                isinstance(d, bool) or not isinstance(d, int) or d < 0 or d > 6 for d in days):
            raise ValueError("Restriction days are weekday numbers 0 (Monday) to 6 (Sunday)")
        start, end = parse_hhmm(w.get("start")), parse_hhmm(w.get("end"))
        if start is None or end is None:
            raise ValueError("Restriction start and end are HH:MM (24-hour)")
        out.append({"days": sorted(set(days)), "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M")})
    return out


def normalize_address(kind: str, address: Any) -> str:
    """Dedupe key for one contact address (host_alert_service._recipients)."""
    addr = str(address or "").strip()
    return addr.lower() if kind == "email" else _PHONE_NOISE.sub("", addr)


def valid_address(kind: str, address: Any) -> bool:
    addr = str(address or "").strip()
    if kind == "email":
        return bool(_EMAIL_RE.match(addr)) and len(addr) <= 255
    if kind == "sms":
        return bool(_PHONE_RE.match(addr))
    return False


# ── Rotation ─────────────────────────────────────────────────────────────────

def _localize(naive: datetime, zone: ZoneInfo) -> datetime:
    """The instant a local wall time names (gap -> just after, fold -> first)."""
    aware = naive.replace(tzinfo=zone, fold=0)
    # A wall time inside a spring-forward gap does not round-trip; zoneinfo
    # maps it with the pre-gap offset, which lands after the gap in UTC.
    return aware.astimezone(timezone.utc)


def shift_boundary(layer: Layer, zone: ZoneInfo, n: int) -> datetime:
    """Start (UTC) of rotation shift ``n`` (0 = the one starting at handoff_at)."""
    base = _utc(layer.handoff_at).astimezone(zone).replace(tzinfo=None)
    return _localize(base + timedelta(hours=layer.shift_hours * n), zone)


def shift_index(layer: Layer, zone: ZoneInfo, at: datetime) -> int:
    """Index of the rotation shift containing ``at`` (negative before handoff_at)."""
    at = _utc(at)
    shift = timedelta(hours=max(1, int(layer.shift_hours)))
    base = _utc(layer.handoff_at).astimezone(zone).replace(tzinfo=None)
    local = at.astimezone(zone).replace(tzinfo=None)
    n = (local - base) // shift
    # Wall-clock arithmetic can be an hour off around a DST change; settle on
    # the shift whose [start, next start) actually contains the instant.
    while shift_boundary(layer, zone, n) > at:
        n -= 1
    while shift_boundary(layer, zone, n + 1) <= at:
        n += 1
    return n


def rotation_user(layer: Layer, zone: ZoneInfo, at: datetime) -> Optional[str]:
    members = [str(m) for m in layer.members if m]
    if not members:
        return None
    return members[shift_index(layer, zone, at) % len(members)]


def _window_bounds(day: date, w: dict) -> tuple[datetime, datetime]:
    start, end = parse_hhmm(w["start"]), parse_hhmm(w["end"])
    s = datetime.combine(day, start)
    e = datetime.combine(day, end)
    if e <= s:
        e += timedelta(days=1)
    return s, e


def restriction_active(restrict_to: list[dict], zone: ZoneInfo, at: datetime) -> bool:
    """Whether ``at`` falls inside any window (no windows = always)."""
    if not restrict_to:
        return True
    local = _utc(at).astimezone(zone)
    for w in restrict_to:
        days = set(w.get("days") or range(7))
        # A window can start today, or have started yesterday and span midnight.
        for back in (0, 1):
            day = local.date() - timedelta(days=back)
            if day.weekday() not in days:
                continue
            s, e = _window_bounds(day, w)
            if _localize(s, zone) <= _utc(at) < _localize(e, zone):
                return True
    return False


def layer_on_duty(layer: Layer, zone: ZoneInfo, at: datetime) -> Optional[str]:
    if not restriction_active(layer.restrict_to, zone, at):
        return None
    return rotation_user(layer, zone, at)


def on_call_at(schedule: Schedule, at: datetime) -> OnCall:
    """Who is on call at ``at`` (overrides, else the highest layer on duty)."""
    at = _utc(at)
    if not schedule.enabled:
        return OnCall(users=[], source="none")
    active = sorted((o for o in schedule.overrides
                     if _utc(o.starts_at) <= at < _utc(o.ends_at)),
                    key=lambda o: (_utc(o.starts_at), o.id))
    if active:
        users = list(dict.fromkeys(str(o.user_id) for o in active))
        return OnCall(users=users, source="override", override_ids=[o.id for o in active])
    zone = schedule.zone
    for layer in sorted(schedule.layers, key=lambda lv: (-int(lv.position), lv.id)):
        uid = layer_on_duty(layer, zone, at)
        if uid:
            return OnCall(users=[uid], source="layer", layer_id=layer.id, layer_name=layer.name)
    return OnCall(users=[], source="none")


def _boundaries(schedule: Schedule, start: datetime, end: datetime) -> list[datetime]:
    """Every instant in [start, end) where the answer can change."""
    zone = schedule.zone
    points = {start}
    for layer in schedule.layers:
        if not layer.members:
            continue
        n = shift_index(layer, zone, start)
        guard = 0
        while guard < 20000:
            b = shift_boundary(layer, zone, n)
            if b >= end:
                break
            if b > start:
                points.add(b)
            n += 1
            guard += 1
        if layer.restrict_to:
            local_start = start.astimezone(zone).date() - timedelta(days=1)
            local_end = end.astimezone(zone).date() + timedelta(days=1)
            day = local_start
            while day <= local_end:
                for w in layer.restrict_to:
                    if day.weekday() not in set(w.get("days") or range(7)):
                        continue
                    s, e = _window_bounds(day, w)
                    for b in (_localize(s, zone), _localize(e, zone)):
                        if start < b < end:
                            points.add(b)
                day += timedelta(days=1)
    for o in schedule.overrides:
        for b in (_utc(o.starts_at), _utc(o.ends_at)):
            if start < b < end:
                points.add(b)
    return sorted(points)


def timeline(schedule: Schedule, start: datetime, end: datetime) -> dict:
    """Shifts in [start, end): ``{"shifts": [...], "gaps": [...]}``.

    A shift is ``{start, end, user_id, layer_id, layer, override}``; adjacent
    segments with the same responder and source are merged. Several
    overrides at once give one shift per user. Gaps are the periods nobody is
    on call. The range is capped at :data:`MAX_TIMELINE_DAYS`.
    """
    start, end = _utc(start), _utc(end)
    if end <= start:
        return {"shifts": [], "gaps": []}
    end = min(end, start + timedelta(days=MAX_TIMELINE_DAYS))
    points = _boundaries(schedule, start, end) + [end]
    shifts: list[dict] = []
    gaps: list[dict] = []
    open_: dict[tuple, dict] = {}
    for i in range(len(points) - 1):
        seg_start, seg_end = points[i], points[i + 1]
        if seg_end <= seg_start:
            continue
        oc = on_call_at(schedule, seg_start)
        keys = set()
        for uid in oc.users:
            key = (uid, oc.source, oc.layer_id if oc.source == "layer" else "override")
            keys.add(key)
            cur = open_.get(key)
            if cur and cur["end"] == seg_start:
                cur["end"] = seg_end
            else:
                cur = {"start": seg_start, "end": seg_end, "user_id": uid,
                       "layer_id": oc.layer_id, "layer": oc.layer_name if oc.source == "layer" else None,
                       "override": oc.source == "override"}
                shifts.append(cur)
                open_[key] = cur
        for key in [k for k in open_ if k not in keys]:
            open_.pop(key)
        if not oc.users:
            if gaps and gaps[-1]["end"] == seg_start:
                gaps[-1]["end"] = seg_end
            else:
                gaps.append({"start": seg_start, "end": seg_end})
    shifts.sort(key=lambda s: (s["start"], s["user_id"]))
    return {"shifts": shifts, "gaps": gaps}


def on_call_until(schedule: Schedule, at: datetime, horizon_days: int = 31) -> Optional[datetime]:
    """When the answer at ``at`` next changes (None within the horizon)."""
    at = _utc(at)
    now = on_call_at(schedule, at)
    for b in _boundaries(schedule, at, at + timedelta(days=horizon_days)):
        if b <= at:
            continue
        nxt = on_call_at(schedule, b)
        if nxt.users != now.users or nxt.source != now.source or nxt.layer_id != now.layer_id:
            return b
    return None


# ── Contacts ─────────────────────────────────────────────────────────────────

@dataclass
class Person:
    user_id: str
    name: str
    active: bool
    email: Optional[str]
    timezone: Optional[str]
    methods: list[dict]            # enabled methods: {kind, address, label}

    def recipients(self, via: str) -> list[dict]:
        """Deliverable recipients for this person (none when inactive)."""
        if not self.active:
            return []
        out = [{"kind": m["kind"], "address": m["address"], "user_id": self.user_id,
                "user_name": self.name, "label": m.get("label"), "via": via}
               for m in self.methods if m.get("kind") in ("email", "sms") and m.get("address")]
        if self.email and not any(m["kind"] == "email" for m in out):
            out.insert(0, {"kind": "email", "address": self.email, "user_id": self.user_id,
                           "user_name": self.name, "label": "Account email", "via": via})
        return out


def dedupe_recipients(items: Iterable[dict]) -> list[dict]:
    """Recipients in order, one per normalised (kind, address)."""
    out, seen = [], set()
    for r in items:
        key = (r.get("kind"), normalize_address(r.get("kind", ""), r.get("address")))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ── Database ─────────────────────────────────────────────────────────────────

def _layer_from_row(r) -> Layer:
    restrict = r.restrict_to if isinstance(r.restrict_to, list) else []
    return Layer(id=str(r.id), name=r.name or "Layer", position=int(r.position or 0),
                 shift_hours=int(r.shift_hours or 168), handoff_at=_utc(r.handoff_at),
                 members=[str(m) for m in (r.members or [])], restrict_to=restrict)


async def load_schedules(db: AsyncSession, ids: Optional[Iterable] = None, *,
                         overrides_from: Optional[datetime] = None) -> dict[str, Schedule]:
    """{schedule_id: Schedule} with layers and overrides (all schedules when
    ``ids`` is None). Overrides that ended before ``overrides_from`` (default
    two days ago) are left out."""
    id_list = None if ids is None else [str(i) for i in ids if i]
    if id_list is not None and not id_list:
        return {}
    where = "" if id_list is None else " WHERE id = ANY(CAST(:ids AS uuid[]))"
    params: dict = {} if id_list is None else {"ids": id_list}
    rows = (await db.execute(text(
        f"SELECT id, name, description, timezone, enabled FROM oncall_schedules{where} ORDER BY lower(name)"
    ), params)).fetchall()
    out = {str(r.id): Schedule(id=str(r.id), name=r.name, timezone=r.timezone or "UTC",
                               enabled=bool(r.enabled)) for r in rows}
    if not out:
        return out
    keys = list(out)
    for r in (await db.execute(text(
        "SELECT id, schedule_id, name, position, shift_hours, handoff_at, members, restrict_to"
        " FROM oncall_layers WHERE schedule_id = ANY(CAST(:ids AS uuid[])) ORDER BY position, id"
    ), {"ids": keys})).fetchall():
        out[str(r.schedule_id)].layers.append(_layer_from_row(r))
    since = overrides_from or (datetime.now(timezone.utc) - timedelta(days=2))
    for r in (await db.execute(text(
        "SELECT id, schedule_id, user_id, starts_at, ends_at, reason FROM oncall_overrides"
        " WHERE schedule_id = ANY(CAST(:ids AS uuid[])) AND ends_at > :since ORDER BY starts_at"
    ), {"ids": keys, "since": since})).fetchall():
        out[str(r.schedule_id)].overrides.append(Override(
            id=str(r.id), user_id=str(r.user_id), starts_at=_utc(r.starts_at),
            ends_at=_utc(r.ends_at), reason=r.reason))
    return out


async def load_people(db: AsyncSession, user_ids: Iterable) -> dict[str, Person]:
    """{user_id: Person} with enabled contact methods."""
    ids = list(dict.fromkeys(str(u) for u in user_ids if u))
    if not ids:
        return {}
    rows = (await db.execute(text(
        "SELECT id, username, full_name, email, is_active, timezone FROM users"
        " WHERE id = ANY(CAST(:ids AS uuid[]))"
    ), {"ids": ids})).fetchall()
    people = {str(r.id): Person(user_id=str(r.id), name=(r.full_name or r.username or "User"),
                                active=bool(r.is_active), email=(r.email or None),
                                timezone=r.timezone, methods=[]) for r in rows}
    for m in (await db.execute(text(
        "SELECT user_id, kind, address, label FROM user_contact_methods"
        " WHERE enabled = true AND user_id = ANY(CAST(:ids AS uuid[])) ORDER BY created_at, id"
    ), {"ids": ids})).fetchall():
        p = people.get(str(m.user_id))
        if p:
            p.methods.append({"kind": m.kind, "address": m.address, "label": m.label})
    return people


async def user_names(db: AsyncSession, user_ids: Iterable) -> dict[str, str]:
    ids = list(dict.fromkeys(str(u) for u in user_ids if u))
    if not ids:
        return {}
    rows = (await db.execute(text(
        "SELECT id, username, full_name FROM users WHERE id = ANY(CAST(:ids AS uuid[]))"
    ), {"ids": ids})).fetchall()
    return {str(r.id): (r.full_name or r.username) for r in rows}


def schedule_dict(s: Schedule, *, names: Optional[dict] = None) -> dict:
    names = names or {}
    return {
        "id": s.id, "name": s.name, "timezone": s.timezone, "enabled": s.enabled,
        "layers": [{
            "id": lv.id, "name": lv.name, "position": lv.position, "shift_hours": lv.shift_hours,
            "handoff_at": lv.handoff_at, "members": lv.members,
            "member_names": [names.get(m) for m in lv.members],
            "restrict_to": lv.restrict_to,
        } for lv in sorted(s.layers, key=lambda lv: lv.position)],
    }
