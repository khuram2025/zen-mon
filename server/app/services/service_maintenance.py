"""Service-check maintenance windows: lifecycle, scope and SLA exclusion.

A window is never physically deleted once it has started: ending it early
records its effective end (ended_at) and a window that never started is
cancelled (cancelled_at). Historical availability therefore stays
reproducible after the window ends (review B04).

Effective interval = [starts_at, LEAST(ends_at, ended_at)); cancelled windows
never apply. Group and tag membership is evaluated as of now (a documented
limitation: moving a check between groups re-attributes old windows).
Tag matching is exact, the same rule the poller uses to suppress alerts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select

from app.core import scoping
from app.models.service_check import ServiceCheck, ServiceCheckGroup, ServiceCheckMaintenance
from app.schemas.service_check import ServiceMaintenanceResponse
from app.services.service_availability import merge_intervals


class MaintenanceError(ValueError):
    """Maps to an HTTP status in the API layer."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def effective_end(m) -> datetime:
    ends = _aware(m.ends_at)
    ended = _aware(getattr(m, "ended_at", None))
    return min(ends, ended) if ended else ends


def window_state(m, now: datetime | None = None) -> str:
    now = now or utcnow()
    if getattr(m, "cancelled_at", None):
        return "cancelled"
    starts = _aware(m.starts_at)
    if starts > now:
        return "scheduled"
    end = effective_end(m)
    if end > now:
        return "active"
    ended = _aware(getattr(m, "ended_at", None))
    if ended and ended < _aware(m.ends_at):
        return "ended"
    return "expired"


def covers(m, check) -> bool:
    """Whether window `m` applies to `check` (id, group_id, tags)."""
    scope = m.scope_type
    if scope == "all":
        return True
    if scope == "check":
        return m.scope_check_id is not None and str(m.scope_check_id) == str(_get(check, "id"))
    if scope == "group":
        gid = _get(check, "group_id")
        return m.scope_group_id is not None and gid is not None and str(m.scope_group_id) == str(gid)
    if scope == "tag":
        return bool(m.scope_tag) and m.scope_tag in list(_get(check, "tags") or [])
    return False


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _effective_end_sql():
    M = ServiceCheckMaintenance
    return func.least(M.ends_at, func.coalesce(M.ended_at, M.ends_at))


async def load_windows(db, start: datetime, end: datetime) -> list:
    """Non-cancelled windows whose effective interval overlaps [start, end)."""
    M = ServiceCheckMaintenance
    rows = (await db.execute(
        select(M)
        .where(M.cancelled_at.is_(None))
        .where(M.starts_at < end)
        .where(_effective_end_sql() > start)
        .order_by(M.starts_at)
    )).scalars().all()
    return list(rows)


def intervals_from_windows(windows, check, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Merged effective intervals of the windows covering `check`, clipped."""
    spans = []
    for m in windows:
        if getattr(m, "cancelled_at", None) or not covers(m, check):
            continue
        lo, hi = max(_aware(m.starts_at), start), min(effective_end(m), end)
        if hi > lo:
            spans.append((lo, hi))
    return merge_intervals(spans)


async def intervals_for_checks(db, checks, start: datetime, end: datetime) -> dict[str, list]:
    """{check_id: merged maintenance intervals} for check-like rows/dicts."""
    checks = list(checks)
    if not checks or end <= start:
        return {}
    windows = await load_windows(db, start, end)
    if not windows:
        return {}
    out = {}
    for c in checks:
        spans = intervals_from_windows(windows, c, start, end)
        if spans:
            out[str(_get(c, "id"))] = spans
    return out


async def active_check_ids(db, now: datetime | None = None) -> set[UUID]:
    """Service-check IDs covered by a window that is active right now."""
    now = now or utcnow()
    M = ServiceCheckMaintenance
    windows = (await db.execute(
        select(M)
        .where(M.cancelled_at.is_(None))
        .where(M.starts_at <= now)
        .where(_effective_end_sql() >= now)
    )).scalars().all()
    if not windows:
        return set()
    affected: set[UUID] = set()
    for w in windows:
        if w.scope_type == "check" and w.scope_check_id:
            affected.add(w.scope_check_id)
        elif w.scope_type == "group" and w.scope_group_id:
            affected.update((await db.execute(
                select(ServiceCheck.id).where(ServiceCheck.group_id == w.scope_group_id)
            )).scalars().all())
        elif w.scope_type == "tag" and w.scope_tag:
            affected.update((await db.execute(
                select(ServiceCheck.id).where(ServiceCheck.tags.any(w.scope_tag))
            )).scalars().all())
        elif w.scope_type == "all":
            affected.update((await db.execute(select(ServiceCheck.id))).scalars().all())
    return affected


# ── Labels / responses ─────────────────────────────────────────────────────

async def _names(db, windows) -> tuple[dict, dict, dict]:
    check_ids = {m.scope_check_id for m in windows if m.scope_check_id}
    group_ids = {m.scope_group_id for m in windows if m.scope_group_id}
    user_ids = {u for m in windows for u in (m.created_by, getattr(m, "ended_by", None)) if u}
    checks, groups, users = {}, {}, {}
    if check_ids:
        checks = {r[0]: r[1] for r in (await db.execute(
            select(ServiceCheck.id, ServiceCheck.name).where(ServiceCheck.id.in_(list(check_ids)))
        )).all()}
    if group_ids:
        groups = {r[0]: r[1] for r in (await db.execute(
            select(ServiceCheckGroup.id, ServiceCheckGroup.name).where(ServiceCheckGroup.id.in_(list(group_ids)))
        )).all()}
    if user_ids:
        try:
            from app.models.user import User
            users = {r[0]: r[1] for r in (await db.execute(
                select(User.id, User.username).where(User.id.in_(list(user_ids)))
            )).all()}
        except Exception:
            users = {}
    return checks, groups, users


def scope_label(m, checks: dict, groups: dict) -> str:
    if m.scope_type == "check":
        return checks.get(m.scope_check_id, "(deleted monitor)")
    if m.scope_type == "group":
        return groups.get(m.scope_group_id, "(deleted group)")
    if m.scope_type == "tag":
        return f"tag:{m.scope_tag}"
    return "All monitors"


def to_response(m, label: str | None = None, users: dict | None = None,
                now: datetime | None = None) -> ServiceMaintenanceResponse:
    now = now or utcnow()
    users = users or {}
    state = window_state(m, now)
    return ServiceMaintenanceResponse(
        id=m.id,
        scope_type=m.scope_type,
        scope_check_id=m.scope_check_id,
        scope_group_id=m.scope_group_id,
        scope_tag=m.scope_tag,
        scope_label=label,
        starts_at=_aware(m.starts_at),
        ends_at=_aware(m.ends_at),
        reason=m.reason,
        active=state == "active",
        created_at=_aware(m.created_at) or now,
        created_by_name=users.get(m.created_by),
        ended_at=_aware(getattr(m, "ended_at", None)),
        ended_by_name=users.get(getattr(m, "ended_by", None)),
        end_reason=getattr(m, "end_reason", None),
        cancelled_at=_aware(getattr(m, "cancelled_at", None)),
        updated_at=_aware(getattr(m, "updated_at", None)),
        effective_ends_at=effective_end(m),
        state=state,
    )


async def responses(db, windows) -> list[ServiceMaintenanceResponse]:
    checks, groups, users = await _names(db, windows)
    now = utcnow()
    return [to_response(m, scope_label(m, checks, groups), users, now) for m in windows]


# ── Scope ──────────────────────────────────────────────────────────────────

async def _visible_check_ids(db, visible_tags: list[str]) -> set:
    from sqlalchemy import text as sql_text
    rows = (await db.execute(
        select(ServiceCheck.id).where(
            sql_text(scoping.text_tags_visible("service_checks.tags"))
            .bindparams(**{scoping.SCOPE_PARAM: visible_tags}))
    )).scalars().all()
    return set(rows)


async def _group_visibility(db, group_ids, visible_tags) -> tuple[set, set]:
    """(groups with >=1 visible check, groups whose every check is visible)."""
    if not group_ids:
        return set(), set()
    rows = (await db.execute(
        select(ServiceCheck.group_id, ServiceCheck.tags).where(ServiceCheck.group_id.in_(list(group_ids)))
    )).all()
    any_visible, hidden = set(), set()
    for gid, tags in rows:
        if scoping.entity_visible(tags, visible_tags):
            any_visible.add(gid)
        else:
            hidden.add(gid)
    all_visible = {g for g in any_visible if g not in hidden}
    return any_visible, all_visible


async def filter_visible(db, windows, visible_tags: list[str] | None) -> list:
    """Hide check/group windows the caller cannot see; tag/all stay visible."""
    if visible_tags is None:
        return list(windows)
    check_ids = {m.scope_check_id for m in windows if m.scope_type == "check" and m.scope_check_id}
    group_ids = {m.scope_group_id for m in windows if m.scope_type == "group" and m.scope_group_id}
    visible_checks = await _visible_check_ids(db, visible_tags) if check_ids else set()
    visible_groups, _ = await _group_visibility(db, group_ids, visible_tags)
    out = []
    for m in windows:
        if m.scope_type == "check" and m.scope_check_id not in visible_checks:
            continue
        if m.scope_type == "group" and m.scope_group_id not in visible_groups:
            continue
        out.append(m)
    return out


async def _assert_manageable(db, scope_type, check_id, group_id, tag, visible_tags) -> None:
    """A scoped user may only place/alter windows that cannot silence monitors
    outside their scope."""
    if visible_tags is None:
        return
    if scope_type == "check":
        return  # visibility of the check itself is enforced by the caller
    if scope_type == "tag":
        if (tag or "").strip().lower() not in set(visible_tags):
            raise MaintenanceError(422, "Scoped users can place tag maintenance only on one of their scope tags")
        return
    if scope_type == "group":
        _any, every = await _group_visibility(db, {group_id}, visible_tags)
        if group_id not in every:
            raise MaintenanceError(422, "This group contains monitors outside your scope")
        return
    raise MaintenanceError(422, "Scoped users cannot place every monitor into maintenance")


# ── Lifecycle ──────────────────────────────────────────────────────────────

async def list_windows(db, visible_tags: list[str] | None = None) -> list[ServiceMaintenanceResponse]:
    rows = (await db.execute(
        select(ServiceCheckMaintenance).order_by(ServiceCheckMaintenance.starts_at.desc())
    )).scalars().all()
    return await responses(db, await filter_visible(db, rows, visible_tags))


async def create_window(db, data, user_id, visible_tags: list[str] | None = None,
                        now: datetime | None = None):
    """Validate and insert (not committed). Returns the ORM row."""
    now = now or utcnow()
    scope_type = data.scope_type
    kw: dict = {"scope_type": scope_type, "reason": data.reason, "created_by": user_id}
    if scope_type == "check":
        if not data.scope_check_id:
            raise MaintenanceError(422, "scope_check_id is required for a monitor window")
        sc = (await db.execute(select(ServiceCheck).where(ServiceCheck.id == data.scope_check_id))).scalar_one_or_none()
        if sc is None or not scoping.entity_visible(sc.tags, visible_tags):
            raise MaintenanceError(422, "Monitor not found")
        kw["scope_check_id"] = data.scope_check_id
    elif scope_type == "group":
        if not data.scope_group_id:
            raise MaintenanceError(422, "scope_group_id is required for a group window")
        group = (await db.execute(select(ServiceCheckGroup.id).where(ServiceCheckGroup.id == data.scope_group_id))).scalar_one_or_none()
        if group is None:
            raise MaintenanceError(422, "Group not found")
        kw["scope_group_id"] = data.scope_group_id
    elif scope_type == "tag":
        tag = (data.scope_tag or "").strip()
        if not tag:
            raise MaintenanceError(422, "scope_tag is required for a tag window")
        kw["scope_tag"] = tag
    await _assert_manageable(db, scope_type, kw.get("scope_check_id"), kw.get("scope_group_id"),
                             kw.get("scope_tag"), visible_tags)

    starts = _aware(data.starts_at) or now
    if data.ends_at is not None and data.duration_minutes is not None:
        raise MaintenanceError(422, "Give either ends_at or duration_minutes, not both")
    if data.duration_minutes is not None:
        ends = starts + timedelta(minutes=data.duration_minutes)
    elif data.ends_at is not None:
        ends = _aware(data.ends_at)
    else:
        raise MaintenanceError(422, "ends_at or duration_minutes is required")
    if ends <= starts:
        raise MaintenanceError(422, "ends_at must be after starts_at")
    if ends <= now:
        raise MaintenanceError(422, "The window would already be over; ends_at must be in the future")
    kw["starts_at"], kw["ends_at"] = starts, ends
    m = ServiceCheckMaintenance(**kw)
    db.add(m)
    await db.flush()
    return m


async def get_window_for_user(db, maintenance_id: UUID, visible_tags: list[str] | None):
    m = (await db.execute(
        select(ServiceCheckMaintenance).where(ServiceCheckMaintenance.id == maintenance_id)
    )).scalar_one_or_none()
    if m is None:
        raise MaintenanceError(404, "Maintenance window not found")
    if not await filter_visible(db, [m], visible_tags):
        raise MaintenanceError(404, "Maintenance window not found")
    return m


async def end_window(db, maintenance_id: UUID, user_id, reason: str | None,
                     visible_tags: list[str] | None = None, now: datetime | None = None):
    """Active -> ended now; not started -> cancelled; otherwise 409. Keeps the row."""
    now = now or utcnow()
    m = await get_window_for_user(db, maintenance_id, visible_tags)
    await _assert_manageable(db, m.scope_type, m.scope_check_id, m.scope_group_id, m.scope_tag, visible_tags)
    state = window_state(m, now)
    if state == "active":
        m.ended_at = now
        m.ended_by = user_id
        m.end_reason = reason
    elif state == "scheduled":
        m.cancelled_at = now
        m.ended_by = user_id
        m.end_reason = reason
    else:
        raise MaintenanceError(409, f"The window is already {state}")
    m.updated_at = now
    await db.flush()
    return m, state


async def patch_window(db, maintenance_id: UUID, data, visible_tags: list[str] | None = None,
                       now: datetime | None = None):
    now = now or utcnow()
    m = await get_window_for_user(db, maintenance_id, visible_tags)
    await _assert_manageable(db, m.scope_type, m.scope_check_id, m.scope_group_id, m.scope_tag, visible_tags)
    state = window_state(m, now)
    if state not in ("scheduled", "active"):
        raise MaintenanceError(409, f"A {state} window cannot be changed")
    changes = {}
    if data.ends_at is not None:
        ends = _aware(data.ends_at)
        if ends <= now:
            raise MaintenanceError(422, "ends_at must be in the future")
        if ends <= _aware(m.starts_at):
            raise MaintenanceError(422, "ends_at must be after starts_at")
        changes["ends_at"] = {"old": _aware(m.ends_at).isoformat(), "new": ends.isoformat()}
        m.ends_at = ends
    if "reason" in data.model_fields_set:
        changes["reason"] = {"old": m.reason, "new": data.reason}
        m.reason = data.reason
    m.updated_at = now
    await db.flush()
    return m, changes


async def cancel_window(db, maintenance_id: UUID, user_id, visible_tags: list[str] | None = None,
                        now: datetime | None = None):
    """DELETE semantics: only a window that has not started; the row is kept."""
    now = now or utcnow()
    m = await get_window_for_user(db, maintenance_id, visible_tags)
    await _assert_manageable(db, m.scope_type, m.scope_check_id, m.scope_group_id, m.scope_tag, visible_tags)
    state = window_state(m, now)
    if state == "cancelled":
        return m
    if state != "scheduled":
        raise MaintenanceError(409, "End the window instead; its history is kept for SLA reporting.")
    m.cancelled_at = now
    m.ended_by = user_id
    m.updated_at = now
    await db.flush()
    return m


async def windows_for_check(db, check, start: datetime, end: datetime, now: datetime | None = None) -> list[dict]:
    """Every window of any scope covering `check` and overlapping [start, end)
    — cancelled ones included for the record (state tells them apart)."""
    now = now or utcnow()
    M = ServiceCheckMaintenance
    rows = (await db.execute(
        select(M).where(M.starts_at < end).where(M.ends_at > start).order_by(M.starts_at)
    )).scalars().all()
    rows = [m for m in rows if covers(m, check)]
    checks, groups, users = await _names(db, rows)
    out = []
    for m in rows:
        state = window_state(m, now)
        out.append({
            "id": str(m.id),
            "scope_type": m.scope_type,
            "scope_label": scope_label(m, checks, groups),
            "starts_at": _aware(m.starts_at).isoformat(),
            "ends_at": _aware(m.ends_at).isoformat(),
            "effective_ends_at": effective_end(m).isoformat(),
            "ended_at": _aware(getattr(m, "ended_at", None)).isoformat() if getattr(m, "ended_at", None) else None,
            "cancelled_at": _aware(m.cancelled_at).isoformat() if getattr(m, "cancelled_at", None) else None,
            "state": state,
            "reason": m.reason,
            "end_reason": getattr(m, "end_reason", None),
            "active": state == "active",
            "inherited": m.scope_type != "check",
            "created_by_name": users.get(m.created_by),
        })
    return out
