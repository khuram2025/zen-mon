"""Shared helpers for the User Device Tracker (UDT) module.

Rule matching (watch / allow / ignore lists), MAC normalization and
endpoint-type classification used by the API router, the sweeper and
the alert evaluator.
"""

from __future__ import annotations

import ipaddress
import logging
import re

logger = logging.getLogger("zenplus.udt")

MAC_RE = re.compile(r"^[0-9a-f]{12}$")

ENDPOINT_TYPES = (
    "workstation", "server", "phone", "printer", "access_point", "camera",
    "virtual", "network", "iot", "unknown",
)


def normalize_mac(raw: str) -> str | None:
    """Accept aa:bb:cc:dd:ee:ff / aa-bb-.. / aabb.ccdd.eeff / aabbccddeeff
    and return canonical aa:bb:cc:dd:ee:ff, or None if not a MAC."""
    hexonly = re.sub(r"[^0-9a-fA-F]", "", raw or "").lower()
    if not MAC_RE.match(hexonly):
        return None
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))


def mac_prefix_hex(raw: str) -> str | None:
    """Normalize a partial MAC / OUI prefix to bare lowercase hex."""
    hexonly = re.sub(r"[^0-9a-fA-F]", "", raw or "").lower()
    if not hexonly or len(hexonly) > 12:
        return None
    return hexonly


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def wildcard_to_like(pattern: str) -> str:
    """Convert a user glob (* and ?) into a SQL LIKE pattern."""
    return _like_escape(pattern).replace("*", "%").replace("?", "_")


def rule_condition(rule: dict, params: dict, i: int) -> str | None:
    """Build one SQL condition over udt_endpoints e for a udt_rules row.

    Adds bind params into `params` using suffix `i`. Returns None for
    rules whose pattern fails to parse (they match nothing).
    """
    mt = rule["match_type"]
    pat = (rule["pattern"] or "").strip()
    if not pat:
        return None
    key = f"r{i}"
    if mt == "mac":
        mac = normalize_mac(pat)
        if not mac:
            return None
        params[key] = mac
        return f"e.mac = CAST(:{key} AS macaddr)"
    if mt == "mac_prefix":
        prefix = mac_prefix_hex(pat)
        if not prefix:
            return None
        params[key] = prefix + "%"
        return f"replace(e.mac::text, ':', '') LIKE :{key}"
    if mt == "ip":
        try:
            ipaddress.ip_address(pat)
        except ValueError:
            return None
        params[key] = pat
        return (
            f"(e.ip_address = CAST(:{key} AS inet) OR EXISTS ("
            f"SELECT 1 FROM udt_ip_history h WHERE h.endpoint_id = e.id AND h.active AND h.ip = CAST(:{key} AS inet)))"
        )
    if mt == "ip_range":
        parts = [p.strip() for p in pat.split("-", 1)]
        if len(parts) != 2:
            return None
        try:
            lo, hi = ipaddress.ip_address(parts[0]), ipaddress.ip_address(parts[1])
        except ValueError:
            return None
        params[key + "a"], params[key + "b"] = str(lo), str(hi)
        return f"(e.ip_address >= CAST(:{key}a AS inet) AND e.ip_address <= CAST(:{key}b AS inet))"
    if mt == "subnet":
        try:
            ipaddress.ip_network(pat, strict=False)
        except ValueError:
            return None
        params[key] = pat
        return f"e.ip_address <<= CAST(:{key} AS inet)"
    if mt == "hostname":
        params[key] = wildcard_to_like(pat)
        return f"e.hostname ILIKE :{key}"
    if mt == "vendor":
        params[key] = "%" + wildcard_to_like(pat).strip("%") + "%"
        return f"e.vendor ILIKE :{key}"
    if mt == "user":
        params[key] = wildcard_to_like(pat)
        return f"e.user_name ILIKE :{key}"
    return None


def rules_where(rules: list[dict], params: dict) -> str | None:
    """OR-combined condition over udt_endpoints e for a rule list, or
    None when no rule yields a usable condition."""
    conds = []
    for i, r in enumerate(rules):
        c = rule_condition(r, params, i)
        if c:
            conds.append(c)
    if not conds:
        return None
    return "(" + " OR ".join(conds) + ")"


# ── Endpoint type classification ─────────────────────────────────────
# Heuristic, vendor/hostname based. Operators can override per endpoint;
# the sweeper only reclassifies rows still marked with a lower-confidence
# auto type ('unknown' or a previous auto guess stays refreshable).

_CLASS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("virtual", re.compile(r"vmware|qemu|virtualbox|xen(source)?|parallels|nutanix|proxmox", re.I)),
    ("phone", re.compile(r"polycom|yealink|avaya|snom|grandstream|mitel|spectralink", re.I)),
    ("printer", re.compile(r"hp inc|hewlett|lexmark|kyocera|ricoh|canon|xerox|brother|zebra tech|epson", re.I)),
    ("camera", re.compile(r"hikvision|dahua|axis comm|hanwha|vivotek|reolink|lorex", re.I)),
    ("access_point", re.compile(r"ubiquiti|aruba|ruckus|mist sys|cambium|extreme netw|engenius", re.I)),
    ("network", re.compile(r"cisco systems|juniper|arista|fortinet|palo alto|mikrotik|tp-link|netgear|d-link|huawei tech", re.I)),
    ("iot", re.compile(r"espressif|raspberry|tuya|sonoff|shelly|nest labs|ring llc|ecobee|philips lighting|signify", re.I)),
    ("workstation", re.compile(r"apple|dell|lenovo|asus|acer|msi|framework|micro-star|wistron|compal|quanta|liteon|azurewave|intel corporate", re.I)),
    ("server", re.compile(r"super\s?micro|synology|qnap|ilo|idrac|american megatrends", re.I)),
]

_HOSTNAME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("phone", re.compile(r"^sep[0-9a-f]{12}$|phone", re.I)),
    ("printer", re.compile(r"printer|prn-|-prt", re.I)),
    ("camera", re.compile(r"^cam-|camera|ipcam", re.I)),
    ("access_point", re.compile(r"^ap-|-ap\d|accesspoint", re.I)),
    ("server", re.compile(r"^srv-|^esxi|^nas-|server", re.I)),
]


def classify_endpoint(vendor: str | None, hostname: str | None, device_type: str | None) -> str:
    """Best-effort endpoint type from monitored-device link, hostname
    and OUI vendor."""
    if device_type:
        mapped = {
            "switch": "network", "router": "network", "firewall": "network",
            "server": "server", "access_point": "access_point",
            "printer": "printer",
        }.get(device_type)
        if mapped:
            return mapped
    for etype, rx in _HOSTNAME_PATTERNS:
        if hostname and rx.search(hostname):
            return etype
    for etype, rx in _CLASS_PATTERNS:
        if vendor and rx.search(vendor):
            return etype
    return "unknown"
