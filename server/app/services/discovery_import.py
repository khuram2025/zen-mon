"""Import discovery results into inventory: the one path for the Import dialog
(``POST /discovery-v2/runs/{id}/import``) and for discovery rules that
auto-import.

The caller holds the ``discovery-import`` advisory lock, reserves the license
budget (``license_capacity.reserve``), commits and writes the audit entry.

Settings precedence for a new device: the operator's dialog choices, then the
values discovery rules set (``result.rule_plan``), then the profile defaults.
Group, template and tags are stored on the result when it is scanned or
re-evaluated (``suggested_group_id``, ``matched_template_id``,
``suggested_tags``: the rule value, else the profile default), so they need no
lookup here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snmp_oid import display_oid
from app.models.device import Device
from app.models.discovery_v2 import (
    DiscoveryImportBatch,
    DiscoveryImportItem,
    DiscoveryProfile,
    DiscoveryResultV2,
    DiscoveryRun,
)
from app.schemas.discovery_v2 import ImportRequest
from app.services import license_capacity
from app.services.device_service import apply_credential as _apply_credential
from app.services.discovery_rules.engine import setting
from app.services.discovery_rules.facts import is_server_like


@dataclass
class ImportOutcome:
    batch_id: uuid.UUID
    total: int
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    conflicts: int = 0
    devices_created: int = 0
    servers_created: int = 0
    items: list[dict] = field(default_factory=list)


def _is_server_result(result: DiscoveryResultV2) -> bool:
    """Server-class host (vs network gear) — drives auto routing."""
    return is_server_like(result.device_type, result.os)


def _result_ports(result: DiscoveryResultV2) -> set[int]:
    out: set[int] = set()
    for p in result.open_ports or []:
        try:
            out.add(int(p))
        except (TypeError, ValueError):
            continue
    return out


def _server_os_type(result: DiscoveryResultV2) -> str:
    os_l = (result.os or "").lower()
    if "windows" in os_l:
        return "windows"
    if any(h in os_l for h in ("linux", "ubuntu", "debian", "centos", "rhel", "red hat", "suse", "fedora")):
        return "linux"
    if "macos" in os_l or "mac os" in os_l:
        return "macos"
    if "bsd" in os_l:
        return "bsd"
    if "esxi" in os_l:
        return "other"
    # No OS probe result — fall back to port heuristics.
    ports = _result_ports(result)
    if ports & {3389, 5985, 5986, 445}:
        return "windows"
    if 22 in ports:
        return "linux"
    return "unknown"


def _server_collection_mode(result: DiscoveryResultV2) -> str:
    """Best agentless mode the scan proved (or hinted) for this host."""
    if result.windows_credential_used:
        return "agentless_winrm"
    if result.credential_used:
        return "snmp"
    ports = _result_ports(result)
    if 5985 in ports or 5986 in ports:
        return "agentless_winrm"
    if 22 in ports:
        return "ssh"
    if 161 in ports:
        return "snmp"
    return "none"


def route_result(result: DiscoveryResultV2, import_as: str, enable_monitoring: bool) -> tuple[bool, bool]:
    """(create_device, create_server) for one result under the chosen mode."""
    if import_as == "device":
        return True, False
    if import_as == "server":
        return False, True
    if import_as == "both":
        return True, True
    # auto: server-class hosts become servers (+ a device for ping/SNMP
    # monitoring when enabled); everything else stays a network device.
    if _is_server_result(result):
        return enable_monitoring, True
    return True, False


async def _create_server_from_result(
    db: AsyncSession,
    result: DiscoveryResultV2,
    payload: ImportRequest,
    actor_id: uuid.UUID | None,
    device_id: uuid.UUID | None,
) -> uuid.UUID:
    display_name = (result.hostname or result.sys_name or result.fqdn
                    or str(result.ip_address))
    hw = " ".join(b for b in (result.vendor, result.model) if b)
    description = f"Imported from network discovery{f' — {hw}' if hw else ''}"
    row = (await db.execute(
        text("""INSERT INTO servers
                    (display_name, hostname, fqdn, primary_ip, device_id,
                     os_type, os_name, os_version, collection_mode,
                     environment, description, tags, status, created_by)
                VALUES (:dn, :hn, :fqdn, :ip, :dev,
                        :ost, :osn, :osv, :cm,
                        :env, :desc, CAST(:tags AS jsonb), 'unknown', :cb)
                RETURNING id"""),
        {
            "dn": display_name[:255],
            "hn": (result.hostname or result.sys_name or None),
            "fqdn": result.fqdn,
            "ip": str(result.ip_address),
            "dev": device_id,
            "ost": _server_os_type(result),
            "osn": result.os,
            "osv": result.os_version,
            "cm": _server_collection_mode(result),
            "env": payload.environment,
            "desc": description,
            "tags": json.dumps(payload.tags or result.suggested_tags or []),
            "cb": actor_id,
        },
    )).first()
    return row[0]


async def import_results(
    db: AsyncSession,
    run: DiscoveryRun,
    payload: ImportRequest,
    *,
    actor_id: uuid.UUID | None,
    budget: license_capacity.NodeBudget,
    route: dict[int, str] | None = None,
    apply_credential: Callable[..., Awaitable[Any]] = _apply_credential,
) -> ImportOutcome:
    """Create devices/servers for ``payload.result_ids`` of ``run``.

    ``route`` overrides ``payload.import_as`` per result id (rules choose it
    per result). ``apply_credential`` is injectable for tests."""
    profile = await db.get(DiscoveryProfile, run.profile_id) if run.profile_id else None
    explicit = payload.model_fields_set
    if payload.group_id:
        from app.services.device_groups.service import assert_static
        await assert_static(db, payload.group_id, what="The import group")

    batch = DiscoveryImportBatch(
        run_id=run.id,
        profile_id=run.profile_id,
        group_id=payload.group_id,
        template_id=payload.template_id,
        snmp_credential_id=payload.snmp_credential_id,
        tags=payload.tags,
        enable_monitoring=payload.enable_monitoring,
        started_by=actor_id,
        status="running",
        total_items=len(payload.result_ids),
    )
    db.add(batch)
    await db.flush()
    out = ImportOutcome(batch_id=batch.id, total=len(payload.result_ids))

    for rid in payload.result_ids:
        result = await db.get(DiscoveryResultV2, rid)
        if not result or result.run_id != run.id:
            # A missing result cannot be referenced by the item foreign key.
            # Report the failure without poisoning valid items in this batch.
            out.failed += 1
            out.items.append({"result_id": rid, "status": "failed", "error": "result not found"})
            continue
        if result.imported:
            db.add(DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="skipped",
                                       error_message="Already imported"))
            out.skipped += 1
            out.items.append({"result_id": rid, "status": "skipped", "reason": "already imported"})
            continue

        plan = getattr(result, "rule_plan", None)
        import_as = (route or {}).get(rid, payload.import_as)
        want_device, want_server = route_result(result, import_as, payload.enable_monitoring)
        report: dict = {"result_id": rid, "ip": str(result.ip_address)}
        conflict_types: list[str] = []
        created_device_id: uuid.UUID | None = None
        created_server_id: uuid.UUID | None = None
        linked_device_id: uuid.UUID | None = None
        error: str | None = None

        # Device leg (network monitoring)
        existing_dev = (
            await db.execute(
                text("SELECT id FROM devices WHERE ip_address = :ip"),
                {"ip": str(result.ip_address)},
            )
        ).first()
        if existing_dev:
            linked_device_id = existing_dev[0]
        if want_device:
            if existing_dev and payload.conflict_strategy == "update":
                # Rediscovery updates discovered identity, preserving operator
                # monitoring, credentials, tags, groups and interface choices.
                await db.execute(text('''UPDATE devices SET
                    sys_object_id = COALESCE(:oid, sys_object_id),
                    vendor = COALESCE(:vendor, vendor), model = COALESCE(:model, model),
                    os_version = COALESCE(:os_version, os_version)
                    WHERE id = :id'''), {'id': existing_dev[0], 'oid': display_oid(result.sys_object_id),
                    'vendor': result.vendor, 'model': result.model, 'os_version': result.os_version})
                created_device_id = existing_dev[0]  # links the import result; not counted as a new node
                report['updated_existing'] = True
            elif existing_dev:
                conflict_types.append("ip_exists")
            elif not budget.take(1):
                error = budget.exhausted_message
                report["license_capacity_reached"] = True
            else:
                try:
                    hostname = result.hostname or result.sys_name or str(result.ip_address)
                    credential = (payload.snmp_credential_id or setting(plan, "snmp_credential")
                                  or result.credential_used)
                    if isinstance(credential, str):
                        credential = uuid.UUID(credential)
                    ping_interval = (payload.ping_interval if "ping_interval" in explicit
                                     else setting(plan, "ping_interval") or payload.ping_interval)
                    template_id = payload.template_id or result.matched_template_id
                    rule_template = setting(plan, "template")
                    template_source = None
                    if template_id:
                        template_source = ("rule" if not payload.template_id and rule_template
                                           and str(rule_template) == str(template_id) else "import")
                    device = Device(
                        hostname=hostname,
                        ip_address=str(result.ip_address),
                        device_type=setting(plan, "device_type") or result.device_type or "other",
                        device_type_source="rule" if setting(plan, "device_type") else None,
                        location=(payload.location or setting(plan, "location")
                                  or (profile.default_location if profile else None)),
                        group_id=payload.group_id or result.suggested_group_id,
                        tags=payload.tags or result.suggested_tags or [],
                        ping_enabled=payload.enable_monitoring,
                        ping_interval=ping_interval,
                        snmp_enabled=bool(payload.enable_monitoring and credential),
                        snmp_credential_id=credential,
                        sys_object_id=display_oid(result.sys_object_id),
                        vendor=result.vendor,
                        model=result.model,
                        os_version=result.os_version,
                        profile_id=template_id,
                        # Who chose the template (migrate-189): a discovery
                        # rule, else the import (dialog or profile default).
                        profile_source=template_source,
                        profile_assigned_at=datetime.now(timezone.utc) if template_id else None,
                        status="unknown",
                        created_by=actor_id,
                    )
                    snmp_interval = setting(plan, "snmp_interval")
                    if snmp_interval:
                        device.snmp_poll_interval = int(snmp_interval)
                    # Copy the credential onto the device row exactly like
                    # Add Device does; the poller polls from these columns.
                    if device.snmp_credential_id:
                        await apply_credential(db, device, device.snmp_credential_id)
                    async with db.begin_nested():
                        db.add(device)
                        await db.flush()
                    created_device_id = device.id
                    linked_device_id = device.id
                    out.devices_created += 1
                except Exception as e:
                    error = f"device: {e}"

        # Server leg (server-monitoring inventory)
        if want_server and error is None:
            hn = result.hostname or result.sys_name
            existing_srv = (
                await db.execute(
                    text("""SELECT id FROM servers
                            WHERE primary_ip = :ip
                               OR (hostname IS NOT NULL AND CAST(:hn AS text) IS NOT NULL
                                   AND lower(hostname) = lower(CAST(:hn AS text)))
                            LIMIT 1"""),
                    {"ip": str(result.ip_address), "hn": hn},
                )
            ).first()
            if existing_srv and payload.conflict_strategy == "skip":
                conflict_types.append("server_exists")
            elif (await license_capacity.server_needs_node(db, linked_device_id)
                  and not budget.take(1)):
                error = budget.exhausted_message
                report["license_capacity_reached"] = True
            else:
                try:
                    async with db.begin_nested():
                        created_server_id = await _create_server_from_result(
                            db, result, payload, actor_id, linked_device_id,
                        )
                    out.servers_created += 1
                except Exception as e:
                    error = f"server: {e}"

        # Record what was created even on partial failure so retries don't duplicate.
        now = datetime.now(timezone.utc)
        if created_device_id or created_server_id:
            result.imported = True
            result.imported_at = now
            if created_device_id:
                result.imported_device_id = created_device_id
            if created_server_id:
                result.imported_server_id = created_server_id
            result.status = "imported"

        if error is not None:
            db.add(DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="failed",
                                       device_id=created_device_id,
                                       server_id=created_server_id,
                                       error_message=error))
            out.failed += 1
            report.update(status="failed", error=error)
        elif created_device_id or created_server_id:
            db.add(DiscoveryImportItem(
                batch_id=batch.id,
                result_id=rid,
                status="imported",
                device_id=created_device_id,
                server_id=created_server_id,
                conflict_type=",".join(conflict_types)[:40] or None,
                processed_at=now,
            ))
            out.successful += 1
            report["status"] = "imported"
            if created_device_id:
                report["device_id"] = str(created_device_id)
            if created_server_id:
                report["server_id"] = str(created_server_id)
            if conflict_types:
                report["partial_conflicts"] = conflict_types
        else:
            db.add(DiscoveryImportItem(batch_id=batch.id, result_id=rid, status="conflict",
                                       device_id=linked_device_id,
                                       conflict_type=",".join(conflict_types)[:40] or "exists"))
            out.conflicts += 1
            report.update(status="conflict", conflicts=conflict_types or ["exists"])
        out.items.append(report)

    batch.successful_items = out.successful
    batch.failed_items = out.failed
    batch.skipped_items = out.skipped
    batch.status = "completed" if out.failed == 0 else "partial"
    batch.completed_at = datetime.now(timezone.utc)
    return out
