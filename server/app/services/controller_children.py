"""Readings, panels, history and availability of controller-managed children.

A FortiGate reports its FortiLink switches and FortiAPs on its own template
rows (``device_template_values`` of the controller, groups ``fortiswitch`` and
``access_points``; history under the controller's ``tpl_<metric>_<instance>``
series). A child (``poll_mode = 'via_controller'``) is never polled itself, so
everything here is *read through* the controller row that materialized it
(``managed_source`` + ``managed_instance``):

* **Readings**: the group's ``children`` mapping names the CPU, memory, boot
  uptime, clients, state and authorization columns. Each reading carries the
  controller, its measurement time and whether it is stale (older than three
  controller polls of that group, at least ten minutes). A row whose state
  maps to down blanks its gauges ("not reported while offline"): missing is
  not zero.
* **Panels**: controller-source groups (``display: "children"``) declare how
  their rows belong to a child (``belongs_to``): by serial column, by raw
  index prefix (``device_template_values.instance_oid``) or through the AP
  profile name. Fortinet panels shape them into ports, radios, SSIDs and
  clients. Client identity needs ``udt.view`` (and ``udt.view_users`` for user
  names); counts and signal summaries do not.
* **History**: the controller's own series, bucketed; nothing is copied.
* **Availability**: the share of controller-reported state samples in which
  the child was up or degraded, with the coverage of the window. Below
  ``COVERAGE_MIN`` it is "insufficient history", never a guess; gaps (a
  controller outage) are unknown, not down.
* **Fan-out**: the sync loop writes fresh cpu/memory/uptime readings to
  ``snmp_metrics`` under the child's own id, so the Performance tab, reports
  and metric alert rules (``network_alert_service``) are tied to the child.

What the FORTINET-FORTIGATE-MIB does not carry (switch port traffic, errors,
LAGs, LLDP, PoE budget; radio utilization and retries; switch boot time) is
listed in ``UNAVAILABLE`` with the read-only source that would supply it, and
shown as unavailable (see docs/controller-managed-children.md).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.controller_children")

STALE_MIN_S = 600          # never call a reading stale sooner than this
STALE_POLLS = 3            # ...or than this many controller polls of its group
COVERAGE_MIN = 0.9         # availability needs 90% of the window observed
AVAILABILITY_BUCKET_S = 300
OFFLINE_TEXT = "Not reported while offline"
CHILD_GROUP_DISPLAY = "children"
FANOUT_POLLER_PREFIX = "controller:"

# Reasons shared by the API and the UI; ``requires`` names the read-only
# source that would supply the reading.
_FOS_API = "FortiOS REST API (read-only admin profile)"
UNAVAILABLE: dict[str, list[dict]] = {
    "switch": [
        {"metric": "boot_uptime", "label": "Uptime since boot",
         "reason": "FORTINET-FORTIGATE-MIB reports when a switch joined FortiLink, not when it booted.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/switch-controller/managed-switch/status, or direct SNMP on the switch"},
        {"metric": "port_traffic", "label": "Port traffic and utilization",
         "reason": "fgSwPortTable has no octet counters.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/switch-controller/managed-switch/port-stats"},
        {"metric": "port_errors", "label": "Port errors and discards",
         "reason": "fgSwPortTable has no error or discard counters.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/switch-controller/managed-switch/port-stats"},
        {"metric": "poe_budget", "label": "PoE budget",
         "reason": "The MIB reports per-port PoE draw only, not the switch power budget.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/switch-controller/managed-switch/poe"},
        {"metric": "lag", "label": "LAGs and trunks",
         "reason": "Trunk membership is configuration, not in the MIB.",
         "requires": f"{_FOS_API}: GET /api/v2/cmdb/switch-controller/managed-switch (ports[].type = trunk, members)"},
        {"metric": "lldp", "label": "LLDP neighbors",
         "reason": "FortiSwitch LLDP neighbors are not exported through the FortiGate MIB.",
         "requires": f"{_FOS_API}: managed-switch LLDP monitor, or direct LLDP-MIB on the switch"},
        {"metric": "uplink", "label": "FortiLink uplink port",
         "reason": "The MIB does not mark which port is the FortiLink/ISL uplink.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/switch-controller/managed-switch/status (fgt_peer_intf_name, isl ports)"},
    ],
    "access_point": [
        {"metric": "radio_utilization", "label": "Channel utilization",
         "reason": "fgWcWtpSessionRadioTable has no channel utilization.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/wifi/managed_ap (radio[] channel utilization)"},
        {"metric": "retries", "label": "Retries",
         "reason": "Transmit retries are not in the MIB.",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/wifi/managed_ap (radio[] retry counters)"},
        {"metric": "noise_floor", "label": "Radio noise floor",
         "reason": "Only per-client noise is in the MIB (fgWcStaNoise).",
         "requires": f"{_FOS_API}: GET /api/v2/monitor/wifi/managed_ap"},
    ],
}

_DOWN_STATUS = "down"


# ─── decoding ──────────────────────────────────────────────────────────────

def decode_index(raw: str | None, layout: Iterable[str]) -> list | None:
    """Decode a raw OID index suffix by ``layout`` ("int", "str", "mac").

    "str" is a length-prefixed DisplayString/OCTET STRING index component;
    "mac" accepts both a length-prefixed (6|8) and a fixed six-octet form.
    Returns None when the suffix does not fit the layout."""
    if not raw:
        return None
    try:
        parts = [int(p) for p in str(raw).strip(".").split(".")]
    except ValueError:
        return None
    out: list = []
    i = 0
    layout = list(layout)
    for n, kind in enumerate(layout):
        if i >= len(parts):
            return None
        if kind == "int":
            out.append(parts[i])
            i += 1
        elif kind == "str":
            length = parts[i]
            chunk = parts[i + 1:i + 1 + length]
            if len(chunk) != length or any(c < 0 or c > 255 for c in chunk):
                return None
            out.append(bytes(chunk).decode("utf-8", "replace"))
            i += 1 + length
        elif kind == "mac":
            rest = len(parts) - i
            remaining_after = len(layout) - n - 1
            if parts[i] in (6, 8) and rest - 1 - remaining_after == parts[i]:
                octets = parts[i + 1:i + 1 + parts[i]]
                i += 1 + parts[i]
            else:
                octets = parts[i:i + 6]
                i += 6
            if len(octets) not in (6, 8) or any(c < 0 or c > 255 for c in octets):
                return None
            out.append(":".join(f"{c:02x}" for c in octets[-6:]))
        else:
            return None
    return out if i == len(parts) else None


def _hex_bytes(value: str | None) -> bytes | None:
    if not value:
        return None
    value = value.strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2})+", value):
        return None
    return bytes.fromhex(value)


def hex_to_ip(value: str | None) -> str | None:
    """InetAddress as hex (format "hex"): 4 or 16 octets. Plain dotted text
    (an IpAddress column) passes through. Unspecified addresses are None."""
    if not value:
        return None
    raw = _hex_bytes(value)
    try:
        ip = ipaddress.ip_address(raw) if raw is not None and len(raw) in (4, 16) else ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return None if ip.is_unspecified else str(ip)


def hex_to_mac(value: str | None) -> str | None:
    raw = _hex_bytes(value)
    if raw is None or len(raw) not in (6, 8) or not any(raw):
        return None
    return ":".join(f"{b:02x}" for b in raw[-6:])


def display_hex(value: str | None) -> str:
    """Human text of a hex-format column: an IP, a MAC, else the hex."""
    if not value:
        return ""
    raw = _hex_bytes(value)
    if raw is None:
        return value
    if len(raw) in (4, 16):
        return hex_to_ip(value) or ""
    if len(raw) in (6, 8):
        return hex_to_mac(value) or ""
    return value


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _text_num(text_value: str | None) -> float | None:
    if text_value is None:
        return None
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(text_value))
    return float(m.group(1)) if m else None


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _groups(oid_groups) -> list[dict]:
    if isinstance(oid_groups, str):
        oid_groups = json.loads(oid_groups)
    return [g for g in (oid_groups or []) if isinstance(g, dict)]


def hidden_group_keys(oid_groups) -> list[str]:
    """Controller-source groups shown on children, never on the controller page."""
    return [g["key"] for g in _groups(oid_groups) if g.get("display") == CHILD_GROUP_DISPLAY and g.get("key")]


def series_key(metric_key: str, instance: str | None) -> str:
    """The poller's ClickHouse series key for a template cell."""
    return f"tpl_{metric_key}_{instance}" if instance else f"tpl_{metric_key}"


def stale_after_s(group: dict | None, controller_interval: int | None) -> int:
    interval = int((group or {}).get("interval_seconds") or 0) or int(controller_interval or 60)
    return max(STALE_MIN_S, STALE_POLLS * interval)


# ─── child context ─────────────────────────────────────────────────────────

@dataclass
class Cell:
    num: float | None
    text: str
    updated_at: datetime | None
    series_key: str | None


@dataclass
class ChildContext:
    child: dict
    controller: dict
    groups: list[dict]
    group: dict
    spec: dict
    cells: dict[str, Cell] = field(default_factory=dict)
    label: str | None = None
    instance_oid: str | None = None
    measured_at: datetime | None = None

    @property
    def group_key(self) -> str:
        return self.child["managed_source"]

    @property
    def instance(self) -> str:
        return self.child["managed_instance"]

    def metric(self, key: str | None) -> dict:
        if not key:
            return {}
        return next((m for m in self.group.get("metrics") or [] if m.get("key") == key), {})

    def group_def(self, key: str) -> dict | None:
        return next((g for g in self.groups if g.get("key") == key), None)


_CHILD_COLS = """d.id, d.hostname, d.device_type, d.status, d.model, d.vendor, d.os_version, d.serial_number,
       d.managed_by_device_id, d.managed_source, d.managed_instance, d.managed_last_seen, d.managed_ip,
       d.poll_mode, d.tags, d.status_changed_at, d.last_seen"""


async def _controllers(db: AsyncSession, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = (await db.execute(text("""
        SELECT c.id, c.hostname, c.status, c.model, c.vendor, c.last_seen, c.snmp_poll_interval, c.tags,
               host(c.ip_address) AS ip, p.oid_groups
        FROM devices c LEFT JOIN device_profiles p ON p.id = c.profile_id
        WHERE c.id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": ids})).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


async def load_contexts(db: AsyncSession, child_ids: list[str] | None = None) -> list[ChildContext]:
    """Contexts for children (all via_controller children when ``child_ids`` is None)."""
    where = "d.poll_mode = 'via_controller' AND d.managed_by_device_id IS NOT NULL AND d.managed_instance IS NOT NULL"
    params: dict = {}
    if child_ids is not None:
        if not child_ids:
            return []
        where += " AND d.id = ANY(CAST(:ids AS uuid[]))"
        params["ids"] = [str(i) for i in child_ids]
    children = [dict(r) for r in (await db.execute(text(f"SELECT {_CHILD_COLS} FROM devices d WHERE {where}"),
                                                    params)).mappings().all()]
    if not children:
        return []
    controllers = await _controllers(db, sorted({str(c["managed_by_device_id"]) for c in children}))

    out: list[ChildContext] = []
    wanted: dict[str, list[tuple[str, str]]] = {}
    for c in children:
        ctrl = controllers.get(str(c["managed_by_device_id"]))
        if not ctrl:
            continue
        groups = _groups(ctrl.get("oid_groups"))
        group = next((g for g in groups if g.get("key") == c["managed_source"]), None)
        if not group or not isinstance(group.get("children"), dict):
            continue
        out.append(ChildContext(child=c, controller=ctrl, groups=groups, group=group, spec=group["children"]))
        wanted.setdefault(str(ctrl["id"]), []).append((c["managed_source"], c["managed_instance"]))

    for cid, pairs in wanted.items():
        rows = (await db.execute(text("""
            SELECT group_key, instance, metric_key, label, value_num, value_text, updated_at, series_key, instance_oid
            FROM device_template_values
            WHERE device_id = :cid AND group_key = ANY(:groups) AND instance = ANY(:instances)
        """), {"cid": cid, "groups": sorted({g for g, _ in pairs}), "instances": sorted({i for _, i in pairs})})).mappings().all()
        by_row: dict[tuple[str, str], list] = {}
        for r in rows:
            by_row.setdefault((r["group_key"], r["instance"]), []).append(r)
        for ctx in out:
            if str(ctx.controller["id"]) != cid:
                continue
            for r in by_row.get((ctx.group_key, ctx.instance), []):
                ctx.cells[r["metric_key"]] = Cell(_num(r["value_num"]), r["value_text"] or "", _aware(r["updated_at"]),
                                                  r["series_key"])
                if r["label"]:
                    ctx.label = r["label"]
                if r["instance_oid"]:
                    ctx.instance_oid = r["instance_oid"]
                ts = _aware(r["updated_at"])
                if ts and (ctx.measured_at is None or ts > ctx.measured_at):
                    ctx.measured_at = ts
    return out


def _enum_text(metric: dict, code) -> str | None:
    if code is None:
        return None
    key = str(int(code)) if float(code).is_integer() else str(code)
    lab = (metric.get("labels") or {}).get(key)
    return lab.get("text") if lab else key


def _enum_sev(metric: dict, code) -> str | None:
    if code is None:
        return None
    key = str(int(code)) if float(code).is_integer() else str(code)
    lab = (metric.get("labels") or {}).get(key)
    return lab.get("sev") if lab else None


def reported_status(ctx: ChildContext) -> str | None:
    """The controller-reported state mapped through the pack's status_map."""
    key = ctx.spec.get("status_key")
    cell = ctx.cells.get(key) if key else None
    if cell is None or cell.num is None:
        return None
    return (ctx.spec.get("status_map") or {}).get(str(int(cell.num)), "unknown")


def readings(ctx: ChildContext, now: datetime | None = None) -> dict:
    """Headline readings of one child from its controller row."""
    now = now or datetime.now(timezone.utc)
    spec = ctx.spec
    status = reported_status(ctx)
    offline = status == _DOWN_STATUS
    limit = stale_after_s(ctx.group, ctx.controller.get("snmp_poll_interval"))
    age = (now - ctx.measured_at).total_seconds() if ctx.measured_at else None
    stale = age is None or age > limit

    def gauge(key_name: str) -> float | None:
        cell = ctx.cells.get(spec.get(key_name) or "")
        if cell is None or offline:
            return None
        return cell.num

    def textual(key_name: str) -> str | None:
        cell = ctx.cells.get(spec.get(key_name) or "")
        return (cell.text or None) if cell else None

    state = None
    if spec.get("state_key") and spec["state_key"] in ctx.cells:
        cell, m = ctx.cells[spec["state_key"]], ctx.metric(spec["state_key"])
        state = {"code": cell.num, "text": _enum_text(m, cell.num), "sev": _enum_sev(m, cell.num), "status": status}
    auth = None
    if spec.get("auth_key") and spec["auth_key"] in ctx.cells:
        cell, m = ctx.cells[spec["auth_key"]], ctx.metric(spec["auth_key"])
        auth = {"code": cell.num, "text": _enum_text(m, cell.num), "sev": _enum_sev(m, cell.num)}

    uptime = gauge("uptime_key")
    serial = ctx.child.get("serial_number") or textual("serial_key") or _serial_from_index(ctx)
    ip_cell = ctx.cells.get(spec.get("ip_key") or "")
    mac_cell = ctx.cells.get(spec.get("mac_key") or "")

    def bps(key: str) -> float | None:
        cell = ctx.cells.get(key)
        return None if cell is None or offline else cell.num

    out = {
        "cpu": gauge("cpu_key"),
        "memory": gauge("memory_key"),
        "uptime_s": uptime if uptime and uptime > 0 else None,
        "clients": gauge("clients_key"),
        "state": state,
        "authorization": auth,
        "model": ctx.child.get("model") or textual("model_key"),
        "firmware": textual("os_version_key") or ctx.child.get("os_version"),
        "serial": serial,
        "ip": hex_to_ip(ip_cell.text) if ip_cell else None,
        "mac": hex_to_mac(mac_cell.text) if mac_cell else None,
        "rx_bps": bps("fgt_ap_rx_bps"),
        "tx_bps": bps("fgt_ap_tx_bps"),
        "session_uptime_s": bps("fgt_ap_session_uptime"),
        "joined_at": _joined_at(ctx.cells.get("fgt_sw_join_time")),
        "flags": (ctx.cells["fgt_sw_flag"].text or None) if "fgt_sw_flag" in ctx.cells else None,
        "offline": offline,
        "offline_text": OFFLINE_TEXT if offline else None,
    }
    return {
        "controller": {"id": str(ctx.controller["id"]), "hostname": ctx.controller["hostname"],
                       "status": ctx.controller["status"], "model": ctx.controller.get("model")},
        "source_group": ctx.group_key,
        "measured_at": _iso(ctx.measured_at),
        "stale": stale,
        "stale_after_s": limit,
        "age_s": round(age) if age is not None else None,
        "reported": bool(ctx.cells),
        "readings": out,
    }


def _serial_from_index(ctx: ChildContext) -> str | None:
    rule = ctx.spec.get("serial_from_index")
    if not isinstance(rule, dict) or not ctx.instance_oid:
        return None
    parts = decode_index(ctx.instance_oid, rule.get("layout") or [])
    part = rule.get("part")
    if not parts or not isinstance(part, int) or part >= len(parts) or not isinstance(parts[part], str):
        return None
    value = parts[part].strip()
    return value[:128] or None


def _joined_at(cell: Cell | None) -> str | None:
    """fgSwDeviceJoinTime: 'time switch device become online'. Only an epoch
    value is shown as a date; anything else is not interpreted."""
    value = _text_num(cell.text) if cell else None
    if value is None or value < 946684800 or value > 4102444800:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


# ─── fan-out to the child's own series ─────────────────────────────────────

FANOUT_KEYS = (("cpu", "%"), ("memory", "%"), ("uptime_s", "seconds"))
_FANOUT_METRIC = {"cpu": "cpu", "memory": "memory", "uptime_s": "uptime"}


def fanout_rows(contexts: Iterable[ChildContext], last_written: dict[str, datetime],
                now: datetime | None = None) -> list[list]:
    """snmp_metrics rows for fresh, not yet written child readings."""
    now = now or datetime.now(timezone.utc)
    rows: list[list] = []
    for ctx in contexts:
        if ctx.measured_at is None:
            continue
        cid = str(ctx.child["id"])
        prev = last_written.get(cid)
        if prev is not None and _aware(prev) >= ctx.measured_at:
            continue
        shaped = readings(ctx, now)
        if shaped["stale"]:
            continue
        r = shaped["readings"]
        ts = ctx.measured_at.astimezone(timezone.utc).replace(tzinfo=None)
        wrote = False
        for key, unit in FANOUT_KEYS:
            value = r.get(key)
            if value is None:
                continue
            rows.append([cid, _FANOUT_METRIC[key], float(value), unit, ts,
                         f"{FANOUT_POLLER_PREFIX}{ctx.controller['id']}"])
            wrote = True
        if wrote:
            last_written[cid] = ctx.measured_at
    return rows


# ─── controller-source rows of one child ───────────────────────────────────

async def _source_rows(db: AsyncSession, ctx: ChildContext, group_key: str) -> dict[str, dict]:
    """{instance: {"label", "oid", "updated_at", "cells": {metric: Cell}}} of one controller group."""
    rows = (await db.execute(text("""
        SELECT instance, metric_key, label, value_num, value_text, updated_at, series_key, instance_oid
        FROM device_template_values WHERE device_id = :cid AND group_key = :g
    """), {"cid": str(ctx.controller["id"]), "g": group_key})).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        row = out.setdefault(r["instance"], {"label": None, "oid": None, "updated_at": None, "cells": {}})
        if r["label"]:
            row["label"] = r["label"]
        if r["instance_oid"]:
            row["oid"] = r["instance_oid"]
        ts = _aware(r["updated_at"])
        if ts and (row["updated_at"] is None or ts > row["updated_at"]):
            row["updated_at"] = ts
        row["cells"][r["metric_key"]] = Cell(_num(r["value_num"]), r["value_text"] or "", ts, r["series_key"])
    return out


def _belongs(ctx: ChildContext, gdef: dict, row: dict, serial: str | None, ap_profile: str | None) -> bool:
    rule = gdef.get("belongs_to") or {}
    by = rule.get("by")
    if by == "serial":
        cell = row["cells"].get(rule.get("metric") or "")
        return bool(serial and cell and cell.text.strip() == serial)
    if by == "index_prefix":
        return bool(ctx.instance_oid and row["oid"] and row["oid"].startswith(ctx.instance_oid + "."))
    if by == "profile":
        parts = decode_index(row["oid"], rule.get("layout") or [])
        return bool(ap_profile and parts and len(parts) > 1 and parts[1] == ap_profile)
    return False


def _cell_text(row: dict, key: str) -> str | None:
    cell = row["cells"].get(key)
    return (cell.text.strip() or None) if cell else None


def _cell_num(row: dict, key: str) -> float | None:
    cell = row["cells"].get(key)
    if cell is None:
        return None
    return cell.num if cell.num is not None else _text_num(cell.text)


def _enum_cell(gdef: dict, row: dict, key: str) -> dict | None:
    cell = row["cells"].get(key)
    if cell is None:
        return None
    m = next((x for x in gdef.get("metrics") or [] if x.get("key") == key), {})
    code = cell.num if cell.num is not None else _text_num(cell.text)
    return {"code": code, "text": _enum_text(m, code) if code is not None else (cell.text or None),
            "sev": _enum_sev(m, code)}


def _flag(value: str | None, yes: str = "1") -> bool | None:
    if value is None:
        return None
    return value.strip() == yes


def _vlan_list(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


async def _children_by_serial(db: AsyncSession, controller_id: str) -> dict[str, dict]:
    rows = (await db.execute(text("""
        SELECT id, hostname, status, model, serial_number, device_type, tags
        FROM devices WHERE managed_by_device_id = :cid AND serial_number IS NOT NULL
    """), {"cid": controller_id})).mappings().all()
    return {r["serial_number"]: dict(r) for r in rows}


async def _udt_ports_for_macs(db: AsyncSession, macs: list[str]) -> dict[str, list[dict]]:
    """{mac: [{switch_serial, port_number, if_name, vlan_id, is_direct, last_seen}]} from
    Device Tracker's controller-collected FortiSwitch MAC tables (active rows)."""
    if not macs:
        return {}
    try:
        rows = (await db.execute(text("""
            SELECT e.mac::text AS mac, s.serial, l.port_number, p.if_name, l.vlan_id, l.is_direct, l.last_seen
            FROM udt_endpoints e
            JOIN udt_managed_locations l ON l.endpoint_id = e.id AND l.active
            JOIN udt_managed_switches s ON s.id = l.switch_id
            LEFT JOIN udt_managed_ports p ON p.switch_id = s.id AND p.port_number = l.port_number
            WHERE e.mac = ANY(CAST(:macs AS macaddr[]))
            ORDER BY l.is_direct DESC, l.last_seen DESC
        """), {"macs": macs})).mappings().all()
    except Exception:
        await db.rollback()
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r["mac"]).lower(), []).append({
            "switch_serial": r["serial"], "port_number": r["port_number"], "if_name": r["if_name"],
            "vlan_id": r["vlan_id"], "is_direct": bool(r["is_direct"]), "last_seen": _iso(r["last_seen"])})
    return out


async def _udt_macs_on_switch(db: AsyncSession, serial: str) -> dict[int, list[str]]:
    """{port_number: [mac, ...]} of endpoints Device Tracker currently sees directly on a switch port."""
    try:
        rows = (await db.execute(text("""
            SELECT l.port_number, e.mac::text AS mac
            FROM udt_managed_switches s
            JOIN udt_managed_locations l ON l.switch_id = s.id AND l.active AND l.is_direct
            JOIN udt_endpoints e ON e.id = l.endpoint_id
            WHERE s.serial = :serial
        """), {"serial": serial})).mappings().all()
    except Exception:
        await db.rollback()
        return {}
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["port_number"]), []).append(str(r["mac"]).lower())
    return out


def _port_number(name: str | None) -> int | None:
    m = re.search(r"(\d+)$", name or "")
    return int(m.group(1)) if m else None


async def fortiswitch_panel(db: AsyncSession, ctx: ChildContext, now: datetime | None = None) -> dict:
    """Ports, VLANs and PoE of one FortiLink switch from fgSwPortTable rows."""
    now = now or datetime.now(timezone.utc)
    gdef = ctx.group_def("fsw_ports")
    serial = readings(ctx, now)["readings"]["serial"]
    if not gdef:
        return {"supported": False, "reason": "The controller's template has no FortiSwitch port table."}
    rows = await _source_rows(db, ctx, "fsw_ports")
    mine = {inst: row for inst, row in rows.items() if _belongs(ctx, gdef, row, serial, None)}
    limit = stale_after_s(gdef, ctx.controller.get("snmp_poll_interval"))
    measured = max((r["updated_at"] for r in mine.values() if r["updated_at"]), default=None)

    # Endpoints seen on each port (Device Tracker), to name APs plugged into this switch.
    by_port_macs = await _udt_macs_on_switch(db, serial) if serial else {}
    ap_by_mac: dict[str, dict] = {}
    if by_port_macs:
        ap_rows = (await db.execute(text("""
            SELECT d.id, d.hostname, d.status, d.model, v.value_text AS mac
            FROM devices d JOIN device_template_values v
              ON v.device_id = d.managed_by_device_id AND v.group_key = d.managed_source
             AND v.instance = d.managed_instance AND v.metric_key = 'fgt_ap_mac'
            WHERE d.managed_by_device_id = :cid AND d.poll_mode = 'via_controller'
        """), {"cid": str(ctx.controller["id"])})).mappings().all()
        for r in ap_rows:
            mac = hex_to_mac(r["mac"])
            if mac:
                ap_by_mac[mac] = {"id": str(r["id"]), "hostname": r["hostname"], "status": r["status"], "model": r["model"]}

    ports = []
    for inst, row in mine.items():
        name = row["label"] or _cell_text(row, "fgt_swp_name") or inst
        status = _enum_cell(gdef, row, "fgt_swp_status")
        poe_cap = _flag(_cell_text(row, "fgt_swp_poe_capable"))
        number = _port_number(name)
        neighbours = [ap_by_mac[m] for m in by_port_macs.get(number or -1, []) if m in ap_by_mac]
        ports.append({
            "instance": inst, "name": name, "number": number,
            "link": status, "up": bool(status and status["code"] == 1),
            "speed": _cell_text(row, "fgt_swp_speed"),
            "native_vlan": _cell_text(row, "fgt_swp_native_vlan"),
            "allowed_vlans": _vlan_list(_cell_text(row, "fgt_swp_allowed_vlans")),
            "untagged_vlans": _vlan_list(_cell_text(row, "fgt_swp_untagged_vlans")),
            "poe": {"capable": poe_cap, "enabled": _flag(_cell_text(row, "fgt_swp_poe_status")),
                    "state": _cell_text(row, "fgt_swp_poe_state"),
                    "draw_w": _cell_num(row, "fgt_swp_poe_power") if poe_cap is not False else None},
            "series": {"link": row["cells"]["fgt_swp_status"].series_key if "fgt_swp_status" in row["cells"] else None,
                       "poe_w": row["cells"]["fgt_swp_poe_power"].series_key if "fgt_swp_poe_power" in row["cells"] else None},
            "connected_devices": neighbours,
            "measured_at": _iso(row["updated_at"]),
        })
    ports.sort(key=lambda p: (p["number"] is None, p["number"] or 0, p["name"]))

    vlans: dict[str, dict] = {}
    for p in ports:
        for vid in [p["native_vlan"]] if p["native_vlan"] not in (None, "", "0") else []:
            v = vlans.setdefault(str(vid), {"vlan": str(vid), "native_ports": [], "member_ports": 0})
            v["native_ports"].append(p["name"])
            v["member_ports"] += 1
    draws = [p["poe"]["draw_w"] for p in ports if p["poe"]["draw_w"] is not None]
    return {
        "supported": True,
        "source": "fgSwPortTable",
        "measured_at": _iso(measured),
        "stale": measured is None or (now - measured).total_seconds() > limit,
        "summary": {"ports": len(ports), "up": sum(1 for p in ports if p["up"]),
                    "poe_capable": sum(1 for p in ports if p["poe"]["capable"]),
                    "poe_draw_w": round(sum(draws), 2) if draws else None,
                    "poe_budget_w": None},
        "ports": ports,
        "vlans": sorted(vlans.values(), key=lambda v: int(v["vlan"]) if v["vlan"].isdigit() else 1 << 30),
        "note": None if ports else "The controller reported no ports for this switch yet.",
    }


def _signal_quality(dbm: float | None) -> str | None:
    if dbm is None:
        return None
    if dbm >= -60:
        return "excellent"
    if dbm >= -67:
        return "good"
    if dbm >= -75:
        return "fair"
    return "poor"


_BANDS = {
    "1": "5 GHz", "2": "2.4 GHz", "3": "2.4 GHz", "4": "5 GHz", "5": "2.4 GHz", "6": "5 GHz", "7": "2.4 GHz",
    "8": "2.4 GHz", "9": "2.4 GHz", "10": "5 GHz", "11": "5 GHz", "12": "5 GHz", "13": "2.4 GHz", "14": "5 GHz",
    "15": "6 GHz", "16": "2.4 GHz", "17": "2.4 GHz", "18": "2.4 GHz", "19": "5 GHz", "20": "5 GHz", "21": "5 GHz",
}
_PHY = {
    "1": "802.11a", "2": "802.11b", "3": "802.11g/b", "4": "802.11n/a", "5": "802.11n/g/b", "6": "802.11ac/n/a",
    "7": "802.11n/g", "8": "802.11g", "9": "802.11n", "10": "802.11n", "11": "802.11ac/n", "12": "802.11ac",
    "13": "802.11ax/n/g/b", "14": "802.11ax/ac/n/a", "15": "802.11ax (6 GHz)", "16": "802.11ax/n/g", "17": "802.11ax/n",
    "18": "802.11ax", "19": "802.11ax/ac/n", "20": "802.11ax/ac", "21": "802.11ax",
}
_WIDTH = {"1": "20 MHz", "2": "40 MHz", "3": "80 MHz", "4": "160 MHz"}


def band_of(channel: float | None) -> str | None:
    if channel is None or channel <= 0:
        return None
    return "2.4 GHz" if channel <= 14 else "5 GHz"


async def fortiap_panel(db: AsyncSession, ctx: ChildContext, *, can_view_clients: bool,
                        can_view_users: bool, now: datetime | None = None) -> dict:
    """Radios, SSIDs, client counts and (with permission) client identity of one FortiAP."""
    now = now or datetime.now(timezone.utc)
    shaped = readings(ctx, now)
    serial = shaped["readings"]["serial"]
    profile_cell = ctx.cells.get("fgt_ap_profile")
    ap_profile = profile_cell.text.strip() if profile_cell and profile_cell.text else None
    offline = shaped["readings"]["offline"]

    radios_def, ssids_def = ctx.group_def("fap_radios"), ctx.group_def("fap_ssids")
    profiles_def, clients_def = ctx.group_def("fap_radio_profiles"), ctx.group_def("fap_clients")
    if not radios_def and not clients_def:
        return {"supported": False, "reason": "The controller's template has no FortiAP radio or client tables."}

    profile_radio: dict[int, dict] = {}
    if profiles_def:
        for row in (await _source_rows(db, ctx, "fap_radio_profiles")).values():
            if not _belongs(ctx, profiles_def, row, serial, ap_profile):
                continue
            parts = decode_index(row["oid"], profiles_def["belongs_to"].get("layout") or [])
            if parts:
                band_code = _cell_text(row, "fgt_apf_band")
                profile_radio[int(parts[2])] = {
                    "phy": _PHY.get(band_code or ""), "band": _BANDS.get(band_code or ""),
                    "width": _WIDTH.get(_cell_text(row, "fgt_apf_width") or ""),
                    "channels": _cell_text(row, "fgt_apf_channels")}

    radios: dict[int, dict] = {}
    measured: list[datetime] = []
    if radios_def:
        for inst, row in (await _source_rows(db, ctx, "fap_radios")).items():
            if not _belongs(ctx, radios_def, row, serial, None):
                continue
            parts = decode_index(row["oid"], radios_def["belongs_to"].get("layout") or [])
            if not parts:
                continue
            rid = int(parts[2])
            channel = _cell_num(row, "fgt_apr_channel")
            prof = profile_radio.get(rid, {})
            if row["updated_at"]:
                measured.append(row["updated_at"])
            radios[rid] = {
                "radio_id": rid, "instance": inst,
                "mode": _enum_cell(radios_def, row, "fgt_apr_mode"),
                "bssid": hex_to_mac(_cell_text(row, "fgt_apr_bssid")),
                "channel": None if offline else channel,
                "band": band_of(channel) or prof.get("band"),
                "phy": prof.get("phy"), "width": prof.get("width"), "configured_channels": prof.get("channels"),
                "tx_power_dbm": None if offline else _cell_num(row, "fgt_apr_power"),
                "clients": None if offline else _cell_num(row, "fgt_apr_clients"),
                "series": {k: row["cells"][m].series_key for k, m in (("clients", "fgt_apr_clients"),
                                                                     ("channel", "fgt_apr_channel"))
                           if m in row["cells"]},
                "utilization_pct": None, "signal": None, "ssids": [],
            }

    ssids: list[dict] = []
    vap_by_if: dict[tuple[int, int], str] = {}
    if ssids_def:
        for inst, row in (await _source_rows(db, ctx, "fap_ssids")).items():
            if not _belongs(ctx, ssids_def, row, serial, None):
                continue
            parts = decode_index(row["oid"], ssids_def["belongs_to"].get("layout") or [])
            if not parts:
                continue
            rid, if_index = int(parts[2]), int(parts[3])
            name = row["label"] or f"VAP {if_index}"
            vap_by_if[(rid, if_index)] = name
            entry = {"ssid": name, "radio_id": rid, "if_index": if_index,
                     "clients": None if offline else _cell_num(row, "fgt_vap_clients"),
                     "rx_bps": None if offline else _cell_num(row, "fgt_vap_rx_bps"),
                     "tx_bps": None if offline else _cell_num(row, "fgt_vap_tx_bps"),
                     "series": {k: row["cells"][m].series_key for k, m in (("clients", "fgt_vap_clients"),
                                                                          ("rx_bps", "fgt_vap_rx_bps"),
                                                                          ("tx_bps", "fgt_vap_tx_bps"))
                                if m in row["cells"]}}
            ssids.append(entry)
            if rid in radios:
                radios[rid]["ssids"].append(name)
    ssids.sort(key=lambda s: (s["radio_id"], s["ssid"].lower()))

    clients: list[dict] = []
    signal_by_radio: dict[int, list[float]] = {}
    by_ssid: dict[str, int] = {}
    truncated = False
    if clients_def:
        all_rows = await _source_rows(db, ctx, "fap_clients")
        truncated = len(all_rows) >= int((clients_def.get("table") or {}).get("max_rows") or 500)
        for row in all_rows.values():
            if not _belongs(ctx, clients_def, row, serial, None):
                continue
            parts = decode_index(row["oid"], clients_def["belongs_to"].get("layout") or [])
            rid = int(_cell_num(row, "fgt_sta_radio") or 0)
            if_index = int(parts[1]) if parts else None
            ssid = vap_by_if.get((rid, if_index or -1))
            signal = _cell_num(row, "fgt_sta_signal")
            if signal is not None:
                signal_by_radio.setdefault(rid, []).append(signal)
            by_ssid[ssid or "Unknown SSID"] = by_ssid.get(ssid or "Unknown SSID", 0) + 1
            client = {"radio_id": rid, "ssid": ssid, "signal_dbm": signal,
                      "signal_quality": _signal_quality(signal), "noise_dbm": _cell_num(row, "fgt_sta_noise"),
                      "channel": _cell_num(row, "fgt_sta_channel"), "vlan": _cell_text(row, "fgt_sta_vlan"),
                      "tx_kbps": _cell_num(row, "fgt_sta_bw_tx"), "rx_kbps": _cell_num(row, "fgt_sta_bw_rx")}
            if can_view_clients:
                client.update(mac=parts[2] if parts else None, ip=hex_to_ip(_cell_text(row, "fgt_sta_ip")),
                              host=_cell_text(row, "fgt_sta_host"))
                if can_view_users:
                    client["user"] = _cell_text(row, "fgt_sta_user")
            clients.append(client)
    for rid, values in signal_by_radio.items():
        if rid in radios and values:
            radios[rid]["signal"] = {"avg_dbm": round(sum(values) / len(values), 1), "min_dbm": min(values),
                                     "poor_clients": sum(1 for v in values if v < -75)}
    clients.sort(key=lambda c: (c["signal_dbm"] is None, c["signal_dbm"] if c["signal_dbm"] is not None else 0))

    # Uplink: the CAPWAP session, and the switch port Device Tracker sees the AP's MAC on.
    mac = shaped["readings"]["mac"]
    switch_ports = (await _udt_ports_for_macs(db, [mac])).get(mac, []) if mac else []
    switches = await _children_by_serial(db, str(ctx.controller["id"])) if switch_ports else {}
    uplink_ports = []
    for loc in switch_ports[:3]:
        sw = switches.get(loc["switch_serial"])
        uplink_ports.append({**loc, "switch": {"id": str(sw["id"]), "hostname": sw["hostname"], "status": sw["status"],
                                               "model": sw["model"]} if sw else None})
    limit = stale_after_s(radios_def or ctx.group, ctx.controller.get("snmp_poll_interval"))
    newest = max(measured, default=None)
    return {
        "supported": True,
        "source": "fgWcWtpSessionRadioTable, fgWcWtpSessionVapTable, fgWcStaTable",
        "measured_at": _iso(newest),
        "stale": newest is None or (now - newest).total_seconds() > limit,
        "summary": {"radios": len(radios), "ssids": len({s["ssid"] for s in ssids}),
                    "clients": shaped["readings"]["clients"],
                    "client_rows": len(clients), "clients_truncated": truncated,
                    "rx_bps": shaped["readings"]["rx_bps"], "tx_bps": shaped["readings"]["tx_bps"]},
        "radios": [radios[k] for k in sorted(radios)],
        "ssids": ssids,
        "clients_by_ssid": [{"ssid": k, "clients": v} for k, v in sorted(by_ssid.items(), key=lambda kv: -kv[1])],
        "clients": clients[:500],
        "clients_restricted": not can_view_clients,
        "users_restricted": not can_view_users,
        "uplink": {"capwap": shaped["readings"]["state"], "session_uptime_s": shaped["readings"]["session_uptime_s"],
                   "ip": shaped["readings"]["ip"], "switch_ports": uplink_ports},
    }


# ─── history ───────────────────────────────────────────────────────────────

def _bucket_s(hours: float) -> int:
    if hours <= 6:
        return 60
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 1800
    return 3600


def _ch_rows(sql: str, params: dict) -> list[tuple]:
    from app.core.database import get_clickhouse_client
    return get_clickhouse_client().query(sql, parameters=params).result_rows


def bucketed(controller_id: str, keys: list[str], since: datetime, until: datetime, step: int) -> dict[str, dict[int, float]]:
    """{series_key: {bucket_epoch_s: avg}} of the controller's own series."""
    if not keys:
        return {}
    rows = _ch_rows(f"""
        SELECT metric_key, toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {int(step)} SECOND)) AS b, avg(value)
        FROM zenplus.snmp_metrics
        WHERE device_id = %(cid)s AND metric_key IN %(keys)s AND timestamp >= %(since)s AND timestamp <= %(until)s
        GROUP BY metric_key, b ORDER BY b
    """, {"cid": controller_id, "keys": keys,
          "since": since.astimezone(timezone.utc).replace(tzinfo=None),
          "until": until.astimezone(timezone.utc).replace(tzinfo=None)})
    out: dict[str, dict[int, float]] = {}
    for key, b, value in rows:
        out.setdefault(key, {})[int(b)] = float(value)
    return out


def _points(series: dict[int, float]) -> list[dict]:
    return [{"ts": datetime.fromtimestamp(b, tz=timezone.utc).isoformat(), "value": round(v, 3)}
            for b, v in sorted(series.items())]


def history(ctx: ChildContext, panel: dict | None, hours: float, now: datetime | None = None) -> dict:
    """History of one child from the controller's series: headline gauges plus
    PoE draw and ports up (switch) or traffic and per-radio clients (AP)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    step = _bucket_s(hours)
    spec = ctx.spec
    wanted: dict[str, str] = {}
    for name, key_name in (("cpu", "cpu_key"), ("memory", "memory_key"), ("clients", "clients_key"),
                           ("uptime_s", "uptime_key")):
        if spec.get(key_name):
            wanted[name] = series_key(spec[key_name], ctx.instance)
    if ctx.group_key == "access_points":
        wanted["rx_bps"] = series_key("fgt_ap_rx_bps", ctx.instance)
        wanted["tx_bps"] = series_key("fgt_ap_tx_bps", ctx.instance)
    port_links, port_poe, radio_keys = [], [], {}
    if panel and panel.get("ports"):
        port_links = [p["series"]["link"] for p in panel["ports"] if p["series"].get("link")]
        port_poe = [p["series"]["poe_w"] for p in panel["ports"] if p["series"].get("poe_w")]
    if panel and panel.get("radios"):
        radio_keys = {r["radio_id"]: r["series"]["clients"] for r in panel["radios"] if r["series"].get("clients")}

    data = bucketed(str(ctx.controller["id"]), list(wanted.values()) + port_links + port_poe + list(radio_keys.values()),
                    since, now, step)
    series = [{"key": name, "points": _points(data.get(key, {}))} for name, key in wanted.items()]
    if port_poe:
        total: dict[int, float] = {}
        for key in port_poe:
            for b, v in data.get(key, {}).items():
                total[b] = total.get(b, 0.0) + v
        series.append({"key": "poe_draw_w", "points": _points(total)})
    if port_links:
        up: dict[int, float] = {}
        for key in port_links:
            for b, v in data.get(key, {}).items():
                up[b] = up.get(b, 0.0) + (1.0 if v >= 0.5 else 0.0)
        series.append({"key": "ports_up", "points": _points(up)})
    for rid, key in sorted(radio_keys.items()):
        series.append({"key": f"radio_{rid}_clients", "radio_id": rid, "points": _points(data.get(key, {}))})
    return {"hours": hours, "step_s": step, "from": since.isoformat(), "to": now.isoformat(),
            "controller_id": str(ctx.controller["id"]), "series": series}


# ─── availability from controller-reported state ───────────────────────────

def availability_from_buckets(buckets: dict[int, tuple[int, int]], since: datetime, until: datetime,
                              excluded: list[tuple[datetime, datetime]] | None = None,
                              step: int = AVAILABILITY_BUCKET_S) -> dict:
    """``buckets`` = {bucket_epoch_s: (available_samples, known_samples)}.

    Buckets inside a maintenance window are left out of the numerator, the
    denominator and the expected coverage. Coverage is observed buckets over
    expected buckets; below COVERAGE_MIN the percentage is withheld."""
    excluded = excluded or []
    start = int(since.timestamp()) // step * step
    end = int(until.timestamp())
    expected = observed = 0
    up = total = 0
    b = start
    while b < end:
        lo, hi = datetime.fromtimestamp(b, tz=timezone.utc), datetime.fromtimestamp(b + step, tz=timezone.utc)
        if not any(s < hi and e > lo for s, e in excluded):
            expected += 1
            if b in buckets and buckets[b][1] > 0:
                observed += 1
                up += buckets[b][0]
                total += buckets[b][1]
        b += step
    coverage = observed / expected if expected else 0.0
    sufficient = expected > 0 and total > 0 and coverage >= COVERAGE_MIN
    return {"pct": round(up / total * 100, 2) if sufficient else None,
            "coverage_pct": round(coverage * 100, 1), "sufficient": sufficient,
            "samples": total, "basis": "controller"}


def status_codes(spec: dict) -> tuple[list[float], list[float]]:
    """(available codes, known codes) from a children status_map."""
    smap = spec.get("status_map") or {}
    known = [float(k) for k, v in smap.items() if _num(k) is not None]
    available = [float(k) for k, v in smap.items() if v in ("up", "degraded") and _num(k) is not None]
    return available, known


def availability_buckets(contexts: list[ChildContext], since: datetime, until: datetime,
                         step: int = AVAILABILITY_BUCKET_S) -> dict[str, dict[int, tuple[int, int]]]:
    """{child_id: {bucket: (available, known)}} from the controllers' status series."""
    by_controller: dict[str, list[ChildContext]] = {}
    for ctx in contexts:
        if ctx.spec.get("status_key"):
            by_controller.setdefault(str(ctx.controller["id"]), []).append(ctx)
    out: dict[str, dict[int, tuple[int, int]]] = {}
    for cid, ctxs in by_controller.items():
        key_to_child: dict[str, ChildContext] = {series_key(c.spec["status_key"], c.instance): c for c in ctxs}
        rows = _ch_rows(f"""
            SELECT metric_key, toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {int(step)} SECOND)) AS b,
                   groupArray(value)
            FROM zenplus.snmp_metrics
            WHERE device_id = %(cid)s AND metric_key IN %(keys)s AND timestamp >= %(since)s AND timestamp < %(until)s
            GROUP BY metric_key, b
        """, {"cid": cid, "keys": list(key_to_child),
              "since": since.astimezone(timezone.utc).replace(tzinfo=None),
              "until": until.astimezone(timezone.utc).replace(tzinfo=None)})
        for key, b, values in rows:
            ctx = key_to_child.get(key)
            if ctx is None:
                continue
            available, known = status_codes(ctx.spec)
            av = sum(1 for v in values if float(v) in available)
            kn = sum(1 for v in values if float(v) in known)
            out.setdefault(str(ctx.child["id"]), {})[int(b)] = (av, kn)
    return out
