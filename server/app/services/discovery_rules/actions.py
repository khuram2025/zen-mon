"""Phase 1 actions: the import decision and the settings an imported asset gets.

Each action is either *single-valued* (the first matching rule, in priority
order, sets it; a later matching rule that also sets it is recorded as
shadowed) or *additive* (every matching rule contributes; tags merge).

Each action names the permission a user needs to activate a rule that uses it
and the asset kinds it applies to. Values that point at other records (group,
template, SNMP credential) are ids; ``validate`` checks the shape here and the
API checks that the referenced row exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

DECISIONS = ("auto_import", "review", "ignore")
IMPORT_AS = ("auto", "device", "server", "both")


@dataclass(frozen=True)
class ActionSpec:
    key: str
    label: str
    kind: str                 # "single" | "additive"
    value: str                # builder input: decision, ref, text, int, tags
    permission: tuple[str, ...]
    asset_kinds: tuple[str, ...]
    source: str | None = None  # reference catalog for "ref" values
    help: str = ""
    min: int | None = None
    max: int | None = None


ACTIONS: dict[str, ActionSpec] = {a.key: a for a in (
    ActionSpec("decision", "Import decision", "single", "decision", ("discovery.run",), ("device", "server"),
               help="Auto-import, keep for review, or ignore. Auto-import and ignore only happen on "
                    "profiles whose import mode allows them; otherwise the result shows what the rule "
                    "would do."),
    ActionSpec("group", "Device group", "single", "ref", ("devices.manage",), ("device", "child"),
               source="device_groups"),
    ActionSpec("template", "Monitoring template", "single", "ref", ("devices.manage",), ("device",),
               source="device_profiles"),
    ActionSpec("device_type", "Device type", "single", "text", ("devices.manage",), ("device",),
               help="router, switch, firewall, access_point, load_balancer, wireless_controller, "
                    "printer, ups, storage, server or other"),
    ActionSpec("location", "Location", "single", "text", ("devices.manage",), ("device", "child")),
    ActionSpec("snmp_credential", "SNMP credential", "single", "ref", ("devices.manage",), ("device",),
               source="snmp_credentials",
               help="Stored credential, referenced by id; no secret is copied into the rule."),
    ActionSpec("ping_interval", "Ping interval (seconds)", "single", "int", ("devices.manage",), ("device",),
               min=10, max=3600),
    ActionSpec("snmp_interval", "SNMP poll interval (seconds)", "single", "int", ("devices.manage",),
               ("device",), min=30, max=3600),
    ActionSpec("owner", "Owner", "single", "text", ("devices.manage",), ("device", "server", "child"),
               help="Owner of the device or server (free text)."),
    ActionSpec("environment", "Server environment", "single", "text", ("servers.manage",), ("server",),
               help="production, staging, development, ..."),
    ActionSpec("ping_enabled", "Turn ping monitoring on", "single", "on", ("devices.manage",), ("device",),
               help="Only turns ping on; a rule never turns monitoring off."),
    ActionSpec("map", "Add to network map", "additive", "ref", ("maps.manage",), ("device", "server", "child"),
               source="manual_maps",
               help="Places the device or server on the map (bottom row, to be arranged); never removes nodes "
                    "an operator placed."),
    ActionSpec("dependency_parent", "Depends on (uplink parent)", "single", "ref", ("devices.manage",),
               ("device",), source="devices",
               help="Adds an uplink dependency on this device, so alerts are suppressed while it is down. "
                    "Existing dependencies are kept."),
    # Phase 4
    ActionSpec("interfaces", "Monitor or ignore interfaces", "additive", "interfaces", ("devices.manage",),
               ("device",),
               help="Sets which interfaces are monitored (interface alerts, utilisation, reports) by name, alias, "
                    "description, IF-MIB type or speed. For each interface the first matching rule, in priority "
                    "order, decides; interfaces no rule matches are left as they are."),
    ActionSpec("sensor", "Monitor from sensor", "additive", "ref", ("devices.manage",), ("device",),
               source="sensors",
               help="Adds the sensor as a monitoring location, like the device's Monitoring locations card. "
                    "Without an explicit choice there, the controller stops polling a device once a sensor "
                    "does. Never removes a location an operator chose."),
    ActionSpec("agent_policy", "Agent policy", "single", "ref", ("servers.manage",), ("server",),
               source="agent_policies",
               help="The policy the server's agent runs (collection intervals, watch lists, update ring). "
                    "Servers without an agent are skipped."),
    ActionSpec("service_template", "Add service check from template", "additive", "ref",
               ("service_checks.manage",), ("device",), source="service_check_templates",
               help="Creates the template's service check for the device (named \"<template> · <hostname>\") "
                    "unless one exists. Undo and un-match leave created checks in place."),
    ActionSpec("tags", "Add tags", "additive", "tags", ("devices.manage",), ("device", "server", "child"),
               help="Tags from every matching rule are merged. A tag is how a new device becomes "
                    "visible to a team whose view is limited by tags."),
)}

INTERFACE_FIELDS = ("if_name", "if_alias", "if_descr", "if_type", "speed")
INTERFACE_SETS = ("monitor", "ignore")
MAX_INTERFACE_PATTERNS = 10
MAX_INTERFACE_PATTERN = 200

DEVICE_TYPES = ("router", "switch", "firewall", "access_point", "load_balancer", "wireless_controller",
                "printer", "ups", "storage", "server", "other")


class ActionError(ValueError):
    pass


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise ActionError(f"{label}: choose a value") from None


def _speed_range(pattern: str) -> tuple[float | None, float | None]:
    """"100-1000", "10000-", "-100" or "1000" (Mbit/s) -> (min, max)."""
    p = pattern.replace(" ", "")
    lo, sep, hi = p.partition("-")
    try:
        lo_v = float(lo) if lo else None
        hi_v = float(hi) if hi else None
    except ValueError:
        raise ActionError("Interface speed: Mbit/s as 1000, 100-1000, 10000- or -100") from None
    if not sep:
        hi_v = lo_v
    if lo_v is None and hi_v is None:
        raise ActionError("Interface speed: give a minimum, a maximum or both")
    if lo_v is not None and hi_v is not None and lo_v > hi_v:
        raise ActionError("Interface speed: the minimum is above the maximum")
    return lo_v, hi_v


def _interface_patterns(value: Any) -> list[dict]:
    """[{set: monitor|ignore, field, pattern}] in the order they are tested."""
    items = value if isinstance(value, list) else [value]
    out: list[dict] = []
    for n, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ActionError(f"Interface rule {n}: expected set, field and pattern")
        setv, field, pattern = raw.get("set"), raw.get("field"), str(raw.get("pattern") or "").strip()
        if setv not in INTERFACE_SETS:
            raise ActionError(f"Interface rule {n}: monitor or ignore")
        if field not in INTERFACE_FIELDS:
            raise ActionError(f"Interface rule {n}: field is one of {', '.join(INTERFACE_FIELDS)}")
        if not pattern or len(pattern) > MAX_INTERFACE_PATTERN:
            raise ActionError(f"Interface rule {n}: a pattern is required (at most {MAX_INTERFACE_PATTERN} characters)")
        if field == "if_type":
            parts = [p.strip() for p in pattern.split(",") if p.strip()]
            if not parts or not all(p.isdigit() for p in parts):
                raise ActionError(f"Interface rule {n}: IF-MIB type numbers separated by commas (6, 24, 131)")
            pattern = ",".join(parts)
        elif field == "speed":
            _speed_range(pattern)
        else:
            from .conditions import ConditionError, validate_regex
            try:
                validate_regex(pattern)
            except ConditionError as e:
                raise ActionError(f"Interface rule {n}: {e}") from None
        out.append({"set": setv, "field": field, "pattern": pattern})
    if not out:
        raise ActionError("Monitor or ignore interfaces: add at least one pattern")
    if len(out) > MAX_INTERFACE_PATTERNS:
        raise ActionError(f"A rule has at most {MAX_INTERFACE_PATTERNS} interface patterns")
    return out


def interface_matches(item: dict, iface: dict) -> bool:
    """One interface pattern against one interface row (pure). Text patterns
    are case-insensitive regular expressions, like condition regexes."""
    from .conditions import MAX_SUBJECT, _regex
    field, pattern = item["field"], item["pattern"]
    if field == "if_type":
        t = iface.get("if_type")
        try:
            return t is not None and int(t) in {int(p) for p in pattern.split(",")}
        except (TypeError, ValueError):
            return False
    if field == "speed":
        bps = iface.get("configured_speed_bps") or iface.get("if_speed")
        if not bps:
            return False
        mbps = float(bps) / 1_000_000
        lo, hi = _speed_range(pattern)
        return (lo is None or mbps >= lo) and (hi is None or mbps <= hi)
    try:
        rx = _regex(pattern)
    except Exception:
        return False
    return bool(rx.search(str(iface.get(field) or "")[:MAX_SUBJECT]))


def validate_action(raw: Any) -> dict:
    """Normalised ``{"key": ..., "value": ..., ["import_as": ...]}``."""
    if not isinstance(raw, dict):
        raise ActionError("An action must be an object")
    key = raw.get("key")
    spec = ACTIONS.get(key)
    if spec is None:
        raise ActionError(f"Unknown action: {key!r}")
    value = raw.get("value")
    out: dict[str, Any] = {"key": key}
    if spec.value == "decision":
        if value not in DECISIONS:
            raise ActionError("Import decision: auto_import, review or ignore")
        out["value"] = value
        if value == "auto_import":
            import_as = raw.get("import_as") or "device"
            if import_as not in IMPORT_AS:
                raise ActionError("Import as: auto, device, server or both")
            out["import_as"] = import_as
    elif spec.value == "ref":
        out["value"] = _uuid(value, spec.label)
    elif spec.value == "on":
        if value not in (True, "true", "on", 1):
            raise ActionError(f"{spec.label}: a rule can only turn it on")
        out["value"] = True
    elif spec.value == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ActionError(f"{spec.label}: a whole number is required") from None
        if (spec.min is not None and n < spec.min) or (spec.max is not None and n > spec.max):
            raise ActionError(f"{spec.label}: between {spec.min} and {spec.max}")
        out["value"] = n
    elif spec.value == "interfaces":
        out["value"] = _interface_patterns(value)
    elif spec.value == "tags":
        tags = value if isinstance(value, list) else str(value or "").split(",")
        clean: list[str] = []
        for t in tags:
            t = str(t).strip()
            if t and t.lower() not in {c.lower() for c in clean}:
                if len(t) > 64:
                    raise ActionError("Tags are limited to 64 characters")
                clean.append(t)
        if not clean:
            raise ActionError("Add tags: give at least one tag")
        if len(clean) > 20:
            raise ActionError("A rule adds at most 20 tags")
        out["value"] = clean
    else:  # text
        text = str(value or "").strip()
        if not text:
            raise ActionError(f"{spec.label}: a value is required")
        if spec.key == "device_type":
            text = text.lower()
            if len(text) > 50:
                raise ActionError("Device type is limited to 50 characters")
        elif len(text) > 255:
            raise ActionError(f"{spec.label} is limited to 255 characters")
        out["value"] = text
    return out


def validate_actions(raw: Any, asset_kinds: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        raise ActionError("Actions must be a list")
    if not raw:
        raise ActionError("A rule needs at least one action")
    seen: set[str] = set()
    out: list[dict] = []
    for item in raw:
        action = validate_action(item)
        if action["key"] in seen:
            raise ActionError(f"{ACTIONS[action['key']].label} is set twice in one rule")
        seen.add(action["key"])
        spec = ACTIONS[action["key"]]
        if not set(asset_kinds) & set(spec.asset_kinds):
            raise ActionError(f"{spec.label} does not apply to {', '.join(asset_kinds)}")
        out.append(action)
    return out


def permissions_needed(actions: list[dict], asset_kinds: list[str] | None = None) -> set[str]:
    """Permissions a user needs to activate (or run) a rule with ``actions``
    on ``asset_kinds``: devices.manage for settings it writes on devices,
    servers.manage for servers, discovery.run for the import decision."""
    kinds = set(asset_kinds or ["device"])
    needed: set[str] = set()
    for a in actions:
        spec = ACTIONS.get(a.get("key"))
        if not spec:
            continue
        if spec.key == "decision":
            needed.add("discovery.run")
            if a.get("import_as") in ("server", "both", "auto"):
                # "auto" creates a server for a server-class host; the default
                # ("device") needs only the device permissions.
                needed.add("servers.manage")
            continue
        if spec.key in ("map", "service_template"):
            needed.update(spec.permission)
            continue
        applies = kinds & set(spec.asset_kinds)
        if applies & {"device", "child"}:
            needed.update(p for p in spec.permission if p != "servers.manage")
        if "server" in applies:
            needed.add("servers.manage")
    return needed


def catalog() -> list[dict]:
    return [
        {
            "key": s.key, "label": s.label, "kind": s.kind, "value": s.value,
            "permission": list(s.permission), "asset_kinds": list(s.asset_kinds),
            "source": s.source, "help": s.help, "min": s.min, "max": s.max,
        }
        for s in ACTIONS.values()
    ]
