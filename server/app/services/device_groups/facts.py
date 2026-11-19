"""Facts about existing devices and servers that a dynamic group condition tests.

The condition language is the discovery rules' one
(``app.services.discovery_rules.conditions``); only the catalog differs. Custom
fields join the catalog as ``custom.<key>`` with the field's type.

``load_assets`` reads every device and server in a few bulk queries, so one
membership pass costs the same whatever the number of groups.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snmp_oid import normalize_oid

# key -> (type, label, help)
BASE_FIELDS: dict[str, tuple[str, str, str]] = {
    "asset_kind": ("enum", "Kind", "device or server"),
    "name": ("text", "Name", "Device hostname or server display name"),
    "ip_address": ("ip", "IP address", "Management address"),
    "device_type": ("reference", "Device type", "From the device type catalogue; servers are 'server'"),
    "vendor": ("text", "Vendor", "Vendor (servers: hardware manufacturer)"),
    "model": ("text", "Model", "Model (servers: hardware model)"),
    "os": ("text", "Operating system", "Servers: OS name (Windows Server 2022, Ubuntu ...)"),
    "os_version": ("version", "OS / firmware version", "Firmware or OS version"),
    "sys_object_id": ("oid", "sysObjectID", "SNMP sysObjectID (devices)"),
    "sys_descr": ("text", "sysDescr", "SNMP sysDescr (devices)"),
    "template": ("reference", "Device template", "The device's template"),
    "primary_group": ("reference", "Primary group", "The device's own group"),
    "tags": ("set", "Tags", "Tags on the device or server"),
    "location": ("text", "Location", "Device location text"),
    "site": ("reference", "Site", "Servers: site"),
    "status": ("text", "Status", "up, down, degraded, unknown, maintenance (servers: healthy, warning ...)"),
    "owner": ("text", "Owner", "Owner text"),
    "environment": ("text", "Environment", "Servers: environment (production, staging ...)"),
    "serial_number": ("text", "Serial number", "Serial number"),
    "controller": ("text", "Controller", "Hostname of the controller of a managed child (FortiAP, FortiSwitch ...)"),
    "is_managed_child": ("boolean", "Controller-managed", "Read through a controller, not polled itself"),
    "snmp_enabled": ("boolean", "SNMP polling on", "Devices: SNMP polling enabled"),
    "ping_enabled": ("boolean", "Ping on", "Devices: ping monitoring enabled"),
    "ncm_enrolled": ("boolean", "In Config Manager", "Devices: configuration backups enabled"),
}
# Servers: what the agent reports (services, software, watched processes), the
# discovery rules' inventory facts (app.services.server_inventory_facts).
from app.services.server_inventory_facts import FIELDS as _INVENTORY  # noqa: E402

BASE_FIELDS.update({k: ("inventory", label, "Servers: " + help_) for k, (label, help_) in _INVENTORY.items()})

BASE_ENUMS: dict[str, list[str]] = {"asset_kind": ["device", "server"]}

REFERENCE_SOURCES: dict[str, str] = {
    "device_type": "device_types",
    "template": "device_profiles",
    "primary_group": "device_groups",
    "site": "sites",
}

CUSTOM_PREFIX = "custom."
_CUSTOM_TYPE = {"text": "text", "number": "number", "date": "date", "choice": "enum", "boolean": "boolean"}


async def custom_defs(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT id::text AS id, key, label, field_type, choices, asset_kinds, description, sort_order, show_in_list
          FROM custom_field_defs ORDER BY sort_order, label
    """))).mappings().all()
    return [dict(r) for r in rows]


def catalog(defs: list[dict]) -> tuple[dict, dict]:
    """(fields, enums) for condition validation and evaluation, custom fields included."""
    fields = dict(BASE_FIELDS)
    enums = {k: list(v) for k, v in BASE_ENUMS.items()}
    for d in defs:
        key = CUSTOM_PREFIX + d["key"]
        fields[key] = (_CUSTOM_TYPE[d["field_type"]], d["label"], d.get("description") or "Custom field")
        if d["field_type"] == "choice":
            enums[key] = [str(c) for c in (d.get("choices") or [])]
    return fields, enums


def _ip(value: Any) -> str | None:
    return str(value).split("/")[0] if value else None


def _value(row: dict) -> Any:
    for k in ("value_text", "value_num", "value_date", "value_bool"):
        if row.get(k) is not None:
            v = row[k]
            return v.isoformat() if k == "value_date" else v
    return None


async def load_assets(db: AsyncSession, kinds: tuple[str, ...] = ("device", "server"),
                      needles: dict | None = None) -> list[tuple[str, str, dict]]:
    """[(kind, id, facts)] for every device and server. ``needles``: the names
    inventory conditions look for (``server_inventory_facts.needles``); only
    those rows are read."""
    custom: dict[tuple[str, str], dict] = {}
    for r in (await db.execute(text("""
        SELECT v.asset_kind, v.asset_id::text AS asset_id, d.key, v.value_text, v.value_num, v.value_date, v.value_bool
          FROM custom_field_values v JOIN custom_field_defs d ON d.id = v.field_id
    """))).mappings().all():
        custom.setdefault((r["asset_kind"], r["asset_id"]), {})[CUSTOM_PREFIX + r["key"]] = _value(dict(r))

    out: list[tuple[str, str, dict]] = []
    if "device" in kinds:
        rows = (await db.execute(text("""
            SELECT d.id::text AS id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor, d.model,
                   d.os_version, d.sys_object_id, d.sys_descr, d.profile_id::text AS template,
                   d.group_id::text AS primary_group, d.tags, d.location, d.status, d.owner, d.serial_number,
                   c.hostname AS controller,
                   (d.managed_by_device_id IS NOT NULL OR d.poll_mode = 'via_controller') AS is_child,
                   d.snmp_enabled, d.ping_enabled,
                   COALESCE(n.enabled, FALSE) AS ncm
              FROM devices d
              LEFT JOIN devices c ON c.id = d.managed_by_device_id
              LEFT JOIN device_ncm n ON n.device_id = d.id
        """))).mappings().all()
        for r in rows:
            facts = {
                "asset_kind": "device", "name": r["hostname"], "ip_address": _ip(r["ip"]),
                "device_type": r["device_type"], "vendor": r["vendor"], "model": r["model"], "os": None,
                "os_version": r["os_version"], "sys_object_id": normalize_oid(r["sys_object_id"]) or None,
                "sys_descr": r["sys_descr"], "template": r["template"], "primary_group": r["primary_group"],
                "tags": [str(t) for t in (r["tags"] or [])], "location": r["location"], "site": None,
                "status": r["status"], "owner": r["owner"], "environment": None,
                "serial_number": r["serial_number"], "controller": r["controller"],
                "is_managed_child": bool(r["is_child"]), "snmp_enabled": bool(r["snmp_enabled"]),
                "ping_enabled": bool(r["ping_enabled"]), "ncm_enrolled": bool(r["ncm"]),
                **{k: None for k in _INVENTORY},
            }
            facts.update(custom.get(("device", r["id"]), {}))
            out.append(("device", r["id"], facts))
    if "server" in kinds:
        inv: dict = {}
        if needles:
            from app.services import server_inventory_facts
            ids = [str(i) for (i,) in (await db.execute(text("SELECT id FROM servers"))).all()]
            inv = await server_inventory_facts.load(db, ids, needles)
        rows = (await db.execute(text("""
            SELECT s.id::text AS id, s.display_name, s.hostname, host(s.primary_ip) AS ip, s.os_name, s.os_version,
                   s.environment, s.owner, s.tags, s.status, s.site_id::text AS site, s.hw_manufacturer, s.hw_model,
                   s.hw_serial
              FROM servers s
        """))).mappings().all()
        for r in rows:
            facts = {
                "asset_kind": "server", "name": r["display_name"] or r["hostname"], "ip_address": _ip(r["ip"]),
                "device_type": "server", "vendor": r["hw_manufacturer"], "model": r["hw_model"],
                "os": r["os_name"], "os_version": r["os_version"], "sys_object_id": None, "sys_descr": None,
                "template": None, "primary_group": None, "tags": [str(t) for t in (r["tags"] or [])],
                "location": None, "site": r["site"], "status": r["status"], "owner": r["owner"],
                "environment": r["environment"], "serial_number": r["hw_serial"], "controller": None,
                "is_managed_child": False, "snmp_enabled": None, "ping_enabled": None, "ncm_enrolled": None,
                **{k: (inv.get(r["id"]) or {}).get(k) for k in _INVENTORY},
            }
            facts.update(custom.get(("server", r["id"]), {}))
            out.append(("server", r["id"], facts))
    return out


def catalog_payload(defs: list[dict]) -> list[dict]:
    """Fields for the condition builder (same shape as the discovery rules catalog)."""
    from app.services.discovery_rules import conditions
    fields, enums = catalog(defs)
    out = []
    for key, (ftype, label, help_) in fields.items():
        out.append({
            "key": key, "type": ftype, "label": label, "help": help_,
            "operators": [{"key": op, "label": lbl, "value": kind} for op, (lbl, kind) in conditions.OPERATORS[ftype].items()],
            "values": enums.get(key), "source": REFERENCE_SOURCES.get(key),
        })
    return out
