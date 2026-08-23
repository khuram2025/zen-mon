from uuid import UUID
from urllib.parse import urlsplit
from sqlalchemy import select, func, delete, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

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

async def get_service_sla(db: AsyncSession, check_id: UUID, hours: int) -> dict:
    """Compute uptime %, MTTR, MTBF, incident stats for a service check over
    the last `hours` hours. Uses ClickHouse rollup tables for speed; falls
    back to raw when needed."""
    from app.core.database import get_clickhouse_client
    ch = get_clickhouse_client()

    # Make sure the check exists
    sc = (await db.execute(
        select(ServiceCheck).where(ServiceCheck.id == check_id)
    )).scalar_one_or_none()
    if not sc:
        return {"error": "not_found"}

    # Uptime: prefer the 5m rollup for windows > 6h; raw otherwise.
    if hours <= 6:
        table = "zenplus.service_metrics"
        up_expr = "countIf(is_up = 1) AS up_count, count() AS total_count"
    else:
        table = "zenplus.service_metrics_5m"
        up_expr = "avg(uptime_pct) * 100 AS up_pct, count() AS total_count"

    try:
        if table.endswith("service_metrics"):
            res = ch.query(
                f"""
                SELECT {up_expr}
                FROM {table}
                WHERE service_check_id = %(id)s
                  AND timestamp >= now() - INTERVAL %(h)s HOUR
                """,
                parameters={"id": str(check_id), "h": hours},
            )
        else:
            res = ch.query(
                f"""
                SELECT {up_expr}
                FROM {table}
                WHERE service_check_id = %(id)s
                  AND timestamp >= now() - INTERVAL %(h)s HOUR
                """,
                parameters={"id": str(check_id), "h": hours},
            )
        row = res.result_rows[0] if res.result_rows else None
    except Exception:
        row = None

    uptime_pct: float | None = None
    sample_count = 0
    if row:
        if table.endswith("service_metrics"):
            up, total = int(row[0] or 0), int(row[1] or 0)
            sample_count = total
            uptime_pct = (up / total * 100) if total > 0 else None
        else:
            uptime_pct = float(row[0]) if row[0] is not None else None
            sample_count = int(row[1] or 0)

    # Incidents: status_log transitions to down/degraded/warning in the window.
    incident_count = 0
    longest_incident_sec = 0.0
    total_downtime_sec = 0.0
    try:
        res = ch.query(
            """
            SELECT new_status, duration_sec
            FROM zenplus.service_status_log
            WHERE service_check_id = %(id)s
              AND timestamp >= now() - INTERVAL %(h)s HOUR
              AND new_status IN ('down', 'degraded', 'warning')
            """,
            parameters={"id": str(check_id), "h": hours},
        )
        for r in res.result_rows:
            incident_count += 1
            d = float(r[1] or 0)
            total_downtime_sec += d
            if d > longest_incident_sec:
                longest_incident_sec = d
    except Exception:
        pass

    # MTTR = total downtime / incident count (only if we had incidents)
    mttr_sec = (total_downtime_sec / incident_count) if incident_count > 0 else None
    # MTBF approx: (window_sec - downtime) / max(incident_count, 1)
    window_sec = hours * 3600
    mtbf_sec = ((window_sec - total_downtime_sec) / incident_count) if incident_count > 0 else None

    # Avg + P95 response time from raw metrics (last N hours clamped to 24 max for cost)
    avg_response_ms: float | None = None
    p95_response_ms: float | None = None
    max_response_ms: float | None = None
    try:
        cap_h = min(hours, 24)
        r = ch.query(
            """
            SELECT avg(response_ms), quantile(0.95)(response_ms), max(response_ms)
            FROM zenplus.service_metrics
            WHERE service_check_id = %(id)s
              AND is_up = 1
              AND timestamp >= now() - INTERVAL %(h)s HOUR
            """,
            parameters={"id": str(check_id), "h": cap_h},
        )
        if r.result_rows and r.result_rows[0][0] is not None:
            avg_response_ms = float(r.result_rows[0][0])
            p95_response_ms = float(r.result_rows[0][1]) if r.result_rows[0][1] is not None else None
            max_response_ms = float(r.result_rows[0][2]) if r.result_rows[0][2] is not None else None
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

    error_rate_pct = 100.0 - uptime_pct if uptime_pct is not None else None

    return {
        "check_id": str(check_id),
        "window_hours": hours,
        "uptime_pct": uptime_pct,
        "sample_count": sample_count,
        "incident_count": incident_count,
        "total_downtime_sec": total_downtime_sec,
        "longest_incident_sec": longest_incident_sec,
        "mttr_sec": mttr_sec,
        "mtbf_sec": mtbf_sec,
        "avg_response_ms": avg_response_ms,
        "p95_response_ms": p95_response_ms,
        "max_response_ms": max_response_ms,
        "error_rate_pct": error_rate_pct,
        "uptime_streak_sec": uptime_streak_sec,
    }


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
    for r in rows:
        ts = r[0]
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        pct = float(r[1]) if r[1] is not None else None
        samples = int(r[2] or 0)
        out.append({
            "ts": ts.isoformat(),
            "uptime_pct": pct,
            "sample_count": samples,
        })

    return {"check_id": str(check_id), "days": days, "hours": out}
