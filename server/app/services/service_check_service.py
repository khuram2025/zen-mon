import math
from uuid import UUID
from urllib.parse import urlsplit
from sqlalchemy import or_, select, func, delete, distinct
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.services import tag_service

from datetime import datetime, timedelta, timezone

from app.models.service_check import (
    ServiceCheck,
    ServiceCredential,
    ServiceCheckGroup,
    ServiceCheckMaintenance,
    ServiceCheckTemplate,
)
from app.core.crypto import decrypt, encrypt
from app.schemas.service_check import (
    ServiceCheckCreate,
    ServiceCheckUpdate,
    ServiceCheckResponse,
    ServiceCheckSummary,
    ServiceCheckGroupCreate,
    ServiceCheckGroupUpdate,
    ServiceCheckGroupResponse,
    ServiceMaintenanceCreate,
    ServiceMaintenanceResponse,
    ServiceCheckTemplateCreate,
    ServiceCheckTemplateUpdate,
    ServiceCheckTemplateResponse,
    ServiceCheckTemplateApply,
    ServiceCheckTemplateApplyResult,
    ServiceCredentialCreate,
    ServiceCredentialUpdate,
    ServiceCredentialResponse,
    ServiceWorkflowStep,
    _validate_workflow_origin,
)


# Fields that do not change how the check probes: editing them must not
# restart an in-progress confirmation.
_OBJECTIVE_FIELDS = {"sla_target_pct", "criticality", "business_owner", "owner_team", "runbook_url", "description"}

TYPE_LABELS = {"http": "HTTP", "tcp": "TCP", "tls": "TLS", "icmp": "ICMP", "dns": "DNS"}


class ServiceCheckValidationError(ValueError):
    """A semantically invalid create/update; the API answers 422."""


def derive_level(check_type: str | None, workflow_steps) -> int:
    """L1 reachability (icmp/tcp), L2 single request/protocol check
    (http/tls/dns), L3 multi-step journey (http with >=1 workflow step)."""
    kind = (check_type or "").lower()
    if kind in ("icmp", "tcp"):
        return 1
    if kind == "http" and len(list(workflow_steps or [])) >= 1:
        return 3
    return 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stale_threshold_sec(status: str | None, check_interval, down_check_interval, timeout) -> float:
    """Evidence is stale beyond 3x the effective interval + timeout + 60 s."""
    interval = (down_check_interval or 30) if (status or "") == "down" else (check_interval or 60)
    return 3 * interval + (timeout or 10) + 60


def evidence_freshness(sc, now: datetime | None = None) -> tuple[bool, float | None]:
    """(stale, evidence_age_sec) against the appliance clock. Paused checks
    are never stale — they are paused."""
    now = now or _utcnow()
    last = getattr(sc, "last_check_at", None)
    age = None
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - last).total_seconds())
    if not getattr(sc, "enabled", True):
        return False, age
    threshold = stale_threshold_sec(getattr(sc, "status", None), getattr(sc, "check_interval", None),
                                    getattr(sc, "down_check_interval", None), getattr(sc, "timeout", None))
    return (age is None or age > threshold), age


def _stale_sql(now: datetime):
    """SQL twin of evidence_freshness() for list filters, counts and sorting."""
    from sqlalchemy import and_, case, literal
    interval = case(
        (ServiceCheck.status == "down", func.coalesce(ServiceCheck.down_check_interval, 30)),
        else_=func.coalesce(ServiceCheck.check_interval, 60),
    )
    threshold = 3 * interval + func.coalesce(ServiceCheck.timeout, 10) + 60
    return and_(
        ServiceCheck.enabled.is_(True),
        or_(
            ServiceCheck.last_check_at.is_(None),
            ServiceCheck.last_check_at < literal(now) - func.make_interval(0, 0, 0, 0, 0, 0, threshold),
        ),
    )


def _scope_where(query, visible_tags: list[str] | None):
    if visible_tags is None:
        return query
    return query.where(
        sql_text(scoping.text_tags_visible("service_checks.tags"))
        .bindparams(**{scoping.SCOPE_PARAM: visible_tags})
    )


def _to_response(sc: ServiceCheck, in_maintenance: bool = False, parent_name: str | None = None,
                 now: datetime | None = None) -> ServiceCheckResponse:
    stale, age = evidence_freshness(sc, now)
    return ServiceCheckResponse(
        id=sc.id,
        device_id=sc.device_id,
        device_hostname=sc.device.hostname if sc.device else None,
        group_id=sc.group_id,
        group_name=sc.group.name if getattr(sc, "group", None) else None,
        parent_check_id=sc.parent_check_id,
        parent_check_name=parent_name,
        name=sc.name,
        check_type=sc.check_type,
        level=sc.level or 1,
        config=sc.config or {},
        tags=list(sc.tags or []),
        retry_count=sc.retry_count or 1,
        retry_delay_s=sc.retry_delay_s or 10,
        down_check_interval=getattr(sc, "down_check_interval", None) or 30,
        verification_attempt=getattr(sc, "verification_attempt", None) or 0,
        credential_id=sc.credential_id,
        credential_name=sc.credential.name if getattr(sc, "credential", None) else None,
        credential_auth_type=sc.credential.auth_type if getattr(sc, "credential", None) else None,
        workflow_operator=sc.workflow_operator or "all",
        workflow_steps=list(sc.workflow_steps or []),
        in_maintenance=in_maintenance,
        enabled=sc.enabled,
        target_host=sc.target_host,
        target_port=sc.target_port,
        target_url=sc.target_url,
        http_method=sc.http_method or "GET",
        http_expected_status=sc.http_expected_status or 200,
        http_expected_statuses=sc.http_expected_statuses,
        http_content_match=sc.http_content_match,
        http_follow_redirects=sc.http_follow_redirects if sc.http_follow_redirects is not None else True,
        http_ignore_tls_errors=bool(sc.http_ignore_tls_errors),
        http_allow_insecure_auth=bool(sc.http_allow_insecure_auth),
        tls_warn_days=sc.tls_warn_days or 30,
        tls_critical_days=sc.tls_critical_days or 7,
        check_interval=sc.check_interval or 60,
        timeout=sc.timeout or 10,
        status=sc.status or "unknown",
        last_check_at=sc.last_check_at,
        last_response_ms=sc.last_response_ms,
        last_error=sc.last_error,
        tls_expiry_date=sc.tls_expiry_date,
        tls_days_remaining=sc.tls_days_remaining,
        tls_issuer=sc.tls_issuer,
        tls_subject=sc.tls_subject,
        description=sc.description,
        sla_target_pct=_finite(getattr(sc, "sla_target_pct", None)),
        criticality=getattr(sc, "criticality", None),
        business_owner=getattr(sc, "business_owner", None),
        owner_team=getattr(sc, "owner_team", None),
        runbook_url=getattr(sc, "runbook_url", None),
        stale=stale,
        evidence_age_sec=age,
        created_at=sc.created_at,
        updated_at=sc.updated_at,
    )


STATUS_SORT_KEYS = {"name", "-name", "status", "last_check", "type"}


def parse_status_set(status: str | None) -> list[str]:
    return [p.strip().lower() for p in (status or "").split(",") if p.strip()]


async def get_service_checks(
    db: AsyncSession,
    device_id: UUID | None = None,
    check_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    group_id: UUID | None = None,
    tag: str | None = None,
    level: int | None = None,
    skip: int = 0,
    limit: int = 50,
    visible_tags: list[str] | None = None,
    sort: str = "name",
    enabled: bool | None = None,
    stale: bool | None = None,
    in_maintenance: bool | None = None,
    has_credential: bool | None = None,
    sensor_id: UUID | None = None,
):
    from sqlalchemy import case, not_

    now = _utcnow()
    query = _scope_where(select(ServiceCheck), visible_tags)
    if device_id:
        query = query.where(ServiceCheck.device_id == device_id)
    if check_type:
        query = query.where(ServiceCheck.check_type == check_type)
    statuses = parse_status_set(status)
    if statuses:
        cond = ServiceCheck.status.in_(statuses)
        if "unknown" in statuses:
            cond = or_(cond, ServiceCheck.status.is_(None))
        query = query.where(cond)
    if group_id:
        query = query.where(ServiceCheck.group_id == group_id)
    if tag:
        query = query.where(ServiceCheck.tags.any(tag))
    if level:
        query = query.where(ServiceCheck.level == level)
    if enabled is not None:
        query = query.where(ServiceCheck.enabled.is_(enabled))
    if has_credential is not None:
        query = query.where(ServiceCheck.credential_id.isnot(None) if has_credential
                            else ServiceCheck.credential_id.is_(None))
    stale_expr = _stale_sql(now)
    if stale is not None:
        query = query.where(stale_expr if stale else not_(stale_expr))
    if sensor_id:
        query = query.where(
            sql_text("service_checks.id IN (SELECT target_id FROM sensor_assignments "
                     "WHERE target_type = 'service_check' AND sensor_id = :sa_sensor_id)")
            .bindparams(sa_sensor_id=sensor_id)
        )
    maint_ids = await active_maintenance_check_ids(db)
    if in_maintenance is not None:
        if in_maintenance:
            query = query.where(ServiceCheck.id.in_(list(maint_ids) or [None]))
        elif maint_ids:
            query = query.where(ServiceCheck.id.notin_(list(maint_ids)))
    if search:
        query = query.where(
            ServiceCheck.name.ilike(f"%{search}%") |
            ServiceCheck.target_host.ilike(f"%{search}%") |
            ServiceCheck.target_url.ilike(f"%{search}%")
        )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Sort on the server before paging so triage order spans pages (B21).
    if sort == "status":
        rank = case(
            (ServiceCheck.enabled.is_(False), 4),
            (ServiceCheck.status == "down", 0),
            (ServiceCheck.status.in_(("warning", "degraded")), 1),
            (or_(ServiceCheck.status.is_(None), ServiceCheck.status == "unknown", stale_expr), 2),
            else_=3,
        )
        order = [rank, ServiceCheck.name, ServiceCheck.id]
    elif sort == "-name":
        order = [ServiceCheck.name.desc(), ServiceCheck.id]
    elif sort == "last_check":
        order = [ServiceCheck.last_check_at.asc().nulls_first(), ServiceCheck.name, ServiceCheck.id]
    elif sort == "type":
        order = [ServiceCheck.check_type, ServiceCheck.name, ServiceCheck.id]
    else:
        order = [ServiceCheck.name, ServiceCheck.id]
    query = query.order_by(*order).offset(skip).limit(limit)
    result = await db.execute(query)
    checks = list(result.scalars().all())

    # Enrich: in_maintenance + parent name
    parent_ids = {sc.parent_check_id for sc in checks if sc.parent_check_id}
    parent_names: dict = {}
    if parent_ids:
        rows = (await db.execute(
            select(ServiceCheck.id, ServiceCheck.name).where(ServiceCheck.id.in_(list(parent_ids)))
        )).all()
        parent_names = {r[0]: r[1] for r in rows}

    return {
        "data": [
            _to_response(
                sc,
                in_maintenance=sc.id in maint_ids,
                parent_name=parent_names.get(sc.parent_check_id),
                now=now,
            )
            for sc in checks
        ],
        "meta": {"total": total, "skip": skip, "limit": limit, "sort": sort,
                 "server_time": now.isoformat()},
    }


async def get_visible_check(db: AsyncSession, check_id: UUID, visible_tags: list[str] | None) -> ServiceCheck | None:
    """The ORM row when it exists AND is inside the caller's scope, else None.

    Every per-check route goes through this so an out-of-scope id is
    indistinguishable from a missing one (404)."""
    sc = (await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))).scalar_one_or_none()
    if sc is None or not scoping.entity_visible(sc.tags, visible_tags):
        return None
    return sc


async def get_visible_checks(db: AsyncSession, check_ids, visible_tags: list[str] | None) -> list[ServiceCheck] | None:
    """All rows for `check_ids`, or None when ANY id is missing/out of scope."""
    wanted = list(dict.fromkeys(check_ids))
    if not wanted:
        return []
    rows = list((await db.execute(
        _scope_where(select(ServiceCheck).where(ServiceCheck.id.in_(wanted)), visible_tags)
    )).scalars().all())
    by_id = {r.id: r for r in rows if scoping.entity_visible(r.tags, visible_tags)}
    if len(by_id) != len(wanted):
        return None
    return [by_id[i] for i in wanted]


async def get_service_check(db: AsyncSession, check_id: UUID, visible_tags: list[str] | None = None):
    sc = await get_visible_check(db, check_id, visible_tags)
    if not sc:
        return None
    maint_ids = await active_maintenance_check_ids(db)
    parent_name = None
    if sc.parent_check_id:
        pr = (await db.execute(
            select(ServiceCheck.name).where(ServiceCheck.id == sc.parent_check_id)
        )).scalar_one_or_none()
        parent_name = pr
    return _to_response(sc, in_maintenance=sc.id in maint_ids, parent_name=parent_name)


async def get_related_service_checks(db: AsyncSession, check_id: UUID, limit: int = 16,
                                     visible_tags: list[str] | None = None):
    """Find checks related to this one: parent, children, same device, host, or group.

    Every sub-list is filtered to the caller's scope (B02)."""
    sc = await get_visible_check(db, check_id, visible_tags)
    if not sc:
        return None

    seen: set[UUID] = {sc.id}
    maint_ids = await active_maintenance_check_ids(db)

    def pack(items: list[ServiceCheck]):
        return [_to_response(c, in_maintenance=c.id in maint_ids) for c in items]

    def scoped(q):
        return _scope_where(q, visible_tags)

    parent = None
    if sc.parent_check_id:
        parent = (await db.execute(
            scoped(select(ServiceCheck).where(ServiceCheck.id == sc.parent_check_id))
        )).scalar_one_or_none()
        if parent:
            seen.add(parent.id)

    children = list((await db.execute(
        scoped(select(ServiceCheck)
        .where(ServiceCheck.parent_check_id == sc.id))
        .order_by(ServiceCheck.name)
        .limit(limit)
    )).scalars().all())
    seen.update(c.id for c in children)

    same_device: list[ServiceCheck] = []
    if sc.device_id:
        same_device = list((await db.execute(
            scoped(select(ServiceCheck)
            .where(ServiceCheck.device_id == sc.device_id, ServiceCheck.id.notin_(seen)))
            .order_by(ServiceCheck.name)
            .limit(limit)
        )).scalars().all())
        seen.update(c.id for c in same_device)

    host = (sc.target_host or "").strip().lower()
    if not host and sc.target_url:
        host = (urlsplit(sc.target_url).hostname or "").lower()

    same_host: list[ServiceCheck] = []
    if host:
        same_host = list((await db.execute(
            scoped(select(ServiceCheck)
            .where(
                ServiceCheck.id.notin_(seen),
                or_(
                    func.lower(ServiceCheck.target_host) == host,
                    ServiceCheck.target_url.ilike(f"%://{host}/%"),
                    ServiceCheck.target_url.ilike(f"%://{host}:%"),
                    ServiceCheck.target_url.ilike(f"%://{host}"),
                ),
            ))
            .order_by(ServiceCheck.name)
            .limit(limit)
        )).scalars().all())
        seen.update(c.id for c in same_host)

    same_group: list[ServiceCheck] = []
    if sc.group_id:
        same_group = list((await db.execute(
            scoped(select(ServiceCheck)
            .where(ServiceCheck.group_id == sc.group_id, ServiceCheck.id.notin_(seen)))
            .order_by(ServiceCheck.name)
            .limit(limit)
        )).scalars().all())

    # Belt and braces: never emit a row the scope predicate should have removed.
    def visible(items):
        return [c for c in items if c is not None and scoping.entity_visible(c.tags, visible_tags)]

    parent = parent if parent is not None and scoping.entity_visible(parent.tags, visible_tags) else None
    return {
        "parent": pack([parent])[0] if parent else None,
        "children": pack(visible(children)),
        "same_device": pack(visible(same_device)),
        "same_host": pack(visible(same_host)),
        "same_group": pack(visible(same_group)),
    }


async def get_runtime_service_check(db: AsyncSession, check_id: UUID):
    """Return the ORM check and an in-memory credential for probe execution.

    The decrypted secret is never attached to the ORM model or a response
    schema, which keeps accidental serialization and audit logging out of the
    secret path.
    """
    sc = await db.get(ServiceCheck, check_id)
    if not sc:
        return None, None
    return sc, await get_runtime_service_credential(db, sc.credential_id)


async def get_runtime_service_credential(db: AsyncSession, credential_id: UUID | None):
    """Resolve a credential for a probe without attaching its secret to a model."""
    if not credential_id:
        return None
    credential = await db.get(ServiceCredential, credential_id)
    if not credential:
        raise ValueError("Linked service credential no longer exists")
    return {
        "auth_type": credential.auth_type,
        "username": credential.username or "",
        "secret": decrypt(credential.secret_cipher) or "",
    }


_SENSITIVE_HEADER_HINTS = ("authorization", "cookie", "token", "key", "secret", "password")
REDACTED = "__REDACTED__"


def _sensitive_header(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _SENSITIVE_HEADER_HINTS)


def redact_headers(headers) -> dict:
    return {k: (REDACTED if _sensitive_header(k) else v) for k, v in dict(headers or {}).items()}


def redact_steps(steps) -> list:
    out = []
    for step in list(steps or []):
        d = step.model_dump() if hasattr(step, "model_dump") else dict(step or {})
        d["headers"] = redact_headers(d.get("headers"))
        if d.get("body"):
            d["body"] = REDACTED
        out.append(d)
    return out


def _audit_value(field: str, value):
    """JSON-safe, secret-free rendering of one field for audit/history."""
    if field == "http_headers":
        return redact_headers(value)
    if field == "http_body":
        return REDACTED if value else value
    if field == "workflow_steps":
        return redact_steps(value)
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, list):
        return [str(v) if isinstance(v, UUID) else v for v in value]
    return value


def diff_changes(before: dict, after: dict) -> dict:
    """{field: {old, new}} for fields whose value changed (secrets redacted)."""
    out = {}
    for key, new in after.items():
        old = before.get(key)
        if _audit_value(key, old) != _audit_value(key, new):
            entry = {"old": _audit_value(key, old), "new": _audit_value(key, new)}
            if key == "http_body":
                entry = {"old": REDACTED if old else None, "new": REDACTED if new else None, "changed": True}
            out[key] = entry
    return out


def _check_scope_tags(tags, visible_tags: list[str] | None) -> None:
    if visible_tags is not None and not scoping.entity_visible(tags, visible_tags):
        raise ServiceCheckValidationError("tags must include one of your scope tags")


async def _validate_parent(db: AsyncSession, check_id: UUID | None, check_name: str,
                           parent_id: UUID | None, visible_tags: list[str] | None) -> None:
    """Parent must exist, be visible, and must not create a dependency cycle."""
    if not parent_id:
        return
    if check_id is not None and parent_id == check_id:
        raise ServiceCheckValidationError(f"Dependency cycle: {check_name} → {check_name}")
    parent = (await db.execute(
        select(ServiceCheck.id, ServiceCheck.name, ServiceCheck.parent_check_id, ServiceCheck.tags)
        .where(ServiceCheck.id == parent_id)
    )).first()
    if parent is None or not scoping.entity_visible(parent[3], visible_tags):
        raise ServiceCheckValidationError("Parent check not found")
    if check_id is None:
        return
    path = [check_name, parent[1]]
    seen = {parent_id}
    cursor = parent[2]
    while cursor:
        if cursor == check_id:
            path.append(check_name)
            raise ServiceCheckValidationError("Dependency cycle: " + " → ".join(path))
        if cursor in seen:
            break  # a pre-existing cycle upstream that does not involve this check
        seen.add(cursor)
        row = (await db.execute(
            select(ServiceCheck.id, ServiceCheck.name, ServiceCheck.parent_check_id)
            .where(ServiceCheck.id == cursor)
        )).first()
        if row is None:
            break
        path.append(row[1])
        cursor = row[2]


async def create_service_check(db: AsyncSession, data: ServiceCheckCreate, user_id: UUID,
                               visible_tags: list[str] | None = None, commit: bool = True):
    credential = None
    if data.credential_id:
        credential = await db.get(ServiceCredential, data.credential_id)
        if not credential:
            raise ValueError("Service credential not found")
    _validate_credential_workflow(credential, data.workflow_steps)
    _validate_credential_transport(
        credential,
        data.target_url,
        data.workflow_steps,
        data.http_allow_insecure_auth,
    )
    _check_scope_tags(data.tags, visible_tags)
    await _validate_parent(db, None, data.name, data.parent_check_id, visible_tags)
    payload = data.model_dump()
    # The server owns the level: the client value is ignored (journey level).
    payload["level"] = derive_level(data.check_type, data.workflow_steps)
    # Registry spellings only; unknown labels auto-register — the same
    # contract devices and servers already follow.
    payload["tags"] = await tag_service.canonicalize_tags(db, payload.get("tags"))
    sc = ServiceCheck(
        **payload,
        created_by=user_id,
    )
    if credential:
        sc.credential = credential
    db.add(sc)
    if not commit:
        await db.flush()
        return sc
    await db.commit()
    await db.refresh(sc)
    return _to_response(sc)


async def update_service_check(db: AsyncSession, check_id: UUID, data: ServiceCheckUpdate,
                               visible_tags: list[str] | None = None, changes_out: dict | None = None):
    result = await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))
    sc = result.scalar_one_or_none()
    if not sc or not scoping.entity_visible(sc.tags, visible_tags):
        return None

    update_data = data.model_dump(exclude_unset=True)
    requested_type = update_data.pop("check_type", None)
    if requested_type is not None and str(requested_type).lower() != (sc.check_type or "").lower():
        label = TYPE_LABELS.get(str(requested_type).lower(), str(requested_type).upper())
        raise ServiceCheckValidationError(
            f"Monitor type cannot be changed. Duplicate the monitor to create a {label} check.")
    # The server derives level; a client value is ignored.
    update_data.pop("level", None)
    effective_credential_id = update_data.get("credential_id", sc.credential_id)
    credential = None
    if effective_credential_id:
        credential = await db.get(ServiceCredential, effective_credential_id)
        if not credential:
            raise ValueError("Service credential not found")
    effective_steps = [
        step if isinstance(step, ServiceWorkflowStep) else ServiceWorkflowStep.model_validate(step)
        for step in update_data.get("workflow_steps", sc.workflow_steps or [])
    ]
    effective_type = sc.check_type
    if effective_steps and effective_type != "http":
        raise ValueError("Multi-step workflows are supported only for HTTP checks")
    _validate_workflow_origin(
        update_data.get("target_url", sc.target_url),
        effective_steps,
        effective_credential_id,
    )
    _validate_credential_workflow(credential, effective_steps)
    _validate_credential_transport(
        credential,
        update_data.get("target_url", sc.target_url),
        effective_steps,
        bool(update_data.get("http_allow_insecure_auth", sc.http_allow_insecure_auth)),
    )
    if "tags" in update_data:
        _check_scope_tags(update_data["tags"], visible_tags)
        update_data["tags"] = await tag_service.canonicalize_tags(db, update_data["tags"])
    if "parent_check_id" in update_data:
        await _validate_parent(db, sc.id, update_data.get("name") or sc.name,
                               update_data["parent_check_id"], visible_tags)
    new_level = derive_level(effective_type, effective_steps)
    if (sc.level or None) != new_level:
        update_data["level"] = new_level
    before = {key: getattr(sc, key, None) for key in update_data}
    for key, value in update_data.items():
        setattr(sc, key, value)
    if changes_out is not None:
        changes_out.update(diff_changes(before, update_data))
    # Business metadata does not change how the check probes, so it must not
    # restart an in-progress confirmation.
    if set(update_data) - _OBJECTIVE_FIELDS - {"level"}:
        sc.verification_attempt = 0
    if "credential_id" in update_data:
        sc.credential = credential

    await db.commit()
    await db.refresh(sc)
    return _to_response(sc)


def _credential_to_response(credential: ServiceCredential, used_by: int = 0) -> ServiceCredentialResponse:
    return ServiceCredentialResponse(
        id=credential.id,
        name=credential.name,
        auth_type=credential.auth_type,
        username=credential.username,
        description=credential.description,
        has_secret=bool(credential.secret_cipher),
        used_by=used_by,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def _validate_credential_workflow(credential: ServiceCredential | None, steps: list) -> None:
    if not credential or credential.auth_type != "form":
        return
    if len(steps) < 2:
        raise ValueError("Form authentication requires a sign-in step followed by a protected-page navigation step")
    first = steps[0]
    body = first.body if isinstance(first, ServiceWorkflowStep) else (first.get("body") or "")
    headers = first.headers if isinstance(first, ServiceWorkflowStep) else (first.get("headers") or {})
    template = body + "\n" + "\n".join(str(value) for value in headers.values())
    if "{{username}}" not in template or "{{password}}" not in template:
        raise ValueError("The form login step must inject {{username}} and {{password}}")


def _validate_credential_transport(
    credential: ServiceCredential | None,
    target_url: str | None,
    steps: list,
    allow_insecure_http_auth: bool = False,
) -> None:
    """Keep reusable secrets off clear-text HTTP except NTLM challenge-response.

    NTLM is allowed for explicitly selected trusted intranet targets because the
    password itself is not transmitted. HTTPS is still preferred because plain
    HTTP remains exposed to tampering and credential-relay attacks.
    """
    if not credential:
        return
    first_url = (steps[0].url if isinstance(steps[0], ServiceWorkflowStep) else steps[0].get("url")) if steps else target_url
    scheme = urlsplit(first_url or "").scheme.lower()
    if scheme == "https" or credential.auth_type == "ntlm" or allow_insecure_http_auth:
        return
    raise ValueError("Basic, bearer, and form credentials require HTTPS unless trusted HTTP credential transmission is explicitly enabled")


async def list_service_credentials(db: AsyncSession) -> list[ServiceCredentialResponse]:
    rows = (await db.execute(
        select(ServiceCredential, func.count(ServiceCheck.id))
        .outerjoin(ServiceCheck, ServiceCheck.credential_id == ServiceCredential.id)
        .group_by(ServiceCredential.id)
        .order_by(ServiceCredential.name)
    )).all()
    return [_credential_to_response(credential, int(used_by or 0)) for credential, used_by in rows]


async def create_service_credential(
    db: AsyncSession, data: ServiceCredentialCreate, user_id: UUID
) -> ServiceCredentialResponse:
    credential = ServiceCredential(
        name=data.name.strip(),
        auth_type=data.auth_type,
        username=(data.username or "").strip() or None,
        secret_cipher=encrypt(data.secret),
        description=data.description,
        created_by=user_id,
    )
    db.add(credential)
    await db.commit()
    await db.refresh(credential)
    return _credential_to_response(credential)


async def update_service_credential(
    db: AsyncSession, credential_id: UUID, data: ServiceCredentialUpdate
) -> ServiceCredentialResponse | None:
    credential = await db.get(ServiceCredential, credential_id)
    if not credential:
        return None
    changes = data.model_dump(exclude_unset=True)
    secret = changes.pop("secret", None)
    for key, value in changes.items():
        if key in {"name", "username"} and isinstance(value, str):
            value = value.strip() or None
        setattr(credential, key, value)
    if secret:
        credential.secret_cipher = encrypt(secret)
    if credential.auth_type in {"basic", "form", "ntlm"} and not credential.username:
        raise ValueError("Username is required for Basic, form, and Windows Integrated authentication")
    await db.commit()
    await db.refresh(credential)
    used_by = (await db.execute(
        select(func.count()).select_from(ServiceCheck).where(ServiceCheck.credential_id == credential_id)
    )).scalar() or 0
    return _credential_to_response(credential, int(used_by))


async def delete_service_credential(db: AsyncSession, credential_id: UUID) -> bool:
    result = await db.execute(delete(ServiceCredential).where(ServiceCredential.id == credential_id))
    await db.commit()
    return bool(result.rowcount)


async def delete_service_check(db: AsyncSession, check_id: UUID) -> bool:
    result = await db.execute(delete(ServiceCheck).where(ServiceCheck.id == check_id))
    await db.commit()
    return result.rowcount > 0


async def get_service_check_summary(
    db: AsyncSession, visible_tags: list[str] | None = None,
) -> ServiceCheckSummary:
    now = _utcnow()
    query = _scope_where(
        select(ServiceCheck.status, func.count(ServiceCheck.id)).group_by(ServiceCheck.status),
        visible_tags,
    )
    result = await db.execute(query)
    counts = {row[0]: row[1] for row in result.all()}
    total = sum(counts.values())
    paused = stale = in_maint = 0
    try:
        from sqlalchemy import case
        extra = (await db.execute(_scope_where(select(
            func.count(case((ServiceCheck.enabled.is_(False), 1))),
            func.count(case((_stale_sql(now), 1))),
        ), visible_tags))).first()
        if extra:
            paused, stale = int(extra[0] or 0), int(extra[1] or 0)
        maint_ids = await active_maintenance_check_ids(db)
        if maint_ids:
            in_maint = (await db.execute(_scope_where(
                select(func.count(ServiceCheck.id)).where(ServiceCheck.id.in_(list(maint_ids))),
                visible_tags,
            ))).scalar() or 0
    except Exception:
        import logging
        logging.getLogger(__name__).exception("service summary freshness counts failed")
    return ServiceCheckSummary(
        total=total,
        up=counts.get("up", 0),
        down=counts.get("down", 0),
        warning=counts.get("warning", 0),
        degraded=counts.get("degraded", 0),
        unknown=counts.get("unknown", 0) + counts.get(None, 0),
        paused=paused,
        stale=stale,
        in_maintenance=int(in_maint),
        server_time=now,
    )


async def get_device_service_checks(db: AsyncSession, device_id: UUID):
    result = await db.execute(
        select(ServiceCheck)
        .where(ServiceCheck.device_id == device_id)
        .order_by(ServiceCheck.name)
    )
    checks = result.scalars().all()
    return [_to_response(sc) for sc in checks]


async def bulk_delete_service_checks(db: AsyncSession, check_ids: list[UUID],
                                     visible_tags: list[str] | None = None) -> int | None:
    """Delete every id or nothing: None when any id is missing/out of scope."""
    if not check_ids:
        return 0
    rows = await get_visible_checks(db, check_ids, visible_tags)
    if rows is None:
        return None
    result = await db.execute(
        delete(ServiceCheck).where(ServiceCheck.id.in_([r.id for r in rows]))
    )
    await db.commit()
    return result.rowcount


def _export_row(sc: ServiceCheck) -> dict:
    """Legacy summary export row (shape kept; behaviour fields added)."""
    return {
        "name": sc.name,
        "check_type": sc.check_type,
        "enabled": sc.enabled,
        "target_host": sc.target_host,
        "target_port": sc.target_port,
        "target_url": sc.target_url,
        "http_method": sc.http_method,
        "http_expected_status": sc.http_expected_status,
        "http_expected_statuses": sc.http_expected_statuses,
        "http_content_match": sc.http_content_match,
        "http_follow_redirects": sc.http_follow_redirects,
        "http_ignore_tls_errors": bool(sc.http_ignore_tls_errors),
        "http_allow_insecure_auth": bool(sc.http_allow_insecure_auth),
        "credential_name": sc.credential.name if getattr(sc, "credential", None) else None,
        "credential_auth_type": sc.credential.auth_type if getattr(sc, "credential", None) else None,
        "workflow_operator": sc.workflow_operator or "all",
        "workflow_steps": redact_steps(sc.workflow_steps),
        "tls_warn_days": sc.tls_warn_days,
        "tls_critical_days": sc.tls_critical_days,
        "check_interval": sc.check_interval,
        "timeout": sc.timeout,
        "status": sc.status,
        "description": sc.description,
        "sla_target_pct": _finite(sc.sla_target_pct),
        "criticality": sc.criticality,
        "business_owner": sc.business_owner,
        # Behaviour and relationships (B27), additive.
        "level": sc.level,
        "tags": list(sc.tags or []),
        "group_name": sc.group.name if getattr(sc, "group", None) else None,
        "retry_count": sc.retry_count,
        "retry_delay_s": sc.retry_delay_s,
        "down_check_interval": sc.down_check_interval,
        "config": dict(sc.config or {}),
        "owner_team": getattr(sc, "owner_team", None),
        "runbook_url": getattr(sc, "runbook_url", None),
    }


async def _export_checks(db: AsyncSession, visible_tags: list[str] | None) -> tuple[list, dict]:
    result = await db.execute(_scope_where(select(ServiceCheck), visible_tags).order_by(ServiceCheck.name))
    checks = [sc for sc in result.scalars().all() if scoping.entity_visible(sc.tags, visible_tags)]
    names = {sc.id: sc.name for sc in checks}
    missing_parents = {sc.parent_check_id for sc in checks if sc.parent_check_id and sc.parent_check_id not in names}
    if missing_parents:
        rows = (await db.execute(
            _scope_where(select(ServiceCheck.id, ServiceCheck.name).where(ServiceCheck.id.in_(list(missing_parents))),
                         visible_tags))).all()
        names.update({r[0]: r[1] for r in rows})
    return checks, names


async def export_service_checks(db: AsyncSession, visible_tags: list[str] | None = None) -> list[dict]:
    checks, names = await _export_checks(db, visible_tags)
    export = []
    for sc in checks:
        row = _export_row(sc)
        row["parent_check_name"] = names.get(sc.parent_check_id) if sc.parent_check_id else None
        export.append(row)
    return export


PORTABLE_FIELDS = (
    "name", "check_type", "level", "enabled", "description", "tags", "group_name", "parent_check_name",
    "device_hostname", "target_host", "target_port", "target_url", "http_method", "http_headers",
    "http_body", "http_expected_status", "http_expected_statuses", "http_content_match",
    "http_follow_redirects", "http_ignore_tls_errors", "http_allow_insecure_auth", "config",
    "retry_count", "retry_delay_s", "down_check_interval", "check_interval", "timeout",
    "tls_warn_days", "tls_critical_days", "workflow_operator", "workflow_steps", "credential_name",
    "sla_target_pct", "criticality", "business_owner", "owner_team", "runbook_url",
)


async def export_portable(db: AsyncSession, visible_tags: list[str] | None = None) -> dict:
    """Versioned, behaviour-preserving definition export (schema_version 2).

    Relationships are by name (group, parent, credential, device). No secret
    ever leaves: credential is a name reference only, sensitive header values
    and request bodies are replaced by "__REDACTED__"."""
    checks_orm, names = await _export_checks(db, visible_tags)
    now = _utcnow()
    checks = []
    for sc in checks_orm:
        full = _export_row(sc)
        full["parent_check_name"] = names.get(sc.parent_check_id) if sc.parent_check_id else None
        full["device_hostname"] = sc.device.hostname if getattr(sc, "device", None) else None
        full["http_headers"] = redact_headers(sc.http_headers)
        full["http_body"] = REDACTED if sc.http_body else None
        full["workflow_steps"] = redact_steps(sc.workflow_steps)
        checks.append({k: full.get(k) for k in PORTABLE_FIELDS})
    return {"schema_version": 2, "exported_at": now.isoformat(), "server_time": now.isoformat(),
            "checks": checks}


# ── Groups ────────────────────────────────────────────────────────────────

def _group_to_response(g: ServiceCheckGroup, check_count: int = 0) -> ServiceCheckGroupResponse:
    return ServiceCheckGroupResponse(
        id=g.id,
        name=g.name,
        description=g.description,
        color=g.color,
        check_count=check_count,
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


async def list_groups(db: AsyncSession, visible_tags: list[str] | None = None) -> list[ServiceCheckGroupResponse]:
    groups = (await db.execute(select(ServiceCheckGroup).order_by(ServiceCheckGroup.name))).scalars().all()
    if not groups:
        return []
    counts_rows = (await db.execute(_scope_where(
        select(ServiceCheck.group_id, func.count())
        .where(ServiceCheck.group_id.in_([g.id for g in groups]))
        .group_by(ServiceCheck.group_id), visible_tags)
    )).all()
    count_map = {row[0]: row[1] for row in counts_rows}
    return [_group_to_response(g, count_map.get(g.id, 0)) for g in groups]


async def create_group(db: AsyncSession, data: ServiceCheckGroupCreate) -> ServiceCheckGroupResponse:
    g = ServiceCheckGroup(**data.model_dump())
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return _group_to_response(g, 0)


async def update_group(db: AsyncSession, group_id: UUID, data: ServiceCheckGroupUpdate) -> ServiceCheckGroupResponse | None:
    g = (await db.execute(select(ServiceCheckGroup).where(ServiceCheckGroup.id == group_id))).scalar_one_or_none()
    if not g:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(g, k, v)
    await db.commit()
    await db.refresh(g)
    count = (await db.execute(
        select(func.count()).select_from(ServiceCheck).where(ServiceCheck.group_id == group_id)
    )).scalar() or 0
    return _group_to_response(g, count)


async def delete_group(db: AsyncSession, group_id: UUID) -> bool:
    res = await db.execute(delete(ServiceCheckGroup).where(ServiceCheckGroup.id == group_id))
    await db.commit()
    return (res.rowcount or 0) > 0


async def list_tags(db: AsyncSession, visible_tags: list[str] | None = None) -> list[str]:
    # Fetch distinct tag values across the visible service_checks by unnesting the array column.
    from sqlalchemy import text
    if visible_tags is None:
        rows = (await db.execute(text(
            "SELECT DISTINCT unnest(tags) AS tag FROM service_checks WHERE array_length(tags,1) > 0 ORDER BY tag"
        ))).all()
    else:
        rows = (await db.execute(text(
            "SELECT DISTINCT unnest(tags) AS tag FROM service_checks "
            f"WHERE array_length(tags,1) > 0 AND {scoping.text_tags_visible('service_checks.tags')} ORDER BY tag"
        ), {scoping.SCOPE_PARAM: visible_tags})).all()
    return [r[0] for r in rows]


# ── Maintenance windows ───────────────────────────────────────────────────
# Lifecycle and SLA exclusion live in app.services.service_maintenance; these
# wrappers keep the historical entry points working for other callers.

async def list_maintenance(db: AsyncSession, visible_tags: list[str] | None = None) -> list[ServiceMaintenanceResponse]:
    from app.services import service_maintenance
    return await service_maintenance.list_windows(db, visible_tags)


async def active_maintenance_check_ids(db: AsyncSession) -> set[UUID]:
    """Service_check IDs covered right now by a non-cancelled window whose
    effective end (LEAST(ends_at, ended_at)) has not passed."""
    from app.services import service_maintenance
    return await service_maintenance.active_check_ids(db)


# ── Templates ─────────────────────────────────────────────────────────────

def _template_to_response(t: ServiceCheckTemplate) -> ServiceCheckTemplateResponse:
    return ServiceCheckTemplateResponse.model_validate(t)


async def list_templates(db: AsyncSession) -> list[ServiceCheckTemplateResponse]:
    rows = (await db.execute(
        select(ServiceCheckTemplate).order_by(ServiceCheckTemplate.name)
    )).scalars().all()
    return [_template_to_response(t) for t in rows]


async def get_template(db: AsyncSession, template_id: UUID) -> ServiceCheckTemplateResponse | None:
    t = (await db.execute(
        select(ServiceCheckTemplate).where(ServiceCheckTemplate.id == template_id)
    )).scalar_one_or_none()
    return _template_to_response(t) if t else None


async def create_template(db: AsyncSession, data: ServiceCheckTemplateCreate, user_id: UUID | None) -> ServiceCheckTemplateResponse:
    t = ServiceCheckTemplate(**data.model_dump(), created_by=user_id)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _template_to_response(t)


async def update_template(db: AsyncSession, template_id: UUID, data: ServiceCheckTemplateUpdate) -> ServiceCheckTemplateResponse | None:
    t = (await db.execute(
        select(ServiceCheckTemplate).where(ServiceCheckTemplate.id == template_id)
    )).scalar_one_or_none()
    if not t:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    await db.commit()
    await db.refresh(t)
    return _template_to_response(t)


async def delete_template(db: AsyncSession, template_id: UUID) -> bool:
    res = await db.execute(delete(ServiceCheckTemplate).where(ServiceCheckTemplate.id == template_id))
    await db.commit()
    return (res.rowcount or 0) > 0


async def apply_template(
    db: AsyncSession,
    template_id: UUID,
    data: ServiceCheckTemplateApply,
    user_id: UUID | None,
    visible_tags: list[str] | None = None,
) -> ServiceCheckTemplateApplyResult:
    """Create one service_check per target device from a template.

    Substitutes `{{hostname}}` and `{{ip}}` in target_url_template. Skips
    devices that already have a check with the generated name. Runs in a
    single transaction so failures roll back.
    """
    from sqlalchemy import text

    t = (await db.execute(
        select(ServiceCheckTemplate).where(ServiceCheckTemplate.id == template_id)
    )).scalar_one_or_none()
    if not t:
        raise ValueError("Template not found")
    # A scoped user must not create monitors that would be invisible to them.
    _check_scope_tags(list(t.tags or []), visible_tags)

    # Resolve target device ids (explicit list + optional group_id)
    device_ids = list(data.device_ids or [])
    if data.group_id:
        rows = (await db.execute(text(
            "SELECT id FROM devices WHERE group_id = :gid"
        ), {"gid": data.group_id})).all()
        device_ids.extend([r[0] for r in rows])
    # Deduplicate while preserving order
    seen: set = set()
    device_ids = [d for d in device_ids if not (d in seen or seen.add(d))]

    if not device_ids:
        return ServiceCheckTemplateApplyResult(created_ids=[], skipped=[])

    # Load device metadata for substitution
    if visible_tags is None:
        dev_rows = (await db.execute(text(
            "SELECT id, hostname, ip_address FROM devices WHERE id = ANY(:ids)"
        ), {"ids": device_ids})).all()
    else:
        # Out-of-scope devices are reported exactly like missing ones.
        dev_rows = (await db.execute(text(
            "SELECT id, hostname, ip_address FROM devices WHERE id = ANY(:ids) AND "
            + scoping.jsonb_tags_visible("devices.tags")
        ), {"ids": device_ids, scoping.SCOPE_PARAM: visible_tags})).all()
    by_id = {str(r[0]): r for r in dev_rows}

    prefix = (data.name_prefix or t.name).strip()
    created: list[UUID] = []
    skipped: list[dict] = []

    for did in device_ids:
        d = by_id.get(str(did))
        if not d:
            skipped.append({"device_id": str(did), "reason": "device not found"})
            continue

        hostname = d[1] or ""
        ip_addr = str(d[2]) if d[2] is not None else ""

        def sub(s: str | None) -> str | None:
            if not s:
                return s
            return s.replace("{{hostname}}", hostname).replace("{{ip}}", ip_addr)

        name = f"{prefix} · {hostname}" if prefix else hostname
        # Skip if a check with this exact name already exists
        existing = (await db.execute(
            select(ServiceCheck.id).where(ServiceCheck.name == name)
        )).scalar_one_or_none()
        if existing:
            skipped.append({"device_id": str(did), "reason": f"duplicate name '{name}'"})
            continue

        kw: dict = {
            "device_id": d[0],
            "name": name,
            "check_type": t.check_type,
            "level": derive_level(t.check_type, []),
            "config": dict(t.config or {}),
            "tags": list(t.tags or []),
            "enabled": data.enabled,
            "target_host": hostname or ip_addr or "",
            "check_interval": t.default_interval,
            "timeout": t.default_timeout,
            "retry_count": t.default_retry_count,
            "retry_delay_s": t.default_retry_delay_s,
            "created_by": user_id,
        }
        if t.check_type == "http":
            kw["target_url"] = sub(t.target_url_template) or f"http://{hostname or ip_addr}/"
            kw["http_method"] = t.http_method or "GET"
            kw["http_expected_status"] = t.http_expected_status or 200
            kw["http_expected_statuses"] = t.http_expected_statuses
            kw["http_content_match"] = t.http_content_match
            kw["http_follow_redirects"] = (
                t.http_follow_redirects if t.http_follow_redirects is not None else True
            )
        elif t.check_type in ("tcp", "tls"):
            kw["target_port"] = t.target_port_default
        if t.check_type == "tls":
            kw["tls_warn_days"] = t.tls_warn_days or 30
            kw["tls_critical_days"] = t.tls_critical_days or 7

        sc = ServiceCheck(**kw)
        db.add(sc)
        await db.flush()
        created.append(sc.id)

    await db.commit()
    return ServiceCheckTemplateApplyResult(created_ids=created, skipped=skipped)


# ── SLA reporting ─────────────────────────────────────────────────────────

# scripts/init-clickhouse.sql gives zenplus.service_metrics a 30-day TTL.
RAW_METRIC_RETENTION_HOURS = 720


def _finite(value) -> float | None:
    """Coerce a ClickHouse aggregate to a JSON-safe float.

    avg()/quantile()/countIf()/count() ratios over an empty match set come back as nan
    rather than NULL, and nan is not JSON-encodable — FastAPI raises
    "Out of range float values are not JSON compliant" and the endpoint 500s.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def slo_block(target_pct: float, target_source: str, covered_sec: float,
              downtime_sec: float | None, uptime_pct: float | None) -> dict:
    """Error budget over *covered* (observed, maintenance-excluded) time."""
    covered = float(covered_sec or 0.0)
    down = float(downtime_sec or 0.0)
    allowed = (100.0 - target_pct) / 100.0
    budget = allowed * covered
    used_pct = None
    if budget > 0:
        used_pct = down / budget * 100.0
    elif covered > 0:
        used_pct = 0.0 if down <= 0 else None
    burn = None
    if covered > 0 and allowed > 0:
        burn = (down / covered) / allowed
    return {
        "target_pct": target_pct,
        "target_source": target_source,
        "error_budget_sec": budget,
        "budget_used_sec": down,
        "error_budget_used_pct": _finite(used_pct),
        "error_budget_remaining_sec": budget - down,
        "burn_rate": _finite(burn),
        "meets_target": (uptime_pct >= target_pct) if uptime_pct is not None else None,
    }


def _window_bounds(hours: int, from_dt: datetime | None, to_dt: datetime | None,
                   now: datetime) -> tuple[datetime, datetime, int]:
    """Validated requested window. Never silently scores a different period (B26)."""
    def _aware(d: datetime) -> datetime:
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    if (from_dt is None) != (to_dt is None):
        raise ServiceCheckValidationError("Pass both 'from' and 'to', or neither")
    if from_dt is not None and to_dt is not None:
        win_from, win_to = _aware(from_dt), _aware(to_dt)
        if win_to <= win_from:
            raise ServiceCheckValidationError("'to' must be after 'from'")
        hours = max(1, round((win_to - win_from).total_seconds() / 3600))
    else:
        win_to = now
        win_from = win_to - timedelta(hours=hours)
    return win_from, win_to, hours


async def get_service_sla(
    db: AsyncSession,
    check_id: UUID,
    hours: int,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    basis: str = "observed_time",
    visible_tags: list[str] | None = None,
) -> dict:
    """Compute uptime %, MTTR, MTBF, incidents and the SLO budget for a check.

    The window is the last `hours` hours, or the explicit [from_dt, to_dt) span when both
    are given. Maintenance (every scope, effective interval, merged) is removed from the
    evidence and the denominator and reported as maintenance_sec.
    """
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance
    from app.services.availability_policy import load_default_target, resolve_target

    sc = await get_visible_check(db, check_id, visible_tags)
    if not sc:
        return {"error": "not_found"}

    def _aware(d: datetime) -> datetime:
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    now = _utcnow()
    win_from, win_to, hours = _window_bounds(hours, from_dt, to_dt, now)
    interval = sc.check_interval or 60
    ch = get_clickhouse_client()

    # Bind the window as explicit literals rather than `now() - INTERVAL n HOUR`. Note that
    # clickhouse-connect renders a bound Python datetime with microseconds, which the
    # ClickHouse DateTime parser rejects, so these are pre-formatted strings.
    bounds = {
        "id": str(check_id),
        "f": win_from.strftime("%Y-%m-%d %H:%M:%S"),
        "t": win_to.strftime("%Y-%m-%d %H:%M:%S"),
    }
    WINDOW = "timestamp >= %(f)s AND timestamp < %(t)s"

    from app.services.service_availability import (
        read_segments, read_confirmed_segments, summarize, recovery_start, continuous_up_start,
        incident_intervals, up_seconds_between,
    )
    observed_start = max(win_from, _aware(sc.created_at)) if sc.created_at else win_from
    observed_end = min(win_to, now)
    if observed_end < observed_start:
        observed_end = observed_start
    excluded = []
    if observed_end > observed_start:
        excluded = (await service_maintenance.intervals_for_checks(
            db, [sc], observed_start, observed_end)).get(str(sc.id), [])
    segments, sample_count = read_segments(ch, check_id, observed_start, observed_end, interval)
    probe_availability = summarize(segments, observed_start, observed_end, excluded)
    if basis == "confirmed":
        segments = read_confirmed_segments(ch, check_id, observed_start, observed_end, segments)
    availability = summarize(segments, observed_start, observed_end, excluded)
    uptime_pct = availability["uptime_pct"]
    incident_count = availability["incident_count"]
    total_downtime_sec = availability["total_downtime_sec"]
    longest_incident_sec = availability["longest_incident_sec"]

    # Incidents with explicit populations (B19). A run interrupted only by a
    # coverage gap shorter than 2x the interval (or by maintenance) is one incident.
    all_incidents = incident_intervals(segments, observed_start, observed_end, excluded,
                                       merge_gap_sec=2 * interval, now=now)
    completed = [i for i in all_incidents if not i["starts_before_window"] and i["end"] is not None]
    mttr_sec = (sum(i["duration_sec"] for i in completed) / len(completed)) if completed else None
    mtbf_sec = None
    if len(all_incidents) >= 2:
        gaps = []
        for prev, nxt in zip(all_incidents, all_incidents[1:]):
            if prev["end"] is None:
                continue
            gaps.append(up_seconds_between(segments, prev["end"], nxt["start"], excluded))
        mtbf_sec = (sum(gaps) / len(gaps)) if gaps else None
    ongoing = next((i for i in all_incidents if i["ongoing"]), None)

    def _iso(v):
        return v.isoformat() if v else None

    incidents_out = [{
        "start": _iso(i["start"]),
        "end": _iso(i["end"]),
        "ongoing": i["ongoing"],
        "starts_before_window": i["starts_before_window"],
        "ends_after_window": i["ends_after_window"],
        "downtime_sec": i["downtime_sec"],
        "duration_sec": i["duration_sec"],
    } for i in all_incidents]

    # Latency over the *selected* window. Raw keeps a true p95 and is retained 30 days, so it
    # serves every window up to that; longer windows fall back to the 90-day rollup, which has
    # no percentile to reconstruct — p95 is reported as null rather than silently quoting 24h.
    # Every statistic is null when no successful probe exists (B18): ClickHouse returns 0
    # for max()/min() over an empty set.
    avg_response_ms: float | None = None
    p95_response_ms: float | None = None
    max_response_ms: float | None = None
    min_response_ms: float | None = None
    latency_sample_count = 0
    latency_basis = "raw"
    try:
        if hours <= RAW_METRIC_RETENTION_HOURS:
            r = ch.query(
                f"""
                SELECT count(), avg(response_ms), quantile(0.95)(response_ms),
                       max(response_ms), min(response_ms)
                FROM zenplus.service_metrics
                WHERE service_check_id = %(id)s AND is_up = 1 AND {WINDOW}
                """,
                parameters=bounds,
            )
            if r.result_rows:
                row = r.result_rows[0]
                latency_sample_count = int(row[0] or 0)
                if latency_sample_count:
                    avg_response_ms = _finite(row[1])
                    p95_response_ms = _finite(row[2])
                    max_response_ms = _finite(row[3])
                    min_response_ms = _finite(row[4])
        else:
            latency_basis = "rollup"
            r = ch.query(
                f"""
                SELECT sum(sample_count),
                       sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0),
                       max(max_response_ms), min(min_response_ms)
                FROM zenplus.service_metrics_5m
                WHERE service_check_id = %(id)s AND {WINDOW}
                """,
                parameters=bounds,
            )
            if r.result_rows:
                row = r.result_rows[0]
                latency_sample_count = int(row[0] or 0)
                if latency_sample_count:
                    avg_response_ms = _finite(row[1])
                    max_response_ms = _finite(row[2])
                    min_response_ms = _finite(row[3])
    except Exception:
        pass

    as_of = now
    latest_transition = ch.query("""
        SELECT timestamp, new_status FROM zenplus.service_status_log
        WHERE service_check_id = %(id)s ORDER BY timestamp DESC LIMIT 1
    """, parameters={"id": str(check_id)}).result_rows
    streak_start = recovery_start(latest_transition, as_of, sc.last_check_at, interval, sc.status)
    if streak_start:
        # The transition establishes recovery; subsequent missing results can still
        # break that streak. Bound the evidence to retained raw observations.
        streak_segments, _ = read_segments(ch, check_id, max(streak_start, as_of - timedelta(days=29)), as_of, interval)
        if basis == "confirmed":
            streak_segments = read_confirmed_segments(ch, check_id, max(streak_start, as_of - timedelta(days=29)), as_of, streak_segments)
        streak_start = continuous_up_start(streak_segments, as_of)
    uptime_streak_sec = (as_of - streak_start).total_seconds() if streak_start else None

    error_rate_pct = _finite(100.0 - uptime_pct) if uptime_pct is not None else None

    policy_target, policy_source = await load_default_target(db)
    target, target_source = resolve_target(getattr(sc, "sla_target_pct", None), policy_target, policy_source)
    slo = slo_block(target, target_source, availability["covered_sec"],
                    total_downtime_sec if uptime_pct is not None else 0.0, _finite(uptime_pct))

    cap = 200
    out = {
        "check_id": str(check_id),
        "window_hours": hours,
        "uptime_pct": _finite(uptime_pct),
        "sample_count": sample_count,
        "incident_count": incident_count,
        "total_downtime_sec": _finite(total_downtime_sec) or 0.0,
        "longest_incident_sec": _finite(longest_incident_sec) or 0.0,
        "mttr_sec": _finite(mttr_sec),
        "mtbf_sec": _finite(mtbf_sec),
        "mttr_population": "completed_in_window",
        "mtbf_basis": "observed_up_time_between_incidents",
        "completed_incident_count": len(completed),
        "ongoing_incident": ({"start": _iso(ongoing["start"]),
                              "duration_sec": (now - ongoing["start"]).total_seconds()}
                             if ongoing else None),
        "incidents": incidents_out[-cap:],
        "incidents_truncated": len(incidents_out) > cap,
        "avg_response_ms": avg_response_ms,
        "p95_response_ms": p95_response_ms,
        "max_response_ms": max_response_ms,
        "min_response_ms": min_response_ms,
        "latency_sample_count": latency_sample_count,
        "latency_basis": latency_basis,
        "error_rate_pct": error_rate_pct,
        "uptime_streak_sec": _finite(uptime_streak_sec),
        "uptime_streak_started_at": streak_start.isoformat() if streak_start else None,
        "as_of": as_of.isoformat(),
        "server_time": as_of.isoformat(),
        "availability_basis": basis,
        "probe_uptime_pct": _finite(probe_availability["uptime_pct"]),
        "probe_downtime_sec": probe_availability["total_downtime_sec"],
        "probe_error_rate_pct": _finite(100 - probe_availability["uptime_pct"]) if probe_availability["uptime_pct"] is not None else None,
        "covered_sec": availability["covered_sec"],
        "coverage_pct": availability["coverage_pct"],
        "unknown_sec": availability["unknown_sec"],
        "maintenance_sec": availability["maintenance_sec"],
        "requested_from": win_from.isoformat(),
        "requested_to": win_to.isoformat(),
        "effective_from": observed_start.isoformat(),
        "effective_to": observed_end.isoformat(),
        "requested_sec": (win_to - win_from).total_seconds(),
    }
    out.update(slo)
    return out


def _status_segments(
    events: list[tuple[datetime, str]],
    prior_status: str | None,
    span_start: datetime,
    span_end: datetime,
) -> list[tuple[datetime, datetime, bool | None]]:
    """Piecewise up/down timeline over [span_start, span_end) from status-log transitions.

    Returns (start, end, is_up) segments; is_up is None while the state is unknown
    (before the first transition when nothing preceded the window).
    """
    state: bool | None = None if prior_status is None else prior_status.lower() == "up"
    segments: list[tuple[datetime, datetime, bool | None]] = []
    cursor = span_start
    for ts, status in events:
        if ts <= span_start:
            state = status.lower() == "up"
            continue
        if ts >= span_end:
            break
        if ts > cursor:
            segments.append((cursor, ts, state))
        state = status.lower() == "up"
        cursor = ts
    if cursor < span_end:
        segments.append((cursor, span_end, state))
    return segments


def _uptime_over(segments: list[tuple[datetime, datetime, bool | None]], b0: datetime, b1: datetime) -> float | None:
    """Percentage of [b0, b1) spent up, over the portion where the state is known."""
    up = known = 0.0
    for s, e, st in segments:
        lo, hi = max(s, b0), min(e, b1)
        if hi <= lo or st is None:
            continue
        dur = (hi - lo).total_seconds()
        known += dur
        if st:
            up += dur
    if known <= 0:
        return None
    return up / known * 100.0


# Fleet confirmed availability is expensive (a history scan per 50 checks) and
# every Services list viewer polls it. Cache per (scope, window, timezone) with
# the window end bucketed to 60 s; entries live FLEET_CACHE_TTL_SEC.
FLEET_CACHE_TTL_SEC = 45.0
_fleet_cache: dict = {}


def _fleet_cache_get(key):
    import time
    hit = _fleet_cache.get(key)
    if hit and time.monotonic() - hit[0] < FLEET_CACHE_TTL_SEC:
        return hit[1]
    return None


def _fleet_cache_put(key, value):
    import time
    now = time.monotonic()
    for k in [k for k, (t, _v) in _fleet_cache.items() if now - t >= FLEET_CACHE_TTL_SEC]:
        _fleet_cache.pop(k, None)
    if len(_fleet_cache) > 256:
        _fleet_cache.clear()
    _fleet_cache[key] = (now, value)


def clear_fleet_cache() -> None:
    _fleet_cache.clear()


async def _scoped_check_rows(db: AsyncSession, visible_tags: list[str] | None):
    query = _scope_where(select(
        ServiceCheck.id, ServiceCheck.created_at, ServiceCheck.check_interval,
        ServiceCheck.group_id, ServiceCheck.tags,
    ), visible_tags)
    return [
        {"id": cid, "created_at": created, "check_interval": interval or 60,
         "group_id": gid, "tags": list(tags or [])}
        for cid, created, interval, gid, tags in (await db.execute(query)).all()
    ]


async def get_confirmed_uptime_all(
    db: AsyncSession, hours: int = 24, days: int | None = None,
    tz_name: str = "UTC", visible_tags: list[str] | None = None,
    use_cache: bool = True,
) -> dict:
    """List headlines and calendar bars share the detail page's confirmed SLA
    (maintenance excluded the same way)."""
    import asyncio
    from zoneinfo import ZoneInfo
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance
    from app.services.service_availability import read_fleet_segments, summarize, daily_summaries

    zone = ZoneInfo(tz_name)
    end = datetime.now(timezone.utc)
    cache_key = (tuple(visible_tags) if visible_tags is not None else None, hours, days, tz_name,
                 int(end.timestamp() // 60))
    if use_cache:
        cached = _fleet_cache_get(cache_key)
        if cached is not None:
            return cached
    start = end - timedelta(hours=hours)
    if days is not None:
        start = (end.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=days - 1)).astimezone(timezone.utc)
    rows = await _scoped_check_rows(db, visible_tags)
    checks = {str(r["id"]): (r["created_at"], r["check_interval"]) for r in rows}
    maintenance = {}
    if rows:
        maintenance = await service_maintenance.intervals_for_checks(db, rows, start, end)

    def collect():
        # Keep the history scan off the API event loop; clients are thread-local.
        values = {}
        for cid, segments, _, observed_start in read_fleet_segments(get_clickhouse_client(), checks, start, end):
            excluded = maintenance.get(cid)
            if days is None:
                values[cid] = _finite(summarize(segments, observed_start, end, excluded)["uptime_pct"])
            else:
                values[cid] = daily_summaries(segments, observed_start, end, zone, excluded)
        return values

    values = await asyncio.to_thread(collect)
    out = {"hours": hours, "days": days, "from": start.isoformat(), "to": end.isoformat(),
           "availability_basis": "confirmed", "timezone": tz_name, "checks": values,
           "maintenance_excluded": True, "server_time": end.isoformat()}
    if use_cache:
        _fleet_cache_put(cache_key, out)
    return out


def _service_maintenance_sql(maintenance: dict, id_col: str = "service_check_id") -> tuple[str, dict]:
    """`AND NOT (...)` dropping samples inside a check's maintenance intervals."""
    clauses, params = [], {}
    i = 0
    for cid, spans in maintenance.items():
        for lo, hi in spans:
            clauses.append(f"(toString({id_col}) = %(msc{i})s AND timestamp >= %(mlo{i})s AND timestamp < %(mhi{i})s)")
            params[f"msc{i}"] = str(cid)
            params[f"mlo{i}"] = lo.strftime("%Y-%m-%d %H:%M:%S")
            params[f"mhi{i}"] = hi.strftime("%Y-%m-%d %H:%M:%S")
            i += 1
    if not clauses:
        return "", {}
    return "AND NOT (" + " OR ".join(clauses) + ")", params


async def get_observed_uptime_all(db: AsyncSession, hours: int, visible_tags: list[str] | None = None) -> dict:
    """Legacy observed (sample-ratio) list headline, scoped, maintenance excluded."""
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance

    to_time = datetime.now(timezone.utc)
    from_time = to_time - timedelta(hours=hours)
    rows = await _scoped_check_rows(db, visible_tags)
    visible = {str(r["id"]) for r in rows}
    if not visible:
        return {"hours": hours, "from": from_time.isoformat(), "to": to_time.isoformat(), "checks": {}}
    maintenance = await service_maintenance.intervals_for_checks(db, rows, from_time, to_time)
    maint_sql, maint_params = _service_maintenance_sql(maintenance)
    client = get_clickhouse_client()
    tables_to_try = ["service_metrics"] if hours <= 6 else ["service_metrics_5m", "service_metrics"]
    params = {"from": from_time.strftime("%Y-%m-%d %H:%M:%S"), "to": to_time.strftime("%Y-%m-%d %H:%M:%S"),
              "ids": sorted(visible), **maint_params}
    uptime_map = {}
    for table in tables_to_try:
        if table == "service_metrics":
            query = f"""
                SELECT service_check_id, countIf(is_up = 1) AS up_count, count() AS total_count
                FROM zenplus.{table}
                WHERE toString(service_check_id) IN %(ids)s
                  AND timestamp >= %(from)s AND timestamp <= %(to)s {maint_sql}
                GROUP BY service_check_id
            """
        else:
            query = f"""
                SELECT service_check_id, sum(uptime_pct * sample_count) AS weighted_up,
                       sum(sample_count) AS total_count
                FROM zenplus.{table}
                WHERE toString(service_check_id) IN %(ids)s
                  AND timestamp >= %(from)s AND timestamp <= %(to)s {maint_sql}
                GROUP BY service_check_id
            """
        try:
            result = client.query(query, parameters=params)
            if len(result.result_rows) > 0:
                for row in result.result_rows:
                    check_id = str(row[0])
                    if check_id not in visible:
                        continue
                    up, total = row[1], row[2]
                    uptime_map[check_id] = round(up / total * 100, 2) if total else None
                break
        except Exception:
            continue
    return {"hours": hours, "from": from_time.isoformat(), "to": to_time.isoformat(), "checks": uptime_map,
            "maintenance_excluded": True}


async def get_daily_uptime_all(db: AsyncSession, days: int, visible_tags: list[str] | None = None) -> dict:
    """Weighted daily uptime for every visible check in one rollup scan.

    Returns {"days": N, "checks": {check_id: [{date, uptime_pct, sample_count,
    maintenance_sec}, ...]}}. Samples inside maintenance are excluded. Days with
    no stored samples are reconstructed from the status log with
    uptime_pct null (no samples = not measured) and the status-log estimate in
    status_uptime_pct: the poller updates Postgres and ClickHouse on separate
    paths, so a metrics-ingestion gap must still show the service as monitored.
    """
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance
    from app.services.service_availability import overlap_seconds
    ch = get_clickhouse_client()

    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await _scoped_check_rows(db, visible_tags)
    visible = {str(r["id"]) for r in rows}
    out: dict[str, list] = {}
    if not visible:
        return {"days": days, "checks": out}
    try:
        maintenance = await service_maintenance.intervals_for_checks(db, rows, window_start, now)
    except Exception:
        maintenance = {}
    maint_sql, maint_params = _service_maintenance_sql(maintenance)

    def day_maint(cid: str, key: str) -> float:
        day = datetime.fromisoformat(key).replace(tzinfo=timezone.utc)
        return overlap_seconds(maintenance.get(cid, []), day, min(now, day + timedelta(days=1)))

    try:
        res = ch.query(
            f"""
            SELECT service_check_id,
                   toDate(timestamp) AS d,
                   sum(uptime_pct * sample_count) * 100.0 / nullIf(sum(sample_count), 0) AS pct,
                   sum(sample_count) AS samples
            FROM zenplus.service_metrics_5m
            WHERE timestamp >= %(f)s {maint_sql}
            GROUP BY service_check_id, d
            ORDER BY d
            """,
            parameters={"f": window_start.strftime("%Y-%m-%d %H:%M:%S"), **maint_params},
        )
        for r in res.result_rows or []:
            cid = str(r[0])
            if cid not in visible:
                continue
            samples = int(r[3] or 0)
            key = r[1].isoformat()
            out.setdefault(cid, []).append({
                "date": key,
                "uptime_pct": _finite(r[2]) if samples else None,
                "sample_count": samples,
                "maintenance_sec": day_maint(cid, key),
            })
    except Exception:
        pass

    # Fill sample-less days from the status log.
    try:
        created = {
            str(r["id"]): (r["created_at"] if r["created_at"] is None or r["created_at"].tzinfo
                           else r["created_at"].replace(tzinfo=timezone.utc))
            for r in rows
        }

        trans: dict[str, list[tuple[datetime, str]]] = {}
        res = ch.query(
            """
            SELECT service_check_id, timestamp, new_status
            FROM zenplus.service_status_log
            WHERE timestamp >= %(f)s
            ORDER BY timestamp
            """,
            parameters={"f": window_start.strftime("%Y-%m-%d %H:%M:%S")},
        )
        for r in res.result_rows or []:
            ts = r[1] if r[1].tzinfo else r[1].replace(tzinfo=timezone.utc)
            trans.setdefault(str(r[0]), []).append((ts, str(r[2] or "")))

        prior: dict[str, str] = {}
        res = ch.query(
            """
            SELECT service_check_id, argMax(new_status, timestamp)
            FROM zenplus.service_status_log
            WHERE timestamp < %(f)s
            GROUP BY service_check_id
            """,
            parameters={"f": window_start.strftime("%Y-%m-%d %H:%M:%S")},
        )
        for r in res.result_rows or []:
            prior[str(r[0])] = str(r[1] or "")

        for cid in created:
            events = trans.get(cid, [])
            if not events and cid not in prior:
                continue
            segments = _status_segments(events, prior.get(cid), window_start, now)
            have = {e["date"] for e in out.get(cid, [])}
            c_at = created[cid]
            day = window_start
            while day < now:
                nxt = day + timedelta(days=1)
                key = day.date().isoformat()
                b0 = day if c_at is None else max(day, c_at)
                b1 = min(nxt, now)
                if key not in have and b1 > b0:
                    pct = _uptime_over(segments, b0, b1)
                    if pct is not None:
                        out.setdefault(cid, []).append({
                            "date": key,
                            "uptime_pct": None,
                            "status_uptime_pct": _finite(pct),
                            "sample_count": 0,
                            "maintenance_sec": day_maint(cid, key),
                        })
                day = nxt
        for rows_ in out.values():
            rows_.sort(key=lambda e: e["date"])
    except Exception:
        pass

    return {"days": days, "checks": out}


async def get_hourly_uptime(db: AsyncSession, check_id: UUID, days: int, basis: str = "observed_time",
                            visible_tags: list[str] | None = None) -> dict:
    """Calendar bins use exactly the same calculation as the SLA (maintenance excluded)."""
    from app.core.database import get_clickhouse_client
    from app.services import service_maintenance
    from app.services.service_availability import read_segments, read_confirmed_segments, summarize
    sc = await get_visible_check(db, check_id, visible_tags)
    if sc is None:
        return {"check_id": str(check_id), "days": days, "hours": [], "error": "not_found"}
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    created = sc.created_at if sc.created_at is None or sc.created_at.tzinfo else sc.created_at.replace(tzinfo=timezone.utc)
    ch = get_clickhouse_client()
    observed_start = max(start, created) if created else start
    excluded = (await service_maintenance.intervals_for_checks(db, [sc], observed_start, now)).get(str(sc.id), [])
    segments, _ = read_segments(ch, check_id, observed_start, now, sc.check_interval or 60)
    if basis == "confirmed":
        segments = read_confirmed_segments(ch, check_id, observed_start, now, segments)
    out = []
    # Split each segment once; avoid scanning 30 days of probes for every hour.
    bins = {}
    for lo, hi, up in segments:
        while lo < hi:
            hour = lo.replace(minute=0, second=0, microsecond=0)
            end = min(hi, hour + timedelta(hours=1))
            bins.setdefault(hour, []).append((lo, end, up))
            lo = end
    for lo, hi in excluded:
        cursor = lo.replace(minute=0, second=0, microsecond=0)
        while cursor < hi:
            bins.setdefault(cursor, [])
            cursor += timedelta(hours=1)
    for hour, values in sorted(bins.items()):
        stats = summarize(values, max(hour, created) if created else hour, min(now, hour + timedelta(hours=1)),
                          excluded)
        out.append({"ts": hour.isoformat(), "uptime_pct": stats["uptime_pct"],
                    "covered_sec": stats["covered_sec"], "sample_count": 0,
                    "maintenance_sec": stats["maintenance_sec"],
                    "downtime_sec": stats["total_downtime_sec"],
                    "availability_basis": basis})
    return {"check_id": str(check_id), "days": days, "hours": out, "server_time": now.isoformat()}
