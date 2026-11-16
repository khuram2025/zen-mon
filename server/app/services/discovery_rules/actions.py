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
    ActionSpec("group", "Device group", "single", "ref", ("devices.manage",), ("device",),
               source="device_groups"),
    ActionSpec("template", "Monitoring template", "single", "ref", ("devices.manage",), ("device",),
               source="device_profiles"),
    ActionSpec("device_type", "Device type", "single", "text", ("devices.manage",), ("device",),
               help="router, switch, firewall, access_point, load_balancer, wireless_controller, "
                    "printer, ups, storage, server or other"),
    ActionSpec("location", "Location", "single", "text", ("devices.manage",), ("device",)),
    ActionSpec("snmp_credential", "SNMP credential", "single", "ref", ("devices.manage",), ("device",),
               source="snmp_credentials",
               help="Stored credential, referenced by id; no secret is copied into the rule."),
    ActionSpec("ping_interval", "Ping interval (seconds)", "single", "int", ("devices.manage",), ("device",),
               min=10, max=3600),
    ActionSpec("snmp_interval", "SNMP poll interval (seconds)", "single", "int", ("devices.manage",),
               ("device",), min=30, max=3600),
    ActionSpec("tags", "Add tags", "additive", "tags", ("devices.manage",), ("device", "server"),
               help="Tags from every matching rule are merged. A tag is how a new device becomes "
                    "visible to a team whose view is limited by tags."),
)}

DEVICE_TYPES = ("router", "switch", "firewall", "access_point", "load_balancer", "wireless_controller",
                "printer", "ups", "storage", "server", "other")


class ActionError(ValueError):
    pass


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise ActionError(f"{label}: choose a value") from None


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
    elif spec.value == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ActionError(f"{spec.label}: a whole number is required") from None
        if (spec.min is not None and n < spec.min) or (spec.max is not None and n > spec.max):
            raise ActionError(f"{spec.label}: between {spec.min} and {spec.max}")
        out["value"] = n
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


def permissions_needed(actions: list[dict]) -> set[str]:
    needed: set[str] = set()
    for a in actions:
        spec = ACTIONS.get(a.get("key"))
        if spec:
            needed.update(spec.permission)
        if a.get("key") == "decision" and a.get("import_as") in ("server", "both", "auto"):
            # "auto" creates a server for a server-class host; the default
            # ("device") needs only the device permissions.
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
