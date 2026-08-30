import math
from uuid import UUID
from urllib.parse import urlsplit
from sqlalchemy import or_, select, func, delete, distinct
from sqlalchemy.ext.asyncio import AsyncSession

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


def _to_response(sc: ServiceCheck, in_maintenance: bool = False, parent_name: str | None = None) -> ServiceCheckResponse:
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
        retry_delay_s=sc.retry_delay_s or 30,
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
        created_at=sc.created_at,
        updated_at=sc.updated_at,
    )


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
):
    query = select(ServiceCheck)

    if device_id:
        query = query.where(ServiceCheck.device_id == device_id)
    if check_type:
        query = query.where(ServiceCheck.check_type == check_type)
    if status:
        query = query.where(ServiceCheck.status == status)
    if group_id:
        query = query.where(ServiceCheck.group_id == group_id)
    if tag:
        query = query.where(ServiceCheck.tags.any(tag))
    if level:
        query = query.where(ServiceCheck.level == level)
    if search:
        query = query.where(
            ServiceCheck.name.ilike(f"%{search}%") |
            ServiceCheck.target_host.ilike(f"%{search}%") |
            ServiceCheck.target_url.ilike(f"%{search}%")
        )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Fetch
    query = query.order_by(ServiceCheck.name).offset(skip).limit(limit)
    result = await db.execute(query)
    checks = list(result.scalars().all())

    # Enrich: in_maintenance + parent name
    maint_ids = await active_maintenance_check_ids(db)
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
            )
            for sc in checks
        ],
        "meta": {"total": total, "skip": skip, "limit": limit},
    }


async def get_service_check(db: AsyncSession, check_id: UUID):
    result = await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))
    sc = result.scalar_one_or_none()
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


async def get_related_service_checks(db: AsyncSession, check_id: UUID, limit: int = 16):
    """Find checks related to this one: parent, children, same device, host, or group."""
    sc = (await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))).scalar_one_or_none()
    if not sc:
        return None

    seen: set[UUID] = {sc.id}
    maint_ids = await active_maintenance_check_ids(db)

    def pack(items: list[ServiceCheck]):
        return [_to_response(c, in_maintenance=c.id in maint_ids) for c in items]

    parent = None
    if sc.parent_check_id:
        parent = (await db.execute(
            select(ServiceCheck).where(ServiceCheck.id == sc.parent_check_id)
        )).scalar_one_or_none()
        if parent:
            seen.add(parent.id)

    children = list((await db.execute(
        select(ServiceCheck)
        .where(ServiceCheck.parent_check_id == sc.id)
        .order_by(ServiceCheck.name)
        .limit(limit)
    )).scalars().all())
    seen.update(c.id for c in children)

    same_device: list[ServiceCheck] = []
    if sc.device_id:
        same_device = list((await db.execute(
            select(ServiceCheck)
            .where(ServiceCheck.device_id == sc.device_id, ServiceCheck.id.notin_(seen))
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
            select(ServiceCheck)
            .where(
                ServiceCheck.id.notin_(seen),
                or_(
                    func.lower(ServiceCheck.target_host) == host,
                    ServiceCheck.target_url.ilike(f"%://{host}/%"),
                    ServiceCheck.target_url.ilike(f"%://{host}:%"),
                    ServiceCheck.target_url.ilike(f"%://{host}"),
                ),
            )
            .order_by(ServiceCheck.name)
            .limit(limit)
        )).scalars().all())
        seen.update(c.id for c in same_host)

    same_group: list[ServiceCheck] = []
    if sc.group_id:
        same_group = list((await db.execute(
            select(ServiceCheck)
            .where(ServiceCheck.group_id == sc.group_id, ServiceCheck.id.notin_(seen))
            .order_by(ServiceCheck.name)
            .limit(limit)
        )).scalars().all())

    return {
        "parent": pack([parent])[0] if parent else None,
        "children": pack(children),
        "same_device": pack(same_device),
        "same_host": pack(same_host),
        "same_group": pack(same_group),
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


async def create_service_check(db: AsyncSession, data: ServiceCheckCreate, user_id: UUID):
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
    sc = ServiceCheck(
        **data.model_dump(),
        created_by=user_id,
    )
    if credential:
        sc.credential = credential
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    return _to_response(sc)


async def update_service_check(db: AsyncSession, check_id: UUID, data: ServiceCheckUpdate):
    result = await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))
    sc = result.scalar_one_or_none()
    if not sc:
        return None

    update_data = data.model_dump(exclude_unset=True)
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
    effective_type = update_data.get("check_type", sc.check_type)
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
    for key, value in update_data.items():
        setattr(sc, key, value)
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


async def get_service_check_summary(db: AsyncSession) -> ServiceCheckSummary:
    result = await db.execute(
        select(ServiceCheck.status, func.count(ServiceCheck.id))
        .group_by(ServiceCheck.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    total = sum(counts.values())
    return ServiceCheckSummary(
        total=total,
        up=counts.get("up", 0),
        down=counts.get("down", 0),
        warning=counts.get("warning", 0),
        degraded=counts.get("degraded", 0),
        unknown=counts.get("unknown", 0),
    )


async def get_device_service_checks(db: AsyncSession, device_id: UUID):
    result = await db.execute(
        select(ServiceCheck)
        .where(ServiceCheck.device_id == device_id)
        .order_by(ServiceCheck.name)
    )
    checks = result.scalars().all()
    return [_to_response(sc) for sc in checks]


async def bulk_delete_service_checks(db: AsyncSession, check_ids: list[UUID]) -> int:
    result = await db.execute(
        delete(ServiceCheck).where(ServiceCheck.id.in_(check_ids))
    )
    await db.commit()
    return result.rowcount


async def export_service_checks(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(ServiceCheck).order_by(ServiceCheck.name))
    checks = result.scalars().all()
    export = []
    for sc in checks:
        export.append({
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
            "workflow_steps": list(sc.workflow_steps or []),
            "tls_warn_days": sc.tls_warn_days,
            "tls_critical_days": sc.tls_critical_days,
            "check_interval": sc.check_interval,
            "timeout": sc.timeout,
            "status": sc.status,
            "description": sc.description,
        })
    return export


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


async def list_groups(db: AsyncSession) -> list[ServiceCheckGroupResponse]:
    groups = (await db.execute(select(ServiceCheckGroup).order_by(ServiceCheckGroup.name))).scalars().all()
    if not groups:
        return []
    counts_rows = (await db.execute(
        select(ServiceCheck.group_id, func.count())
        .where(ServiceCheck.group_id.in_([g.id for g in groups]))
        .group_by(ServiceCheck.group_id)
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


async def list_tags(db: AsyncSession) -> list[str]:
    # Fetch distinct tag values across all service_checks by unnesting the array column.
    from sqlalchemy import text
    rows = (await db.execute(text(
        "SELECT DISTINCT unnest(tags) AS tag FROM service_checks WHERE array_length(tags,1) > 0 ORDER BY tag"
    ))).all()
    return [r[0] for r in rows]


# ── Maintenance windows ───────────────────────────────────────────────────

def _maint_to_response(m: ServiceCheckMaintenance, label: str | None = None) -> ServiceMaintenanceResponse:
    now = datetime.now(timezone.utc)
    starts = m.starts_at if m.starts_at.tzinfo else m.starts_at.replace(tzinfo=timezone.utc)
    ends = m.ends_at if m.ends_at.tzinfo else m.ends_at.replace(tzinfo=timezone.utc)
    return ServiceMaintenanceResponse(
        id=m.id,
        scope_type=m.scope_type,
        scope_check_id=m.scope_check_id,
        scope_group_id=m.scope_group_id,
        scope_tag=m.scope_tag,
        scope_label=label,
        starts_at=starts,
        ends_at=ends,
        reason=m.reason,
        active=starts <= now <= ends,
        created_at=m.created_at,
    )


async def list_maintenance(db: AsyncSession) -> list[ServiceMaintenanceResponse]:
    rows = (await db.execute(
        select(ServiceCheckMaintenance).order_by(ServiceCheckMaintenance.starts_at.desc())
    )).scalars().all()

    # Build labels — check name / group name / tag / "All checks"
    check_ids = [m.scope_check_id for m in rows if m.scope_check_id]
    group_ids = [m.scope_group_id for m in rows if m.scope_group_id]
    check_names: dict = {}
    group_names: dict = {}
    if check_ids:
        check_names = dict((await db.execute(
            select(ServiceCheck.id, ServiceCheck.name).where(ServiceCheck.id.in_(check_ids))
        )).all())
    if group_ids:
        group_names = dict((await db.execute(
            select(ServiceCheckGroup.id, ServiceCheckGroup.name).where(ServiceCheckGroup.id.in_(group_ids))
        )).all())

    out: list[ServiceMaintenanceResponse] = []
    for m in rows:
        if m.scope_type == "check":
            label = check_names.get(m.scope_check_id, "(deleted check)")
        elif m.scope_type == "group":
            label = group_names.get(m.scope_group_id, "(deleted group)")
        elif m.scope_type == "tag":
            label = f"tag:{m.scope_tag}"
        else:
            label = "All checks"
        out.append(_maint_to_response(m, label))
    return out


async def create_maintenance(db: AsyncSession, data: ServiceMaintenanceCreate, user_id: UUID | None) -> ServiceMaintenanceResponse:
    # Normalise scope payload based on scope_type
    scope_type = data.scope_type
    kw: dict = {
        "scope_type": scope_type,
        "starts_at": data.starts_at,
        "ends_at": data.ends_at,
        "reason": data.reason,
        "created_by": user_id,
    }
    if scope_type == "check":
        kw["scope_check_id"] = data.scope_check_id
    elif scope_type == "group":
        kw["scope_group_id"] = data.scope_group_id
    elif scope_type == "tag":
        kw["scope_tag"] = data.scope_tag
    m = ServiceCheckMaintenance(**kw)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return _maint_to_response(m)


async def delete_maintenance(db: AsyncSession, maintenance_id: UUID) -> bool:
    res = await db.execute(
        delete(ServiceCheckMaintenance).where(ServiceCheckMaintenance.id == maintenance_id)
    )
    await db.commit()
    return (res.rowcount or 0) > 0


async def active_maintenance_check_ids(db: AsyncSession) -> set[UUID]:
    """Return set of service_check IDs currently covered by an active maintenance window."""
    now = datetime.now(timezone.utc)
    windows = (await db.execute(
        select(ServiceCheckMaintenance)
        .where(ServiceCheckMaintenance.starts_at <= now)
        .where(ServiceCheckMaintenance.ends_at >= now)
    )).scalars().all()
    if not windows:
        return set()

    affected: set[UUID] = set()
    for w in windows:
        if w.scope_type == "check" and w.scope_check_id:
            affected.add(w.scope_check_id)
        elif w.scope_type == "group" and w.scope_group_id:
            rows = (await db.execute(
                select(ServiceCheck.id).where(ServiceCheck.group_id == w.scope_group_id)
            )).scalars().all()
            affected.update(rows)
        elif w.scope_type == "tag" and w.scope_tag:
            rows = (await db.execute(
                select(ServiceCheck.id).where(ServiceCheck.tags.any(w.scope_tag))
            )).scalars().all()
            affected.update(rows)
        elif w.scope_type == "all":
            rows = (await db.execute(select(ServiceCheck.id))).scalars().all()
            affected.update(rows)
    return affected


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
    dev_rows = (await db.execute(text(
        "SELECT id, hostname, ip_address FROM devices WHERE id = ANY(:ids)"
    ), {"ids": device_ids})).all()
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
            "level": t.level,
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


async def get_service_sla(
    db: AsyncSession,
    check_id: UUID,
    hours: int,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> dict:
    """Compute uptime %, MTTR, MTBF and incident stats for a service check.

    The window is the last `hours` hours, or the explicit [from_dt, to_dt) span when both
    are given — a caller looking at a historical range needs stats for *that* range, not
    for the same number of hours ending now.
    """
    from app.core.database import get_clickhouse_client
    ch = get_clickhouse_client()

    # Make sure the check exists
    sc = (await db.execute(
        select(ServiceCheck).where(ServiceCheck.id == check_id)
    )).scalar_one_or_none()
    if not sc:
        return {"error": "not_found"}

    # Bind the window as explicit literals rather than `now() - INTERVAL n HOUR`. Note that
    # clickhouse-connect renders a bound Python datetime with microseconds, which the
    # ClickHouse DateTime parser rejects, so these are pre-formatted strings.
    def _aware(d: datetime) -> datetime:
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    if from_dt and to_dt:
        win_from, win_to = _aware(from_dt), _aware(to_dt)
        if win_to <= win_from:
            win_to = win_from + timedelta(hours=1)
        hours = max(1, round((win_to - win_from).total_seconds() / 3600))
    else:
        win_to = datetime.now(timezone.utc)
        win_from = win_to - timedelta(hours=hours)

    bounds = {
        "id": str(check_id),
        "f": win_from.strftime("%Y-%m-%d %H:%M:%S"),
        "t": win_to.strftime("%Y-%m-%d %H:%M:%S"),
    }
    WINDOW = "timestamp >= %(f)s AND timestamp < %(t)s"

    # Uptime: raw probes for short windows, the 5m rollup beyond that.
    # service_metrics_5m.uptime_pct is a 0..1 fraction (avg(is_up)) per bucket, and buckets
    # hold differing sample counts, so re-aggregating it has to be weighted by sample_count —
    # a plain avg() lets a 1-sample bucket outweigh a 12-sample one.
    use_raw = hours <= 6
    if use_raw:
        up_expr = "countIf(is_up = 1) * 100.0 / count() AS up_pct, count() AS total_count"
        table = "zenplus.service_metrics"
    else:
        up_expr = (
            "sum(uptime_pct * sample_count) * 100.0 / nullIf(sum(sample_count), 0) AS up_pct, "
            "sum(sample_count) AS total_count"
        )
        table = "zenplus.service_metrics_5m"

    try:
        res = ch.query(
            f"SELECT {up_expr} FROM {table} WHERE service_check_id = %(id)s AND {WINDOW}",
            parameters=bounds,
        )
        row = res.result_rows[0] if res.result_rows else None
    except Exception:
        row = None

    uptime_pct: float | None = None
    sample_count = 0
    if row:
        uptime_pct = _finite(row[0])
        sample_count = int(row[1] or 0)

    # Outages: pair every transition away from "up" with the recovery that follows it.
    # The poller writes duration_sec = 0 on every status_log row, so an outage's length only
    # exists as the gap between two rows and has to be reconstructed. An outage already open
    # when the window starts is clipped to the window; one still open now runs to now.
    now_utc = min(datetime.now(timezone.utc), win_to)
    window_start = win_from

    incident_count = 0
    longest_incident_sec = 0.0
    total_downtime_sec = 0.0
    transitions: list[tuple[datetime, str]] = []
    entered_down = False
    try:
        res = ch.query(
            f"""
            SELECT timestamp, new_status
            FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s AND {WINDOW}
            ORDER BY timestamp
            """,
            parameters=bounds,
        )
        for r in res.result_rows or []:
            ts = r[0]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            transitions.append((ts, str(r[1] or "").lower()))

        # State on entering the window, so a pre-existing outage still counts.
        prior = ch.query(
            """
            SELECT new_status
            FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s AND timestamp < %(f)s
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            parameters=bounds,
        )
        prior_rows = prior.result_rows or []
        if prior_rows:
            entered_down = str(prior_rows[0][0] or "").lower() not in ("up", "")
        else:
            # No prior transition at all: a window whose first event is a recovery must have
            # been in a bad state before it. The poller logs no transition for the initial
            # unknown -> down edge, so this is the only trace such an outage leaves.
            entered_down = bool(transitions) and transitions[0][1] == "up"

        # Clamp an inferred outage to the check's creation, so a 30-day window over a check
        # that is a week old does not bill three weeks of downtime against it.
        opened_at = window_start
        if entered_down and sc.created_at:
            created = sc.created_at if sc.created_at.tzinfo else sc.created_at.replace(tzinfo=timezone.utc)
            opened_at = max(window_start, created)

        def close(start: datetime, end: datetime) -> None:
            nonlocal incident_count, total_downtime_sec, longest_incident_sec
            dur = (end - start).total_seconds()
            if dur > 0:
                incident_count += 1
                total_downtime_sec += dur
                longest_incident_sec = max(longest_incident_sec, dur)

        open_at: datetime | None = opened_at if entered_down else None
        for ts, status in transitions:
            if status == "up":
                if open_at is not None:
                    close(open_at, ts)
                    open_at = None
            elif open_at is None:
                open_at = ts
        if open_at is not None:
            close(open_at, now_utc)
    except Exception:
        pass

    # MTTR = total downtime / incident count (only if we had incidents)
    mttr_sec = (total_downtime_sec / incident_count) if incident_count > 0 else None
    # MTBF approx: (window_sec - downtime) / max(incident_count, 1)
    window_sec = max(1.0, (win_to - win_from).total_seconds())
    mtbf_sec = ((window_sec - total_downtime_sec) / incident_count) if incident_count > 0 else None

    # Availability from the reconstructed outage timeline, the way uptime products score it:
    # wall-clock downtime over monitored wall-clock time. Probe samples are biased whenever
    # ingestion has gaps — a stretch that stored no samples contributes nothing to a
    # sample-based percentage even though the status log knows exactly what happened there.
    # Samples remain the fallback for checks with no status-log history at all.
    had_log = bool(transitions) or entered_down or incident_count > 0
    if had_log:
        mon_start = window_start
        if sc.created_at:
            created_aware = sc.created_at if sc.created_at.tzinfo else sc.created_at.replace(tzinfo=timezone.utc)
            mon_start = max(window_start, created_aware)
        monitored_sec = (now_utc - mon_start).total_seconds()
        if monitored_sec > 0:
            uptime_pct = _finite(max(0.0, 100.0 * (1.0 - total_downtime_sec / monitored_sec)))
    elif total_downtime_sec > 0:
        time_based_pct = max(0.0, 100.0 * (1.0 - total_downtime_sec / window_sec))
        uptime_pct = time_based_pct if uptime_pct is None else min(uptime_pct, time_based_pct)

    # Latency over the *selected* window. Raw keeps a true p95 and is retained 30 days, so it
    # serves every window up to that; longer windows fall back to the 90-day rollup, which has
    # no percentile to reconstruct — p95 is reported as null rather than silently quoting 24h.
    avg_response_ms: float | None = None
    p95_response_ms: float | None = None
    max_response_ms: float | None = None
    try:
        if hours <= RAW_METRIC_RETENTION_HOURS:
            r = ch.query(
                f"""
                SELECT avg(response_ms), quantile(0.95)(response_ms), max(response_ms)
                FROM zenplus.service_metrics
                WHERE service_check_id = %(id)s AND is_up = 1 AND {WINDOW}
                """,
                parameters=bounds,
            )
            if r.result_rows:
                avg_response_ms = _finite(r.result_rows[0][0])
                p95_response_ms = _finite(r.result_rows[0][1])
                max_response_ms = _finite(r.result_rows[0][2])
        else:
            r = ch.query(
                f"""
                SELECT sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0),
                       max(max_response_ms)
                FROM zenplus.service_metrics_5m
                WHERE service_check_id = %(id)s AND {WINDOW}
                """,
                parameters=bounds,
            )
            if r.result_rows:
                avg_response_ms = _finite(r.result_rows[0][0])
                max_response_ms = _finite(r.result_rows[0][1])
    except Exception:
        pass

    # Uptime streak: seconds since the most recent "up" transition that persists.
    # If the check is currently down, streak is 0. Otherwise it's (now - last down
    # transition) or (now - created_at) when there's never been a down event.
    uptime_streak_sec: float | None = None
    if sc.status == "up":
        from datetime import timezone as _tz, datetime as _dt
        last_down = None
        try:
            r = ch.query(
                """
                SELECT max(timestamp)
                FROM zenplus.service_status_log
                WHERE service_check_id = %(id)s
                  AND new_status != 'up'
                """,
                parameters={"id": str(check_id)},
            )
            raw = r.result_rows[0][0] if r.result_rows else None
            # ClickHouse max() returns 1970-01-01 on an empty set rather than NULL.
            if raw is not None and raw.year > 1990:
                last_down = raw if raw.tzinfo else raw.replace(tzinfo=_tz.utc)
        except Exception:
            pass

        origin = last_down or (
            sc.created_at if sc.created_at and sc.created_at.tzinfo else
            (sc.created_at.replace(tzinfo=_tz.utc) if sc.created_at else None)
        )
        if origin is not None:
            uptime_streak_sec = max(0.0, (_dt.now(_tz.utc) - origin).total_seconds())
    else:
        uptime_streak_sec = 0.0

    error_rate_pct = _finite(100.0 - uptime_pct) if uptime_pct is not None else None

    return {
        "check_id": str(check_id),
        "window_hours": hours,
        "uptime_pct": _finite(uptime_pct),
        "sample_count": sample_count,
        "incident_count": incident_count,
        "total_downtime_sec": _finite(total_downtime_sec) or 0.0,
        "longest_incident_sec": _finite(longest_incident_sec) or 0.0,
        "mttr_sec": _finite(mttr_sec),
        "mtbf_sec": _finite(mtbf_sec),
        "avg_response_ms": avg_response_ms,
        "p95_response_ms": p95_response_ms,
        "max_response_ms": max_response_ms,
        "error_rate_pct": error_rate_pct,
        "uptime_streak_sec": _finite(uptime_streak_sec),
    }


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


async def get_daily_uptime_all(db: AsyncSession, days: int) -> dict:
    """Weighted daily uptime for every check in one rollup scan.

    Returns {"days": N, "checks": {check_id: [{date, uptime_pct, sample_count}, ...]}}.
    Days with no stored samples are reconstructed from the status log (sample_count 0):
    the poller updates Postgres and ClickHouse on separate paths, so a metrics-ingestion
    gap must not paint an actively monitored service as "not monitored".
    """
    from app.core.database import get_clickhouse_client
    ch = get_clickhouse_client()

    out: dict[str, list] = {}
    try:
        res = ch.query(
            """
            SELECT service_check_id,
                   toDate(timestamp) AS d,
                   sum(uptime_pct * sample_count) * 100.0 / nullIf(sum(sample_count), 0) AS pct,
                   sum(sample_count) AS samples
            FROM zenplus.service_metrics_5m
            WHERE timestamp >= now() - INTERVAL %(d)s DAY
            GROUP BY service_check_id, d
            ORDER BY d
            """,
            parameters={"d": days},
        )
        for r in res.result_rows or []:
            out.setdefault(str(r[0]), []).append({
                "date": r[1].isoformat(),
                "uptime_pct": _finite(r[2]),
                "sample_count": int(r[3] or 0),
            })
    except Exception:
        pass

    # Fill sample-less days from the status log.
    try:
        created_rows = (await db.execute(select(ServiceCheck.id, ServiceCheck.created_at))).all()
        created = {
            str(cid): (c if c is None or c.tzinfo else c.replace(tzinfo=timezone.utc))
            for cid, c in created_rows
        }

        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

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
                            "uptime_pct": _finite(pct),
                            "sample_count": 0,
                        })
                day = nxt
        for rows in out.values():
            rows.sort(key=lambda e: e["date"])
    except Exception:
        pass

    return {"days": days, "checks": out}


async def get_hourly_uptime(db: AsyncSession, check_id: UUID, days: int) -> dict:
    """Hour-by-hour uptime percentages for the last N days.
    Returns {"hours": [{ts, uptime_pct, sample_count, had_down}]}.
    Powers the calendar heatmap on the detail page.
    """
    from app.core.database import get_clickhouse_client
    ch = get_clickhouse_client()

    try:
        # Pull from 5m rollup (90d TTL) — 30d worth is ~720*12 = 8640 5m rows max.
        res = ch.query(
            """
            SELECT toStartOfHour(timestamp) AS ts,
                   avg(uptime_pct) * 100 AS pct,
                   sum(sample_count) AS samples
            FROM zenplus.service_metrics_5m
            WHERE service_check_id = %(id)s
              AND timestamp >= now() - INTERVAL %(d)s DAY
            GROUP BY ts
            ORDER BY ts
            """,
            parameters={"id": str(check_id), "d": days},
        )
        rows = res.result_rows or []
    except Exception:
        rows = []

    # Fallback to raw for very recent windows if rollup empty
    if not rows:
        try:
            res = ch.query(
                """
                SELECT toStartOfHour(timestamp) AS ts,
                       countIf(is_up = 1) * 100.0 / count() AS pct,
                       count() AS samples
                FROM zenplus.service_metrics
                WHERE service_check_id = %(id)s
                  AND timestamp >= now() - INTERVAL %(d)s DAY
                GROUP BY ts
                ORDER BY ts
                """,
                parameters={"id": str(check_id), "d": days},
            )
            rows = res.result_rows or []
        except Exception:
            rows = []

    out = []
    have: set[str] = set()
    for r in rows:
        ts = r[0]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        pct = _finite(r[1])
        samples = int(r[2] or 0)
        have.add(ts.isoformat())
        out.append({
            "ts": ts.isoformat(),
            "uptime_pct": pct,
            "sample_count": samples,
        })

    # Reconstruct sample-less hours from the status log (sample_count 0), so a
    # metrics-ingestion gap does not render an actively monitored service as "no data".
    try:
        sc = (await db.execute(select(ServiceCheck).where(ServiceCheck.id == check_id))).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)

        res = ch.query(
            """
            SELECT timestamp, new_status
            FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s AND timestamp >= %(f)s
            ORDER BY timestamp
            """,
            parameters={"id": str(check_id), "f": window_start.strftime("%Y-%m-%d %H:%M:%S")},
        )
        events = [
            ((r[0] if r[0].tzinfo else r[0].replace(tzinfo=timezone.utc)), str(r[1] or ""))
            for r in res.result_rows or []
        ]
        prior_status: str | None = None
        res = ch.query(
            """
            SELECT new_status
            FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s AND timestamp < %(f)s
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            parameters={"id": str(check_id), "f": window_start.strftime("%Y-%m-%d %H:%M:%S")},
        )
        if res.result_rows:
            prior_status = str(res.result_rows[0][0] or "")

        if events or prior_status is not None:
            segments = _status_segments(events, prior_status, window_start, now)
            created = sc.created_at if sc and sc.created_at else None
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            hour = window_start
            while hour < now:
                nxt = hour + timedelta(hours=1)
                if hour.isoformat() not in have:
                    b0 = hour if created is None else max(hour, created)
                    b1 = min(nxt, now)
                    if b1 > b0:
                        pct = _uptime_over(segments, b0, b1)
                        if pct is not None:
                            out.append({"ts": hour.isoformat(), "uptime_pct": _finite(pct), "sample_count": 0})
                hour = nxt
            out.sort(key=lambda e: e["ts"])
    except Exception:
        pass

    return {"check_id": str(check_id), "days": days, "hours": out}
