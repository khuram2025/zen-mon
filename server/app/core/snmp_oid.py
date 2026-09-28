"""sysObjectID normalisation shared by discovery and template matching."""


def normalize_oid(oid: str | None) -> str:
    """Canonical numeric OID without a leading dot.

    net-snmp prints ``iso.3.6.1…`` without MIBs, gosnmp ``.1.3.6.1…``; both
    must compare equal to the ``1.3.6.1…`` prefixes in match rules.
    """
    value = (oid or "").strip().strip('"')
    if value.lower().startswith("iso."):
        value = "1." + value[4:]
    elif value.lower() == "iso":
        value = "1"
    return value.lstrip(".")


def display_oid(oid: str | None) -> str | None:
    """Stored form, matching what the poller writes (``.1.3.6.1…``)."""
    value = normalize_oid(oid)
    return f".{value}" if value else None
