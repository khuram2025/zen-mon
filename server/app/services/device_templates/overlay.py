"""Customised templates: a parent's metric groups plus recorded differences.

A customised template ("overlay") stores only what differs from its parent,
so the parent (usually a built-in) keeps receiving release updates and the
customer's changes are never overwritten. ``device_profiles.oid_groups`` of
an overlay holds the resolved result; the poller and the device page read it
like any other template.

Overlay document (every key optional)::

    {
      "disable_groups":  ["vdom_resources"],
      "disable_metrics": {"system": ["disk_pct"]},
      "group_changes":   {"system": {"interval_seconds": 300, "name": "...", "tab": "...",
                                     "description": "...", "applies_when": {...}}},
      "metric_changes":  {"system": {"cpu_pct": {"name": "...", "unit": "%",
                                                  "thresholds": {"warn": 80, "crit": 95, "op": ">="},
                                                  "labels": {...}}}},
      "add_groups":      [<OidGroup>, ...],
      "add_metrics":     {"system": [<OidMetric>, ...]}
    }

A change that points at a group or metric the parent no longer has is kept in
the overlay, skipped during resolution and reported as a warning, so an
update that renames a key never silently loses the customer's intent.
"""
from __future__ import annotations

import copy
from typing import Any

OVERLAY_KEYS = ("disable_groups", "disable_metrics", "group_changes", "metric_changes", "add_groups", "add_metrics")
GROUP_CHANGE_FIELDS = ("name", "interval_seconds", "tab", "description", "applies_when")
METRIC_CHANGE_FIELDS = ("name", "unit", "thresholds", "labels")
MAX_DEPTH = 3
# Values a stored group or metric may omit; compared as if present.
_GROUP_DEFAULTS = {"kind": "scalar", "interval_seconds": 0}
_METRIC_DEFAULTS = {"type": "gauge"}


def _val(obj: dict, field: str, defaults: dict):
    v = obj.get(field)
    if v is None or v == {} or v == []:
        return defaults.get(field)
    return v


def empty() -> dict:
    return {}


def normalise(overlay: dict | None) -> dict:
    """Drop unknown keys and empty sections; keep a stable shape."""
    src = overlay or {}
    out: dict[str, Any] = {}
    if src.get("disable_groups"):
        out["disable_groups"] = sorted({str(k) for k in src["disable_groups"]})
    if src.get("disable_metrics"):
        dm = {g: sorted({str(m) for m in ms}) for g, ms in src["disable_metrics"].items() if ms}
        if dm:
            out["disable_metrics"] = dm
    if src.get("group_changes"):
        gc = {g: {f: v for f, v in ch.items() if f in GROUP_CHANGE_FIELDS}
              for g, ch in src["group_changes"].items() if isinstance(ch, dict)}
        gc = {g: ch for g, ch in gc.items() if ch}
        if gc:
            out["group_changes"] = gc
    if src.get("metric_changes"):
        mc: dict[str, dict] = {}
        for g, metrics in src["metric_changes"].items():
            if not isinstance(metrics, dict):
                continue
            kept = {m: {f: v for f, v in ch.items() if f in METRIC_CHANGE_FIELDS}
                    for m, ch in metrics.items() if isinstance(ch, dict)}
            kept = {m: ch for m, ch in kept.items() if ch}
            if kept:
                mc[g] = kept
        if mc:
            out["metric_changes"] = mc
    if src.get("add_groups"):
        out["add_groups"] = [g for g in src["add_groups"] if isinstance(g, dict)]
    if src.get("add_metrics"):
        am = {g: [m for m in ms if isinstance(m, dict)] for g, ms in src["add_metrics"].items() if ms}
        if am:
            out["add_metrics"] = am
    return out


def problems(overlay: dict, parent_groups: list[dict]) -> list[str]:
    """Errors that make an overlay unsavable against the current parent."""
    errs: list[str] = []
    parent_keys = {g.get("key") for g in parent_groups}
    for g in overlay.get("add_groups", []):
        if g.get("key") in parent_keys:
            errs.append(f"added group '{g.get('key')}' already exists in the parent template")
    seen: set[str] = set()
    for g in overlay.get("add_groups", []):
        if g.get("key") in seen:
            errs.append(f"group '{g.get('key')}' is added twice")
        seen.add(g.get("key"))
    by_key = {g.get("key"): g for g in parent_groups}
    for gk, metrics in overlay.get("add_metrics", {}).items():
        group = by_key.get(gk)
        if group is None:
            errs.append(f"metrics added to group '{gk}', which the parent template does not have")
            continue
        existing = {m.get("key") for m in group.get("metrics") or []}
        for m in metrics:
            if m.get("key") in existing:
                errs.append(f"metric '{gk}.{m.get('key')}' already exists in the parent template")
    return errs


def resolve(parent_groups: list[dict], overlay: dict | None) -> tuple[list[dict], list[str]]:
    """The parent's groups with the overlay applied, and warnings for skipped entries."""
    ov = normalise(overlay)
    groups = copy.deepcopy(parent_groups or [])
    warnings: list[str] = []
    keys = {g.get("key") for g in groups}

    for gk in ov.get("disable_groups", []):
        if gk not in keys:
            warnings.append(f"disabled group '{gk}' is no longer in the parent template")
    for gk in ov.get("group_changes", {}):
        if gk not in keys:
            warnings.append(f"changes to group '{gk}' skipped: no longer in the parent template")
    for gk in ov.get("metric_changes", {}):
        if gk not in keys:
            warnings.append(f"metric changes in group '{gk}' skipped: no longer in the parent template")
    for gk in ov.get("disable_metrics", {}):
        if gk not in keys:
            warnings.append(f"disabled metrics in group '{gk}' skipped: no longer in the parent template")

    disabled = set(ov.get("disable_groups", []))
    out: list[dict] = []
    for g in groups:
        gk = g.get("key")
        if gk in disabled:
            continue
        for f, v in ov.get("group_changes", {}).get(gk, {}).items():
            g[f] = v
        metrics = g.get("metrics") or []
        mkeys = {m.get("key") for m in metrics}
        off = set(ov.get("disable_metrics", {}).get(gk, []))
        for mk in off - mkeys:
            warnings.append(f"disabled metric '{gk}.{mk}' is no longer in the parent template")
        changes = ov.get("metric_changes", {}).get(gk, {})
        for mk in set(changes) - mkeys:
            warnings.append(f"changes to metric '{gk}.{mk}' skipped: no longer in the parent template")
        kept = []
        for m in metrics:
            if m.get("key") in off:
                continue
            for f, v in changes.get(m.get("key"), {}).items():
                if v is None:
                    m.pop(f, None)
                else:
                    m[f] = v
            kept.append(m)
        for m in ov.get("add_metrics", {}).get(gk, []):
            if m.get("key") in mkeys:
                warnings.append(f"added metric '{gk}.{m.get('key')}' skipped: the parent template now has it")
                continue
            kept.append(copy.deepcopy(m))
        g["metrics"] = kept
        if kept or g.get("children") or g.get("display"):
            out.append(g)
        else:
            warnings.append(f"group '{gk}' has no metrics left and is not polled")
    for gk in set(ov.get("add_metrics", {})) - keys:
        warnings.append(f"metrics added to group '{gk}' skipped: no longer in the parent template")
    for g in ov.get("add_groups", []):
        if g.get("key") in keys:
            warnings.append(f"added group '{g.get('key')}' skipped: the parent template now has a group with that key")
            continue
        out.append(copy.deepcopy(g))
    return out, warnings


def diff_to_overlay(parent_groups: list[dict], edited_groups: list[dict]) -> dict:
    """The overlay that turns ``parent_groups`` into ``edited_groups``.

    Used when an operator edits the resolved groups of a customised template
    in the form editor: the result is stored as differences only. Changes the
    overlay cannot express (an OID, a table definition, children mapping) are
    rejected by the caller through ``problems_from_edit``.
    """
    parent = {g.get("key"): g for g in parent_groups}
    edited = {g.get("key"): g for g in edited_groups}
    ov: dict[str, Any] = {}
    disable = [k for k in parent if k not in edited]
    if disable:
        ov["disable_groups"] = disable
    add = [copy.deepcopy(g) for k, g in edited.items() if k not in parent]
    if add:
        ov["add_groups"] = add
    gc: dict[str, dict] = {}
    mc: dict[str, dict] = {}
    dm: dict[str, list] = {}
    am: dict[str, list] = {}
    for k, pg in parent.items():
        eg = edited.get(k)
        if eg is None:
            continue
        changes = {f: eg.get(f) for f in GROUP_CHANGE_FIELDS
                   if _val(eg, f, _GROUP_DEFAULTS) != _val(pg, f, _GROUP_DEFAULTS)}
        if changes:
            gc[k] = changes
        pm = {m.get("key"): m for m in pg.get("metrics") or []}
        em = {m.get("key"): m for m in eg.get("metrics") or []}
        off = [mk for mk in pm if mk not in em]
        if off:
            dm[k] = off
        added = [copy.deepcopy(m) for mk, m in em.items() if mk not in pm]
        if added:
            am[k] = added
        for mk, m in pm.items():
            e = em.get(mk)
            if e is None:
                continue
            ch = {f: e.get(f) for f in METRIC_CHANGE_FIELDS
                  if _val(e, f, _METRIC_DEFAULTS) != _val(m, f, _METRIC_DEFAULTS)}
            if ch:
                mc.setdefault(k, {})[mk] = ch
    if gc:
        ov["group_changes"] = gc
    if mc:
        ov["metric_changes"] = mc
    if dm:
        ov["disable_metrics"] = dm
    if am:
        ov["add_metrics"] = am
    return normalise(ov)


_LOCKED_GROUP_FIELDS = ("kind", "table", "children", "display", "belongs_to")
_LOCKED_METRIC_FIELDS = ("oid", "type", "scale", "value_map", "format")


def problems_from_edit(parent_groups: list[dict], edited_groups: list[dict]) -> list[str]:
    """Edits to inherited groups that an overlay cannot carry (they would fork the parent)."""
    errs: list[str] = []
    parent = {g.get("key"): g for g in parent_groups}
    for eg in edited_groups:
        pg = parent.get(eg.get("key"))
        if pg is None:
            continue
        for f in _LOCKED_GROUP_FIELDS:
            if _val(eg, f, _GROUP_DEFAULTS) != _val(pg, f, _GROUP_DEFAULTS):
                errs.append(f"group '{eg.get('key')}': '{f}' comes from the parent template and cannot be changed here")
        pm = {m.get("key"): m for m in pg.get("metrics") or []}
        for m in eg.get("metrics") or []:
            base = pm.get(m.get("key"))
            if base is None:
                continue
            for f in _LOCKED_METRIC_FIELDS:
                if _val(m, f, _METRIC_DEFAULTS) != _val(base, f, _METRIC_DEFAULTS):
                    errs.append(f"metric '{eg.get('key')}.{m.get('key')}': '{f}' comes from the parent template "
                                "and cannot be changed here (add a new metric instead)")
    return errs
