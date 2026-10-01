"""Per-user tag visibility scoping.

Roles answer "what can you do"; users.scope_tags answers "what can you
see". A user with a non-empty scope sees only entities whose tags share at
least one name (case-insensitively) with that scope — devices, servers,
service checks, APM services, and the alerts attached to them. Untagged
entities are hidden from scoped users: fail-closed, so an unclassified
device never leaks into a team's filtered view.

Two rules keep this from ever locking the fleet away:
- an empty scope (the default for every pre-existing user) is unrestricted;
- a role carrying system.admin is never scoped, whatever the column says.

Every enforcement point goes through visible_tags() plus exactly one of the
predicate builders below, so the matching semantics (lowercased, ANY-of)
cannot drift between endpoints the way the earlier per-endpoint tag filters
did (@> in servers vs LOWER()= in devices). The builders emit SQL against
the two tag storage shapes in the schema: JSONB text arrays
(devices/servers/apm_services/...) and TEXT[] (service_checks/...).

Detail endpoints return 404 (not 403) for out-of-scope entities: a scoped
user should not be able to confirm an entity exists by probing ids.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SUPERUSER_PERMISSION
from app.core.security import get_role_permissions
from app.models.user import User
from app.services.tag_service import tag_set

# Bind the scope list under this name when using the predicate builders.
SCOPE_PARAM = "vis_tags"


async def visible_tags(db: AsyncSession, user: User) -> list[str] | None:
    """The user's visibility scope as lowercased tag names, or None if
    unrestricted (empty scope, or a role carrying system.admin)."""
    scope = sorted(tag_set(getattr(user, "scope_tags", None)))
    if not scope:
        return None
    perms = await get_role_permissions(db, user.role)
    if SUPERUSER_PERMISSION in perms:
        return None
    return scope


def jsonb_tags_visible(col: str, param: str = SCOPE_PARAM) -> str:
    """SQL predicate: JSONB text-array column shares a tag with :param."""
    return (
        f"EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        f"COALESCE({col}, '[]'::jsonb)) _vt "
        f"WHERE LOWER(_vt) = ANY(CAST(:{param} AS text[])))"
    )


def text_tags_visible(col: str, param: str = SCOPE_PARAM) -> str:
    """SQL predicate: TEXT[] column shares a tag with :param."""
    return (
        f"EXISTS (SELECT 1 FROM unnest(COALESCE({col}, ARRAY[]::text[])) _vt "
        f"WHERE LOWER(_vt) = ANY(CAST(:{param} AS text[])))"
    )


def alert_visible(alert_alias: str = "alerts", param: str = SCOPE_PARAM) -> str:
    """SQL predicate: the alert's entity (device, server, or service check)
    is in scope. Alerts tied to no entity are system-level and hidden from
    scoped users — they describe the appliance, not any team's slice."""
    a = alert_alias
    return (
        f"(({a}.device_id IS NOT NULL AND EXISTS ("
        f"SELECT 1 FROM devices _vd WHERE _vd.id = {a}.device_id"
        f" AND {jsonb_tags_visible('_vd.tags', param)}))"
        f" OR ({a}.server_id IS NOT NULL AND EXISTS ("
        f"SELECT 1 FROM servers _vs WHERE _vs.id = {a}.server_id"
        f" AND {jsonb_tags_visible('_vs.tags', param)}))"
        f" OR ({a}.service_check_id IS NOT NULL AND EXISTS ("
        f"SELECT 1 FROM service_checks _vc WHERE _vc.id = {a}.service_check_id"
        f" AND {text_tags_visible('_vc.tags', param)}))"
        f" OR ({a}.sensor_id IS NOT NULL AND EXISTS ("
        f"SELECT 1 FROM sensors _vsen WHERE _vsen.id = {a}.sensor_id"
        f" AND {jsonb_tags_visible('_vsen.tags', param)})))"
    )


def entity_visible(tags_value, scope: list[str] | None) -> bool:
    """Python-side check for rows already in hand (detail endpoints,
    ClickHouse-sourced lists). tags_value tolerates list/JSON-string/NULL."""
    return scope is None or bool(tag_set(tags_value) & set(scope))


async def alert_in_scope(db: AsyncSession, alert_id, scope: list[str] | None) -> bool:
    """Whether one alert's entity is visible to the scope. For detail
    endpoints, where the row is fetched by id without the list predicate."""
    if scope is None:
        return True
    row = (await db.execute(
        text(f"SELECT 1 FROM alerts WHERE id = :id AND {alert_visible('alerts')}"),
        {"id": alert_id, SCOPE_PARAM: scope},
    )).first()
    return row is not None


async def visible_server_ids(db: AsyncSession, scope: list[str]) -> set[str]:
    """Server ids in scope, for filtering metric maps keyed by server id."""
    rows = (await db.execute(
        text(f"SELECT id FROM servers s WHERE {jsonb_tags_visible('s.tags')}"),
        {SCOPE_PARAM: scope},
    )).scalars().all()
    return {str(r) for r in rows}


async def visible_apm_service_names(db: AsyncSession, scope: list[str]) -> set[str]:
    """APM service names in scope. apm_services rows are (name, env) pairs;
    a name is visible when any of its env rows carries a scope tag."""
    rows = (await db.execute(
        text(f"SELECT DISTINCT name FROM apm_services s WHERE {jsonb_tags_visible('s.tags')}"),
        {SCOPE_PARAM: scope},
    )).scalars().all()
    return set(rows)


# A UUID no row can carry. Filters treat an empty id list as "no filter", so a
# scope that matches nothing must be expressed as a list that matches nothing.
NO_MATCH_ID = "00000000-0000-0000-0000-000000000000"


async def visible_device_ids(db: AsyncSession, scope: list[str]) -> list[str]:
    """Device ids in scope (devices sharing at least one scope tag)."""
    rows = (await db.execute(
        text(f"SELECT id::text FROM devices d WHERE {jsonb_tags_visible('d.tags')}"),
        {SCOPE_PARAM: scope},
    )).scalars().all()
    return list(rows)


async def scoped_device_filter(
    db: AsyncSession, user: User, requested: list[str] | None
) -> tuple[bool, list[str] | None]:
    """Resolve a caller-supplied device filter against the user's scope.

    Returns ``(scoped, ids)``. For an unrestricted user ``ids`` is the request
    unchanged. For a scoped user it is the request narrowed to visible devices
    (or every visible device when none was requested); a client filter can
    never widen visibility, and an empty result is expressed as ``[NO_MATCH_ID]``.
    """
    scope = await visible_tags(db, user)
    if scope is None:
        return False, requested
    visible = set(await visible_device_ids(db, scope))
    ids = [i for i in (requested or sorted(visible)) if str(i) in visible]
    return True, ids or [NO_MATCH_ID]


async def visible_exporter_ips(db: AsyncSession, scope: list[str]) -> list[str]:
    """NetFlow exporter IPs that belong to devices in scope: a device's own
    management IP, or an exporter an operator explicitly mapped to such a
    device. An exporter that resolves to no visible device is not visible."""
    rows = (await db.execute(
        text(
            "SELECT DISTINCT ip FROM ("
            " SELECT host(CAST(d.ip_address AS inet)) AS ip FROM devices d"
            f" WHERE d.ip_address IS NOT NULL AND {jsonb_tags_visible('d.tags')}"
            " UNION"
            " SELECT host(m.exporter_ip) FROM netflow_exporter_devices m"
            f" JOIN devices d ON d.id = m.device_id WHERE {jsonb_tags_visible('d.tags')}"
            ") x WHERE ip IS NOT NULL"
        ),
        {SCOPE_PARAM: scope},
    )).scalars().all()
    return sorted(rows)
