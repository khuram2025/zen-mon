"""Typed facts a rule condition can test.

Phase 1 facts come from one discovery scan result (``discovery_results_v2``).
Every fact has a type; the type decides which operators a condition may use
(see ``conditions.OPERATORS``). The same catalog drives the API's
``GET /discovery-v2/rules/catalog`` and so the rule builder in the dashboard.
"""

from __future__ import annotations

from typing import Any

from app.core.snmp_oid import normalize_oid

# key -> (type, label, help)
FIELDS: dict[str, tuple[str, str, str]] = {
    "ip_address": ("ip", "IP address", "Address the scan reached"),
    "hostname": ("text", "Hostname", "Reverse DNS or SNMP name"),
    "fqdn": ("text", "FQDN", "Fully qualified name from reverse DNS"),
    "sys_name": ("text", "sysName", "SNMP sysName"),
    "sys_descr": ("text", "sysDescr", "SNMP sysDescr"),
    "sys_location": ("text", "sysLocation", "SNMP sysLocation"),
    "sys_object_id": ("oid", "sysObjectID", "SNMP sysObjectID, dotted (1.3.6.1.4.1...)"),
    "vendor": ("text", "Vendor", "Vendor the scan identified"),
    "model": ("text", "Model", "Model the scan identified"),
    "device_type": ("text", "Device type", "Type the scan identified (router, switch, firewall, ...)"),
    "os": ("text", "Operating system", "OS the scan identified"),
    "os_version": ("version", "OS version", "Firmware or OS version"),
    "serial_number": ("text", "Serial number", "Serial number from SNMP"),
    "mac_address": ("text", "MAC address", "MAC from the ARP table"),
    "open_ports": ("set", "Open TCP ports", "Ports that answered the TCP scan"),
    "protocols_detected": ("set", "Protocols detected", "icmp, snmp, ssh, http, https, winrm ..."),
    "credential_status": ("enum", "Credential status", "valid, invalid, permission_issue, not_tested"),
    "snmp_credential_id": ("reference", "SNMP credential that worked", "Saved SNMP credential"),
    "confidence_score": ("number", "Identification confidence", "0 to 100"),
    "result_status": ("enum", "Result status", "new, existing, changed, unknown"),
    "profile_id": ("reference", "Discovery profile", "Profile that ran the scan"),
    "is_server": ("boolean", "Server-class host", "Windows / Linux / ESXi host rather than network gear"),
}

ENUM_VALUES: dict[str, list[str]] = {
    "credential_status": ["valid", "invalid", "permission_issue", "not_tested"],
    "result_status": ["new", "existing", "changed", "unknown"],
}

# reference fields -> the catalog the dashboard picks values from
REFERENCE_SOURCES: dict[str, str] = {
    "snmp_credential_id": "snmp_credentials",
    "profile_id": "discovery_profiles",
}

_SERVER_OS_HINTS = ("windows", "linux", "ubuntu", "debian", "centos", "rhel",
                    "red hat", "suse", "fedora", "esxi", "macos")


def is_server_like(device_type: str | None, os_name: str | None) -> bool:
    """Server-class host (vs network gear); the import's ``auto`` routing uses it too."""
    if (device_type or "").lower() == "server":
        return True
    os_l = (os_name or "").lower()
    return any(h in os_l for h in _SERVER_OS_HINTS)


def _ports(values) -> list[int]:
    out: list[int] = []
    for p in values or []:
        try:
            out.append(int(p))
        except (TypeError, ValueError):
            continue
    return out


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def facts_from_result(result: Any) -> dict[str, Any]:
    """Fact dict for one scan result (ORM row, mapping or plain dict).

    Unknown values are ``None``; the condition evaluator treats them as
    missing (see ``conditions``)."""
    raw = _get(result, "raw_data") or {}
    oid = normalize_oid(_get(result, "sys_object_id"))
    status = _get(result, "status")
    ip = _get(result, "ip_address")
    cred = _get(result, "credential_used")
    profile = _get(result, "profile_id")
    facts = {
        "ip_address": str(ip) if ip else None,
        "hostname": _get(result, "hostname"),
        "fqdn": _get(result, "fqdn"),
        "sys_name": _get(result, "sys_name"),
        "sys_descr": raw.get("sys_descr") if isinstance(raw, dict) else None,
        "sys_location": raw.get("sys_location") if isinstance(raw, dict) else None,
        "sys_object_id": oid or None,
        "vendor": _get(result, "vendor"),
        "model": _get(result, "model"),
        "device_type": _get(result, "device_type"),
        "os": _get(result, "os"),
        "os_version": _get(result, "os_version"),
        "serial_number": _get(result, "serial_number"),
        "mac_address": _get(result, "mac_address"),
        "open_ports": _ports(_get(result, "open_ports")),
        "protocols_detected": [str(p).lower() for p in (_get(result, "protocols_detected") or [])],
        "credential_status": _get(result, "credential_status"),
        "snmp_credential_id": str(cred) if cred else None,
        "confidence_score": _get(result, "confidence_score"),
        "result_status": status,
        "profile_id": str(profile) if profile else None,
        "is_server": is_server_like(_get(result, "device_type"), _get(result, "os")),
    }
    return facts


def asset_kind_of(facts: dict[str, Any]) -> str:
    """The kind of asset a scan result would become: ``server`` or ``device``."""
    return "server" if facts.get("is_server") else "device"


def sample_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Facts typed in by hand ("test a device"): unknown keys dropped,
    the same normalisation as a scan result."""
    clean = {k: v for k, v in (raw or {}).items() if k in FIELDS}
    if "sys_object_id" in clean:
        clean["sys_object_id"] = normalize_oid(clean["sys_object_id"]) or None
    if "open_ports" in clean:
        clean["open_ports"] = _ports(clean["open_ports"] if isinstance(clean["open_ports"], list)
                                     else str(clean["open_ports"]).replace(",", " ").split())
    if "protocols_detected" in clean and isinstance(clean["protocols_detected"], list):
        clean["protocols_detected"] = [str(p).lower() for p in clean["protocols_detected"]]
    if "is_server" not in clean:
        clean["is_server"] = is_server_like(clean.get("device_type"), clean.get("os"))
    for k in FIELDS:
        clean.setdefault(k, None)
    return clean
