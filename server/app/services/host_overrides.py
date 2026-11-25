"""Manual host addresses for service checks (docs/service-check-connections.md).

An administrator maps a host name to the address service checks connect to.
Only the address the check dials changes: the URL, the Host header, the TLS
server name and the certificate name check keep the name (``curl --resolve``).
This is deliberately not ``/etc/hosts``: that would also redirect updates,
licensing, mail and directory sign-in, and would never reach remote sensors.

A check that carries a credential sends it to the address given here, so every
change is administrator-only and audit-logged, like probe trust.

Stored in ``system_settings['monitoring.host_overrides']`` as
``{"entries": [{id, host, address, scope, sensor_id, note, updated_at, updated_by}]}``.
The Go poller reads the same document (``checker.ResolveHostOverrides``); both
sides pass ``scripts/dev/host-overrides-contract/vectors.json``.
"""
from __future__ import annotations

import ipaddress
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text

SETTINGS_KEY = "monitoring.host_overrides"
MAX_ENTRIES = 200
MAX_NOTE = 200
SCOPE_ALL, SCOPE_CENTRAL, SCOPE_SENSOR = "all", "central", "sensor"
SCOPES = (SCOPE_ALL, SCOPE_CENTRAL, SCOPE_SENSOR)
CENTRAL = "central"
# What a sensor names in its heartbeat when it applies these entries.
SENSOR_CAPABILITY = "host_overrides"

_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


class HostOverrideError(ValueError):
    """A refused entry; ``field`` names the form field to mark."""

    def __init__(self, message: str, field: str = "host", status: int = 422):
        super().__init__(message)
        self.field = field
        self.status = status


async def load_host_overrides(db) -> dict[str, Any]:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key=:key"), {"key": SETTINGS_KEY})).first()
    value = row[0] if row else None
    if isinstance(value, str):  # a driver that hands jsonb back as text
        value = json.loads(value)
    entries = (value or {}).get("entries") if isinstance(value, dict) else None
    return {"entries": [dict(entry) for entry in entries or [] if isinstance(entry, dict)]}


async def lock_host_overrides(db) -> dict[str, Any]:
    # Serialize edits, including creation of the first row.
    await db.execute(text("SELECT pg_advisory_xact_lock(783241907)"))
    return await load_host_overrides(db)


async def save_host_overrides(db, document: dict[str, Any]) -> None:
    await db.execute(
        text("INSERT INTO system_settings(key,value) VALUES (:key,CAST(:value AS jsonb)) "
             "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"),
        {"key": SETTINGS_KEY, "value": json.dumps({"entries": document.get("entries", [])})},
    )


def normalize_host(raw: Any) -> str:
    """One exact DNS name, lower case, without a trailing dot."""
    host = str(raw or "").strip()
    if not host:
        raise HostOverrideError("Enter the host name the checks use")
    if "://" in host or "/" in host:
        raise HostOverrideError("Enter the host name only, without http:// or a path")
    if "*" in host:
        raise HostOverrideError("Wildcards are not supported: enter each host name exactly")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise HostOverrideError("This is an address, not a name. A check of an address already connects to it")
    if ":" in host:
        raise HostOverrideError("Enter the host name only, without a port")
    try:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise HostOverrideError("This is not a valid host name") from None
    if not host or len(host) > 253 or any(not _LABEL.fullmatch(label) for label in host.split(".")):
        raise HostOverrideError("This is not a valid host name")
    return host


def normalize_address(raw: Any) -> str:
    """One IPv4 or IPv6 address a check can connect to."""
    value = str(raw or "").strip()
    if not value:
        raise HostOverrideError("Enter the IPv4 or IPv6 address to connect to", "address")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if "%" in value:
        raise HostOverrideError("Addresses with an interface zone (%) are not supported", "address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise HostOverrideError("Enter one IPv4 or IPv6 address, without a port, range or name", "address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_unspecified or address.is_multicast:
        raise HostOverrideError("This address cannot be connected to", "address")
    return str(address)


def build_entry(data: dict[str, Any], *, sensor_ids: Iterable[str], actor: str | None,
                entry_id: str | None = None) -> dict[str, Any]:
    scope = str(data.get("scope") or SCOPE_ALL)
    if scope not in SCOPES:
        raise HostOverrideError("Choose where this address applies", "scope")
    sensor_id = None
    if scope == SCOPE_SENSOR:
        try:
            sensor_id = str(uuid.UUID(str(data.get("sensor_id") or "")))
        except ValueError:
            raise HostOverrideError("Choose the sensor this address applies to", "sensor_id") from None
        if sensor_id not in {str(item).lower() for item in sensor_ids}:
            raise HostOverrideError("That sensor no longer exists", "sensor_id")
    note = " ".join(str(data.get("note") or "").split())
    if len(note) > MAX_NOTE:
        raise HostOverrideError(f"Keep the note within {MAX_NOTE} characters", "note")
    return {
        "id": entry_id or uuid.uuid4().hex,
        "host": normalize_host(data.get("host")),
        "address": normalize_address(data.get("address")),
        "scope": scope,
        "sensor_id": sensor_id,
        "note": note,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_by": actor,
    }


def _place(entry: dict[str, Any]) -> tuple[str, str]:
    return str(entry.get("scope") or SCOPE_ALL), str(entry.get("sensor_id") or "").lower()


def put_entry(document: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    """Add ``entry`` or replace the one with its id; returns the row it replaced.

    One name has one address per place: a second row for the same name and
    place is refused, so the table never depends on row order.
    """
    entries = document.setdefault("entries", [])
    previous = next((item for item in entries if item.get("id") == entry["id"]), None)
    for item in entries:
        if item is not previous and item.get("host") == entry["host"] and _place(item) == _place(entry):
            raise HostOverrideError("This name already has an address for that place. Edit that row instead", "host", 409)
    if previous is None:
        if len(entries) >= MAX_ENTRIES:
            raise HostOverrideError(f"At most {MAX_ENTRIES} manual addresses may be stored", "host")
        entries.append(entry)
        return None
    entries[entries.index(previous)] = entry
    return previous


def remove_entry(document: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    entries = document.get("entries", [])
    found = next((item for item in entries if item.get("id") == entry_id), None)
    if found is not None:
        entries.remove(found)
    return found


def resolve_for(entries: Iterable[dict[str, Any]], place: str) -> dict[str, str]:
    """Name -> address at one place (``"central"`` or a sensor id).

    A row for that place wins over an "everywhere" row for the same name. Rows
    that are not a name and an address are skipped, as in the poller.
    """
    place = str(place or "").lower()
    table: dict[str, str] = {}
    specific: set[str] = set()
    for entry in entries:
        try:
            host = normalize_host(entry.get("host"))
            address = normalize_address(entry.get("address"))
        except HostOverrideError:
            continue
        scope, sensor_id = _place(entry)
        if scope == SCOPE_ALL:
            if host not in specific:
                table[host] = address
        elif scope == SCOPE_CENTRAL and place == CENTRAL:
            table[host] = address
            specific.add(host)
        elif scope == SCOPE_SENSOR and place and place != CENTRAL and sensor_id == place:
            table[host] = address
            specific.add(host)
    return table


def lookup(table: dict[str, str] | None, host: Any) -> str | None:
    """The manual address for ``host``; addresses and unknown names give None."""
    if not table or not host:
        return None
    return table.get(str(host).strip().rstrip(".").lower())


def sensor_payload(entries: Iterable[dict[str, Any]], sensor_id: Any) -> list[dict[str, str]]:
    """What one sensor receives: only the rows that apply to it, already chosen."""
    table = resolve_for(entries, str(sensor_id))
    return [{"host": host, "address": table[host]} for host in sorted(table)]


async def central_overrides(db) -> dict[str, str]:
    """The table the central appliance uses, for on-demand tests."""
    return resolve_for((await load_host_overrides(db))["entries"], CENTRAL)


def sensor_applies(capabilities: Any) -> bool:
    return isinstance(capabilities, (list, tuple)) and SENSOR_CAPABILITY in capabilities
