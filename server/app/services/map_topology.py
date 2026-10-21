"""Discovered-topology logic for network maps (manual_maps API).

Pure functions over plain dicts so they can be unit-tested without a
database:

* interface canonicalisation: an LLDP/CDP neighbour reports its long port
  name ("GigabitEthernet1/0/50") while the device's own interface table says
  "Gi1/0/50"; both resolve to the device_interfaces row and the device's own
  ``if_name`` is what a map link stores.
* adjacency freshness: topology_links rows are never deleted (the poller only
  bumps ``last_seen_at``), so a re-cabled or decommissioned port keeps its old
  row forever. An adjacency is *stale* when it was last seen more than
  ``FRESH_HOURS`` ago, when either bound interface is down and has never been
  up, or when an end's interface is missing from a device that has an
  interface inventory.
* physical members: one cable is reported from both sides (A sees B and B
  sees A); members are de-duplicated per unordered (device, port) pair.
* layouts: tiered placement for "build from site" and a half ring for
  "add neighbours", on the 4000 x 2400 design canvas the dashboard uses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

FRESH_HOURS = 24
CANVAS_W = 4000
CANVAS_H = 2400
MAX_PER_ROW = 16
TIER_BAND_PX = 450
SUBROW_PX = 170
TOP_PX = 300
BOTTOM_PX = 2300
RING_RADIUS_PX = 320

WAN_EDGE_TYPES = {"firewall", "router"}
SWITCH_TYPES = {"switch", "l3_switch"}

# Longest names first: "fortygigabitethernet" contains "gigabitethernet".
_IFACE_ABBREV = [
    ("hundredgigabitethernet", "hu"),
    ("fortygigabitethernet", "fo"),
    ("twentyfivegigabitethernet", "twe"),
    ("twentyfivegige", "twe"),
    ("twogigabitethernet", "tw"),
    ("fivegigabitethernet", "fi"),
    ("tengigabitethernet", "te"),
    ("gigabitethernet", "gi"),
    ("fastethernet", "fa"),
    ("hundredgige", "hu"),
    ("fortygige", "fo"),
    ("tengige", "te"),
    ("port-channel", "po"),
    ("ethernet", "eth"),
]
_FIBER_PREFIXES = ("te", "twe", "fo", "hu")
_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


# ─── interfaces ─────────────────────────────────────────────────────────────

def norm_iface(name: Optional[str]) -> str:
    """Case-, whitespace- and abbreviation-insensitive interface key."""
    if not name:
        return ""
    n = re.sub(r"\s+", "", str(name).strip().lower())
    for full, short in _IFACE_ABBREV:
        n = n.replace(full, short)
    return n


def _norm_mac(value: Optional[str]) -> str:
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def resolve_interface(name: Optional[str], ifaces: Iterable[dict], *,
                      if_index: Optional[int] = None) -> Optional[dict]:
    """The device_interfaces row a reported/typed interface refers to.

    Exact matches only (an ifIndex, then the normalised if_name, if_descr,
    if_alias, then a numeric port id as ifIndex, then a MAC port id) — never a
    substring, so "Gi1/0/5" can not resolve to "Gi1/0/50".
    """
    rows = list(ifaces or [])
    if not rows:
        return None
    if if_index is not None:
        for row in rows:
            if row.get("if_index") is not None and int(row["if_index"]) == int(if_index):
                return row
    needle = norm_iface(name)
    if not needle:
        return None
    for fld in ("if_name", "if_descr", "if_alias"):
        for row in rows:
            if norm_iface(row.get(fld)) == needle:
                return row
    if needle.isdigit():
        for row in rows:
            if row.get("if_index") is not None and int(row["if_index"]) == int(needle):
                return row
    mac = _norm_mac(name)
    if _MAC_RE.match(mac):
        for row in rows:
            if _norm_mac(row.get("mac_address")) == mac:
                return row
    return None


# Long → short interface names, the same family as norm_iface's table but
# with the conventional short-form capitalisation ("Gi1/0/52").
_SHORT_FORMS = [
    ("hundredgigabitethernet", "Hu"),
    ("fortygigabitethernet", "Fo"),
    ("twentyfivegigabitethernet", "Twe"),
    ("twentyfivegige", "Twe"),
    ("twogigabitethernet", "Tw"),
    ("fivegigabitethernet", "Fi"),
    ("tengigabitethernet", "Te"),
    ("gigabitethernet", "Gi"),
    ("fastethernet", "Fa"),
    ("hundredgige", "Hu"),
    ("fortygige", "Fo"),
    ("tengige", "Te"),
    ("port-channel", "Po"),
    ("ethernet", "Eth"),
]


def short_iface(name: Optional[str]) -> Optional[str]:
    """'GigabitEthernet1/0/52' → 'Gi1/0/52', 'Port-channel10' → 'Po10'.

    Used for names that could not be resolved against an interface table
    (unmanaged neighbours, a remote device without that interface), so map
    chips read like every other cable. Anything else is returned unchanged.
    """
    if not name:
        return name
    text = str(name).strip()
    low = text.lower()
    for full, short in _SHORT_FORMS:
        if low.startswith(full):
            rest = text[len(full):].lstrip()
            if rest[:1].isdigit():
                return short + rest
            return text
    return text


def iface_label(row: Optional[dict], fallback: Optional[str] = None) -> Optional[str]:
    """The device's own name for an interface row (if_name, else if_descr);
    an unresolved name is abbreviated (short_iface)."""
    if row:
        return row.get("if_name") or row.get("if_descr") or short_iface(fallback)
    return short_iface(fallback)


def is_oper_up(status: Optional[str]) -> Optional[bool]:
    """True/False for a known oper status, None when not reported."""
    if status is None or str(status).strip() == "":
        return None
    return str(status).strip().lower() in ("up", "1")


def never_up(row: Optional[dict]) -> bool:
    """Down now and never seen up (an unused port)."""
    if not row:
        return False
    return is_oper_up(row.get("oper_status")) is False and row.get("last_up_at") is None


def iface_speed(row: Optional[dict]) -> Optional[int]:
    if not row:
        return None
    for key in ("configured_speed_bps", "if_speed"):
        value = row.get(key)
        if value:
            try:
                v = int(value)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    return None


def speed_label(bps: Optional[int]) -> Optional[str]:
    """1_000_000_000 → '1G', 100_000_000 → '100M', 2_500_000_000 → '2.5G'."""
    if not bps or bps <= 0:
        return None
    for unit, div in (("T", 10**12), ("G", 10**9), ("M", 10**6), ("K", 10**3)):
        if bps >= div:
            value = bps / div
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return f"{bps}"


def link_kind(src_if: Optional[str], dst_if: Optional[str], speed_bps: Optional[int]) -> str:
    """'fiber' for 10G-class ports (Te/Twe/Fo/Hu) or speed >= 10G, else 'ethernet'."""
    if speed_bps and speed_bps >= 10_000_000_000:
        return "fiber"
    for name in (src_if, dst_if):
        # "te1/0/1", "twe1/0/1", "fo1/1/1", "hu1/0/49" (not "tw1/0/1" = 2.5G copper)
        prefix = re.match(r"[a-z]+(?=\d)", norm_iface(name))
        if prefix and prefix.group(0) in _FIBER_PREFIXES:
            return "fiber"
    return "ethernet"


# ─── adjacencies ────────────────────────────────────────────────────────────

def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def age_text(hours: float) -> str:
    if hours >= 48:
        return f"{int(hours // 24)} days"
    return f"{int(hours)} h"


# Reason codes for a stale adjacency (suggested-links ``reasons``), oriented
# so "local" is the suggestion's source device and "remote" its target.
REASON_OLD = "old_evidence"
_SIDE_FLAGS = ("port_never_up", "port_down", "port_missing")


@dataclass
class Member:
    """One physical cable between devices ``a`` and ``b`` (a < b by id)."""

    a: str
    b: str
    a_if: Optional[str]
    b_if: Optional[str]
    a_row: Optional[dict]
    b_row: Optional[dict]
    last_seen_at: Optional[datetime]
    protocol: Optional[str]
    confidence: int
    old: bool = False
    a_flags: list[str] = field(default_factory=list)
    b_flags: list[str] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        return self.old or bool(self.a_flags) or bool(self.b_flags)

    def oriented(self, src: str) -> tuple[Optional[str], Optional[str], Optional[dict], Optional[dict]]:
        if src == self.a:
            return self.a_if, self.b_if, self.a_row, self.b_row
        return self.b_if, self.a_if, self.b_row, self.a_row

    def reasons(self, src: str) -> list[str]:
        """Reason codes, "local" = ``src``'s end: old_evidence,
        local/remote_port_never_up, local/remote_port_down,
        local/remote_port_missing."""
        mine, theirs = (self.a_flags, self.b_flags) if src == self.a else (self.b_flags, self.a_flags)
        out = [REASON_OLD] if self.old else []
        out += [f"local_{f}" for f in _SIDE_FLAGS if f in mine]
        out += [f"remote_{f}" for f in _SIDE_FLAGS if f in theirs]
        return out


def port_flags(row: Optional[dict], has_inventory: bool) -> list[str]:
    """port_missing / port_never_up / port_down for one end of an adjacency."""
    if row is None:
        return ["port_missing"] if has_inventory else []
    if never_up(row):
        return ["port_never_up"]
    if is_oper_up(row.get("oper_status")) is False:
        return ["port_down"]
    return []


def classify_member(member: Member, ifaces_by_device: dict, now: datetime,
                    fresh_hours: int = FRESH_HOURS) -> Member:
    """Stale when last seen more than ``fresh_hours`` ago, or either end's
    interface is missing (on a device with an interface inventory), down, or
    down and never up."""
    seen = _aware(member.last_seen_at)
    member.old = seen is None or (now - seen).total_seconds() / 3600 > fresh_hours
    member.a_flags = port_flags(member.a_row, bool(ifaces_by_device.get(member.a)))
    member.b_flags = port_flags(member.b_row, bool(ifaces_by_device.get(member.b)))
    return member


def build_adjacencies(edges: Iterable[dict], ifaces_by_device: dict, hostnames: dict,
                      now: Optional[datetime] = None,
                      fresh_hours: int = FRESH_HOURS) -> dict[frozenset, list[Member]]:
    """Group managed topology_links rows into physical members per device pair.

    ``edges`` carry local_device_id, local_if_index, local_if_name,
    remote_device_id, remote_port_id, remote_if_name, protocol, confidence,
    last_seen_at. Rows without a managed remote device are ignored.
    """
    now = now or datetime.now(timezone.utc)
    merged: dict[tuple, Member] = {}
    for e in edges:
        local = str(e.get("local_device_id") or "")
        remote = str(e.get("remote_device_id") or "")
        if not local or not remote or local == remote or e.get("remote_device_id") is None:
            continue
        l_ifs = ifaces_by_device.get(local) or []
        r_ifs = ifaces_by_device.get(remote) or []
        l_row = resolve_interface(e.get("local_if_name"), l_ifs, if_index=e.get("local_if_index"))
        r_raw = e.get("remote_port_id") or e.get("remote_if_name")
        r_row = resolve_interface(e.get("remote_port_id"), r_ifs) or resolve_interface(e.get("remote_if_name"), r_ifs)
        l_name = iface_label(l_row, e.get("local_if_name"))
        r_name = iface_label(r_row, r_raw)
        if local < remote:
            a, b, a_if, b_if, a_row, b_row = local, remote, l_name, r_name, l_row, r_row
        else:
            a, b, a_if, b_if, a_row, b_row = remote, local, r_name, l_name, r_row, l_row
        key = (a, norm_iface(a_if), b, norm_iface(b_if))
        seen = _aware(e.get("last_seen_at"))
        conf = int(e.get("confidence") or 0)
        cur = merged.get(key)
        if cur is None:
            merged[key] = Member(a, b, a_if, b_if, a_row, b_row, seen, e.get("protocol"), conf)
            continue
        if seen and (cur.last_seen_at is None or seen > cur.last_seen_at):
            cur.last_seen_at = seen
            cur.protocol = e.get("protocol") or cur.protocol
        cur.confidence = max(cur.confidence, conf)
        cur.a_row = cur.a_row or a_row
        cur.b_row = cur.b_row or b_row
        cur.a_if = cur.a_if or a_if
        cur.b_if = cur.b_if or b_if

    pairs: dict[frozenset, list[Member]] = {}
    for m in merged.values():
        classify_member(m, ifaces_by_device, now, fresh_hours)
        pairs.setdefault(frozenset((m.a, m.b)), []).append(m)
    return pairs


def _member_score(m: Member) -> tuple:
    seen = m.last_seen_at.timestamp() if m.last_seen_at else 0
    return (0 if m.stale else 1, 1 if (m.a_if and m.b_if) else 0, m.confidence, seen)


def summarize_pair(members: list[Member], src: str) -> dict:
    """Suggestion fields for one device pair, oriented with ``src`` first."""
    fresh = [m for m in members if not m.stale]
    stale = not fresh
    use = sorted(fresh or members, key=_member_score, reverse=True)
    rep = use[0]
    s_if, d_if, s_row, d_row = rep.oriented(src)
    reasons: list[str] = []
    if stale:
        for m in use:
            for r in m.reasons(src):
                if r not in reasons:
                    reasons.append(r)
    seen = max((m.last_seen_at for m in members if m.last_seen_at), default=None)
    out_members = []
    for m in use:
        ms, md, _, _ = m.oriented(src)
        out_members.append({"src_interface": ms, "dst_interface": md})
    return {
        "src_interface": s_if,
        "dst_interface": d_if,
        "protocol": rep.protocol,
        "confidence": rep.confidence,
        "physical_links": len(use),
        "members": out_members,
        "last_seen_at": seen.isoformat() if seen else None,
        "stale": stale,
        "reasons": reasons,
        "src_oper": s_row.get("oper_status") if s_row else None,
        "dst_oper": d_row.get("oper_status") if d_row else None,
        "speed_bps": iface_speed(s_row) or iface_speed(d_row),
    }


def link_metadata(summary: dict) -> dict:
    """metadata for a manual_map_links row created from a suggestion."""
    speed = summary.get("speed_bps")
    md = {
        "src_interface": summary.get("src_interface"),
        "dst_interface": summary.get("dst_interface"),
        "kind": link_kind(summary.get("src_interface"), summary.get("dst_interface"), speed),
        "shape": "straight",
        "width_scale": 1,
        "discovered": True,
        "protocol": summary.get("protocol"),
    }
    label = speed_label(speed)
    if label:
        md["speed"] = label
    if (summary.get("physical_links") or 0) > 1:
        md["members"] = summary.get("members") or []
        md["physical_links"] = summary["physical_links"]
    return md


def fresh_pairs(adjacencies: dict[frozenset, list[Member]]) -> set[frozenset]:
    return {key for key, members in adjacencies.items() if any(not m.stale for m in members)}


def guess_icon(text: Optional[str]) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("router", "ios xr", "ios-xr", "asr", "isr")):
        return "router"
    if any(k in t for k in ("switch", "catalyst", "nexus", "procurve", "aruba", "c9", "ws-c", "ex2", "ex3", "ex4")):
        return "switch"
    return "other"


def unmanaged_neighbours(edges: Iterable[dict], device_id: str, local_ifs: list[dict],
                         now: Optional[datetime] = None, fresh_hours: int = FRESH_HOURS) -> list[dict]:
    """Fresh LLDP/CDP neighbours of ``device_id`` that are not monitored devices."""
    now = now or datetime.now(timezone.utc)
    found: dict[str, dict] = {}
    for e in edges:
        if str(e.get("local_device_id")) != str(device_id) or e.get("remote_device_id") is not None:
            continue
        seen = _aware(e.get("last_seen_at"))
        if seen is None or (now - seen).total_seconds() / 3600 > fresh_hours:
            continue
        row = resolve_interface(e.get("local_if_name"), local_ifs, if_index=e.get("local_if_index"))
        if never_up(row):
            continue
        ident = (e.get("remote_chassis_id") or e.get("remote_hostname") or "").strip().lower()
        if not ident:
            continue
        md = e.get("metadata") or {}
        if not isinstance(md, dict):
            md = {}
        cur = found.get(ident)
        if cur and cur["_seen"] >= seen:
            continue
        found[ident] = {
            "hostname": e.get("remote_hostname") or e.get("remote_chassis_id"),
            "chassis_id": e.get("remote_chassis_id"),
            "port_id": short_iface(e.get("remote_port_id") or e.get("remote_if_name")),
            "local_if": iface_label(row, e.get("local_if_name")),
            "protocol": e.get("protocol"),
            "icon": guess_icon(f"{md.get('sys_descr') or ''} {e.get('remote_hostname') or ''}"),
            "speed_bps": iface_speed(row),
            "_seen": seen,
        }
    out = sorted(found.values(), key=lambda n: (str(n["hostname"] or "").lower(), str(n["chassis_id"] or "")))
    for n in out:
        n.pop("_seen", None)
    return out


# ─── layouts ────────────────────────────────────────────────────────────────

def px_to_pct(x: float, y: float) -> tuple[float, float]:
    xp = min(98.0, max(2.0, x / CANVAS_W * 100))
    yp = min(98.0, max(2.0, y / CANVAS_H * 100))
    return round(xp, 2), round(yp, 2)


def compute_tiers(devices: list[dict], pairs: Iterable[frozenset]) -> dict[str, Optional[int]]:
    """tier per device id: 0 WAN edge, 1 core, 2+ hop distance from the core,
    None for devices without a fresh adjacency to another selected device.

    Core = the most-connected switch(es), plus a redundant partner that is
    adjacent to one of them, has at least 75 % of its degree and also
    connects to the WAN edge. Components the core cannot reach get their own
    most-connected device as a core.

    ``devices``: [{id, hostname, device_type}]; ``pairs``: unordered device-id
    pairs with a fresh adjacency.
    """
    ids = {str(d["id"]) for d in devices}
    dtype = {str(d["id"]): (d.get("device_type") or "").lower() for d in devices}
    host = {str(d["id"]): (d.get("hostname") or "").lower() for d in devices}
    nbrs: dict[str, set[str]] = {i: set() for i in ids}
    for p in pairs:
        if len(p) != 2:
            continue
        a, b = tuple(p)
        if a in ids and b in ids:
            nbrs[a].add(b)
            nbrs[b].add(a)
    tier: dict[str, Optional[int]] = {}
    for i in ids:
        if dtype[i] in WAN_EDGE_TYPES:
            tier[i] = 0
    remaining = {i for i in ids if i not in tier and nbrs[i]}
    order = lambda i: (-len(nbrs[i]), host[i], i)  # noqa: E731

    first = True
    while remaining:
        if first:
            cands = [i for i in remaining if dtype[i] in SWITCH_TYPES] or list(remaining)
            maxdeg = max(len(nbrs[i]) for i in cands)
            primary = sorted((i for i in cands if len(nbrs[i]) == maxdeg), key=order)
            # A redundant core partner: adjacent to a primary core, nearly as
            # connected, and uplinked to the WAN edge as well.
            floor = max(1, math.ceil(0.75 * maxdeg))
            edge_ids = {i for i, t in tier.items() if t == 0}
            partners = [i for i in cands if i not in primary and len(nbrs[i]) >= floor
                        and nbrs[i] & set(primary) and nbrs[i] & edge_ids]
            core = (primary + sorted(partners, key=order))[:4]
            first = False
        else:
            # Another component not reachable from the first core.
            core = [sorted(remaining, key=order)[0]]
        dist = {c: 0 for c in core}
        queue = list(core)
        while queue:
            nxt = []
            for cur in queue:
                for n in sorted(nbrs[cur]):
                    if n not in dist:
                        dist[n] = dist[cur] + 1
                        nxt.append(n)
            queue = nxt
        for i, d in dist.items():
            if i in remaining:
                tier[i] = 1 + d
                remaining.discard(i)
        remaining -= set(core)
    for i in ids:
        tier.setdefault(i, None)
    return tier


def tier_name(t: Optional[int]) -> str:
    if t is None:
        return "unconnected"
    return {0: "edge", 1: "core"}.get(t, f"tier{t}")


def tiered_layout(devices: list[dict], pairs: Iterable[frozenset]) -> tuple[dict[str, tuple[float, float]], dict[str, int], dict[str, Optional[int]]]:
    """Positions (x_pct, y_pct) per device id, device counts per tier name,
    and the tier of each device.

    Rows top to bottom: WAN edge, core, then by hop distance from the core,
    unconnected devices last. Each row is evenly spaced across the canvas,
    rows longer than MAX_PER_ROW wrap into sub-rows, and children are ordered
    under their parent. Rows start at y=300 px and step 450 px (compressed to
    fit the 2400 px canvas when there are many rows).
    """
    pairs = [p for p in pairs if len(p) == 2]
    tier = compute_tiers(devices, pairs)
    host = {str(d["id"]): (d.get("hostname") or "").lower() for d in devices}
    nbrs: dict[str, set[str]] = {str(d["id"]): set() for d in devices}
    for p in pairs:
        a, b = tuple(p)
        if a in nbrs and b in nbrs:
            nbrs[a].add(b)
            nbrs[b].add(a)

    present = sorted({t for t in tier.values() if t is not None})
    rows: list[Optional[int]] = list(present) + ([None] if any(t is None for t in tier.values()) else [])
    xpos: dict[str, float] = {}
    ypos: dict[str, float] = {}
    rank: dict[str, int] = {}
    y = float(TOP_PX)
    placed_rows = 0
    for t in rows:
        members = [i for i, ti in tier.items() if ti == t]

        def parent_key(i: str) -> tuple:
            parents = [rank[n] for n in nbrs[i] if n in rank and tier.get(n) is not None
                       and (t is None or (tier[n] or 0) < t)]
            return (min(parents) if parents else 10**9, host[i], i)

        members.sort(key=parent_key)
        if placed_rows:
            y += TIER_BAND_PX
        chunks = [members[k:k + MAX_PER_ROW] for k in range(0, len(members), MAX_PER_ROW)] or [[]]
        for ci, chunk in enumerate(chunks):
            if ci:
                y += SUBROW_PX
            n = len(chunk)
            for j, i in enumerate(chunk):
                xpos[i] = CANVAS_W * (j + 1) / (n + 1)
                ypos[i] = y
                rank[i] = len(rank)
        placed_rows += 1

    max_y = max(ypos.values(), default=TOP_PX)
    if max_y > BOTTOM_PX:
        scale = (BOTTOM_PX - TOP_PX) / (max_y - TOP_PX)
        for i in ypos:
            ypos[i] = TOP_PX + (ypos[i] - TOP_PX) * scale
    positions = {i: px_to_pct(xpos[i], ypos[i]) for i in xpos}
    counts: dict[str, int] = {}
    for t in tier.values():
        counts[tier_name(t)] = counts.get(tier_name(t), 0) + 1
    return positions, counts, tier


def ring_positions(center_x_pct: float, center_y_pct: float, n: int,
                   radius_px: float = RING_RADIUS_PX) -> list[tuple[float, float]]:
    """``n`` positions on a half ring below the centre (above it when the
    centre is near the bottom edge). Rings grow outward when one is full."""
    if n <= 0:
        return []
    cx = center_x_pct / 100 * CANVAS_W
    cy = center_y_pct / 100 * CANVAS_H
    below = cy + radius_px <= CANVAS_H - 100
    out: list[tuple[float, float]] = []
    remaining = n
    ring = 0
    while remaining > 0:
        cap = 9 + 4 * ring
        k = min(cap, remaining)
        r = radius_px + 180 * ring
        start, span = (20.0, 140.0) if below else (200.0, 140.0)
        for j in range(k):
            ang = math.radians(start + (j + 0.5) * span / k)
            out.append(px_to_pct(cx + r * math.cos(ang), cy + r * math.sin(ang)))
        remaining -= k
        ring += 1
    return out



NODE_CLEARANCE_PX = 150
SHAPE_CLEARANCE_PX = 60
RING_STEP_PX = 160
ANGLE_STEP_DEG = 20
MAX_RINGS = 12


def _ring_angles() -> list[float]:
    """Angles (degrees, screen coordinates: 90 = straight down) for one ring:
    the lower half-plane first, nearest to straight down first, then the
    upper half the same way."""
    lower = [a for a in range(10, 180, ANGLE_STEP_DEG)]
    upper = [a for a in range(190, 360, ANGLE_STEP_DEG)]
    return sorted(lower, key=lambda a: (abs(a - 90), a)) + sorted(upper, key=lambda a: (abs(a - 270), a))


def free_positions(center_x_pct: float, center_y_pct: float, n: int,
                   node_centres_pct: Iterable[tuple[float, float]] = (),
                   shape_rects_pct: Iterable[tuple[float, float, float, float]] = (),
                   radius_px: float = RING_RADIUS_PX) -> list[tuple[float, float]]:
    """``n`` (x_pct, y_pct) node centres around a node that avoid what is
    already on the canvas.

    Candidates sit on rings of radius 320, 480, 640 … px (4000 x 2400
    logical canvas) at 20° steps, lower half-plane first. A candidate is
    skipped when its centre is within 150 px of an existing node centre or of
    a position already chosen, or inside / within 60 px of a shape rectangle
    (x, y = top-left, w, h in pct). Rectangles that contain the centre node
    (its zone) are not obstacles. Positions are clamped to 2..98 pct; if the
    rings run out, the remaining positions fall back to ring_positions.
    """
    if n <= 0:
        return []
    cx, cy = center_x_pct / 100 * CANVAS_W, center_y_pct / 100 * CANVAS_H
    nodes_px = [(x / 100 * CANVAS_W, y / 100 * CANVAS_H) for x, y in node_centres_pct]
    rects_px = []
    for x, y, w, h in shape_rects_pct:
        x0, y0 = x / 100 * CANVAS_W, y / 100 * CANVAS_H
        x1, y1 = x0 + w / 100 * CANVAS_W, y0 + h / 100 * CANVAS_H
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            continue  # the node's own zone
        rects_px.append((x0 - SHAPE_CLEARANCE_PX, y0 - SHAPE_CLEARANCE_PX,
                         x1 + SHAPE_CLEARANCE_PX, y1 + SHAPE_CLEARANCE_PX))
    chosen_px: list[tuple[float, float]] = []
    out: list[tuple[float, float]] = []
    min_x, max_x = 0.02 * CANVAS_W, 0.98 * CANVAS_W
    min_y, max_y = 0.02 * CANVAS_H, 0.98 * CANVAS_H

    def free(px: float, py: float) -> bool:
        for ox, oy in nodes_px + chosen_px:
            if math.hypot(px - ox, py - oy) < NODE_CLEARANCE_PX:
                return False
        return not any(x0 <= px <= x1 and y0 <= py <= y1 for x0, y0, x1, y1 in rects_px)

    for ring in range(MAX_RINGS):
        r = radius_px + RING_STEP_PX * ring
        for ang in _ring_angles():
            px = min(max_x, max(min_x, cx + r * math.cos(math.radians(ang))))
            py = min(max_y, max(min_y, cy + r * math.sin(math.radians(ang))))
            if free(px, py):
                chosen_px.append((px, py))
                out.append(px_to_pct(px, py))
                if len(out) == n:
                    return out
    return out + ring_positions(center_x_pct, center_y_pct, n - len(out), radius_px)

# ─── verify ─────────────────────────────────────────────────────────────────

@dataclass
class Observation:
    """One LLDP/CDP sighting seen from a (device, port)."""

    peer_device: Optional[str]
    peer_if: str              # normalised peer port ('' when unknown)
    peer_label: str
    peer_if_label: Optional[str]
    last_seen_at: Optional[datetime]
    fresh: bool


def evidence_index(edges: Iterable[dict], ifaces_by_device: dict, hostnames: dict,
                   now: Optional[datetime] = None,
                   fresh_hours: int = FRESH_HOURS) -> dict[tuple[str, str], list[Observation]]:
    """(device_id, normalised port) → what LLDP/CDP saw on that port, from
    either side of the cable (managed and unmanaged neighbours)."""
    now = now or datetime.now(timezone.utc)
    index: dict[tuple[str, str], list[Observation]] = {}
    for e in edges:
        local = str(e.get("local_device_id") or "")
        if not local:
            continue
        remote = str(e["remote_device_id"]) if e.get("remote_device_id") is not None else None
        l_row = resolve_interface(e.get("local_if_name"), ifaces_by_device.get(local) or [],
                                  if_index=e.get("local_if_index"))
        l_name = iface_label(l_row, e.get("local_if_name"))
        r_raw = e.get("remote_port_id") or e.get("remote_if_name")
        r_row = None
        if remote:
            r_ifs = ifaces_by_device.get(remote) or []
            r_row = resolve_interface(e.get("remote_port_id"), r_ifs) or resolve_interface(e.get("remote_if_name"), r_ifs)
        r_name = iface_label(r_row, r_raw)
        seen = _aware(e.get("last_seen_at"))
        fresh = seen is not None and (now - seen).total_seconds() / 3600 <= fresh_hours
        peer_label = (hostnames.get(remote) if remote else None) or e.get("remote_hostname") \
            or e.get("remote_chassis_id") or "unknown neighbour"
        if l_name:
            index.setdefault((local, norm_iface(l_name)), []).append(
                Observation(remote, norm_iface(r_name), peer_label, r_name, seen, fresh))
        if remote and r_name:
            index.setdefault((remote, norm_iface(r_name)), []).append(
                Observation(local, norm_iface(l_name), hostnames.get(local) or local, l_name, seen, fresh))
    return index


def verify_link(link_id: str, annotation: bool, ends: list[tuple[Optional[dict], Optional[str]]],
                ifaces_by_device: dict, evidence: dict[tuple[str, str], list[Observation]],
                now: Optional[datetime] = None) -> list[dict]:
    """Issues for one map link.

    ``ends`` = [(endpoint, typed interface)] for source and target, where an
    endpoint is {kind: device|server|service_check|shape, device_id, hostname}.
    Only device ends can be bound to an interface. Codes: unbound,
    half_bound, port_not_found, port_down, port_never_up, no_evidence,
    contradicted, stale_adjacency. An annotation link bound on no side is
    info, not a warning.
    """
    now = now or datetime.now(timezone.utc)
    issues: list[dict] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"link_id": link_id, "annotation": annotation, "severity": severity,
                       "code": code, "message": message})

    names = " ⇄ ".join(str((ep or {}).get("hostname") or "?") for ep, _ in ends)
    device_ends = [(ep, (typed or "").strip()) for ep, typed in ends if ep and ep.get("kind") == "device"]
    if not device_ends:
        return issues
    bound = [(ep, typed) for ep, typed in device_ends if typed]
    if not bound:
        add("info" if annotation else "warning", "unbound", f"{names}: no interface bound on either end")
        return issues
    if not annotation and len(device_ends) == 2 and len(bound) == 1:
        ep, typed = bound[0]
        add("warning", "half_bound", f"{names}: only {ep.get('hostname')} {typed} is bound")

    resolved: list[tuple[dict, dict]] = []
    problem = False
    for ep, typed in bound:
        ifs = ifaces_by_device.get(ep["device_id"]) or []
        row = resolve_interface(typed, ifs)
        host = ep.get("hostname")
        if row is None:
            if ifs:
                add("warning", "port_not_found", f"{typed} not found on {host}")
                problem = True
            continue
        name = iface_label(row, typed)
        if never_up(row):
            add("warning", "port_never_up", f"{name} on {host} is down and has never been up")
            problem = True
        elif is_oper_up(row.get("oper_status")) is False:
            add("critical", "port_down", f"{name} on {host} is down")
            problem = True
        resolved.append((ep, row))

    if annotation or problem or len(device_ends) != 2 or len(resolved) != 2:
        return issues
    (ep_a, row_a), (ep_b, row_b) = resolved
    a, b = ep_a["device_id"], ep_b["device_id"]
    if a == b:
        return issues
    na, nb = norm_iface(iface_label(row_a)), norm_iface(iface_label(row_b))
    seen_from = [(o, b, nb, ep_a, row_a) for o in evidence.get((a, na), [])] + \
                [(o, a, na, ep_b, row_b) for o in evidence.get((b, nb), [])]
    if not seen_from:
        add("info", "no_evidence",
            f"No LLDP/CDP evidence for {ep_a.get('hostname')} {iface_label(row_a)} ⇄ "
            f"{ep_b.get('hostname')} {iface_label(row_b)}")
        return issues

    def matches(o: Observation, peer: str, peer_if: str) -> bool:
        return o.peer_device == peer and (not o.peer_if or o.peer_if == peer_if)

    if any(o.fresh and matches(o, p, pi) for o, p, pi, _, _ in seen_from):
        return issues
    other = [(o, ep, row) for o, p, pi, ep, row in seen_from if o.fresh and not matches(o, p, pi)]
    if other:
        o, ep, row = other[0]
        add("warning", "contradicted",
            f"LLDP/CDP shows {ep.get('hostname')} {iface_label(row)} connected to "
            f"{o.peer_label}{(' ' + o.peer_if_label) if o.peer_if_label else ''}")
        return issues
    old = [o.last_seen_at for o, p, pi, _, _ in seen_from if matches(o, p, pi) and o.last_seen_at]
    if old:
        hours = (now - max(old)).total_seconds() / 3600
        add("warning", "stale_adjacency", f"{names}: LLDP/CDP last saw this cable {age_text(hours)} ago")
    else:
        add("info", "no_evidence", f"{names}: no current LLDP/CDP evidence for this cable")
    return issues
