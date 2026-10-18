from uuid import UUID
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow import access as nf_access
from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.schemas.device import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceSummary, DeviceGroupResponse,
    BulkImportRequest, BulkImportResult,
    DeviceMaintenanceCreate, DeviceMaintenanceResponse,
)
from app.schemas.metric import MetricResponse, StatusChangeEvent
from app.services import device_service, metric_service
from app.services import device_status_explanation as explain
from app.services import device_status_machine as dsm
from app.services.audit_service import write_audit_log
from app.api.v1.snmp import (
    _snmpget,
    _snmpget_detail,
    _ping_once,
    SYS_DESCR_OID,
    SYS_OBJECT_OID,
    SYS_NAME_OID,
)
from app.core.crypto import decrypt, decrypt_secret
from app.services.ping_rollups import ping_table_for_hours, uptime_agg_sql
from sqlalchemy import text
import time
import asyncio
import json
import re

router = APIRouter(prefix="/devices", tags=["Devices"])


async def _device_in_scope(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """404 for a device outside the caller's tag scope, on every
    ``/devices/{device_id}/...`` route (reads and writes alike), the same
    answer GET /devices/{id} gives -- a scoped operator must not read or
    change another team's device by id (interface speeds feed that team's
    utilization, interface alerts and NetFlow capacity)."""
    scope = await scoping.visible_tags(db, user)
    if scope is None:
        return
    row = (await db.execute(text("SELECT tags FROM devices WHERE id = :id"), {"id": device_id})).first()
    if row is None or not scoping.entity_visible(row[0], scope):
        raise HTTPException(status_code=404, detail="Device not found")


async def _ids_in_scope(db: AsyncSession, user: User, ids) -> None:
    """404 unless every device id is visible to the caller (bulk writes)."""
    scope = await scoping.visible_tags(db, user)
    if scope is None or not ids:
        return
    rows = (await db.execute(text("SELECT id::text, tags FROM devices WHERE id = ANY(CAST(:ids AS uuid[]))"),
                             {"ids": [str(i) for i in ids]})).all()
    seen = {r[0]: r[1] for r in rows}
    if any(str(i) not in seen or not scoping.entity_visible(seen[str(i)], scope) for i in ids):
        raise HTTPException(status_code=404, detail="Device not found")

SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"


async def _device_snmp_settings(db: AsyncSession, device) -> dict:
    """Resolve effective SNMP settings for a device, including saved credential
    overrides. Decrypts any v3 passphrases. Returns a dict ready for _snmpget."""
    out = {
        "version": device.snmp_version or "2c",
        "port": device.snmp_port or 161,
        "timeout_ms": device.snmp_timeout_ms or 2000,
        "community": decrypt_secret(device.snmp_community),
        "v3_username": device.snmp_v3_username,
        "v3_context": device.snmp_v3_context,
        "v3_auth_protocol": device.snmp_auth_protocol,
        "v3_auth_passphrase": None,
        "v3_priv_protocol": device.snmp_priv_protocol,
        "v3_priv_passphrase": None,
        "v3_security_level": None,
    }
    try:
        if device.snmp_auth_passphrase:
            out["v3_auth_passphrase"] = decrypt_secret(device.snmp_auth_passphrase)
        if device.snmp_priv_passphrase:
            out["v3_priv_passphrase"] = decrypt_secret(device.snmp_priv_passphrase)
    except Exception:
        pass

    # If a saved credential is linked, its values take precedence.
    if device.snmp_credential_id:
        row = (await db.execute(
            text("""SELECT snmp_version, community, port, timeout_ms,
                           v3_username, v3_context, v3_security_level,
                           v3_auth_protocol, v3_auth_passphrase,
                           v3_priv_protocol, v3_priv_passphrase
                    FROM snmp_credentials WHERE id = :id"""),
            {"id": device.snmp_credential_id},
        )).mappings().first()
        if row:
            out["version"] = row["snmp_version"] or out["version"]
            out["port"] = row["port"] or out["port"]
            out["timeout_ms"] = row["timeout_ms"] or out["timeout_ms"]
            out["community"] = decrypt_secret(row["community"]) or out["community"]
            out["v3_username"] = row["v3_username"] or out["v3_username"]
            out["v3_context"] = row["v3_context"] or out["v3_context"]
            out["v3_security_level"] = row["v3_security_level"] or out["v3_security_level"]
            out["v3_auth_protocol"] = row["v3_auth_protocol"] or out["v3_auth_protocol"]
            out["v3_priv_protocol"] = row["v3_priv_protocol"] or out["v3_priv_protocol"]
            try:
                if row["v3_auth_passphrase"]:
                    out["v3_auth_passphrase"] = decrypt_secret(row["v3_auth_passphrase"])
                if row["v3_priv_passphrase"]:
                    out["v3_priv_passphrase"] = decrypt_secret(row["v3_priv_passphrase"])
            except Exception:
                pass
    return out


@router.get("", response_model=dict)
async def list_devices(
    status: str | None = None,
    group_id: UUID | None = None,
    device_type: str | None = None,
    location: str | None = None,
    search: str | None = None,
    tags: str | None = Query(default=None, description="Comma-separated tag names"),
    tags_match: str = Query(default="any", pattern="^(any|all)$"),
    managed_by: UUID | None = Query(default=None, description="Only children of this controller"),
    vendor: str | None = Query(default=None, max_length=200, description="Exact vendor, case-insensitive; 'unknown' for none"),
    model: str | None = Query(default=None, max_length=200, description="Exact model, case-insensitive; 'unknown' for none"),
    os_version: str | None = Query(default=None, max_length=200,
                                   description="Exact displayed OS version (os_version_display), case-insensitive; 'unknown' for none"),
    facets: bool = Query(default=False, description="Also return vendor, model and os_version menus with counts over ALL devices"),
    flapping: bool | None = Query(default=None,
                                  description="Only devices flagged as flapping (true) or not flapping (false); combines with status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The devices the caller may see, 50 per page (at most 200), ordered by hostname.

    Identity filters ``vendor``, ``model`` and ``os_version`` (Inventory & Compliance). They are exact, case-insensitive,
    trimmed (``%`` and ``_`` are literal), narrow what the tag scope already allows and are ignored when blank.
    ``unknown`` (any case) means the device has NO value (NULL, blank or the text "unknown"), the meaning the Inventory API
    gives ``vendor=unknown``, ``model=unknown`` and ``os_version=unknown``; a bare blank value is "not set" and filters
    nothing. ``vendor`` and ``model`` are the device's own columns (``vendor``/``model`` of each item). ``os_version``
    matches ``os_version_display`` of each item: the normalised version of the Inventory identity where it has one ("7.2.5"),
    else the device's own ``os_version``; the item's legacy ``os_version`` is unchanged.

    ``facets=true`` adds ``facets``: ``{vendor, model, os_version}``, each ``{items: [{value, label, count}], distinct}`` over
    ALL the devices the caller may see, not the page. Each facet counts over every active filter except its own, so a
    ``count`` is the ``meta.total`` this list gives when its ``value`` is passed as that filter (``unknown`` for "Unknown").
    Without the flag nothing is computed.

    ``flapping`` filters on the flapping flag, an overlay beside the status (``status=down&flapping=true`` is the down
    devices that are also flapping); the facets honour it like every other filter.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    visible = await scoping.visible_tags(db, user)
    devices, total = await device_service.get_devices(
        db, status, group_id, device_type, location, search, skip, limit,
        tags=tag_list, tags_match=tags_match, managed_by=managed_by,
        visible_tags=visible,
        vendor=vendor, model=model, os_version=os_version, flapping=flapping,
    )
    profile_names = await _profile_name_map(db)
    parent_names, child_counts = await _managed_maps(db, devices)
    os_display = await device_service.get_os_version_display(db, devices)
    data = []
    for d in devices:
        item = jsonable_encoder(_device_to_response(d, profile_names, parent_names, child_counts))
        item["os_version_display"] = os_display.get(d.id)
        data.append(item)
    out: dict = {"data": data, "meta": {"total": total, "skip": skip, "limit": limit}}
    if facets:
        out["facets"] = await device_service.get_device_facets(
            db, status, group_id, device_type, location, search, tag_list, tags_match, managed_by, visible,
            vendor, model, os_version, flapping=flapping)
    return out


@router.get("/summary", response_model=DeviceSummary)
async def device_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_device_summary(
        db, visible_tags=await scoping.visible_tags(db, user))




def _ping_uptime_sql(table: str, where: str) -> str:
    """`SELECT device_id, up_samples, total_samples` over a ping table.

    Counting `is_up` against the 5m/1h rollups raises UNKNOWN_IDENTIFIER — the
    column does not exist there — which the callers below swallow as "no data"
    and silently retry on raw `ping_metrics`. Every long window was therefore
    answered from raw retention instead of the rollup history it asked for.
    See app.services.ping_rollups for the column shapes.
    """
    return f"""
        SELECT device_id, {uptime_agg_sql(table)}
        FROM zenplus.{table}
        WHERE {where}
        GROUP BY device_id
    """


@router.get("/dashboard/uptime-stats")
async def dashboard_uptime_stats(
    hours: int = Query(default=24, ge=1, le=8760),
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    device_id: Optional[UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get uptime percentages per device over a time range from ClickHouse metrics.

    Explicit `from`/`to` bound the window exactly (so a historical range is not
    answered with one ending now); otherwise it is the last `hours`.
    ``device_id`` narrows the same calculation to one device (the device page
    reads its headline this way instead of the whole fleet).
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    def naive_utc(ts: datetime) -> datetime:
        return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts

    to_time = naive_utc(to) if to else datetime.now(timezone.utc).replace(tzinfo=None)
    from_time = naive_utc(from_) if from_ else to_time - timedelta(hours=hours)
    if from_time >= to_time:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")
    span_hours = (to_time - from_time).total_seconds() / 3600

    # Tag visibility scope, as on the device list: a scoped user only gets
    # uptime for devices they can see; unscoped users are unrestricted.
    where = "timestamp >= %(from)s AND timestamp <= %(to)s"
    params: dict = {"from": from_time, "to": to_time}
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        visible = (await db.execute(
            text(f"SELECT d.id::text FROM devices d WHERE {scoping.jsonb_tags_visible('d.tags')}"),
            {scoping.SCOPE_PARAM: scope},
        )).scalars().all()
        params["ids"] = [str(i) for i in visible]
        where += " AND toString(device_id) IN %(ids)s"
    if device_id is not None:
        params["device"] = str(device_id)
        where += " AND toString(device_id) = %(device)s"

    # Coarsest-but-longest-lived table that covers the window, then finer ones
    # as a fallback for installs whose rollups have not caught up yet.
    preferred = ping_table_for_hours(span_hours)
    tables_to_try = [preferred] + [
        t for t in ("ping_metrics_5m", "ping_metrics") if t != preferred
    ]

    # (up, down, total) per device from the chosen table, in *samples*.
    counts: dict[str, list[float]] = {}
    chosen_table: str | None = None
    for table in tables_to_try:
        if scope is not None and not params["ids"]:
            break
        try:
            result = client.query(_ping_uptime_sql(table, where), parameters=params)
            if len(result.result_rows) > 0:
                for row in result.result_rows:
                    up, total = float(row[1] or 0), float(row[2] or 0)
                    counts[str(row[0])] = [up, max(0.0, total - up), total]
                chosen_table = table
                break
        except Exception:
            continue

    # Maintenance-aware SLA: samples that fall inside a device_maintenance
    # window are excluded from numerator AND denominator, so planned downtime
    # never dents availability. Windows are clamped to the query range.
    if counts and chosen_table:
        try:
            win_rows = (await db.execute(
                text(f"""
                    SELECT d.id AS device_id,
                           GREATEST(m.starts_at, :from_ts) AS s,
                           LEAST(m.ends_at, :to_ts) AS e
                    FROM device_maintenance m
                    JOIN devices d ON {MAINT_COVERS_DEVICE_SQL}
                    WHERE m.starts_at < :to_ts AND m.ends_at > :from_ts
                      {"AND d.id = CAST(:one_device AS uuid)" if device_id is not None else ""}
                    LIMIT 500
                """),
                {"from_ts": from_time.replace(tzinfo=timezone.utc),
                 "to_ts": to_time.replace(tzinfo=timezone.utc),
                 **({"one_device": str(device_id)} if device_id is not None else {})},
            )).all()
        except Exception:
            win_rows = []
        if win_rows:
            conds, params = [], {}
            for i, r in enumerate(win_rows):
                conds.append(
                    f"(device_id = %(d{i})s AND timestamp >= %(s{i})s AND timestamp <= %(e{i})s)"
                )
                params[f"d{i}"] = str(r.device_id)
                params[f"s{i}"] = r.s.astimezone(timezone.utc).replace(tzinfo=None)
                params[f"e{i}"] = r.e.astimezone(timezone.utc).replace(tzinfo=None)
            try:
                mres = client.query(
                    _ping_uptime_sql(chosen_table, " OR ".join(conds)),
                    parameters=params,
                )
                for row in mres.result_rows:
                    did = str(row[0])
                    if did in counts:
                        up, total = float(row[1] or 0), float(row[2] or 0)
                        counts[did][0] = max(0.0, counts[did][0] - up)
                        counts[did][1] = max(0.0, counts[did][1] - (total - up))
                        counts[did][2] = max(0.0, counts[did][2] - total)
            except Exception:
                pass

    uptime_map: dict[str, float] = {}
    failed_map: dict[str, int] = {}
    for did, (up, down, total) in counts.items():
        # A device whose entire window was maintenance has no SLA-relevant
        # samples — omit it so the UI shows "—" rather than 0% or 100%.
        if total <= 0:
            continue
        uptime_map[did] = round(up / total * 100, 2)
        failed_map[did] = int(round(down))

    return {
        "hours": hours,
        "from": from_time.isoformat(),
        "to": to_time.isoformat(),
        "devices": uptime_map,
        "failed_checks": failed_map,
        # Which resolution actually answered — a caller asking for 30 days off
        # the 5-minute rollup is looking at a shorter window than it thinks.
        "source_table": chosen_table,
    }


# Statuses in which a device answers pings: continuous uptime runs through
# them, so a degraded<->up change never resets it.
_RESPONDING_SQL = "('up', 'degraded')"


def _uptime_origins() -> dict[str, tuple]:
    """device_id -> (last_back, n_back, last_lost, n_lost) from the status log: the last
    transition INTO up/degraded from any other status (down, unknown, maintenance) and the
    last transition into a non-responding status, with their counts."""
    from app.core.database import get_clickhouse_client
    back = f"new_status IN {_RESPONDING_SQL} AND old_status NOT IN {_RESPONDING_SQL}"
    lost = f"new_status NOT IN {_RESPONDING_SQL}"
    rows = get_clickhouse_client().query(f"""
        SELECT device_id,
               maxIf(timestamp, {back}) AS last_back, countIf({back}) AS n_back,
               maxIf(timestamp, {lost}) AS last_lost, countIf({lost}) AS n_lost
        FROM zenplus.device_status_log
        GROUP BY device_id
    """).result_rows
    return {str(r[0]): (r[1], int(r[2] or 0), r[3], int(r[4] or 0)) for r in rows}


def _aware(ts: datetime) -> datetime:
    # ClickHouse (and query parameters without an offset) give naive datetimes: UTC.
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


@router.get("/current-uptime")
async def current_uptime(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Current continuous uptime (seconds) of every responding (up or degraded) device the caller may see.

    The origin is the device's last status-log transition INTO up or degraded from a status in
    which it did not respond (down, unknown, maintenance): an up<->degraded change does not reset
    it. A responding device whose log holds no non-responding status at all has responded since it
    was added (or since before the log's retention): its origin is ``created_at``. A device whose
    log ends in a non-responding status although its current status is responding (a lost log
    write) gets no value, and neither does any device while the log cannot be read: not measured,
    never a guess.
    """
    now = datetime.now(timezone.utc)
    try:
        log = await asyncio.to_thread(_uptime_origins)
    except Exception:
        return {"generated_at": now.isoformat(), "devices": {}}

    where, params = f"status IN {_RESPONDING_SQL}", {}
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        where += f" AND {scoping.jsonb_tags_visible('tags')}"
        params[scoping.SCOPE_PARAM] = scope
    rows = (await db.execute(text(f"SELECT id::text, created_at FROM devices WHERE {where}"), params)).all()

    uptime: dict[str, float] = {}
    for did, created_at in rows:
        last_back, n_back, last_lost, n_lost = log.get(did, (None, 0, None, 0))
        if n_lost == 0:
            origin = last_back if n_back else created_at
        elif n_back and _aware(last_back) >= _aware(last_lost):
            origin = last_back
        else:
            continue
        if origin is None:
            continue
        delta = (now - _aware(origin)).total_seconds()
        if delta > 0:
            uptime[did] = delta

    return {"generated_at": now.isoformat(), "devices": uptime}


@router.get("/current-metrics")
async def current_metrics(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Latest scalar SNMP metric values per device (cpu, memory, ...).

    Returns `{"devices": {device_id: {metric_key: value, ...}}}` where each
    value is the most recent point from `zenplus.snmp_metrics` within the
    last 15 minutes. Used by the devices list to show real CPU/memory
    instead of synthesizing them on the client.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    try:
        res = client.query(
            """
            SELECT device_id, metric_key,
                   argMax(value, timestamp) AS val,
                   max(timestamp) AS ts
            FROM zenplus.snmp_metrics
            WHERE timestamp >= now() - INTERVAL 15 MINUTE
            GROUP BY device_id, metric_key
            """
        )
    except Exception:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "devices": {}}

    out: dict[str, dict] = {}
    latest_ts: dict[str, datetime] = {}
    for row in res.result_rows:
        did = str(row[0])
        key = row[1]
        val = float(row[2])
        ts = row[3]
        bucket = out.setdefault(did, {})
        bucket[key] = val
        if did not in latest_ts or ts > latest_ts[did]:
            latest_ts[did] = ts
    for did, ts in latest_ts.items():
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out[did]["_ts"] = ts.isoformat()

    return {"generated_at": datetime.now(timezone.utc).isoformat(), "devices": out}


@router.get("/groups", response_model=list[DeviceGroupResponse])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    groups = await device_service.get_device_groups(db)
    return groups


@router.get("/locations")
async def list_locations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_distinct_locations(db)


@router.get("/device-types")
async def list_device_types(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await device_service.get_distinct_device_types(db)


@router.post("/bulk-delete")
async def bulk_delete_devices(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    ids = data.get("device_ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No device IDs provided")
    try:
        uuids = [UUID(str(i)) for i in ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device id") from None
    await _ids_in_scope(db, user, uuids)
    deleted = await device_service.bulk_delete_devices(db, uuids)
    return {"deleted": deleted}


class BulkTagRequest(BaseModel):
    device_ids: list[UUID]
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@router.post("/bulk-tag")
async def bulk_tag_devices(
    data: BulkTagRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    if not data.device_ids:
        raise HTTPException(status_code=400, detail="No device IDs provided")
    if not data.add and not data.remove:
        raise HTTPException(status_code=400, detail="Provide tags to add and/or remove")
    await _ids_in_scope(db, user, data.device_ids)
    if await scoping.visible_tags(db, user) is not None:
        # Each device's resulting tags must stay in the caller's scope and
        # take no other team's scope tag (see scoping.assert_tags_assignable).
        remove = {t.strip().lower() for t in data.remove if t and t.strip()}
        rows = (await db.execute(text("SELECT tags FROM devices WHERE id = ANY(CAST(:ids AS uuid[]))"),
                                 {"ids": [str(i) for i in data.device_ids]})).scalars().all()
        for tags in rows:
            current = list(tags or [])
            result = [t for t in current + list(data.add) if str(t).strip().lower() not in remove]
            await scoping.assert_tags_assignable(db, user, result, previous=current)
    updated = await device_service.bulk_tag_devices(db, data.device_ids, data.add, data.remove)
    return {"updated": updated}


@router.post("", response_model=DeviceResponse, status_code=201)
async def create_device(
    data: DeviceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    # A scoped operator may only create devices inside its own scope, and not
    # with a management address that is another team's (or an unowned) flow
    # exporter: ownership follows the management IP (NF-04).
    await scoping.assert_tags_assignable(db, user, data.tags)
    await nf_access.authorize_device_address(db, user, data.ip_address)
    device = await device_service.create_device(db, data, user.id)
    return _device_to_response(device)


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_devices(
    data: BulkImportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    if await scoping.visible_tags(db, user) is not None:
        for i, item in enumerate(data.devices):
            try:
                await scoping.assert_tags_assignable(db, user, item.tags)
                await nf_access.authorize_device_address(db, user, item.ip_address)
            except HTTPException as exc:
                raise HTTPException(status_code=exc.status_code, detail=f"Row {i + 1}: {exc.detail}") from None
    return await device_service.bulk_import_devices(db, data.devices, user.id)


@router.get("/export")
async def export_devices(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    devices = await device_service.export_devices(db)
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        devices = [d for d in devices if scoping.entity_visible(d.get("tags"), scope)]
    return JSONResponse(content={"devices": devices})


@router.get("/{device_id}/availability-events", dependencies=[Depends(_device_in_scope)])
async def get_device_availability_events(
    device_id: UUID,
    from_time: datetime = Query(..., alias="from"),
    to_time: datetime = Query(..., alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Confirmed episodes, using exactly the Availability report's semantics.

    Probe failures must never be inferred as incidents from chart buckets.
    Historical windows include incidents already open when the window starts.
    """
    device = await device_service.get_device(db, device_id)
    if not device or not scoping.entity_visible(device.tags, await scoping.visible_tags(db, user)):
        raise HTTPException(status_code=404, detail="Device not found")
    from app.services.report_data_service import _device_transitions, window_episodes
    from app.services.report_service import _fetch_device_maintenance_windows
    start = from_time.replace(tzinfo=timezone.utc) if from_time.tzinfo is None else from_time.astimezone(timezone.utc)
    end = to_time.replace(tzinfo=timezone.utc) if to_time.tzinfo is None else to_time.astimezone(timezone.utc)
    if end <= start or end - start > timedelta(days=366):
        raise HTTPException(status_code=422, detail="Select a period between zero and 366 days")
    try:
        maintenance = await _fetch_device_maintenance_windows(db, start, end, device_ids=[str(device_id)], strict=True)
        transitions = await asyncio.to_thread(_device_transitions, start, end, [str(device_id)])
        episodes = window_episodes(transitions, start, end, start, maintenance)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Confirmed outage history is temporarily unavailable") from exc
    return {"from": start.isoformat(), "to": end.isoformat(), "basis": "confirmed",
            "outages": len(episodes),
            "downtime_seconds": sum(e["in_window_s"] for e in episodes),
            "events": sorted(episodes, key=lambda e: e["clipped_start"], reverse=True)[:100],
            "events_truncated": len(episodes) > 100}


_STATE_COLUMNS_SQL = ", ".join(dsm.STATE_COLUMNS)


def _settings_document(value) -> dict:
    """A system_settings JSONB value as a dict (the driver may hand back text); anything else is empty."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


@router.get("/{device_id}/status-explanation")
async def get_device_status_explanation(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Why the device has its status, from the real policy and the owning writer's state.

    ``policy`` is the effective global 'monitoring' policy (derived recovery limits included);
    ``thresholds`` its degraded and recovery limits. For ping-polled devices, central or sensor
    owned: ``confirmation`` (the legacy keys, from the policy), ``state`` (confirmed status,
    pending progress, streaks and the degraded window), ``flapping`` and the owner's latest
    sample (``latest``) plus the most recent degraded transition (``recent_degraded``).
    """
    device = await device_service.get_device(db, device_id)
    if not device or not scoping.entity_visible(device.tags, await scoping.visible_tags(db, user)):
        raise HTTPException(status_code=404, detail="Device not found")
    row = (await db.execute(text("SELECT value, updated_at FROM system_settings WHERE key = 'monitoring'"))).mappings().first()
    settings = _settings_document(row["value"] if row else None)
    policy = dsm.policy_from_settings(settings)
    thresholds = explain.thresholds_for(policy)
    # Saving other settings (confirmation, flapping) moves updated_at but not the limits.
    thresholds_changed_at = explain.thresholds_changed_at(settings, row["updated_at"] if row else None)
    output = {"status": device.status, "mode": "ping", "thresholds": thresholds,
              # v1 has one global policy; per-device overrides would change this scope.
              "policy": {**dsm.effective_policy_dict(policy), "scope": "global"},
              "source": "Controller", "latest": None, "recent_degraded": None,
              "state": None, "flapping": None}
    if device.poll_mode == "via_controller":
        return dict(output, mode="managed")
    if not device.ping_enabled:
        return dict(output, mode="disabled")
    owner = (await db.execute(text("""SELECT o.owner_kind, o.sensor_id, s.name
        FROM device_polling_owner o LEFT JOIN sensors s ON s.id = o.sensor_id
        WHERE o.device_id = :id"""), {"id": device_id})).mappings().first()
    if not owner or (owner["owner_kind"] != "central" and not owner["sensor_id"]):
        return dict(output, mode="unassigned")
    import os
    central = owner["owner_kind"] == "central"
    source = "central" if central else "sensor"
    owner_key = dsm.OWNER_CENTRAL if central else str(owner["sensor_id"])
    poller_id = os.getenv("POLLER_ID", "poller-01") if central else str(owner["sensor_id"])
    interval = device.ping_interval or 60
    output["source"] = "Controller" if central else (owner["name"] or "Assigned sensor")
    output["confirmation"] = explain.confirmation_block(policy, source, interval)

    try:
        measured = await asyncio.to_thread(explain.evidence, device_id, poller_id, thresholds,
                                           thresholds_changed_at, interval)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Status measurements are temporarily unavailable") from exc
    output.update(measured)

    state_row = (await db.execute(text(f"SELECT {_STATE_COLUMNS_SQL} FROM device_status_state WHERE device_id = :id"),
                                  {"id": device_id})).mappings().first()
    state, seeded = explain.current_state(state_row, owner_key, device.status, device.status_changed_at)
    last_check = None
    if central and not seeded:
        # The poller rewrites an unchanged state row only every 10 minutes; each of its checks
        # is a ping sample and each answered one also moves devices.last_seen. (A sensor's row
        # is rewritten with every accepted batch, and its last_seen can come from a sample the
        # machine never took, so the row alone is the measure there.)
        latest = measured.get("latest")
        last_check = explain.newest(device.last_seen, latest["timestamp"] if latest else None)
    now = datetime.now(timezone.utc)
    output["state"] = explain.state_block(state, policy, source, interval, seeded, now, last_check_at=last_check)
    output["flapping"] = explain.flapping_block(device, state, policy, source, interval, now)
    return output


@router.get("/{device_id}/flap-episodes", dependencies=[Depends(_device_in_scope)])
async def get_device_flap_episodes(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Flapping episodes of one device that overlap ``[from, to]`` (default: the last 30 days), newest first,
    as ``{from, to, items, truncated}`` (``truncated``: more than ``limit`` episodes overlap).

    An episode with ``ended_at`` null is ongoing; ``duration_s`` runs to now for it. ``kind`` is the
    union of the classes that flapped during the episode (reachability | performance | both);
    ``end_reason`` is stable | disabled | maintenance | owner_change | reseed | not_polled.
    """
    row = (await db.execute(text("SELECT tags FROM devices WHERE id = :id"), {"id": device_id})).first()
    if row is None or not scoping.entity_visible(row[0], await scoping.visible_tags(db, user)):
        raise HTTPException(status_code=404, detail="Device not found")
    now = datetime.now(timezone.utc)
    end = _aware(to_time).astimezone(timezone.utc) if to_time else now
    start = _aware(from_time).astimezone(timezone.utc) if from_time else end - timedelta(days=30)
    if end <= start or end - start > timedelta(days=366):
        raise HTTPException(status_code=422, detail="Select a period between zero and 366 days")
    rows = (await db.execute(text("""
        SELECT id, started_at, ended_at, kind, start_changes, window_minutes, status_at_start,
               status_at_end, end_reason, start_notice, stop_notice
        FROM device_flap_episodes
        WHERE device_id = :id AND started_at <= :end AND (ended_at IS NULL OR ended_at >= :start)
        ORDER BY started_at DESC
        LIMIT :lim
    """), {"id": device_id, "start": start, "end": end, "lim": limit + 1})).mappings().all()
    items = [{
        "id": str(r["id"]),
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "ongoing": r["ended_at"] is None,
        "duration_s": max(0.0, ((r["ended_at"] or now) - r["started_at"]).total_seconds()),
        "kind": r["kind"],
        "start_changes": r["start_changes"],
        "window_minutes": r["window_minutes"],
        "status_at_start": r["status_at_start"],
        "status_at_end": r["status_at_end"],
        "end_reason": r["end_reason"],
        "start_notice": r["start_notice"],
        "stop_notice": r["stop_notice"],
    } for r in rows[:limit]]
    return {"from": start.isoformat(), "to": end.isoformat(), "items": items, "truncated": len(rows) > limit}


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not scoping.entity_visible(device.tags, await scoping.visible_tags(db, user)):
        # 404, not 403: out-of-scope ids must not be confirmable.
        raise HTTPException(status_code=404, detail="Device not found")
    parent_names, child_counts = await _managed_maps(db, [device])
    return _device_to_response(device, await _profile_name_map(db), parent_names, child_counts)


@router.put("/{device_id}", response_model=DeviceResponse, dependencies=[Depends(_device_in_scope)])
async def update_device(
    device_id: UUID,
    data: DeviceUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    current = await device_service.get_device(db, device_id)
    if not current:
        raise HTTPException(status_code=404, detail="Device not found")
    if data.tags is not None:
        await scoping.assert_tags_assignable(db, user, data.tags, previous=current.tags)
    if data.ip_address is not None and nf_access.canonical_ip(data.ip_address) != nf_access.canonical_ip(
            str(current.ip_address) if current.ip_address else None):
        await nf_access.authorize_device_address(db, user, data.ip_address)

    # Controller-reported children can be IP-less; direct polling needs one.
    new_ip = data.ip_address if data.ip_address is not None else (
        str(current.ip_address) if current.ip_address else None)
    wants_ping = data.ping_enabled if data.ping_enabled is not None else current.ping_enabled
    wants_snmp = data.snmp_enabled if data.snmp_enabled is not None else current.snmp_enabled
    if (wants_ping or wants_snmp) and not new_ip:
        raise HTTPException(
            status_code=400,
            detail="This device has no IP address (it is monitored via its "
                   "controller). Set an IP address before enabling direct polling.",
        )

    promote_was = bool(current.promote_managed)
    device = await device_service.update_device(db, device_id, data)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Turning promotion on materializes children immediately so the UI
    # doesn't wait a sweeper interval to show them.
    if data.promote_managed and not promote_was:
        from app.services.managed_device_service import sync_controller_now
        await sync_controller_now(device_id)

    parent_names, child_counts = await _managed_maps(db, [device])
    return _device_to_response(device, await _profile_name_map(db), parent_names, child_counts)


@router.delete("/{device_id}", status_code=204, dependencies=[Depends(_device_in_scope)])
async def delete_device(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    deleted = await device_service.delete_device(db, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Device not found")


@router.get("/{device_id}/metrics", response_model=MetricResponse, dependencies=[Depends(_device_in_scope)])
async def get_device_metrics(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="auto", pattern="^(raw|5m|1h|auto)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Verify device exists
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_device_metrics(device_id, from_time, to_time, granularity)


@router.get("/{device_id}/ping-series", dependencies=[Depends(_device_in_scope)])
async def get_device_ping_series(
    device_id: UUID,
    minutes: int = Query(default=60, ge=5, le=1440),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-device ping time-series for the manual-map inspector sparkline.

    Returns ~60 evenly-bucketed samples over the requested window.
    Uses 5-minute rollup for windows > 6 h, raw otherwise.
    """
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    bucket_seconds = max(30, (minutes * 60) // 60)
    table = "ping_metrics" if minutes <= 360 else "ping_metrics_5m"
    try:
        res = client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL %(bs)s SECOND) AS ts,
                   avg(rtt_ms)      AS rtt_ms,
                   avg(packet_loss) AS packet_loss,
                   max(is_up)       AS is_up
            FROM zenplus.{table}
            WHERE device_id = %(id)s
              AND timestamp BETWEEN %(start)s AND %(end)s
            GROUP BY ts ORDER BY ts
            """,
            parameters={"id": str(device_id), "bs": bucket_seconds, "start": start, "end": end},
        )
        points = [
            {
                "ts": r[0].isoformat() if r[0] else None,
                "rtt_ms": round(float(r[1] or 0.0), 2),
                "packet_loss": round(float(r[2] or 0.0), 3),
                "is_up": bool(r[3]) if r[3] is not None else None,
            }
            for r in res.result_rows
        ]
    except Exception:
        points = []

    return {
        "device_id": str(device_id),
        "minutes": minutes,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "points": points,
    }


@router.get("/{device_id}/interfaces", dependencies=[Depends(_device_in_scope)])
async def get_device_interfaces(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List SNMP-discovered interfaces for a device.

    ``last_up_at`` is the last time the poller saw the port up; ``ever_up`` is
    false for a port never seen up (an unused access port), the baseline the
    Interface-down alert uses to tell an unexpected outage from a spare port."""
    from sqlalchemy import text
    sql = """
            SELECT id, if_index, if_name, if_descr, if_alias, if_type, if_speed,
                   configured_speed_bps,
                   mac_address::text AS mac_address, admin_status, oper_status,
                   monitored, first_seen, last_seen, {last_up}
            FROM device_interfaces
            WHERE device_id = :id
            ORDER BY if_index
        """
    try:
        rows = (await db.execute(
            text(sql.format(last_up="last_up_at, last_up_at IS NOT NULL AS ever_up")), {"id": device_id},
        )).mappings().all()
    except Exception as exc:
        # Rolling upgrade: code ahead of migrate-126. Every port counts as seen up.
        if "last_up_at" not in str(exc):
            raise
        await db.rollback()
        rows = (await db.execute(
            text(sql.format(last_up="NULL::timestamptz AS last_up_at, TRUE AS ever_up")), {"id": device_id},
        )).mappings().all()
    return [dict(r) for r in rows]


# Declared before every /{device_id}/interfaces/{if_index}... route so the
# literal "error-totals" is never parsed as an ifIndex.
@router.get("/{device_id}/interfaces/error-totals", dependencies=[Depends(_device_in_scope)])
async def get_device_interface_error_totals(
    device_id: UUID,
    hours: int = Query(default=1, ge=1, le=720),
    user: User = Depends(get_current_user),
):
    """Errors and discards per interface over the last ``hours``.

    The counters are cumulative, so each total is the sum of the positive steps
    between consecutive polls of one interface (the get_interface_detail_metrics
    approach): a counter wrap or an agent restart reads as a backwards step and
    never counts. Returns ``{if_index: {in_errors, out_errors, in_discards,
    out_discards}}``; an interface without two samples in the window is absent
    (unknown, not zero). A ClickHouse failure is a 503, never zeros."""
    import logging
    from app.core.database import get_clickhouse_client

    def _delta(col: str, prev: str) -> str:
        return f"sum(if(rn > 1, greatest(toInt64({col}) - toInt64({prev}), 0), 0))"

    sql = f"""
        SELECT if_index,
               {_delta('in_errors', 'p_ie')} AS in_errors,
               {_delta('out_errors', 'p_oe')} AS out_errors,
               {_delta('in_discards', 'p_id')} AS in_discards,
               {_delta('out_discards', 'p_od')} AS out_discards,
               count() AS samples
        FROM (
            SELECT if_index, in_errors, out_errors, in_discards, out_discards,
                   lagInFrame(in_errors)    OVER w AS p_ie,
                   lagInFrame(out_errors)   OVER w AS p_oe,
                   lagInFrame(in_discards)  OVER w AS p_id,
                   lagInFrame(out_discards) OVER w AS p_od,
                   row_number() OVER w AS rn
            FROM zenplus.snmp_if_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            WINDOW w AS (PARTITION BY if_index ORDER BY timestamp)
        )
        GROUP BY if_index
        HAVING samples > 1
    """
    try:
        res = get_clickhouse_client().query(sql, parameters={"id": str(device_id), "hours": hours})
        rows = list(res.result_rows)
    except Exception:
        logging.getLogger(__name__).warning("interface error totals query failed for %s", device_id, exc_info=True)
        raise HTTPException(status_code=503, detail="Interface error counters are unavailable right now.")

    fields = ("in_errors", "out_errors", "in_discards", "out_discards")
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        totals = out.setdefault(int(r[0]), dict.fromkeys(fields, 0))
        for n, field in enumerate(fields, start=1):
            totals[field] += max(int(r[n] or 0), 0)
    return out


class InterfaceSpeedUpdate(BaseModel):
    """Set or clear manual speed override (bps). NULL clears override."""
    configured_speed_bps: Optional[int] = Field(
        None, ge=1, le=10_000_000_000_000,
        description="Line rate in bits per second; omit or null to use SNMP if_speed",
    )


class InterfaceBulkSpeedUpdate(BaseModel):
    if_indexes: list[int] = Field(..., min_length=1)
    configured_speed_bps: Optional[int] = Field(
        None, ge=1, le=10_000_000_000_000,
        description="Line rate in bps for all listed interfaces; null clears overrides",
    )


@router.patch("/{device_id}/interfaces/{if_index}", dependencies=[Depends(_device_in_scope)])
async def update_device_interface_speed(
    device_id: UUID,
    if_index: int,
    payload: InterfaceSpeedUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    """Set or clear manual speed/bandwidth for a single interface."""
    # The override is the utilization denominator for link utilization,
    # interface alert thresholds and NetFlow capacity, so every change is
    # audited with its previous value. The old value is read in the same
    # statement (the CTE sees the pre-update row) so the pair is consistent.
    row = (await db.execute(
        text("""
            WITH prev AS (
                SELECT configured_speed_bps FROM device_interfaces
                WHERE device_id = :device_id AND if_index = :if_index
            )
            UPDATE device_interfaces
            SET configured_speed_bps = :speed
            WHERE device_id = :device_id AND if_index = :if_index
            RETURNING id, if_index, if_name, if_descr, if_alias, if_type, if_speed,
                      configured_speed_bps, mac_address::text AS mac_address,
                      admin_status, oper_status, monitored, first_seen, last_seen,
                      (SELECT configured_speed_bps FROM prev) AS previous_speed_bps
        """),
        {
            "device_id": device_id,
            "if_index": if_index,
            "speed": payload.configured_speed_bps,
        },
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Interface not found")
    out = dict(row)
    previous = out.pop("previous_speed_bps")
    await write_audit_log(
        db, actor=user, action="device_interface.speed.update",
        resource_type="device_interface", resource_id=f"{device_id}:{if_index}",
        metadata={"device_id": str(device_id), "if_index": if_index,
                  "configured_speed_bps": {"old": previous, "new": payload.configured_speed_bps}},
    )
    await db.commit()
    return out


@router.post("/{device_id}/interfaces/bulk-speed", dependencies=[Depends(_device_in_scope)])
async def bulk_update_device_interface_speed(
    device_id: UUID,
    payload: InterfaceBulkSpeedUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    """Apply manual speed/bandwidth to multiple interfaces at once."""
    result = await db.execute(
        text("""
            WITH prev AS (
                SELECT if_index, configured_speed_bps FROM device_interfaces
                WHERE device_id = :device_id AND if_index = ANY(:indexes)
            )
            UPDATE device_interfaces di
            SET configured_speed_bps = :speed
            FROM prev
            WHERE di.device_id = :device_id AND di.if_index = prev.if_index
            RETURNING di.if_index, prev.configured_speed_bps AS previous_speed_bps
        """),
        {
            "device_id": device_id,
            "indexes": payload.if_indexes,
            "speed": payload.configured_speed_bps,
        },
    )
    rows = sorted(((int(r[0]), r[1]) for r in result.fetchall()), key=lambda t: t[0])
    updated = [idx for idx, _ in rows]
    if not updated:
        raise HTTPException(status_code=404, detail="No matching interfaces found")
    # One audit entry per interface, keyed "<device>:<ifIndex>" exactly like a
    # single change, with that interface's previous value: an interface's
    # speed history (NetFlow capacity trends) must see a bulk change too.
    for idx, old in rows:
        await write_audit_log(
            db, actor=user, action="device_interface.speed.bulk_update",
            resource_type="device_interface", resource_id=f"{device_id}:{idx}",
            metadata={"device_id": str(device_id), "if_index": idx, "bulk_size": len(updated),
                      "configured_speed_bps": {"old": old, "new": payload.configured_speed_bps}},
        )
    await db.commit()
    return {
        "updated": len(updated),
        "if_indexes": updated,
        "configured_speed_bps": payload.configured_speed_bps,
    }


@router.get("/{device_id}/entities", dependencies=[Depends(_device_in_scope)])
async def get_device_entities(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import text
    rows = (await db.execute(
        text("""
            SELECT id, ent_index, parent_index, class, name, serial_number,
                   model_name, hw_revision, fw_revision, first_seen, last_seen
            FROM device_entities WHERE device_id = :id ORDER BY ent_index
        """),
        {"id": device_id},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{device_id}/sensors", dependencies=[Depends(_device_in_scope)])
async def get_device_sensors(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import text
    rows = (await db.execute(
        text("""
            SELECT id, sensor_index, sensor_type, description, unit, monitored,
                   first_seen, last_seen
            FROM device_sensors WHERE device_id = :id ORDER BY sensor_index
        """),
        {"id": device_id},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{device_id}/snmp-metrics", dependencies=[Depends(_device_in_scope)])
async def get_device_snmp_metrics(
    device_id: UUID,
    hours: int = Query(default=24, ge=1, le=2160),
    offset_hours: int = Query(default=0, ge=0, le=2160,
                              description="Shift the window back: the previous period of a 24h window is offset_hours=24"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Scalar SNMP metrics (CPU, memory, temperature...) from ClickHouse.

    Returns a dict keyed by metric_key with an array of {ts, value} points.
    Uses 5m rollups for windows > 6h, raw otherwise. ``offset_hours`` moves
    the window back (``now - offset - hours`` to ``now - offset``) for the
    Performance tab's previous-period comparison; the bucket follows ``hours``.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Pick a table + bucket based on window. Some upgraded appliances have an
    # empty 5m SNMP rollup even though raw samples exist, so larger windows
    # fall back to on-the-fly raw aggregation when the rollup returns no rows.
    if hours <= 6:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   value AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(start_hours)s HOUR{upper}
            GROUP BY metric_key, timestamp, val
            ORDER BY metric_key, timestamp
        """]
    elif hours <= 168:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   avg(avg_value) AS val,
                   '' AS unit
            FROM zenplus.snmp_metrics_5m
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(start_hours)s HOUR{upper}
            GROUP BY metric_key, timestamp
            ORDER BY metric_key, timestamp
        """, """
            SELECT metric_key,
                   toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL 300 SECOND)) * 1000 AS ts_ms,
                   avg(value) AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(start_hours)s HOUR{upper}
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """]
    else:
        sql_candidates = ["""
            SELECT metric_key,
                   toUnixTimestamp(toStartOfHour(timestamp)) * 1000 AS ts_ms,
                   avg(avg_value) AS val,
                   '' AS unit
            FROM zenplus.snmp_metrics_5m
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(start_hours)s HOUR{upper}
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """, """
            SELECT metric_key,
                   toUnixTimestamp(toStartOfHour(timestamp)) * 1000 AS ts_ms,
                   avg(value) AS val,
                   any(unit) AS unit
            FROM zenplus.snmp_metrics
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(start_hours)s HOUR{upper}
            GROUP BY metric_key, ts_ms
            ORDER BY metric_key, ts_ms
        """]

    res = None
    last_error = None
    upper = " AND timestamp < now() - INTERVAL %(offset_hours)s HOUR" if offset_hours else ""
    sql_candidates = [sql.replace("{upper}", upper) for sql in sql_candidates]
    params = {"id": str(device_id), "hours": hours, "start_hours": hours + offset_hours,
              "offset_hours": offset_hours}
    for sql in sql_candidates:
        try:
            candidate = client.query(sql, parameters=params)
        except Exception as e:
            last_error = e
            continue
        if candidate.result_rows or res is None:
            res = candidate
        if candidate.result_rows:
            break
    if res is None:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {last_error}")

    out: dict[str, dict] = {}
    for r in res.result_rows:
        key = r[0]
        if key not in out:
            out[key] = {"unit": r[3] or "", "points": []}
        out[key]["points"].append({"ts": r[1], "value": float(r[2])})
    return out


@router.get("/{device_id}/snmp-if-metrics", dependencies=[Depends(_device_in_scope)])
async def get_device_snmp_if_metrics(
    device_id: UUID,
    hours: int = Query(default=1, ge=1, le=720),
    if_index: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-interface bps/errors time series from ClickHouse.

    Always bucketed from the raw `snmp_if_metrics` table (30-day TTL). Bucket
    width is chosen to keep response payloads reasonable while preserving
    enough resolution for the chart.
    """
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Bucket width: raw for short windows, then progressively coarser.
    if hours <= 6:
        bucket_seconds = 0  # no bucketing — return raw points
    elif hours <= 24:
        bucket_seconds = 300       # 5 minutes
    elif hours <= 24 * 7:
        bucket_seconds = 1800      # 30 minutes
    else:
        bucket_seconds = 3600 * 2  # 2 hours

    where = "device_id = %(id)s AND timestamp >= now() - INTERVAL %(hours)s HOUR"
    params: dict = {"id": str(device_id), "hours": hours}
    if if_index is not None:
        where += " AND if_index = %(if)s"
        params["if"] = if_index

    if bucket_seconds == 0:
        sql = f"""
            SELECT if_index,
                   toUnixTimestamp64Milli(timestamp) AS ts_ms,
                   in_bps,
                   out_bps
            FROM zenplus.snmp_if_metrics
            WHERE {where}
            ORDER BY if_index, timestamp
        """
    else:
        sql = f"""
            SELECT if_index,
                   toUnixTimestamp(
                     toStartOfInterval(timestamp, INTERVAL {bucket_seconds} SECOND)
                   ) * 1000 AS ts_ms,
                   avg(in_bps) AS in_bps,
                   avg(out_bps) AS out_bps
            FROM zenplus.snmp_if_metrics
            WHERE {where}
            GROUP BY if_index, ts_ms
            ORDER BY if_index, ts_ms
        """

    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    out: dict[int, list] = {}
    for r in res.result_rows:
        idx = int(r[0])
        out.setdefault(idx, []).append({
            "ts": r[1],
            "in_bps": float(r[2]),
            "out_bps": float(r[3]),
        })
    return out


@router.get("/{device_id}/interfaces/{if_index}/metrics", dependencies=[Depends(_device_in_scope)])
async def get_interface_detail_metrics(
    device_id: UUID,
    if_index: int,
    hours: int = Query(default=6, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Detailed per-interface metrics including traffic, errors, discards, packets."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()

    # Bucket from the raw 30-day-retention table; rollups are unreliable.
    #
    # in_errors/out_errors/in_discards/out_discards and the packet counters are
    # cumulative SNMP counters. Plotting the reading draws a rising staircase,
    # and summing readings across a bucket multiplies the running total by the
    # sample count — this endpoint reported error totals millions of times the
    # real value and disagreed with /link-utilization for the same interface.
    # Difference consecutive samples instead, keeping only the positive step so
    # an agent restart or counter wrap doesn't register as a burst of errors.
    lag_cols = """
                       lagInFrame(in_errors)     OVER w AS p_ie,
                       lagInFrame(out_errors)    OVER w AS p_oe,
                       lagInFrame(in_discards)   OVER w AS p_id,
                       lagInFrame(out_discards)  OVER w AS p_od,
                       lagInFrame(in_ucast_pkts) OVER w AS p_ip,
                       lagInFrame(out_ucast_pkts) OVER w AS p_op,
                       row_number() OVER w AS rn"""

    def _delta(col: str, prev: str) -> str:
        return f"if(rn > 1, greatest(toInt64({col}) - toInt64({prev}), 0), 0)"

    if hours <= 6:
        sql = f"""
            SELECT ts_ms, in_bps, out_bps,
                   {_delta('in_errors', 'p_ie')} AS in_errors,
                   {_delta('out_errors', 'p_oe')} AS out_errors,
                   {_delta('in_discards', 'p_id')} AS in_discards,
                   {_delta('out_discards', 'p_od')} AS out_discards,
                   {_delta('in_ucast_pkts', 'p_ip')} AS in_ucast_pkts,
                   {_delta('out_ucast_pkts', 'p_op')} AS out_ucast_pkts
            FROM (
                SELECT toUnixTimestamp64Milli(timestamp) AS ts_ms, timestamp,
                       in_bps, out_bps, in_errors, out_errors,
                       in_discards, out_discards, in_ucast_pkts, out_ucast_pkts,
                       {lag_cols}
                FROM zenplus.snmp_if_metrics
                WHERE device_id = %(id)s AND if_index = %(if)s
                  AND timestamp >= now() - INTERVAL %(hours)s HOUR
                WINDOW w AS (ORDER BY timestamp)
            )
            ORDER BY ts_ms
        """
    else:
        bucket_seconds = 300 if hours <= 24 else (1800 if hours <= 24 * 7 else 7200)
        sql = f"""
            SELECT toUnixTimestamp(
                     toStartOfInterval(timestamp, INTERVAL {bucket_seconds} SECOND)
                   ) * 1000 AS ts_ms,
                   avg(in_bps) AS in_bps,
                   avg(out_bps) AS out_bps,
                   sum({_delta('in_errors', 'p_ie')}) AS in_errors,
                   sum({_delta('out_errors', 'p_oe')}) AS out_errors,
                   sum({_delta('in_discards', 'p_id')}) AS in_discards,
                   sum({_delta('out_discards', 'p_od')}) AS out_discards,
                   sum({_delta('in_ucast_pkts', 'p_ip')}) AS in_ucast_pkts,
                   sum({_delta('out_ucast_pkts', 'p_op')}) AS out_ucast_pkts
            FROM (
                SELECT timestamp, in_bps, out_bps, in_errors, out_errors,
                       in_discards, out_discards, in_ucast_pkts, out_ucast_pkts,
                       {lag_cols}
                FROM zenplus.snmp_if_metrics
                WHERE device_id = %(id)s AND if_index = %(if)s
                  AND timestamp >= now() - INTERVAL %(hours)s HOUR
                WINDOW w AS (ORDER BY timestamp)
            )
            GROUP BY ts_ms
            ORDER BY ts_ms
        """

    params = {"id": str(device_id), "if": if_index, "hours": hours}

    try:
        res = client.query(sql, parameters=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    traffic = []
    errors = []
    for r in res.result_rows:
        traffic.append({
            "ts": r[0], "in_bps": float(r[1]), "out_bps": float(r[2]),
        })
        errors.append({
            "ts": r[0],
            "in_errors": int(r[3]), "out_errors": int(r[4]),
            "in_discards": int(r[5]), "out_discards": int(r[6]),
        })

    # Summary stats
    if traffic:
        in_vals = [p["in_bps"] for p in traffic]
        out_vals = [p["out_bps"] for p in traffic]
        err_total = sum(e["in_errors"] + e["out_errors"] for e in errors)
        disc_total = sum(e["in_discards"] + e["out_discards"] for e in errors)
        summary = {
            "in_avg_bps": sum(in_vals) / len(in_vals),
            "in_max_bps": max(in_vals),
            "in_current_bps": in_vals[-1],
            "out_avg_bps": sum(out_vals) / len(out_vals),
            "out_max_bps": max(out_vals),
            "out_current_bps": out_vals[-1],
            "total_errors": err_total,
            "total_discards": disc_total,
            "samples": len(traffic),
        }
    else:
        summary = {}

    return {"traffic": traffic, "errors": errors, "summary": summary}


@router.get("/{device_id}/traps", dependencies=[Depends(_device_in_scope)])
async def get_device_traps(
    device_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    hours: int = Query(default=24, ge=1, le=720),
    user: User = Depends(get_current_user),
):
    """Recent SNMP traps for this device, sourced from ClickHouse."""
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    try:
        res = client.query(
            """
            SELECT toString(source_ip), trap_oid, trap_name, severity, message,
                   bindings, toUnixTimestamp64Milli(timestamp)
            FROM zenplus.snmp_traps
            WHERE device_id = %(id)s
              AND timestamp >= now() - INTERVAL %(hours)s HOUR
            ORDER BY timestamp DESC
            LIMIT %(limit)s
            """,
            parameters={"id": str(device_id), "hours": hours, "limit": limit},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clickhouse query failed: {e}")

    return [
        {
            "source_ip": r[0],
            "trap_oid": r[1],
            "trap_name": r[2],
            "severity": r[3],
            "message": r[4],
            "bindings": r[5],
            "timestamp": datetime.fromtimestamp(r[6] / 1000, tz=timezone.utc).isoformat(),
        }
        for r in res.result_rows
    ]


async def _record_test_run(
    db: AsyncSession,
    device_id: UUID,
    kind: str,
    ok: bool,
    summary: str | None,
    detail: dict,
    user_id,
) -> None:
    """Persist the latest on-demand test for a device so every operator sees
    it, not just the browser that ran it. One row per (device, kind) — the UI
    only shows the most recent run, so we overwrite rather than append.

    Never lets a bookkeeping failure break the probe response the caller
    actually asked for.
    """
    import json
    try:
        await db.execute(
            text("""
                INSERT INTO device_test_runs (device_id, kind, ok, summary, detail, ran_at, ran_by)
                VALUES (:device_id, :kind, :ok, :summary, CAST(:detail AS JSONB), NOW(), :ran_by)
                ON CONFLICT (device_id, kind) DO UPDATE
                   SET ok = EXCLUDED.ok,
                       summary = EXCLUDED.summary,
                       detail = EXCLUDED.detail,
                       ran_at = EXCLUDED.ran_at,
                       ran_by = EXCLUDED.ran_by
            """),
            {
                "device_id": str(device_id),
                "kind": kind,
                "ok": ok,
                "summary": (summary or "")[:500] or None,
                "detail": json.dumps(detail, default=str),
                "ran_by": str(user_id) if user_id else None,
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()


async def _passive_snmp_evidence(device_id: UUID) -> dict | None:
    """When was this device last successfully polled over SNMP?

    A manual SNMP test proves the same thing the poller proves every cycle, so
    a device nobody has ever clicked "SNMP Test" on is not untested — it is
    continuously tested. Read the most recent write in either SNMP metrics
    table and report that as passive evidence.
    """
    # Thread-local client: this runs in the threadpool alongside the other
    # ClickHouse-backed device endpoints the page fires at the same time, and a
    # single clickhouse_connect session rejects concurrent queries.
    from app.core.database import get_ch_client
    sql = """
        SELECT max(ts) FROM (
            SELECT max(timestamp) AS ts FROM zenplus.snmp_if_metrics
             WHERE device_id = %(id)s AND timestamp >= now() - INTERVAL 7 DAY
            UNION ALL
            SELECT max(timestamp) AS ts FROM zenplus.snmp_metrics
             WHERE device_id = %(id)s AND timestamp >= now() - INTERVAL 7 DAY
        )
    """
    def _run():
        # Resolve the client *inside* the worker thread — get_ch_client is
        # thread-local, so calling it on the event loop would hand this query
        # the loop thread's client and reintroduce the sharing it avoids.
        return get_ch_client().query(sql, parameters={"id": str(device_id)})

    try:
        res = await asyncio.to_thread(_run)
    except Exception:
        return None
    if not res.result_rows:
        return None
    ts = res.result_rows[0][0]
    if not ts or getattr(ts, "year", 1970) <= 1970:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "at": ts.isoformat(),
        "ok": True,
        "summary": "polled by the SNMP collector",
        "source": "poller",
    }


@router.get("/{device_id}/test-runs", dependencies=[Depends(_device_in_scope)])
async def get_device_test_runs(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Last-known result for each device test, from both available sources.

    * `source: "manual"` — somebody pressed the button (shared across users).
    * `source: "poller"` — no manual run on record, but the background
      collector proves the same path works: recent SNMP samples in ClickHouse,
      or the ping poller's own `last_seen`/`last_rtt_ms` on the device row.

    The manual record wins only when it is newer than the passive evidence;
    otherwise a two-day-old click would misrepresent a device being polled
    right now.
    """
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    rows = (await db.execute(
        text("""
            SELECT r.kind, r.ok, r.summary, r.ran_at, u.username
              FROM device_test_runs r
              LEFT JOIN users u ON u.id = r.ran_by
             WHERE r.device_id = :device_id
        """),
        {"device_id": str(device_id)},
    )).all()

    manual: dict[str, dict] = {}
    for kind, ok, summary, ran_at, username in rows:
        if ran_at is not None and ran_at.tzinfo is None:
            ran_at = ran_at.replace(tzinfo=timezone.utc)
        manual[kind] = {
            "at": ran_at.isoformat() if ran_at else None,
            "ok": bool(ok),
            "summary": summary or "",
            "source": "manual",
            "by": username,
        }

    passive: dict[str, dict] = {}
    if device.snmp_enabled:
        snmp_evidence = await _passive_snmp_evidence(device_id)
        if snmp_evidence:
            passive["snmp"] = snmp_evidence
    if device.ping_enabled and device.last_seen:
        last_seen = device.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        rtt = device.last_rtt_ms
        passive["ping"] = {
            "at": last_seen.isoformat(),
            "ok": device.status != "down",
            "summary": (
                f"polled by the ICMP monitor · {rtt:.1f} ms" if rtt is not None
                else "polled by the ICMP monitor"
            ),
            "source": "poller",
        }

    now = datetime.now(timezone.utc)

    def _preferred(kind: str) -> dict | None:
        m, p = manual.get(kind), passive.get(kind)
        if not (m and m.get("at")):
            return p if p and p.get("at") else None
        if not (p and p.get("at")):
            return m
        # A test somebody just ran stays on screen for a while — otherwise the
        # result you asked for is replaced by "the poller polled it" seconds
        # later, and a *failed* manual probe would be the first thing hidden.
        # Past that window the freshest evidence is the honest one.
        if now - datetime.fromisoformat(m["at"]) <= timedelta(minutes=15):
            return m
        return max((m, p), key=lambda c: c["at"])

    return {
        "ping": _preferred("ping"),
        "snmp": _preferred("snmp"),
        # Both sources verbatim, so the UI can show "polling now" alongside
        # "last manual test" without a second request.
        "manual": manual,
        "passive": passive,
    }


@router.get("/{device_id}/status-history", response_model=list[StatusChangeEvent], dependencies=[Depends(_device_in_scope)])
async def get_status_history(
    device_id: UUID,
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return metric_service.get_status_history(device_id, from_time, to_time, limit)


@router.post("/{device_id}/ping-test", dependencies=[Depends(_device_in_scope)])
async def test_device_ping(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    """Run an on-demand ICMP probe (3 packets) and return whether the device
    replied, along with an approximate round-trip time."""
    import subprocess

    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    ip = str(device.ip_address)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "3", "-W", "1", "-n", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await proc.communicate()
        raw = out_b.decode("utf-8", errors="replace")
        duration_ms = int((time.monotonic() - started) * 1000)

        # Parse "3 packets transmitted, 3 received" and "rtt min/avg/max/mdev = 0.123/0.456/0.789/…"
        import re as _re
        received = 0
        transmitted = 0
        rtt_avg_ms = None
        m = _re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received", raw)
        if m:
            transmitted = int(m.group(1))
            received = int(m.group(2))
        m = _re.search(r"rtt min/avg/max/[^\s=]*\s*=\s*[\d.]+/([\d.]+)/[\d.]+/", raw)
        if m:
            rtt_avg_ms = float(m.group(1))

        ok = received > 0
        loss_pct = ((transmitted - received) / transmitted * 100) if transmitted else 100
        result = {
            "ok": ok,
            "reachable": ok,
            "transmitted": transmitted,
            "received": received,
            "loss_pct": round(loss_pct, 1),
            "rtt_avg_ms": rtt_avg_ms,
            "duration_ms": duration_ms,
            "reason": None if ok else "Host did not reply to ICMP echo",
        }
        summary = (
            f"{received}/{transmitted} · {rtt_avg_ms:.1f} ms" if ok and rtt_avg_ms is not None
            else f"{received}/{transmitted}" if ok
            else result["reason"]
        )
        await _record_test_run(db, device_id, "ping", ok, summary, result, user.id)
        return result
    except Exception as e:
        result = {
            "ok": False,
            "reachable": False,
            "transmitted": 0,
            "received": 0,
            "loss_pct": 100,
            "rtt_avg_ms": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "reason": f"ping failed: {e}",
        }
        await _record_test_run(db, device_id, "ping", False, result["reason"], result, user.id)
        return result


@router.post("/{device_id}/snmp-test", dependencies=[Depends(_device_in_scope)])
async def test_device_snmp(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    """Run an on-demand SNMP probe against the device and return whether it
    responded, plus the sysDescr/sysName/sysObjectID/sysUpTime values we got."""
    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device.snmp_enabled:
        result = {
            "ok": False,
            "reason": "SNMP monitoring is disabled for this device",
            "snmp_responded": False,
            "reachable": None,
            "duration_ms": 0,
        }
        await _record_test_run(db, device_id, "snmp", False, result["reason"], result, user.id)
        return result

    cfg = await _device_snmp_settings(db, device)

    ip = str(device.ip_address)
    reachable = await _ping_once(ip, timeout_s=2.0)

    # Give the manual probe more headroom than the configured timeout so we
    # don't falsely report "timed out" on slow devices. Minimum 5s.
    probe_timeout_ms = max(5000, cfg["timeout_ms"] * 3)

    started = time.monotonic()
    oids = [SYS_DESCR_OID, SYS_OBJECT_OID, SYS_NAME_OID, SYS_UPTIME_OID]
    result, snmp_err = await _snmpget_detail(
        ip=ip,
        community=cfg["community"] or "public",
        version=cfg["version"],
        port=cfg["port"],
        timeout_ms=probe_timeout_ms,
        oids=oids,
        v3_username=cfg["v3_username"],
        v3_security_level=cfg["v3_security_level"],
        v3_auth_protocol=cfg["v3_auth_protocol"],
        v3_auth_passphrase=cfg["v3_auth_passphrase"],
        v3_priv_protocol=cfg["v3_priv_protocol"],
        v3_priv_passphrase=cfg["v3_priv_passphrase"],
        v3_context=cfg["v3_context"],
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    if result is None:
        # Map net-snmp's stderr to a more actionable reason where we can.
        # The SNMP GET is always sent, so describe its own outcome; the ping
        # result is only context (ICMP may be filtered while UDP/161 works).
        err_lc = (snmp_err or "").lower()
        if "timeout" in err_lc and "no response" in err_lc:
            reason = (
                "SNMP timed out — packets reach the host but no reply (ACL, wrong community/v3 user, or SNMP service disabled)"
                if reachable else
                f"SNMP timed out — no reply on UDP/{cfg['port']}, and ping is unanswered too (host down, routing, or both protocols filtered)"
            )
        elif "authentication failure" in err_lc or "usm" in err_lc:
            reason = "SNMPv3 authentication failed — wrong username/auth/priv protocol or passphrase"
        elif "unknown" in err_lc and "protocol" in err_lc:
            reason = f"net-snmp rejected the protocol — {snmp_err}"
        elif "snmpget binary not installed" in err_lc:
            reason = "snmpget is missing in the API runtime — install the net-snmp tools package and restart the API"
        elif snmp_err:
            reason = snmp_err
        else:
            reason = "SNMP probe failed for an unknown reason"
        if not reachable and not reason.startswith("SNMP timed out"):
            reason = f"{reason} (ping also unanswered)"
        result = {
            "ok": False,
            "snmp_responded": False,
            "reachable": reachable,
            "reason": reason,
            "snmp_error": snmp_err,
            "duration_ms": duration_ms,
            "config": {
                "version": cfg["version"],
                "port": cfg["port"],
                "timeout_ms": cfg["timeout_ms"],
            },
        }
        await _record_test_run(db, device_id, "snmp", False, reason, result, user.id)
        return result

    # Parse uptime (timeticks: "(12345) 0:02:03.45")
    uptime_raw = result.get(SYS_UPTIME_OID, "")
    uptime_seconds = None
    try:
        if uptime_raw:
            # Some net-snmp versions return bare timeticks like "12345"
            digits = "".join(ch for ch in uptime_raw if ch.isdigit())
            if digits:
                uptime_seconds = int(digits) / 100
    except Exception:
        pass

    response = {
        "ok": True,
        "snmp_responded": True,
        "reachable": reachable,
        "duration_ms": duration_ms,
        "sys_descr": result.get(SYS_DESCR_OID),
        "sys_object_id": result.get(SYS_OBJECT_OID),
        "sys_name": result.get(SYS_NAME_OID),
        "sys_uptime_seconds": uptime_seconds,
        "sys_uptime_raw": uptime_raw,
        "config": {
            "version": cfg["version"],
            "port": cfg["port"],
            "timeout_ms": cfg["timeout_ms"],
        },
    }
    await _record_test_run(
        db, device_id, "snmp", True,
        f"responded in {duration_ms} ms (v{cfg['version']})",
        response, user.id,
    )
    return response


async def _managed_maps(db, devices) -> tuple[dict, dict]:
    """(parent_id -> hostname, controller_id -> child count) for one page of
    devices — powers the 'via <controller>' badge and children counts."""
    parent_names: dict = {}
    child_counts: dict = {}
    ids = [str(d.id) for d in devices]
    parent_ids = list({str(d.managed_by_device_id) for d in devices if d.managed_by_device_id})
    if parent_ids:
        rows = (await db.execute(
            text("SELECT id, hostname FROM devices WHERE id = ANY(:ids)"),
            {"ids": parent_ids},
        )).all()
        parent_names = {r.id: r.hostname for r in rows}
    if ids:
        rows = (await db.execute(
            text("""
                SELECT managed_by_device_id AS pid, count(*) AS n
                FROM devices WHERE managed_by_device_id = ANY(:ids)
                GROUP BY managed_by_device_id
            """),
            {"ids": ids},
        )).all()
        child_counts = {r.pid: r.n for r in rows}
    return parent_names, child_counts


async def _profile_name_map(db) -> dict:
    """profile_id -> template name (tiny table, one query)."""
    rows = (await db.execute(text("SELECT id, name FROM device_profiles"))).all()
    return {r[0]: r[1] for r in rows}


# Shared with the hardware report pack, which imports these names from here.
from app.services import device_insights as _insights
_SEV_RANK = _insights.SEV_RANK
_STANDALONE_RING_METRICS = _insights.STANDALONE_RING_METRICS
_metric_status = _insights.metric_status
_metric_display = _insights.metric_display


@router.get("/{device_id}/template-insights", dependencies=[Depends(_device_in_scope)])
async def device_template_insights(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Vendor-template metrics for one device, shaped for direct rendering:
    the attached template's groups with the latest collected values, per-row
    labels for tables, evaluated statuses and ClickHouse series keys for
    charting via /devices/{id}/snmp-metrics."""
    import json as _json

    dev = (await db.execute(
        text("""
            SELECT d.profile_id, d.promote_managed, p.name, p.vendor, p.description,
                   p.builtin, p.oid_groups
            FROM devices d
            LEFT JOIN device_profiles p ON p.id = d.profile_id
            WHERE d.id = :id
        """),
        {"id": device_id},
    )).mappings().first()
    if dev is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if dev["profile_id"] is None:
        return {"template": None, "groups": [], "updated_at": None,
                "children_capable": False, "promote_managed": False}

    oid_groups = dev["oid_groups"] or []
    if isinstance(oid_groups, str):
        oid_groups = _json.loads(oid_groups)

    # Groups whose rows are controller-managed devices: map (group, instance)
    # to the materialized child so the UI can link each row to its device.
    children_capable = any(
        gr.get("kind") == "table" and isinstance(gr.get("children"), dict)
        for gr in oid_groups
    )
    child_ids: dict = {}
    if children_capable:
        child_rows = (await db.execute(
            text("""
                SELECT id, managed_source, managed_instance
                FROM devices WHERE managed_by_device_id = :id
            """),
            {"id": device_id},
        )).all()
        child_ids = {(r.managed_source, r.managed_instance): str(r.id) for r in child_rows}

    vals = (await db.execute(
        text("""
            SELECT group_key, metric_key, instance, series_key, label, unit,
                   value_num, value_text, updated_at
            FROM device_template_values WHERE device_id = :id
        """),
        {"id": device_id},
    )).mappings().all()
    groups_out, latest_ts = _insights.shape_template_groups(oid_groups, vals, child_ids)

    return {
        "template": {
            "id": str(dev["profile_id"]),
            "name": dev["name"],
            "vendor": dev["vendor"],
            "description": dev["description"],
            "builtin": dev["builtin"],
        },
        "updated_at": latest_ts.isoformat() if latest_ts else None,
        "groups": groups_out,
        "tabs": _insights.template_tabs(groups_out),
        "children_capable": children_capable,
        "promote_managed": bool(dev["promote_managed"]),
    }


_SUMMARY_ALERT_SEVERITY = {"critical": "critical", "high": "critical", "warning": "warning", "medium": "warning"}


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return _aware(ts).isoformat()
    return str(ts)


def _latest_interface_rates(device_id: UUID) -> dict[int, dict]:
    """Latest in/out bps per interface from the last 15 minutes of samples."""
    from app.core.database import get_clickhouse_client
    res = get_clickhouse_client().query(
        """
        SELECT if_index, argMax(in_bps, timestamp), argMax(out_bps, timestamp),
               toUnixTimestamp64Milli(max(timestamp))
        FROM zenplus.snmp_if_metrics
        WHERE device_id = %(id)s AND timestamp >= now() - INTERVAL 15 MINUTE
        GROUP BY if_index
        """,
        parameters={"id": str(device_id)},
    )
    return {int(r[0]): {"in_bps": float(r[1] or 0), "out_bps": float(r[2] or 0), "ts": int(r[3])}
            for r in res.result_rows}


def _recent_traps(device_id: UUID, limit: int = 5) -> list[dict]:
    from app.core.database import get_clickhouse_client
    res = get_clickhouse_client().query(
        """
        SELECT trap_oid, trap_name, severity, message, toUnixTimestamp64Milli(timestamp)
        FROM zenplus.snmp_traps
        WHERE device_id = %(id)s AND timestamp >= now() - INTERVAL 7 DAY
        ORDER BY timestamp DESC
        LIMIT %(limit)s
        """,
        parameters={"id": str(device_id), "limit": limit},
    )
    return [{"trap_oid": r[0], "trap_name": r[1], "severity": r[2], "message": r[3],
             "timestamp": datetime.fromtimestamp(r[4] / 1000, tz=timezone.utc).isoformat()}
            for r in res.result_rows]


_WAN_NAME = re.compile(r"^(wan\d*|internet\d*|isp\d*)$|\b(wan|isp|dia|internet|uplink)\b", re.I)


@router.get("/{device_id}/summary", dependencies=[Depends(_device_in_scope)])
async def get_device_page_summary(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Everything the device page header, issues bar and Summary tab need in
    one request: resolved identity, the confirmed status (unchanged, as stored),
    a separate health rollup with its issues, interface counts, the busiest
    interfaces, open alerts and recent events.

    The health rollup never changes the confirmed status: a device can be Up
    with three issues. Sections whose source is unavailable are ``null`` (with
    the source named in ``unavailable``), never zero."""
    from app.core.security import user_has_permission
    from app.services import alert_workflow as wf

    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    unavailable: list[str] = []

    # Template readings, shaped exactly as the template-insights API shapes them.
    profile = (await db.execute(text("""
        SELECT p.name, p.vendor, p.oid_groups FROM device_profiles p WHERE p.id = :pid
    """), {"pid": device.profile_id})).mappings().first() if device.profile_id else None
    groups: list[dict] = []
    template_updated = None
    if profile:
        oid_groups = profile["oid_groups"] or []
        if isinstance(oid_groups, str):
            oid_groups = json.loads(oid_groups)
        vals = (await db.execute(text("""
            SELECT group_key, metric_key, instance, series_key, label, unit,
                   value_num, value_text, updated_at
            FROM device_template_values WHERE device_id = :id
        """), {"id": device_id})).mappings().all()
        groups, template_updated = _insights.shape_template_groups(oid_groups, vals)
    shown_groups = [g for g in groups if (g.get("rows") if g.get("kind") == "table"
                                          else any(m.get("has_data") for m in g.get("metrics") or []))]

    chassis = (await db.execute(text("""
        SELECT model_name, serial_number, fw_revision FROM device_entities
        WHERE device_id = :id AND class = 'chassis'
        ORDER BY COALESCE(parent_index, 0) <> 0, ent_index LIMIT 1
    """), {"id": device_id})).mappings().first()
    try:
        normalized = (await db.execute(text("""
            SELECT os_product, os_version, os_build, hw_model, hw_serial
            FROM inventory_identity WHERE asset_kind = 'device' AND asset_id = :id
        """), {"id": device_id})).mappings().first()
    except Exception:
        await db.rollback()
        normalized = None
    identity = _insights.resolve_identity(
        {"model": device.model, "os_version": device.os_version, "firmware_version": device.firmware_version,
         "serial_number": device.serial_number},
        groups, dict(chassis) if chassis else None, dict(normalized) if normalized else None)

    # Interfaces: counted by what an operator can act on.
    try:
        if_rows = (await db.execute(text("""
            SELECT if_index, if_name, if_descr, if_alias, if_type, if_speed, configured_speed_bps,
                   admin_status, oper_status, monitored, last_up_at IS NOT NULL AS ever_up
            FROM device_interfaces WHERE device_id = :id ORDER BY if_index
        """), {"id": device_id})).mappings().all()
    except Exception:
        await db.rollback()
        if_rows = (await db.execute(text("""
            SELECT if_index, if_name, if_descr, if_alias, if_type, if_speed, configured_speed_bps,
                   admin_status, oper_status, monitored, TRUE AS ever_up
            FROM device_interfaces WHERE device_id = :id ORDER BY if_index
        """), {"id": device_id})).mappings().all()
    interfaces = _insights.interface_breakdown([dict(r) for r in if_rows]) if if_rows else None

    busiest: list[dict] | None = None
    if if_rows and device.snmp_enabled:
        try:
            rates = await asyncio.to_thread(_latest_interface_rates, device_id)
            busiest = []
            for r in if_rows:
                rate = rates.get(int(r["if_index"]))
                if not rate:
                    continue
                speed = r["configured_speed_bps"] or r["if_speed"] or None
                total = rate["in_bps"] + rate["out_bps"]
                name = r["if_name"] or r["if_descr"] or f"ifIndex {r['if_index']}"
                busiest.append({
                    "if_index": r["if_index"], "name": name, "alias": r["if_alias"] or None,
                    "speed_bps": speed, "in_bps": rate["in_bps"], "out_bps": rate["out_bps"],
                    # Utilisation of the busier direction, as link utilisation reports it.
                    "util_pct": round(max(rate["in_bps"], rate["out_bps"]) / speed * 100, 1) if speed else None,
                    "wan": bool(_WAN_NAME.search(name) or _WAN_NAME.search(r["if_alias"] or "")),
                    "oper_status": r["oper_status"], "sampled_at": rate["ts"],
                })
            busiest.sort(key=lambda i: (not i["wan"], -(i["in_bps"] + i["out_bps"])))
            busiest = busiest[:8]
        except Exception:
            unavailable.append("interface_rates")
            busiest = None

    # Open alerts (incidents, not notices), only for callers who may read alerts.
    alerts = None
    # Alert titles on the device's own page drop its repeated hostname and IP.
    host = getattr(device, "hostname", None)
    host_ip = str(device.ip_address) if getattr(device, "ip_address", None) else None
    if await user_has_permission(db, user, "alerts.view"):
        rows = (await db.execute(text(f"""
            SELECT a.id, a.severity, a.status, a.message, a.triggered_at,
                   a.metadata->>'condition' AS condition, a.rule_id, r.name AS rule_name,
                   a.metadata->>'condition_metric' AS condition_metric,
                   a.metadata->>'new_status' AS new_status, a.metadata->>'if_index' AS if_index
            FROM alerts a
            LEFT JOIN alert_rules r ON r.id = a.rule_id
            WHERE a.device_id = :id AND a.status IN ('pending', 'active', 'acknowledged')
              AND NOT {wf.notice_sql('a')}
            ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.triggered_at ASC
            LIMIT 50
        """), {"id": device_id})).mappings().all()
        shaped = [{"id": str(r["id"]), "severity": r["severity"], "status": r["status"],
                   "message": re.sub(r"^\[ZenPlus\s+[A-Z]+\]\s*", "", r["message"] or "Alert"),
                   "title": _insights.alert_title(r["message"], r.get("condition"), host, host_ip),
                   "triggered_at": _iso(r["triggered_at"]),
                   "rule_id": str(r["rule_id"]) if r.get("rule_id") else None, "rule_name": r.get("rule_name"),
                   "metadata": {"condition_metric": r.get("condition_metric"), "new_status": r.get("new_status"),
                                "if_index": r.get("if_index")}} for r in rows]
        for a in shaped:
            a["group_key"] = _insights.alert_group_key(a["rule_id"], a["rule_name"], a["metadata"])
        alerts = {
            "active": sum(1 for r in rows if r["status"] in ("active", "pending")),
            "acknowledged": sum(1 for r in rows if r["status"] == "acknowledged"),
            "items": [{k: v for k, v in a.items() if k != "metadata"} for a in shaped[:10]],
            # Repeats of one condition from one rule as one row (D-09), over every open alert read.
            "groups": _insights.group_device_alerts(shaped),
        }
        firing = _insights.group_device_alerts([a for a in shaped if a["status"] in ("active", "pending")])

    maintenance_rows = (await db.execute(text(f"""
        SELECT m.ends_at, m.reason FROM device_maintenance m
        JOIN devices d ON d.id = :did AND {MAINT_COVERS_DEVICE_SQL}
        WHERE m.starts_at <= now() AND m.ends_at >= now()
        ORDER BY m.ends_at DESC LIMIT 1
    """), {"did": str(device_id)})).mappings().all()
    maintenance = ({"active": True, "ends_at": _iso(maintenance_rows[0]["ends_at"]),
                    "reason": maintenance_rows[0]["reason"]} if maintenance_rows else {"active": False})

    # Recent events for the Summary card (the Events tab reads the full,
    # range-bound timeline itself).
    recent: dict = {"status_changes": None, "traps": None, "flap_episodes": None}
    try:
        history = await asyncio.to_thread(metric_service.get_status_history, device_id, None, None, 10)
        recent["status_changes"] = [jsonable_encoder(e) for e in history]
    except Exception:
        unavailable.append("status_history")
    if device.snmp_enabled:
        try:
            recent["traps"] = await asyncio.to_thread(_recent_traps, device_id)
        except Exception:
            unavailable.append("traps")
    flap_rows = (await db.execute(text("""
        SELECT id, started_at, ended_at, kind, start_changes, window_minutes, status_at_start,
               status_at_end, end_reason
        FROM device_flap_episodes WHERE device_id = :id
          AND (ended_at IS NULL OR ended_at >= now() - INTERVAL '30 days')
        ORDER BY started_at DESC LIMIT 5
    """), {"id": device_id})).mappings().all()
    recent["flap_episodes"] = [{**{k: r[k] for k in ("kind", "start_changes", "window_minutes",
                                                      "status_at_start", "status_at_end", "end_reason")},
                                "id": str(r["id"]), "started_at": _iso(r["started_at"]),
                                "ended_at": _iso(r["ended_at"]), "ongoing": r["ended_at"] is None}
                               for r in flap_rows]

    # Issues: the confirmed status itself first, then everything that needs attention.
    issues: list[dict] = []
    changed = _iso(device.status_changed_at)
    if device.status == "down":
        issues.append({"source": "status", "severity": "critical", "title": "Device is down (confirmed)",
                       "detail": None, "since": changed, "tab": "events"})
    elif device.status == "degraded":
        reason = next((e.get("reason") for e in (recent["status_changes"] or []) if e.get("new_status") == "degraded"), None)
        issues.append({"source": "status", "severity": "warning", "title": "Device is degraded (confirmed)",
                       "detail": reason, "since": changed, "tab": "events"})
    if device.is_flapping:
        issues.append({"source": "flapping", "severity": "warning", "title": "Status is flapping",
                       "detail": "Status alerts are held until it is stable", "since": _iso(device.flapping_since),
                       "tab": "events"})
    if alerts:
        # One issue per repeated condition: "Degraded (packet loss)" with its count and first time.
        for g in firing[:5]:
            issue = {"source": "alert", "severity": _SUMMARY_ALERT_SEVERITY.get(g["severity"], "info"),
                     "title": g["label"] if g["count"] > 1 else g["latest_title"], "detail": None,
                     "since": g["first_at"], "tab": "events", "alert_id": g["members"][0]["id"]}
            if g["count"] > 1:
                issue.update(count=g["count"], last_at=g["last_at"], rule_id=g["rule_id"],
                             detail=f"latest: {g['latest_title']}" if g["latest_title"] != g["label"] else None)
            issues.append(issue)
    if interfaces and interfaces["unexpected_down"]:
        names = [i["name"] for i in interfaces["unexpected"]]
        n = len(names)
        issues.append({"source": "interfaces", "severity": "warning",
                       "title": f"{n} interface{'s' if n != 1 else ''} down unexpectedly",
                       "detail": ", ".join(names[:3]) + (f", +{n - 3}" if n > 3 else ""),
                       "since": None, "tab": "interfaces"})
    issues.extend(_insights.template_issues(shown_groups, template_updated))
    issues = _insights.sort_issues(issues)
    if maintenance["active"]:
        issues.append({"source": "maintenance", "severity": "info", "title": "In maintenance",
                       "detail": maintenance.get("reason"), "since": None, "until": maintenance["ends_at"],
                       "tab": "configuration"})
    not_applicable = [{"key": g["key"], "name": g.get("name"), "reason": g.get("not_applicable_reason")}
                      for g in shown_groups if g.get("applicable") is False]

    return {
        "device_id": str(device_id),
        "status": device.status,
        "status_changed_at": changed,
        "flapping": bool(device.is_flapping),
        "identity": identity,
        "template": {"name": profile["name"], "vendor": profile["vendor"],
                     "updated_at": _iso(template_updated),
                     "tabs": _insights.template_tabs(shown_groups)} if profile else None,
        "health": {**_insights.health_rollup(issues), "groups": [_insights.group_digest(g) for g in shown_groups]},
        "kpis": _insights.template_kpis(shown_groups),
        "issues": issues,
        "not_applicable": not_applicable,
        "interfaces": {k: v for k, v in interfaces.items() if k != "unexpected"} | {
            "unexpected": interfaces["unexpected"][:20]} if interfaces else None,
        "busiest_interfaces": busiest,
        "alerts": alerts,
        "maintenance": maintenance,
        "recent": recent,
        "polling": {"ping_interval_s": device.ping_interval if device.ping_enabled else None,
                    "snmp_interval_s": device.snmp_poll_interval if device.snmp_enabled else None,
                    "last_response": _iso(device.last_seen)},
        "unavailable": unavailable,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _device_to_response(
    device,
    profile_names: dict | None = None,
    parent_names: dict | None = None,
    child_counts: dict | None = None,
) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        hostname=device.hostname,
        ip_address=str(device.ip_address) if device.ip_address else None,
        device_type=device.device_type,
        location=device.location,
        group_id=device.group_id,
        group_name=device.group.name if device.group else None,
        tags=device.tags or [],
        ping_enabled=device.ping_enabled,
        ping_interval=device.ping_interval,
        status=device.status,
        last_seen=device.last_seen,
        last_rtt_ms=device.last_rtt_ms,
        is_flapping=bool(device.is_flapping),
        flapping_since=device.flapping_since,
        flap_kind=device.flap_kind,
        status_changed_at=device.status_changed_at,
        description=device.description,
        owner=getattr(device, "owner", None),
        notes=getattr(device, "notes", None),
        created_at=device.created_at,
        updated_at=device.updated_at,
        # SNMP — passphrases never exposed; presence signalled by *_configured flags.
        snmp_enabled=bool(device.snmp_enabled),
        snmp_version=device.snmp_version,
        snmp_port=device.snmp_port,
        snmp_community=None,
        snmp_v3_username=device.snmp_v3_username,
        snmp_v3_context=device.snmp_v3_context,
        snmp_auth_protocol=device.snmp_auth_protocol,
        snmp_priv_protocol=device.snmp_priv_protocol,
        snmp_timeout_ms=device.snmp_timeout_ms,
        snmp_retries=device.snmp_retries,
        snmp_max_repetitions=device.snmp_max_repetitions,
        snmp_poll_interval=device.snmp_poll_interval,
        sys_object_id=device.sys_object_id,
        vendor=device.vendor,
        model=device.model,
        os_version=device.os_version,
        profile_id=device.profile_id,
        profile_name=(profile_names or {}).get(device.profile_id),
        snmp_credential_id=device.snmp_credential_id,
        snmp_auth_configured=device.snmp_auth_passphrase is not None,
        snmp_priv_configured=device.snmp_priv_passphrase is not None,
        poll_mode=device.poll_mode or "direct",
        managed_by_device_id=device.managed_by_device_id,
        managed_by_hostname=(parent_names or {}).get(device.managed_by_device_id),
        serial_number=device.serial_number,
        firmware_version=device.firmware_version,
        managed_ip=str(device.managed_ip) if device.managed_ip else None,
        managed_source=device.managed_source,
        managed_last_seen=device.managed_last_seen,
        promote_managed=bool(device.promote_managed),
        managed_children_count=(child_counts or {}).get(device.id, 0),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Device maintenance windows (planned downtime)
#
# Mirrors the service-check maintenance feature (migrate-006): while a window
# is active the poller keeps collecting metrics but suppresses status
# transitions + alerting, the device shows status 'maintenance', and
# SLA/uptime calculations exclude samples inside the window.
# ═══════════════════════════════════════════════════════════════════════════

maintenance_router = APIRouter(prefix="/device-maintenance", tags=["Device Maintenance"])

# "Window m covers device d" — shared by every maintenance query. devices.tags
# is a JSONB string array, so tag scope uses jsonb_exists().
MAINT_COVERS_DEVICE_SQL = """(
       (m.scope_type = 'device' AND m.scope_device_id = d.id)
    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
    OR (m.scope_type = 'all')
)"""


def _maint_response(m, label: str) -> DeviceMaintenanceResponse:
    now = datetime.now(timezone.utc)
    starts = m.starts_at if m.starts_at.tzinfo else m.starts_at.replace(tzinfo=timezone.utc)
    ends = m.ends_at if m.ends_at.tzinfo else m.ends_at.replace(tzinfo=timezone.utc)
    return DeviceMaintenanceResponse(
        id=m.id,
        scope_type=m.scope_type,
        scope_device_id=m.scope_device_id,
        scope_group_id=m.scope_group_id,
        scope_tag=m.scope_tag,
        scope_label=label,
        starts_at=m.starts_at,
        ends_at=m.ends_at,
        reason=m.reason,
        created_by=m.created_by,
        created_at=m.created_at,
        active=starts <= now <= ends,
    )


async def _maint_labels(db: AsyncSession, rows) -> dict:
    """id -> human label for a batch of maintenance rows."""
    from app.models.device import Device as DeviceModel, DeviceGroup as GroupModel
    device_ids = [m.scope_device_id for m in rows if m.scope_device_id]
    group_ids = [m.scope_group_id for m in rows if m.scope_group_id]
    device_names: dict = {}
    group_names: dict = {}
    if device_ids:
        res = await db.execute(
            text("SELECT id, hostname FROM devices WHERE id = ANY(:ids)"), {"ids": device_ids}
        )
        device_names = {r.id: r.hostname for r in res}
    if group_ids:
        res = await db.execute(
            text("SELECT id, name FROM device_groups WHERE id = ANY(:ids)"), {"ids": group_ids}
        )
        group_names = {r.id: r.name for r in res}
    labels = {}
    for m in rows:
        if m.scope_type == "device":
            labels[m.id] = device_names.get(m.scope_device_id, "(deleted device)")
        elif m.scope_type == "group":
            labels[m.id] = group_names.get(m.scope_group_id, "(deleted group)")
        elif m.scope_type == "tag":
            labels[m.id] = f"tag:{m.scope_tag}"
        else:
            labels[m.id] = "All devices"
    return labels


async def _covered_device_ids(db: AsyncSession, m) -> list:
    """Device ids covered by one maintenance window."""
    res = await db.execute(
        text(f"""
            SELECT d.id FROM devices d
            JOIN device_maintenance m ON m.id = :mid
            WHERE {MAINT_COVERS_DEVICE_SQL}
        """),
        {"mid": str(m.id)},
    )
    return [r.id for r in res]


@maintenance_router.get("", response_model=list[DeviceMaintenanceResponse])
async def list_device_maintenance(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from app.models.device import DeviceMaintenance
    rows = (await db.execute(
        select(DeviceMaintenance).order_by(DeviceMaintenance.starts_at.desc()).limit(500)
    )).scalars().all()
    labels = await _maint_labels(db, rows)
    return [_maint_response(m, labels[m.id]) for m in rows]


@maintenance_router.post("", response_model=DeviceMaintenanceResponse, status_code=201)
async def create_device_maintenance(
    data: DeviceMaintenanceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    from app.models.device import DeviceMaintenance
    from app.services.audit_service import write_audit_log

    m = DeviceMaintenance(
        scope_type=data.scope_type,
        scope_device_id=data.scope_device_id if data.scope_type == "device" else None,
        scope_group_id=data.scope_group_id if data.scope_type == "group" else None,
        scope_tag=data.scope_tag.strip() if data.scope_type == "tag" and data.scope_tag else None,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        reason=data.reason,
        created_by=user.id,
    )
    db.add(m)
    await db.flush()

    now = datetime.now(timezone.utc)
    starts = m.starts_at if m.starts_at.tzinfo else m.starts_at.replace(tzinfo=timezone.utc)
    ends = m.ends_at if m.ends_at.tzinfo else m.ends_at.replace(tzinfo=timezone.utc)
    if starts <= now <= ends:
        # Window is active right now: flip covered devices to 'maintenance'
        # immediately (the poller would do it within one poll anyway) and
        # auto-resolve their open alerts so the alert list reflects planned
        # downtime, not an incident.
        covered = await _covered_device_ids(db, m)
        if covered:
            await db.execute(
                text("""
                    UPDATE devices SET status = 'maintenance', updated_at = now(),
                        status_changed_at = CASE WHEN status IS DISTINCT FROM 'maintenance'
                                                 THEN now() ELSE status_changed_at END
                    WHERE id = ANY(:ids)
                """),
                {"ids": covered},
            )
            await db.execute(
                text("""
                    UPDATE alerts SET status = 'resolved', resolved_at = now(),
                        metadata = COALESCE(metadata, '{}'::jsonb)
                                   || jsonb_build_object('resolved_by', 'maintenance',
                                                         'maintenance_id', CAST(:mid AS text))
                    WHERE status = 'active' AND device_id = ANY(:ids)
                """),
                {"mid": str(m.id), "ids": covered},
            )

    await write_audit_log(
        db, actor=user, action="create", resource_type="device_maintenance",
        resource_id=str(m.id),
        metadata={"scope_type": m.scope_type, "starts_at": str(m.starts_at),
                  "ends_at": str(m.ends_at), "reason": m.reason},
    )
    await db.commit()
    await db.refresh(m)
    labels = await _maint_labels(db, [m])
    return _maint_response(m, labels[m.id])


@maintenance_router.delete("/{maintenance_id}", status_code=204)
async def delete_device_maintenance(
    maintenance_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.manage")),
):
    from sqlalchemy import select
    from app.models.device import DeviceMaintenance
    from app.services.audit_service import write_audit_log

    m = (await db.execute(
        select(DeviceMaintenance).where(DeviceMaintenance.id == maintenance_id)
    )).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Maintenance window not found")

    now = datetime.now(timezone.utc)
    starts = m.starts_at if m.starts_at.tzinfo else m.starts_at.replace(tzinfo=timezone.utc)
    ends = m.ends_at if m.ends_at.tzinfo else m.ends_at.replace(tzinfo=timezone.utc)
    was_active = starts <= now <= ends
    covered = await _covered_device_ids(db, m) if was_active else []

    await db.execute(
        text("DELETE FROM device_maintenance WHERE id = :mid"), {"mid": str(maintenance_id)}
    )

    if covered:
        # Devices the poller pings recover their real status within one poll
        # cycle. Ping-disabled devices have no status writer, so reset any
        # that are no longer covered by another active window.
        await db.execute(
            text(f"""
                UPDATE devices SET status = 'unknown', updated_at = now(), status_changed_at = now()
                WHERE id = ANY(:ids) AND status = 'maintenance' AND ping_enabled = false
                  AND NOT EXISTS (
                      SELECT 1 FROM device_maintenance m
                      JOIN devices d ON d.id = devices.id AND {MAINT_COVERS_DEVICE_SQL}
                      WHERE m.starts_at <= now() AND m.ends_at >= now()
                  )
            """),
            {"ids": covered},
        )

    await write_audit_log(
        db, actor=user, action="delete", resource_type="device_maintenance",
        resource_id=str(maintenance_id),
        metadata={"scope_type": m.scope_type, "was_active": was_active},
    )
    await db.commit()


@router.get("/{device_id}/maintenance", dependencies=[Depends(_device_in_scope)])
async def get_device_maintenance(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Active + upcoming maintenance windows covering one device."""
    from app.models.device import DeviceMaintenance
    from sqlalchemy import select

    res = await db.execute(
        text(f"""
            SELECT m.id FROM device_maintenance m
            JOIN devices d ON d.id = :did AND {MAINT_COVERS_DEVICE_SQL}
            WHERE m.ends_at >= now()
            ORDER BY m.starts_at ASC
            LIMIT 100
        """),
        {"did": str(device_id)},
    )
    ids = [r.id for r in res]
    if not ids:
        return {"active": [], "upcoming": []}
    rows = (await db.execute(
        select(DeviceMaintenance).where(DeviceMaintenance.id.in_(ids))
        .order_by(DeviceMaintenance.starts_at.asc())
    )).scalars().all()
    labels = await _maint_labels(db, rows)
    out = [_maint_response(m, labels[m.id]) for m in rows]
    return {
        "active": [o for o in out if o.active],
        "upcoming": [o for o in out if not o.active],
    }


# ── Related panel (device page, Phase 3) ──────────────────────────────────

from app.services import device_related as _related


@router.get("/{device_id}/related", dependencies=[Depends(_device_in_scope)])
async def get_device_related(
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What this device depends on, what depends on it, the maps that show it
    with the devices linked to it there, and devices whose confirmed status
    changed within ±2 minutes of this device's last confirmed transitions.

    Reads device_status_log, never writes it. Every listed device is filtered
    by the caller's tag scope; maps need maps.view. A section whose source is
    unavailable is ``null`` and named in ``unavailable``."""
    from app.core.security import user_has_permission

    device = await device_service.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    scope = await scoping.visible_tags(db, user)
    visible = lambda tags: scoping.entity_visible(tags, scope)  # noqa: E731
    unavailable: list[str] = []
    cols = "d.id, d.hostname, d.ip_address, d.device_type, d.status, d.tags"

    parent = None
    if device.managed_by_device_id:
        row = (await db.execute(text(f"SELECT {cols} FROM devices d WHERE d.id = :id"),
                                {"id": device.managed_by_device_id})).mappings().first()
        if row and visible(row["tags"]):
            parent = _related.device_brief(row)

    child_rows = [r for r in (await db.execute(text(f"""
        SELECT {cols} FROM devices d WHERE d.managed_by_device_id = :id
    """), {"id": device_id})).mappings().all() if visible(r["tags"])]
    children_by_status: dict[str, int] = {}
    for r in child_rows:
        children_by_status[r["status"] or "unknown"] = children_by_status.get(r["status"] or "unknown", 0) + 1
    children = {"total": len(child_rows), "by_status": children_by_status,
                "items": _related.by_status_then_name([_related.device_brief(r) for r in child_rows])[:_related.MAX_LISTED]}

    dep_rows = (await db.execute(text(f"""
        SELECT 'upstream' AS direction, td.dependency_type, td.suppress_alerts, td.enabled, {cols}
        FROM topology_dependencies td JOIN devices d ON d.id = td.parent_device_id
        WHERE td.child_device_id = :id
        UNION ALL
        SELECT 'downstream' AS direction, td.dependency_type, td.suppress_alerts, td.enabled, {cols}
        FROM topology_dependencies td JOIN devices d ON d.id = td.child_device_id
        WHERE td.parent_device_id = :id
    """), {"id": device_id})).mappings().all()
    dependencies = {"upstream": [], "downstream": []}
    for r in dep_rows:
        if visible(r["tags"]):
            dependencies[r["direction"]].append({**_related.device_brief(r), "dependency_type": r["dependency_type"],
                                                 "suppress_alerts": bool(r["suppress_alerts"]), "enabled": bool(r["enabled"])})
    for k in dependencies:
        dependencies[k] = _related.by_status_then_name(dependencies[k])[:_related.MAX_LISTED]

    maps = None
    if await user_has_permission(db, user, "maps.view"):
        map_rows = (await db.execute(text("""
            SELECT DISTINCT m.id, m.name FROM manual_map_nodes n JOIN manual_maps m ON m.id = n.map_id
            WHERE n.device_id = :id ORDER BY m.name
        """), {"id": device_id})).mappings().all()
        neighbour_rows = (await db.execute(text(f"""
            SELECT DISTINCT {cols}
            FROM manual_map_nodes me
            JOIN manual_map_links l ON me.id IN (l.source_node_id, l.target_node_id)
            JOIN manual_map_nodes other ON other.id = CASE WHEN l.source_node_id = me.id
                                                          THEN l.target_node_id ELSE l.source_node_id END
            JOIN devices d ON d.id = other.device_id
            WHERE me.device_id = :id AND other.device_id <> :id
        """), {"id": device_id})).mappings().all()
        maps = {"maps": [{"id": str(r["id"]), "name": r["name"]} for r in map_rows],
                "neighbours": _related.by_status_then_name(
                    [_related.device_brief(r) for r in neighbour_rows if visible(r["tags"])])[:_related.MAX_LISTED]}

    same_time = None
    try:
        own = await asyncio.to_thread(_related.own_transitions, str(device_id))
        near = await asyncio.to_thread(_related.transitions_near, str(device_id), own)
        matches = _related.same_time_matches(own, near)
        ids = [m["device_id"] for m in matches]
        rows = {str(r["id"]): r for r in (await db.execute(text(f"""
            SELECT {cols} FROM devices d WHERE d.id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids})).mappings().all()} if ids else {}
        devices_out = []
        for m in matches:
            r = rows.get(m["device_id"])
            if r is None or not visible(r["tags"]):
                continue
            devices_out.append({**_related.device_brief(r), "matches": m["matches"][:5],
                                "shared_transitions": m["shared_transitions"], "closest_s": m["closest_s"]})
            if len(devices_out) >= _related.MAX_SAME_TIME:
                break
        same_time = {
            "window_s": _related.SAME_TIME_WINDOW_S,
            "transitions": [{"at": _related._iso_ms(t["ts"]), "old_status": t["old_status"],
                             "new_status": t["new_status"]} for t in own],
            "devices": devices_out,
        }
    except Exception:
        unavailable.append("status_log")

    return {
        "device_id": str(device_id),
        "parent": parent,
        "children": children,
        "dependencies": dependencies,
        "maps": maps,
        "same_time": same_time,
        "unavailable": unavailable,
    }


# ── Server-saved widget layouts (migrate-165) ─────────────────────────────
# A user's own arrangement of the device page widgets. Reading or saving one's
# own layout needs only devices.view (the explicit guard replaces the module's
# devices.manage default for writes); the device itself is scope-checked.

from app.services import device_layouts as _layouts


def _layout_template(vendor: str | None, device_type: str | None, required: bool):
    template = _layouts.template_identity(vendor, device_type)
    if required and template is None:
        raise HTTPException(status_code=400, detail="A vendor/type layout needs a known vendor and device type")
    return template


def _layout_section(section: str) -> str:
    if not _layouts.valid_section(section):
        raise HTTPException(status_code=400, detail="Invalid layout section")
    return section


@router.get("/{device_id}/widget-layouts", dependencies=[Depends(_device_in_scope)])
async def get_device_widget_layouts(
    device_id: UUID,
    vendor: str | None = Query(default=None, max_length=200),
    device_type: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.view")),
):
    """The caller's saved layouts for this device (every tab) and for its
    vendor/type template (``vendor`` and ``device_type`` as the page uses
    them). A device layout wins over the template on the page."""
    return await _layouts.read_layouts(db, user.id, device_id, _layout_template(vendor, device_type, False))


@router.put("/{device_id}/widget-layouts/{section}", dependencies=[Depends(_device_in_scope)])
async def put_device_widget_layout(
    device_id: UUID,
    section: str,
    data: _layouts.LayoutWrite,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.view")),
):
    """Save the caller's layout for one tab, for this device or for every
    device of the same vendor and device type."""
    section = _layout_section(section)
    template = _layout_template(data.vendor, data.device_type, data.scope == "template")
    return await _layouts.save_layout(db, user.id, device_id, section, data.scope, template,
                                      _layouts.clean_items(data.items))


@router.delete("/{device_id}/widget-layouts/{section}", dependencies=[Depends(_device_in_scope)])
async def delete_device_widget_layout(
    device_id: UUID,
    section: str,
    scope: str = Query(default="device", pattern="^(device|template)$"),
    vendor: str | None = Query(default=None, max_length=200),
    device_type: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("devices.view")),
):
    """Forget the caller's layout for one tab (the device one: the tab then
    follows the vendor/type template, or the standard layout)."""
    section = _layout_section(section)
    template = _layout_template(vendor, device_type, scope == "template")
    deleted = await _layouts.delete_layout(db, user.id, device_id, section, scope, template)
    return {"deleted": deleted}
