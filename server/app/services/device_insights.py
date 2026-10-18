"""Template-insight shaping, health rollup and identity resolution for the
device page.

The device page shows three separate things that used to be easy to confuse:

* the **confirmed status** (Up / Degraded / Down), owned by the status machine
  and never changed here;
* the **health rollup**: what the device's own template readings, interfaces
  and alerts say is wrong right now ("Up · 3 issues");
* the **identity** (model, OS, serial), resolved once on the server so every
  tab shows the same value.

``shape_template_groups`` is the one shaping of ``device_template_values`` for
the template-insights API and the summary API. It applies two rules the raw
values cannot express on their own:

* **Applicability**: a group that describes a feature the device does not use
  (HA members on a standalone FortiGate) is "Not applicable" and is left out of
  health. Templates may declare ``applies_when``; built-in packs get a rule here.
* **Missing is not zero**: a table row whose own state says it is offline has
  no CPU, memory or client reading. Its gauges are blanked (shown as "—"), and a
  zero "capacity" reading is treated as not reported.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

SEV_RANK = {"ok": 0, "info": 0, "warn": 1, "crit": 2}

# Ring-redundancy metrics that report "open" on a standalone switch, keyed to
# the template group listing stack members.
STANDALONE_RING_METRICS = {"cisco_stack_ring": "stack_members"}

# Group applicability for built-in templates whose stored JSON predates
# ``applies_when``. A rule applies only when the template has the named metric,
# so it is inert for every other template.
BUILTIN_APPLICABILITY: dict[str, dict] = {
    "ha_members": {
        "group": "ha", "metric": "fgt_ha_mode", "not_in": [1],
        "reason": "HA mode is Standalone, so there are no cluster members to check.",
    },
}

NOT_APPLICABLE = "na"
SUPPRESSED_OFFLINE = "Not reported while offline"
NOT_REPORTED = "Not reported"

# Row states that mean the thing is not there to measure.
_ABSENT_STATE = re.compile(
    r"^\s*(offline|down|dead|not present|absent|disconnected|unreachable|removed|missing)\s*$", re.I)
_STATE_COLUMN = re.compile(r"status|state|presence|oper", re.I)
_CAPACITY = re.compile(r"capacity", re.I)


def metric_status(metric: dict, value_num, value_text: str) -> str:
    """Evaluate a template metric's status from its enum labels or thresholds."""
    labels = metric.get("labels") or {}
    if labels and value_num is not None:
        code = str(int(value_num)) if float(value_num).is_integer() else str(value_num)
        lab = labels.get(code)
        if lab:
            return lab.get("sev", "info")
    th = metric.get("thresholds") or {}
    if value_num is None or (th.get("warn") is None and th.get("crit") is None):
        return "none"
    op = th.get("op", ">=")
    v = float(value_num)
    if op == "<=":
        if th.get("crit") is not None and v <= th["crit"]:
            return "crit"
        if th.get("warn") is not None and v <= th["warn"]:
            return "warn"
    else:
        if th.get("crit") is not None and v >= th["crit"]:
            return "crit"
        if th.get("warn") is not None and v >= th["warn"]:
            return "warn"
    return "ok"


def metric_display(metric: dict, value_num, value_text: str) -> str:
    """Human display string for enum metrics (label text beats raw text/code)."""
    labels = metric.get("labels") or {}
    if value_num is not None and labels:
        code = str(int(value_num)) if float(value_num).is_integer() else str(value_num)
        lab = labels.get(code)
        if lab and lab.get("text"):
            return lab["text"]
    return value_text or ""


def _code(value) -> Any:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return int(f) if f.is_integer() else f


def group_applicability(gdef: dict, values_by_group: dict[str, list]) -> tuple[bool, str | None]:
    """(applicable, reason). ``applies_when`` on the group wins over the
    built-in rule; an unknown controlling reading keeps the group applicable."""
    rule = gdef.get("applies_when") or BUILTIN_APPLICABILITY.get(gdef.get("key") or "")
    if not isinstance(rule, dict) or not rule.get("metric"):
        return True, None
    candidates = values_by_group.get(rule.get("group") or "", []) if rule.get("group") else [
        v for vals in values_by_group.values() for v in vals]
    reading = next((v for v in candidates if v["metric_key"] == rule["metric"]), None)
    if reading is None:
        return True, None
    code = _code(reading["value_num"]) if reading["value_num"] is not None else reading["value_text"]
    if code is None:
        return True, None
    in_values = rule.get("in")
    not_in = rule.get("not_in")
    applicable = True
    if isinstance(in_values, list):
        applicable = code in [_code(x) for x in in_values]
    if isinstance(not_in, list) and code in [_code(x) for x in not_in]:
        applicable = False
    if applicable:
        return True, None
    return False, str(rule.get("reason") or "Not applicable to this device's configuration.")


def _row_absent(cells: dict, metrics_def: dict) -> bool:
    for key, cell in cells.items():
        m = metrics_def.get(key) or {}
        if m.get("type") != "enum":
            continue
        if not _STATE_COLUMN.search(f"{key} {m.get('name') or ''}"):
            continue
        if _ABSENT_STATE.match(cell.get("text") or ""):
            return True
    return False


def shape_template_groups(oid_groups: list[dict], vals: Iterable, child_ids: dict | None = None) -> tuple[list[dict], datetime | None]:
    """Shape ``device_template_values`` rows into the device page's groups.

    ``vals`` are mappings with group_key, metric_key, instance, series_key,
    label, unit, value_num, value_text, updated_at."""
    child_ids = child_ids or {}
    by_group: dict[str, list] = {}
    latest_ts = None
    for v in vals:
        by_group.setdefault(v["group_key"], []).append(v)
        if v["updated_at"] is not None and (latest_ts is None or v["updated_at"] > latest_ts):
            latest_ts = v["updated_at"]

    groups_out: list[dict] = []
    for gr in oid_groups:
        gkey = gr.get("key")
        metrics_def = {m["key"]: m for m in gr.get("metrics", []) if isinstance(m, dict) and m.get("key")}
        rows_for_group = by_group.get(gkey, [])
        applicable, na_reason = group_applicability(gr, by_group)
        worst = "none"

        def bump(status: str):
            nonlocal worst
            if SEV_RANK.get(status, -1) > SEV_RANK.get(worst, -1):
                worst = status

        if gr.get("kind") == "table":
            by_inst: dict = {}
            for v in rows_for_group:
                inst = by_inst.setdefault(v["instance"], {"label": v["label"], "cells": {}})
                if v["label"]:
                    inst["label"] = v["label"]
                m = metrics_def.get(v["metric_key"], {})
                inst["cells"][v["metric_key"]] = {
                    "value": v["value_num"],
                    "text": metric_display(m, v["value_num"], v["value_text"]),
                    "status": metric_status(m, v["value_num"], v["value_text"]),
                    "series_key": v["series_key"],
                }
            for data in by_inst.values():
                if _row_absent(data["cells"], metrics_def):
                    data["absent"] = True
                    for key, cell in data["cells"].items():
                        if (metrics_def.get(key) or {}).get("type", "gauge") == "gauge" and cell["value"] is not None:
                            cell.update(value=None, text="", status="none", suppressed=SUPPRESSED_OFFLINE)
                for cell in data["cells"].values():
                    bump(cell["status"])
            has_children = isinstance(gr.get("children"), dict)
            rows = [
                {"instance": inst, "label": data["label"] or inst, "cells": data["cells"],
                 **({"absent": True} if data.get("absent") else {}),
                 **({"child_device_id": child_ids.get((gkey, inst))} if has_children else {})}
                for inst, data in sorted(by_inst.items(), key=lambda kv: (kv[1]["label"] or kv[0]).lower())
            ]
            group = {
                "key": gkey, "name": gr.get("name"), "kind": "table",
                "description": gr.get("description"),
                "children_capable": has_children,
                "status": worst,
                "columns": [
                    {"key": m["key"], "name": m.get("name"), "unit": m.get("unit"),
                     "type": m.get("type", "gauge")}
                    for m in metrics_def.values()
                ],
                "rows": rows,
            }
        else:
            by_metric = {v["metric_key"]: v for v in rows_for_group}
            metrics_out = []
            for m in metrics_def.values():
                v = by_metric.get(m["key"])
                status = metric_status(m, v["value_num"], v["value_text"]) if v else "none"
                text_out = metric_display(m, v["value_num"], v["value_text"]) if v else ""
                members_group = STANDALONE_RING_METRICS.get(m["key"])
                if v and members_group and status == "warn" and v["value_num"] == 2:
                    members = {r["instance"] for r in by_group.get(members_group, [])}
                    if len(members) <= 1:
                        # A single-member "stack" can never close its ring.
                        status, text_out = "info", "Standalone — no stack ring"
                has_data = v is not None
                value = v["value_num"] if v else None
                if has_data and value is not None and float(value) == 0 and _CAPACITY.search(m.get("name") or m["key"]):
                    # A capacity of zero is a counter the device does not fill in.
                    has_data, value, text_out, status = False, None, "", "none"
                if has_data:
                    bump(status)
                metrics_out.append({
                    "key": m["key"], "name": m.get("name"),
                    "type": m.get("type", "gauge"), "unit": m.get("unit"),
                    "thresholds": m.get("thresholds"),
                    "value": value,
                    "text": text_out,
                    "status": status,
                    "series_key": v["series_key"] if v else f"tpl_{m['key']}",
                    "has_data": has_data,
                })
            group = {
                "key": gkey, "name": gr.get("name"), "kind": "scalar",
                "description": gr.get("description"),
                "status": worst,
                "metrics": metrics_out,
            }
        if not applicable:
            group["status"] = NOT_APPLICABLE
            group["applicable"] = False
            group["not_applicable_reason"] = na_reason
        groups_out.append(group)

    from app.services.f5_insights import enrich_f5_groups
    return enrich_f5_groups(groups_out), latest_ts


# ── Health rollup ──────────────────────────────────────────────────────────

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_TEMPLATE_SEVERITY = {"crit": "critical", "warn": "warning"}


def _state_column(group: dict) -> dict | None:
    enums = [c for c in group.get("columns") or [] if c.get("type") == "enum"]
    return next((c for c in enums if _STATE_COLUMN.search(f"{c['key']} {c.get('name') or ''}")), None) or (enums[0] if enums else None)


def _fmt_number(value: float, unit: str | None) -> str:
    u = (unit or "").strip()
    if abs(value) >= 1000:
        num = f"{round(value):,}"
    elif float(value).is_integer():
        num = str(int(value))
    else:
        num = f"{value:.1f}"
    if not u:
        return num
    if u == "%":
        return f"{num}%"
    return f"{num} {u}"


def group_digest(group: dict) -> dict:
    """One line of values for a health tile: "APs 9/13 online", "Tunnels 1/2 up",
    "max 32 °C", "CPU Usage 13% · Memory Usage 54%"."""
    status = group.get("status") or "none"
    out = {"key": group["key"], "name": group.get("name") or group["key"], "kind": group.get("kind"),
           "status": status, "summary": "", "attention": 0, "total": 0}
    if group.get("applicable") is False:
        out["summary"] = group.get("not_applicable_reason") or "Not applicable"
        return out
    if group.get("kind") == "table":
        rows = group.get("rows") or []
        out["total"] = len(rows)
        attention = [r for r in rows if any(SEV_RANK.get(c.get("status"), -1) >= 1 for c in r["cells"].values())]
        out["attention"] = len(attention)
        state = _state_column(group)
        if state and rows:
            ok_rows = [r for r in rows if (r["cells"].get(state["key"]) or {}).get("status") == "ok"]
            ok_texts = [(r["cells"][state["key"]].get("text") or "").lower() for r in ok_rows]
            word = max(set(ok_texts), key=ok_texts.count) if ok_texts else "ok"
            out["summary"] = f"{len(ok_rows)}/{len(rows)} {word or 'ok'}"
            return out
        gauges = [c for c in group.get("columns") or [] if c.get("type") == "gauge"]
        for col in gauges:
            readings = [r["cells"][col["key"]]["value"] for r in rows
                        if r["cells"].get(col["key"], {}).get("value") is not None]
            if readings:
                peak = max(readings)
                noun = "entry" if len(rows) == 1 else "entries"
                out["summary"] = f"{len(rows)} {noun} · max {_fmt_number(peak, col.get('unit'))}"
                return out
        out["summary"] = f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'}"
        return out
    readings = [m for m in group.get("metrics") or [] if m.get("has_data") and m.get("type") != "string"][:2]
    parts = []
    for m in readings:
        shown = m.get("text") if m.get("type") == "enum" and m.get("text") else (
            _fmt_number(float(m["value"]), m.get("unit")) if m.get("value") is not None else "")
        if shown:
            parts.append(f"{m.get('name')} {shown}")
    out["summary"] = " · ".join(parts)
    return out


def template_issues(groups: list[dict], updated_at: datetime | None = None, limit_rows: int = 3) -> list[dict]:
    """Issues raised by template readings: one per row needing attention, or a
    count when more than ``limit_rows`` rows of a group do."""
    issues: list[dict] = []
    since = updated_at.isoformat() if updated_at else None
    for g in groups:
        if g.get("applicable") is False or SEV_RANK.get(g.get("status"), -1) < 1:
            continue
        name = g.get("name") or g["key"]
        if g.get("kind") == "table":
            rows = g.get("rows") or []
            state = _state_column(g)

            def row_sev(r):
                return max((SEV_RANK.get(c.get("status"), -1) for c in r["cells"].values()), default=-1)

            bad = sorted([r for r in rows if row_sev(r) >= 1], key=lambda r: (-row_sev(r), r["label"].lower()))
            if not bad:
                continue

            def what(r):
                if state and r["cells"].get(state["key"], {}).get("text") and SEV_RANK.get(r["cells"][state["key"]].get("status"), -1) >= 1:
                    return r["cells"][state["key"]]["text"]
                cells = [(k, c) for k, c in r["cells"].items() if SEV_RANK.get(c.get("status"), -1) >= 1]
                cols = {c["key"]: c for c in g.get("columns") or []}
                parts = []
                for k, c in cells[:2]:
                    col = cols.get(k, {})
                    shown = c.get("text") if col.get("type") in ("enum", "string") and c.get("text") else (
                        _fmt_number(float(c["value"]), col.get("unit")) if c.get("value") is not None else c.get("text") or "")
                    parts.append(f"{col.get('name') or k} {shown}".strip())
                return ", ".join(parts)

            if len(bad) <= limit_rows:
                for r in bad:
                    issues.append({
                        "source": "template", "group": g["key"],
                        "severity": _TEMPLATE_SEVERITY["crit" if row_sev(r) >= 2 else "warn"],
                        "title": f"{name}: {r['label']} {what(r)}".strip(),
                        "detail": None, "since": since, "tab": "metrics",
                    })
            else:
                examples = ", ".join(r["label"] for r in bad[:2])
                more = f", +{len(bad) - 2}" if len(bad) > 2 else ""
                common = {what(r) for r in bad}
                state_text = f" {common.pop()}" if len(common) == 1 else " need attention"
                issues.append({
                    "source": "template", "group": g["key"],
                    "severity": _TEMPLATE_SEVERITY["crit" if any(row_sev(r) >= 2 for r in bad) else "warn"],
                    "title": f"{name}: {len(bad)} of {len(rows)}{state_text}",
                    "detail": f"{examples}{more}", "since": since, "tab": "metrics",
                })
        else:
            for m in g.get("metrics") or []:
                if not m.get("has_data") or SEV_RANK.get(m.get("status"), -1) < 1:
                    continue
                shown = m.get("text") if m.get("type") in ("enum", "string") and m.get("text") else (
                    _fmt_number(float(m["value"]), m.get("unit")) if m.get("value") is not None else "")
                issues.append({
                    "source": "template", "group": g["key"],
                    "severity": _TEMPLATE_SEVERITY[m["status"]],
                    "title": f"{name}: {m.get('name')} {shown}".strip(),
                    "detail": None, "since": since, "tab": "metrics",
                })
    return issues


def sort_issues(issues: list[dict]) -> list[dict]:
    """Worst first, then oldest first (an issue open for days outranks a new one
    of the same severity); undated issues after dated ones."""
    def key(i):
        since = i.get("since")
        return (_SEVERITY_ORDER.get(i.get("severity"), 3), since is None, since or "")
    return sorted(issues, key=key)


def health_rollup(issues: list[dict]) -> dict:
    counted = [i for i in issues if i.get("severity") in ("critical", "warning")]
    worst = "critical" if any(i["severity"] == "critical" for i in counted) else (
        "warning" if counted else "ok")
    return {"state": worst, "issue_count": len(counted),
            "critical": sum(1 for i in counted if i["severity"] == "critical"),
            "warning": sum(1 for i in counted if i["severity"] == "warning")}


# ── Interfaces ─────────────────────────────────────────────────────────────

_UP = {"up", "1"}
_DOWN = {"down", "2", "lowerlayerdown", "7"}
_ABSENT_IF = {"notpresent", "6"}
_ADMIN_DOWN = {"down", "2"}


def _norm(value) -> str:
    return re.sub(r"[\s_-]", "", str(value or "")).lower()


def interface_breakdown(interfaces: Iterable[dict]) -> dict:
    """Counts the way an operator reads them. Only *unexpected* down ports are
    actionable: administratively up, operationally down, monitored, and seen up
    at least once (the same baseline the Interface-down alert uses). Unused
    access ports and admin-down ports are not failures."""
    out = {"total": 0, "up": 0, "unexpected_down": 0, "admin_down": 0, "not_present": 0,
           "unused": 0, "unmonitored_down": 0, "other": 0, "unexpected": []}
    for i in interfaces:
        out["total"] += 1
        oper, admin = _norm(i.get("oper_status")), _norm(i.get("admin_status"))
        if oper in _UP:
            out["up"] += 1
        elif admin in _ADMIN_DOWN:
            out["admin_down"] += 1
        elif oper in _ABSENT_IF:
            out["not_present"] += 1
        elif oper in _DOWN:
            if not i.get("ever_up"):
                out["unused"] += 1
            elif i.get("monitored") is False:
                out["unmonitored_down"] += 1
            else:
                out["unexpected_down"] += 1
                out["unexpected"].append({
                    "if_index": i.get("if_index"),
                    "name": i.get("if_name") or i.get("if_descr") or f"ifIndex {i.get('if_index')}",
                    "alias": i.get("if_alias") or None,
                })
        else:
            out["other"] += 1
    return out


# ── Identity ───────────────────────────────────────────────────────────────

_FORTIOS = re.compile(r"^v?(\d+\.\d+\.\d+)\s*,?\s*build\s*(\d+)", re.I)
_TEMPLATE_IDENTITY = {
    "os_version": re.compile(r"^(os version|software version|tmos version|firmware|firmware version|version|panos version|sw version)$", re.I),
    "serial_number": re.compile(r"^(serial|serial number|chassis serial|system serial number)$", re.I),
    "model": re.compile(r"^(model|chassis model|platform|product name)$", re.I),
}


def _clean(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def compact_version(text: str) -> str:
    m = _FORTIOS.match(text)
    if m:
        return f"{m.group(1)} build {m.group(2)}"
    return text


def resolve_identity(device: dict, groups: list[dict], chassis: dict | None,
                     normalized: dict | None = None) -> dict:
    """One value per identity field with its source, in precedence order:
    the normalised inventory identity, the device record (poller), the
    template's own reading, then the ENTITY-MIB chassis row. Every tab reads
    this one answer, so the Inventory tab can no longer say "Not reported"
    while the metrics tab shows the firmware."""
    template_text: dict[str, str] = {}
    for g in groups:
        if g.get("kind") != "scalar":
            continue
        for m in g.get("metrics") or []:
            if not m.get("has_data") or m.get("type") != "string" or not _clean(m.get("text")):
                continue
            for field, pattern in _TEMPLATE_IDENTITY.items():
                if field not in template_text and pattern.match((m.get("name") or "").strip()):
                    template_text[field] = m["text"].strip()

    normalized = normalized or {}
    os_norm = None
    if _clean(normalized.get("os_version")):
        os_norm = " ".join(p for p in (_clean(normalized.get("os_product")), _clean(normalized.get("os_version"))) if p)
        if _clean(normalized.get("os_build")) and normalized["os_build"] not in os_norm:
            os_norm += f" build {normalized['os_build']}"

    def pick(field: str, *candidates) -> dict:
        for source, value in candidates:
            v = _clean(value)
            if v:
                return {"value": compact_version(v) if field in ("os_version", "firmware") else v, "source": source}
        return {"value": None, "source": None}

    chassis = chassis or {}
    return {
        "model": pick("model", ("inventory", normalized.get("hw_model")), ("device", device.get("model")),
                      ("template", template_text.get("model")), ("entity", chassis.get("model_name"))),
        "os_version": pick("os_version", ("inventory", os_norm), ("device", device.get("os_version")),
                           ("template", template_text.get("os_version"))),
        "firmware": pick("firmware", ("device", device.get("firmware_version")), ("entity", chassis.get("fw_revision")),
                         ("template", template_text.get("os_version"))),
        "serial_number": pick("serial_number", ("inventory", normalized.get("hw_serial")),
                              ("device", device.get("serial_number")), ("template", template_text.get("serial_number")),
                              ("entity", chassis.get("serial_number"))),
    }


# ── Summary KPIs from template readings ────────────────────────────────────

_CPU_NAME = re.compile(r"^(cpu|cpu usage|cpu utili[sz]ation|cpu load)$", re.I)
_MEM_NAME = re.compile(r"^(memory|memory usage|memory utili[sz]ation|system ram utili[sz]ation)$", re.I)
_HIGHLIGHTS = (
    ("sessions", re.compile(r"^(active sessions|sessions|active connections|connections|current connections)$", re.I),
     re.compile(r"(session setup rate|session rate|connections/s|new connections)", re.I)),
    ("clients", re.compile(r"^(wifi clients|clients|wireless clients|associated clients)$", re.I), None),
)


def _scalar_metrics(groups: list[dict]):
    for g in groups:
        if g.get("kind") == "scalar" and g.get("applicable") is not False:
            for m in g.get("metrics") or []:
                yield g, m


def template_kpis(groups: list[dict]) -> dict:
    """Thresholds for the CPU and memory tiles and up to two vendor highlights
    (sessions for a firewall, PoE for a switch, clients for a controller)."""
    out: dict = {"cpu_thresholds": None, "memory_thresholds": None, "highlights": []}
    for _, m in _scalar_metrics(groups):
        name = (m.get("name") or "").strip()
        if out["cpu_thresholds"] is None and m.get("thresholds") and _CPU_NAME.match(name):
            out["cpu_thresholds"] = m["thresholds"]
        if out["memory_thresholds"] is None and m.get("thresholds") and _MEM_NAME.match(name):
            out["memory_thresholds"] = m["thresholds"]
    for kind, main, rate in _HIGHLIGHTS:
        found = next((m for _, m in _scalar_metrics(groups)
                      if m.get("has_data") and m.get("value") is not None and main.match((m.get("name") or "").strip())), None)
        if not found:
            continue
        sub = None
        if rate:
            r = next((m for _, m in _scalar_metrics(groups)
                      if m.get("has_data") and m.get("value") is not None and rate.search(m.get("name") or "")), None)
            if r:
                sub = f"{_fmt_number(float(r['value']), r.get('unit'))} setup rate"
        out["highlights"].append({"kind": kind, "label": "Sessions" if kind == "sessions" else "Wireless clients",
                                  "value": found["value"], "unit": found.get("unit"), "sub": sub,
                                  "series_key": found.get("series_key")})
    poe = next((g for g in groups if g.get("key") == "poe" and g.get("kind") == "table"
                and g.get("applicable") is not False), None)
    if poe:
        cols = {(c.get("name") or "").lower(): c["key"] for c in poe.get("columns") or []}
        budget_key, used_key = cols.get("budget"), cols.get("consumed")
        if budget_key and used_key:
            budget = sum(r["cells"][budget_key]["value"] for r in poe.get("rows") or []
                         if r["cells"].get(budget_key, {}).get("value") is not None)
            used = sum(r["cells"][used_key]["value"] for r in poe.get("rows") or []
                       if r["cells"].get(used_key, {}).get("value") is not None)
            if budget:
                out["highlights"].append({"kind": "poe", "label": "PoE power", "value": used, "unit": "W",
                                          "sub": f"of {_fmt_number(budget, 'W')} budget ({used / budget * 100:.0f}%)",
                                          "series_key": None})
    out["highlights"] = out["highlights"][:2]
    return out
