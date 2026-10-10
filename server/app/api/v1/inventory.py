"""Inventory & Compliance read API (phase 1).

Mounted in ``app.main`` with ``dependencies=[policy("inventory")]``: every GET needs
``inventory.view`` (a precise permission, so a route that is not GET must declare its own
manage/export guard; ``tests/test_inventory_route_guards.py`` enforces that). Every handler resolves
the caller's tag scope with ``scoping.visible_tags`` and hands it to ``services/inventory_query``,
which applies it inside the asset union, so a handler cannot forget it.

The handlers are deliberately thin: parse and validate, call one service function, translate the
vocabulary error into a 422. The contract (parameters, TypeScript response types, examples, error
cases and caching) is ``Documentation/Inventory/05-P1-API-CONTRACT.md``.

Routes (prefix /api/v1/inventory):
    GET /summary                          Overview figures (cached 60 s per scope)
    GET /assets                           the asset list (server-side filter, sort, page)
    GET /assets/facets                    facet counts for the asset filters
    GET /assets/{kind}/{id}               the asset flyout payload (404 when unknown or out of scope)
    GET /os                               operating system / firmware tree
    GET /hardware                         network vendors and models, modules, server hardware
    GET /changes                          change events with window stats and a daily series
    GET /software                         software catalog (product rows with version distribution)
    GET /software/publishers              publisher facet of the catalog
    GET /software/{product_key}           product page: header, every version ranked, raw-name evidence
    GET /software/{product_key}/installations   where the product is installed (one row per software row)
    GET /trends                           daily snapshot series (200 with restricted=true for a scoped caller)
    GET /settings                         module settings (inventory.view); PUT needs inventory.manage, is audited and
                                          answers 503 (nothing saved) when its audit entry cannot be stored
    GET /export                           CSV of a dataset, needs inventory.export; audited before the first byte (503 and
                                          nothing sent when the entry cannot be stored); 429 + Retry-After past 2 exports
                                          at once per worker process or 1 per user
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas import inventory as schemas
from app.services import inventory_export as export
from app.services import inventory_query as iq
from app.services import inventory_software_query as isq
from app.services import inventory_trends_settings as its
from app.services.audit_service import AuditUnavailable, write_audit_log

router = APIRouter(prefix="/inventory", tags=["Inventory"])

_SORT_PATTERN = "^(" + "|".join(iq.SORT_KEYS) + ")$"
_NOT_FOUND = "Asset not found"
_PRODUCT_NOT_FOUND = "Product not found"
_AUDIT_UNAVAILABLE = "The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."
_CATALOG_SORT_PATTERN = "^(" + "|".join(isq.CATALOG_SORTS) + ")$"
_INSTALL_SORT_PATTERN = "^(" + "|".join(isq.INSTALL_SORTS) + ")$"
_INSTANT_MAX = 64            # a date-time query parameter: text, parsed by iq.parse_instant (a malformed one is a 422)


def _unprocessable(exc: iq.InvalidFilter) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def asset_filters(
    q: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Free text over name, ip, serial, model, vendor, OS product and version"),
    kind: Optional[Literal["server", "device"]] = Query(None),
    os_family: Optional[str] = Query(None, max_length=64, description="Normalised OS family; 'unknown' for none"),
    os_product: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="OS product; 'unknown' for none"),
    os_release: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="OS release; 'unknown' for none"),
    os_version: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="OS version; 'unknown' for none"),
    vendor: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Hardware vendor; 'unknown' for none"),
    model: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Hardware model; 'unknown' for none"),
    site: Optional[str] = Query(None, max_length=iq.MAX_TEXT),
    tag: list[str] = Query(default=[], description="Repeatable; the asset must carry every tag"),
    source: Optional[str] = Query(None, max_length=32, description="agent, snmp, wmi, winrm, ssh, controller or icmp"),
    freshness: Optional[Literal["fresh", "aging", "stale", "never", "not_applicable", "stale_or_never"]] = Query(None),
    health: Optional[Literal[
        "missing_os_version", "missing_model", "missing_serial", "unknown_vendor",
        "stale", "never", "software_truncated", "missing_security_agent"]] = Query(None),
    product: Optional[str] = Query(None, max_length=160, description="A software product key: assets that have it installed"),
    product_version: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="With product: only this exact version"),
    cpu_model: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Servers by CPU model; 'unknown' for none"),
    memory: Optional[Literal["le8", "le16", "le32", "le64", "le128", "gt128", "unknown"]] = Query(None),
    disks: Optional[Literal["1", "2", "3-4", "5-8", "ge9", "unknown"]] = Query(None),
    virtualization: Optional[str] = Query(None, max_length=32, description="Servers by virtualisation; 'unknown' for none"),
    security_agent: Optional[Literal["present", "missing", "unknown"]] = Query(
        None, description="Endpoint protection of a server: present (an endpoint-protection title or a running Defender "
                          "service), missing (a complete, normalised list shows none) or unknown (no list, a cut list or "
                          "rows not normalised yet)"),
    module_class: Optional[Literal["chassis", "backplane", "module", "powerSupply", "fan", "cpu", "stack"]] = Query(
        None, description="Network devices that have an ENTITY-MIB component of this class"),
    module_model: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="With or without module_class: the component model; 'unknown' for none"),
) -> iq.AssetFilters:
    """The asset filter vocabulary (one set of names for the list, the facets and the export)."""
    return iq.AssetFilters(
        q=q, kind=kind, os_family=os_family, os_product=os_product, os_release=os_release, os_version=os_version,
        vendor=vendor, model=model, site=site, tag=tuple(tag), source=source, freshness=freshness, health=health,
        product=product, product_version=product_version, cpu_model=cpu_model, memory=memory, disks=disks,
        virtualization=virtualization, security_agent=security_agent,
        module_class=module_class, module_model=module_model,
    )


@router.get("/summary", response_model=schemas.SummaryResponse)
async def get_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Overview figures for the caller's visible assets. Cached for 60 s per scope."""
    scope = await scoping.visible_tags(db, user)
    return await iq.summary(db, scope=scope)


@router.get("/assets", response_model=schemas.AssetList)
async def list_assets(
    filters: iq.AssetFilters = Depends(asset_filters),
    sort: str = Query("name", pattern=_SORT_PATTERN),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1, le=iq.MAX_PAGE),
    page_size: int = Query(iq.DEFAULT_PAGE_SIZE, ge=1, le=iq.MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One page of the asset union (servers, plus devices that are not a server's SNMP side)."""
    scope = await scoping.visible_tags(db, user)
    try:
        return await iq.list_assets(db, filters, scope=scope, sort=sort, order=order, page=page, page_size=page_size)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


@router.get("/assets/facets", response_model=schemas.FacetsResponse)
async def get_asset_facets(
    filters: iq.AssetFilters = Depends(asset_filters),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Facet counts. Each facet counts over every active filter except its own."""
    scope = await scoping.visible_tags(db, user)
    try:
        return await iq.asset_facets(db, filters, scope=scope)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


@router.get("/assets/{kind}/{asset_id}", response_model=schemas.AssetDetail)
async def get_asset(
    kind: str,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The flyout payload. A malformed id, an unknown asset and an asset outside the caller's scope
    all answer the same 404, so the endpoint cannot be used to find out what exists."""
    scope = await scoping.visible_tags(db, user)
    detail = await iq.asset_detail(db, kind, asset_id, scope=scope)
    if detail is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return detail


@router.get("/os", response_model=schemas.OsResponse)
async def get_os(
    kind: Optional[Literal["server", "device"]] = Query(None, description="Limit to servers or network devices"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """OS family, product, release and version tree with asset counts and build currency."""
    scope = await scoping.visible_tags(db, user)
    return await iq.os_tree(db, scope=scope, kind=kind)


@router.get("/hardware", response_model=schemas.HardwareResponse)
async def get_hardware(
    vendor: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Vendor for the model x release matrix"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Network vendors and models, module groups and server hardware distributions."""
    scope = await scoping.visible_tags(db, user)
    return await iq.hardware(db, scope=scope, vendor=vendor)


@router.get("/changes", response_model=schemas.ChangesResponse)
async def get_changes(
    category: Optional[Literal["software", "os", "firmware", "hardware"]] = Query(None),
    action: Optional[Literal["added", "removed", "changed"]] = Query(None),
    kind: Optional[Literal["server", "device"]] = Query(None),
    asset: Optional[str] = Query(None, max_length=80, description="'<kind>:<uuid>'"),
    subject_key: Optional[str] = Query(None, max_length=160),
    q: Optional[str] = Query(None, max_length=iq.MAX_TEXT),
    label: Optional[Literal["upgrade", "downgrade"]] = Query(None),
    since: Optional[str] = Query(None, max_length=_INSTANT_MAX, description="ISO 8601 date-time. Default: seven days before now; between 1970 and 2100"),
    until: Optional[str] = Query(None, max_length=_INSTANT_MAX, description="ISO 8601 date-time. Exclusive; default: open-ended; between 1970 and 2100"),
    tz: Optional[str] = Query(None, max_length=64, description="IANA zone for the daily series; default: the appliance zone. "
                              "A well-formed zone this host cannot open answers in UTC (window.tz_fallback)"),
    page: int = Query(1, ge=1, le=iq.MAX_PAGE),
    page_size: int = Query(iq.DEFAULT_PAGE_SIZE, ge=1, le=iq.MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change events, newest first, with stats over the window, a per-day series, the time capture began
    (``history_start``) and the retention in days. A date outside 1970 to 2100 or a malformed zone is a 422."""
    scope = await scoping.visible_tags(db, user)
    try:
        params = iq.ChangeFilters(category=category, action=action, kind=kind, asset=asset, subject_key=subject_key,
                                  q=q, label=label, since=iq.parse_instant("since", since), until=iq.parse_instant("until", until))
        return await iq.changes(db, scope=scope, params=params, page=page, page_size=page_size, tz=tz)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


# ── Software catalog ─────────────────────────────────────────────────

def _catalog_order(sort: str, order: Optional[str]) -> str:
    """Counts first by default (most hosts, most versions), everything else A to Z."""
    return order or ("desc" if sort in ("hosts", "version_count") else "asc")


@router.get("/software", response_model=schemas.CatalogResponse)
async def list_software(
    q: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Words of the product key, product name or publisher"),
    category: list[str] = Query(default=[], description="Repeatable; a category named here is never hidden"),
    publisher: Optional[str] = Query(None, max_length=96, description="A publisher key from /software/publishers; 'unknown' for none"),
    hidden: Literal["exclude", "include"] = Query("exclude", description="exclude hides the default-hidden categories"),
    unmatched: bool = Query(False, description="Only products the normaliser could not place"),
    sort: str = Query(isq.DEFAULT_CATALOG_SORT, pattern=_CATALOG_SORT_PATTERN),
    order: Optional[Literal["asc", "desc"]] = Query(None, description="Default: desc for hosts and version_count, else asc"),
    page: int = Query(1, ge=1, le=iq.MAX_PAGE),
    page_size: int = Query(iq.DEFAULT_PAGE_SIZE, ge=1, le=iq.MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One page of the software catalog with each product's version distribution and the footer counts."""
    scope = await scoping.visible_tags(db, user)
    filters = isq.CatalogFilters(q=q, category=tuple(category), publisher=publisher, hidden=hidden, unmatched=unmatched)
    try:
        return await isq.catalog(db, filters, scope=scope, sort=sort, order=_catalog_order(sort, order), page=page, page_size=page_size)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


@router.get("/software/publishers", response_model=schemas.PublishersResponse)
async def list_software_publishers(
    q: Optional[str] = Query(None, max_length=iq.MAX_TEXT),
    category: list[str] = Query(default=[]),
    hidden: Literal["exclude", "include"] = Query("exclude"),
    unmatched: bool = Query(False),
    limit: int = Query(isq.PUBLISHER_LIMIT_DEFAULT, ge=1, le=isq.PUBLISHER_LIMIT_MAX),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Publisher facet: products, installs and hosts per publisher over the catalog's filters except publisher."""
    scope = await scoping.visible_tags(db, user)
    filters = isq.CatalogFilters(q=q, category=tuple(category), hidden=hidden, unmatched=unmatched)
    try:
        return await isq.publishers(db, filters, scope=scope, limit=limit)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


@router.get("/software/{product_key}", response_model=schemas.ProductResponse)
async def get_software_product(
    product_key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The product page. An unknown product and a product with no host in the caller's scope answer the same 404."""
    scope = await scoping.visible_tags(db, user)
    out = await isq.product(db, product_key, scope=scope)
    if out is None:
        raise HTTPException(status_code=404, detail=_PRODUCT_NOT_FOUND)
    return out


@router.get("/software/{product_key}/installations", response_model=schemas.InstallationsResponse)
async def list_software_installations(
    product_key: str,
    version: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="A version (every spelling of it: 7.2 lists 7.2.0 too); "
                                   "'__none__' for the rows that report no version"),
    q: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="Server name, package title or version"),
    sort: str = Query("asset_name", pattern=_INSTALL_SORT_PATTERN),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1, le=iq.MAX_PAGE),
    page_size: int = Query(iq.DEFAULT_PAGE_SIZE, ge=1, le=iq.MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Where the product is installed, one row per software row. 404 as for the product page."""
    scope = await scoping.visible_tags(db, user)
    try:
        out = await isq.installations(db, product_key, scope=scope, version=version, q=q, sort=sort, order=order,
                                      page=page, page_size=page_size)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)
    if out is None:
        raise HTTPException(status_code=404, detail=_PRODUCT_NOT_FOUND)
    return out


# ── Trends and settings ──────────────────────────────────────────────

@router.get("/trends", response_model=schemas.TrendsResponse)
async def get_trends(
    metric: Literal["assets", "os_release", "software_products", "coverage", "health", "changes"] = Query(...),
    dimension: Optional[str] = Query(None, max_length=255, description="Exact dimension of the metric; default: all of them"),
    days: int = Query(its.DEFAULT_TREND_DAYS, ge=its.MIN_TREND_DAYS, le=its.MAX_TREND_DAYS),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Daily snapshot series. The snapshots cover the whole estate, so a restricted caller gets restricted=true and no series."""
    scope = await scoping.visible_tags(db, user)
    try:
        return await its.trends(db, scope=scope, metric=metric, dimension=dimension, days=days)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)


@router.get("/settings", response_model=schemas.SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    """The module settings in force, the defaults, the limits and the category vocabulary."""
    return await its.read_settings(db)


@router.put("/settings", response_model=schemas.SettingsResponse)
async def put_settings(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("inventory.manage")),
):
    """Change settings (keys left out keep their value). Unknown keys and wrong types are 422; out-of-range numbers
    are clamped and the response shows what was stored."""
    try:
        return await its.update_settings(db, user, payload)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)
    except AuditUnavailable:
        raise HTTPException(status_code=503, detail=_AUDIT_UNAVAILABLE)


# ── Export ───────────────────────────────────────────────────────────

@router.get("/export")
async def export_csv(
    dataset: Literal["assets", "software", "installations", "os", "hardware", "changes"] = Query(...),
    filters: iq.AssetFilters = Depends(asset_filters),
    category: list[str] = Query(default=[], description="software: product categories; changes: software, os, firmware or hardware"),
    publisher: Optional[str] = Query(None, max_length=96),
    hidden: Literal["exclude", "include"] = Query("exclude"),
    unmatched: bool = Query(False),
    version: Optional[str] = Query(None, max_length=iq.MAX_TEXT, description="installations: a version (every spelling of it), or '__none__' for rows that report none"),
    action: Optional[Literal["added", "removed", "changed"]] = Query(None),
    asset: Optional[str] = Query(None, max_length=80),
    subject_key: Optional[str] = Query(None, max_length=160),
    label: Optional[Literal["upgrade", "downgrade"]] = Query(None),
    since: Optional[str] = Query(None, max_length=_INSTANT_MAX, description="changes: ISO 8601 date-time, between 1970 and 2100"),
    until: Optional[str] = Query(None, max_length=_INSTANT_MAX, description="changes: ISO 8601 date-time, between 1970 and 2100"),
    sort: Optional[str] = Query(None, max_length=32, description="The dataset's own sort key; default: name / asset_name / newest first"),
    order: Optional[Literal["asc", "desc"]] = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("inventory.export")),
):
    """A dataset as CSV (UTF-8 with BOM, CRLF), at most 200,000 rows, with the filters of the matching list endpoint.

    assets: the asset filters. software: q, category (repeatable), publisher, hidden, unmatched. installations:
    product (required), version, q. os: kind. hardware: vendor. changes: category, action, kind, asset, subject_key,
    q, label, since, until (between 1970 and 2100, else 422). The caller's tag scope applies as on the lists.

    The export is audit-logged before the first byte; when the entry cannot be stored the answer is 503 and nothing is
    sent. At most two exports run at once per API worker process and one per user: the rest get 429 with Retry-After.
    """
    # A malformed date is the caller's mistake and is answered before the export asks for a slot (BN-08).
    try:
        change_since, change_until = iq.parse_instant("since", since), iq.parse_instant("until", until)
    except iq.InvalidFilter as exc:
        raise _unprocessable(exc)
    # Admission first: planning counts the whole selection, and a running export holds a connection and a snapshot.
    try:
        slot = export.limiter.acquire(user.id)
    except export.ExportBusy as busy:
        raise HTTPException(status_code=429, detail=str(busy), headers={"Retry-After": str(busy.retry_after)})
    try:
        scope = await scoping.visible_tags(db, user)
        if dataset == "changes":
            if len(category) > 1:
                raise HTTPException(status_code=422, detail="category takes one value for dataset=changes")
            change_category = category[0] if category else None
        else:
            change_category = None
        params = export.ExportParams(
            dataset=dataset,
            assets=filters,
            software=isq.CatalogFilters(q=filters.q, category=tuple(category) if dataset == "software" else (),
                                        publisher=publisher, hidden=hidden, unmatched=unmatched),
            changes=iq.ChangeFilters(category=change_category, action=action, kind=filters.kind, asset=asset,
                                     subject_key=subject_key, q=filters.q, label=label, since=change_since, until=change_until),
            version=version, sort=sort, order=order,
        )
        try:
            plan = await export.plan_export(db, params, scope=scope)
        except iq.InvalidFilter as exc:
            raise _unprocessable(exc)
        except export.NotFound:
            raise HTTPException(status_code=404, detail=_PRODUCT_NOT_FOUND)
        # Audited before the first byte is sent: the grant of the data is the event, whether or not the client reads on.
        # Fail closed (F1): when the entry cannot be stored nothing is sent.
        try:
            await write_audit_log(db, actor=user, action=export.ACTION, resource_type="inventory_export", resource_id=dataset,
                                  metadata={"dataset": dataset, "filters": plan.filters, "rows": plan.rows, "truncated": plan.truncated},
                                  required=True)
        except AuditUnavailable:
            await db.rollback()
            raise HTTPException(status_code=503, detail=_AUDIT_UNAVAILABLE)
        await db.commit()
        run = export.ExportRun(db.bind, plan, release=slot.release)       # from here the producer owns the slot
    except BaseException:
        slot.release()
        raise
    headers = {
        "Content-Disposition": f'attachment; filename="{plan.filename}"',
        "Cache-Control": "no-store",
        "X-Export-Rows": str(plan.rows),
    }
    if plan.truncated:
        headers["X-Export-Truncated"] = "true"
    return StreamingResponse(run.chunks(), media_type="text/csv; charset=utf-8", headers=headers)
