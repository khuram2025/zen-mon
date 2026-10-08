"""Interface sections: 95th-percentile utilization, errors/discards/flaps,
traffic trend and volume, down and unused ports, interface inventory and
operational status changes.

Data:
- PG ``device_interfaces``: names, ifType, ifSpeed, configured circuit speed
  (migrate-033), admin/oper state, monitored flag, tags (migrate-079) and
  ``last_up_at`` (migrate-126; NULL = not seen up since tracking began).
- CH ``snmp_if_metrics`` (per poll, 30 days): in/out bit rates, CUMULATIVE
  error/discard/unicast counters and ifOperStatus (IF-MIB codes, 0 = not read).
- CH ``snmp_if_metrics_5m`` (90 days): average/peak rates per 5-minute slot.
  Its ``sum_*_errors``/``sum_*_discards`` add cumulative counters and are
  meaningless; ``snmp_if_metrics_1h`` is never populated. Neither is read.

Rules:
- Utilization divides by interface_speed's effective speed (configured
  circuit speed, then the NetFlow interface override, then SNMP). An unknown
  speed shows "—", never 0 %.
- Cumulative counters are never summed: error/discard increases use the
  reset-safe two-pass method shared with Link Utilization (cheap aggregates
  for every interface, a lagInFrame pass only for flagged ones).
- Every ClickHouse query filters ctx.device_ids and aggregates in ClickHouse
  (one row per interface or slot, never raw samples), so the sections stay
  cheap on large fleets.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import text

from app.services.interface_speed import (
    SOURCE_CONFIGURED,
    SOURCE_OVERRIDE,
    SOURCE_SNMP,
    effective_speed_source,
    effective_speed_sql,
)
from app.services.report_sections import (
    SectionCtx,
    _fmt_bps,
    _fmt_bytes,
    _fmt_pct,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

logger = logging.getLogger("zenplus.reports.interfaces")

RAW_TABLE = "zenplus.snmp_if_metrics"
ROLLUP_TABLE = "zenplus.snmp_if_metrics_5m"
RAW_DAYS = 30
ROLLUP_DAYS = 90
SLOT_SECONDS = 300
# TTL deletes are lazy and report windows are computed a moment before the
# query runs: a "last 30 days" window still reads per-poll data.
_EDGE = timedelta(hours=2)
ETHERNET_TYPES = [6, 117]  # ethernetCsmacd, gigabitEthernet

_UP = ("up", "1")
_ADMIN_DOWN = ("down", "2")

WAN_RULE = ("WAN links are interfaces tagged “wan”, given the NetFlow interface role WAN, "
            "or with a configured circuit speed.")
WAN_EMPTY = "No WAN links in scope. " + WAN_RULE
SPEED_RULE = ("Utilization divides by the effective speed: the configured circuit speed, else "
              "the NetFlow interface override, else SNMP ifSpeed.")

LINKS_OPTION = {
    "key": "links", "label": "Interfaces", "type": "select", "default": "all",
    "choices": [["all", "All monitored interfaces"], ["wan", "WAN links only"]],
    "help": WAN_RULE,
}

_IF_TYPES = {
    1: "Other", 6: "Ethernet", 18: "DS1/T1", 22: "Serial", 23: "PPP", 24: "Loopback",
    30: "DS3/T3", 32: "Frame Relay", 37: "ATM", 39: "SONET", 53: "Virtual", 62: "Fast Ethernet",
    71: "Wi-Fi (802.11)", 94: "ADSL", 97: "VDSL", 108: "PPP multilink", 117: "Gigabit Ethernet",
    131: "Tunnel", 135: "L2 VLAN", 136: "L3 VLAN", 150: "MPLS tunnel", 161: "Link aggregation",
    166: "MPLS", 209: "Bridge",
}
_OPER_CODES = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "not present",
               7: "down"}
_BANDS = ("Below 100 Mbps", "100–999 Mbps", "1–9.9 Gbps", "10–24.9 Gbps", "25–99.9 Gbps",
          "100 Gbps and above", "Unknown")
_STATES = ("Up", "Down", "Admin down", "Dormant", "Not present", "Unknown")
_BUCKET_WORDS = {"5 MINUTE": "5 minutes", "30 MINUTE": "30 minutes", "2 HOUR": "2 hours",
                 "1 DAY": "day"}


# ─── Helpers ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime) -> datetime:
    """ClickHouse stores naive UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _raw_covers(ctx: SectionCtx) -> bool:
    """Whether per-poll data (30 days) still reaches back to the window start."""
    return _naive(ctx.frm) >= _now() - timedelta(days=RAW_DAYS) - _EDGE


def _raw_window(ctx: SectionCtx) -> tuple[datetime, datetime, bool]:
    """(from, to, reaches_past_retention): the part of the window per-poll data covers."""
    frm, to = _naive(ctx.frm), _naive(ctx.to)
    floor = _now() - timedelta(days=RAW_DAYS)
    past = frm < floor - _EDGE
    if past and to > floor:
        frm = floor
    return frm, to, past


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_count(n: Any) -> str:
    return f"{_i(n):,}"


def _fmt_speed(bps: Any) -> str:
    v = _f(bps)
    if not v or v <= 0:
        return "—"
    for unit in ("bps", "Kbps", "Mbps", "Gbps", "Tbps"):
        if v < 1000 or unit == "Tbps":
            return f"{v:,.2f}".rstrip("0").rstrip(".") + f" {unit}"
        v /= 1000
    return "—"  # pragma: no cover


def _fmt_rate(v: Optional[float]) -> str:
    return "—" if v is None else _fmt_bps(v)


def _fmt_ppm(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _fmt_util(v: Optional[float]) -> str:
    return _fmt_pct(v, 1)


def _delta_subtitle(cur: float, prev: Optional[float]) -> str:
    if prev is None or prev <= 0:
        return "No data for the previous period"
    pct = (cur - prev) / prev * 100.0
    if abs(pct) < 0.05:
        return "Unchanged vs previous period"
    return f"{'▲' if pct > 0 else '▼'} {abs(pct):.1f}% vs previous period"


def _tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _node_label(hostname: Any, ip: Any) -> str:
    return str(hostname or ip or "—")


def _if_label(name: Any, alias: Any, descr: Any, idx: Any) -> str:
    base = str(name or descr or f"ifIndex {idx}").strip()
    alias = str(alias or "").strip()
    if alias and alias != base:
        if len(alias) > 40:
            alias = alias[:39] + "…"
        return f"{base} ({alias})"
    return base


def _short(label: str, limit: int = 48) -> str:
    return label if len(label) <= limit else label[:limit - 1] + "…"


def _util(bps: Optional[float], speed: Optional[int]) -> Optional[float]:
    if bps is None or not speed:
        return None
    return bps / speed * 100.0


def _pairs_sql(keys: Iterable[tuple[str, int]]) -> str:
    """Inline (uuid, ifindex) tuples. Both halves are re-typed from values
    ClickHouse itself returned (UUID() / int()), so nothing can be injected."""
    return ",".join(f"(toUUID('{UUID(str(d))}'),{int(i)})" for d, i in keys)


# ~60 bytes per inlined pair: batches stay far below ClickHouse's default
# 256 KiB max_query_size however many interfaces a large fleet flags.
PAIR_BATCH = 500


def _batches(keys: list[tuple[str, int]]) -> Iterable[list[tuple[str, int]]]:
    for i in range(0, len(keys), PAIR_BATCH):
        yield keys[i:i + PAIR_BATCH]


def _oper_word(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s.isdigit():
        return _OPER_CODES.get(int(s), "unknown")
    return {"lowerlayerdown": "down", "notpresent": "not present", "": "unknown"}.get(s, s)


def _admin_word(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    return {"up": "Enabled", "1": "Enabled", "down": "Disabled", "2": "Disabled",
            "testing": "Testing", "3": "Testing"}.get(s, "—")


def _state(admin: Any, oper: Any) -> str:
    a, o = str(admin or "").lower(), str(oper or "").lower()
    if a in _ADMIN_DOWN:
        return "Admin down"
    if o in _UP:
        return "Up"
    if o in ("down", "2", "lowerlayerdown", "7"):
        return "Down"
    if o in ("dormant", "5"):
        return "Dormant"
    if o in ("notpresent", "6"):
        return "Not present"
    return "Unknown"


def _band(speed: Any) -> str:
    s = _f(speed)
    if not s or s <= 0:
        return "Unknown"
    if s < 100e6:
        return _BANDS[0]
    if s < 1e9:
        return _BANDS[1]
    if s < 10e9:
        return _BANDS[2]
    if s < 25e9:
        return _BANDS[3]
    if s < 100e9:
        return _BANDS[4]
    return _BANDS[5]


def _if_type(t: Any) -> str:
    if t is None:
        return "Unknown"
    return _IF_TYPES.get(_i(t), f"ifType {_i(t)}")


def _truncated(title: str, shown: int, total: int, noun: str = "interfaces") -> str:
    return f"{title} (top {shown} of {total:,} {noun})" if total > shown else title


async def _overrides(ctx: SectionCtx, device_ids: list[str]) -> dict[tuple[str, int], tuple]:
    """NetFlow per-interface capacity overrides keyed by the owning device:
    an explicit exporter→device mapping wins over a management-IP match.
    Optional data: a missing table degrades to "no overrides"."""
    clause, params = ctx.pg_device_clause("COALESCE(m.device_id, d.id)")
    sql = text(
        "SELECT COALESCE(m.device_id, d.id)::text, o.ifindex, o.speed_in_bps, o.speed_out_bps, o.role "
        "FROM netflow_interface_overrides o "
        "LEFT JOIN netflow_exporter_devices m ON m.exporter_ip = o.exporter_ip "
        "LEFT JOIN devices d ON m.exporter_ip IS NULL AND host(d.ip_address) = host(o.exporter_ip) "
        "WHERE COALESCE(m.device_id, d.id) = ANY(CAST(:cand AS uuid[]))" + clause)
    try:
        nested = getattr(ctx.db, "begin_nested", None)
        if nested is not None:
            async with nested():
                rows = (await ctx.db.execute(sql, {"cand": device_ids, **params})).fetchall()
        else:
            rows = (await ctx.db.execute(sql, {"cand": device_ids, **params})).fetchall()
    except Exception:
        logger.debug("interface overrides unavailable", exc_info=True)
        return {}
    out: dict[tuple[str, int], tuple] = {}
    for r in rows:
        out.setdefault((str(r[0]), _i(r[1])), (r[2], r[3], r[4]))
    return out


async def _interfaces(ctx: SectionCtx, device_ids: Iterable[str]) -> dict[tuple[str, int], dict]:
    """Monitored interfaces of the given (already device-filtered) devices with
    display names and effective speeds per direction."""
    ids = sorted({str(d) for d in device_ids})
    if not ids:
        return {}
    clause, params = ctx.pg_device_clause("di.device_id")
    rows = (await ctx.db.execute(text(
        "SELECT di.device_id::text, di.if_index, d.hostname, host(d.ip_address), di.if_name, "
        "di.if_alias, di.if_descr, di.if_speed, di.configured_speed_bps, di.admin_status, "
        "di.oper_status, COALESCE(di.tags, '[]'::jsonb) "
        "FROM device_interfaces di JOIN devices d ON d.id = di.device_id "
        "WHERE di.monitored = TRUE AND di.device_id = ANY(CAST(:cand AS uuid[]))" + clause),
        {"cand": ids, **params})).fetchall()
    if not rows:
        return {}
    overrides = await _overrides(ctx, ids)
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        key = (str(r[0]), _i(r[1]))
        ov_in, ov_out, role = overrides.get(key, (None, None, None))
        speed_in, src_in = effective_speed_source(r[8], ov_in, r[7])
        speed_out, src_out = effective_speed_source(r[8], ov_out, r[7])
        tags = _tags(r[11])
        out[key] = {
            "node": _node_label(r[2], r[3]),
            "iface": _if_label(r[4], r[5], r[6], r[1]),
            "speed_in": speed_in, "speed_out": speed_out,
            "source": src_in if speed_in else src_out,
            "admin": str(r[9] or "").lower(), "oper": str(r[10] or "").lower(),
            "wan": (any(t.strip().lower() == "wan" for t in tags)
                    or str(role or "").lower() == "wan"
                    or (_f(r[8]) or 0) > 0),
        }
    return out


def _speed_cell(meta: dict) -> str:
    if meta["speed_in"] == meta["speed_out"]:
        return _fmt_speed(meta["speed_in"])
    return f"{_fmt_speed(meta['speed_in'])} / {_fmt_speed(meta['speed_out'])}"


def _label(meta: dict) -> str:
    return _short(f"{meta['node']} · {meta['iface']}")


def _links_filter(meta: Optional[dict], links: str) -> bool:
    return meta is not None and (links != "wan" or meta["wan"])


def _noun(links: str) -> str:
    return "WAN links" if links == "wan" else "interfaces"


def _cap(s: str) -> str:
    """First letter upper-case, the rest untouched ("WAN links" stays WAN)."""
    return s[:1].upper() + s[1:]


def _no_samples(links: str, stats: dict, noun: str, what: str = "traffic") -> str:
    if links == "wan" and stats:
        return WAN_EMPTY
    return f"No {what} samples for monitored {noun} in this period."


# ─── interface_utilization_p95 ──────────────────────────────────────────────

_DIRECTION_WORDS = {"max": "", "in": "inbound ", "out": "outbound "}


@register_section(
    "interface_utilization_p95", title="Top Interfaces by 95th-Percentile Utilization",
    category="Interfaces",
    description=("Interfaces ranked by 95th-percentile utilization of their effective speed "
                 "(configured circuit speed, NetFlow override or SNMP) over the period."),
    scope="device", requires=("devices.view",),
    options=[
        top_n_option(25, 200),
        {"key": "direction", "label": "Direction", "type": "select", "default": "max",
         "choices": [["max", "Busier direction"], ["in", "Inbound"], ["out", "Outbound"]]},
        {"key": "threshold", "label": "Warning threshold (%)", "type": "number", "default": 80,
         "min": 1, "max": 100},
        {"key": "only_above", "label": "Only interfaces at or above the threshold", "type": "bool",
         "default": False},
        LINKS_OPTION,
    ],
)
async def interface_utilization_p95(ctx: SectionCtx) -> dict:
    sid = "interface_utilization_p95"
    top_n, direction = int(ctx.opt("top_n")), ctx.opt("direction")
    threshold, only_above, links = float(ctx.opt("threshold")), bool(ctx.opt("only_above")), ctx.opt("links")
    noun = _noun(links)
    head = "WAN Links" if links == "wan" else "Interfaces"
    dir_word = {"in": " (Inbound)", "out": " (Outbound)"}.get(direction, "")
    if only_above:
        title = f"{head} at or above {threshold:g}% Utilization (95th Percentile){dir_word}"
    else:
        title = f"Top {top_n} {head} by 95th-Percentile Utilization{dir_word}"

    raw = _raw_covers(ctx)
    dev, dparams = ctx.ch_device_clause("device_id")
    params = {"f": _naive(ctx.frm), "t": _naive(ctx.to), **dparams}
    if raw:
        sql = (
            "SELECT device_id, if_index, avg(in_bps), avg(out_bps), "
            "quantile(0.95)(in_bps), quantile(0.95)(out_bps), max(in_bps), max(out_bps) "
            f"FROM {RAW_TABLE} WHERE timestamp >= %(f)s AND timestamp < %(t)s{dev} "
            "GROUP BY device_id, if_index")
    else:
        # One weighted average per 5-minute slot first (the rollup may hold
        # several rows per slot), then the percentile over the slots.
        sql = (
            "SELECT device_id, if_index, sum(s_in) / sum(n), sum(s_out) / sum(n), "
            "quantile(0.95)(b_in), quantile(0.95)(b_out), max(m_in), max(m_out) "
            "FROM (SELECT device_id, if_index, timestamp AS slot, "
            "sum(avg_in_bps * sample_count) AS s_in, sum(avg_out_bps * sample_count) AS s_out, "
            "sum(sample_count) AS n, s_in / n AS b_in, s_out / n AS b_out, "
            "max(max_in_bps) AS m_in, max(max_out_bps) AS m_out "
            f"FROM {ROLLUP_TABLE} WHERE timestamp >= %(f)s AND timestamp < %(t)s{dev} "
            "GROUP BY device_id, if_index, slot) "
            "GROUP BY device_id, if_index")
    rows = await ctx.ch_rows(sql, params)
    stats = {(str(r[0]), _i(r[1])): r for r in rows}
    inv = await _interfaces(ctx, (k[0] for k in stats))

    notes: list[str] = []
    method = ("95th percentile of every poll in the period." if raw else
              "95th percentile of 5-minute average rates (per-poll data is kept for 30 days; "
              "longer or older periods use the 5-minute rollup).")
    if not raw:
        n = ctx.window_note(ROLLUP_DAYS, "The 5-minute interface rollup")
        if n:
            notes.append(n)

    items = []
    for key, r in stats.items():
        meta = inv.get(key)
        if not _links_filter(meta, links):
            continue
        avg_in, avg_out, p_in, p_out = _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])
        peak = max(_f(r[6]) or 0.0, _f(r[7]) or 0.0)
        u_in, u_out = _util(p_in, meta["speed_in"]), _util(p_out, meta["speed_out"])
        a_in, a_out = _util(avg_in, meta["speed_in"]), _util(avg_out, meta["speed_out"])
        if direction == "in":
            u, a, p_bps = u_in, a_in, p_in
        elif direction == "out":
            u, a, p_bps = u_out, a_out, p_out
        else:
            known = [x for x in (u_in, u_out) if x is not None]
            u = max(known) if known else None
            known_a = [x for x in (a_in, a_out) if x is not None]
            a = max(known_a) if known_a else None
            p_bps = max(p_in or 0.0, p_out or 0.0)
        items.append({"meta": meta, "u": u, "a": a, "p_in": p_in, "p_out": p_out,
                      "p_bps": p_bps or 0.0, "peak": peak})

    if not items:
        return _section(sid, title, description=f"95th-percentile utilization of {noun} over the period.",
                        notes=[_no_samples(links, stats, noun)] + notes)

    items.sort(key=lambda x: (x["u"] is None, -(x["u"] or 0.0), -x["p_bps"]))
    measured = len(items)
    known = [x for x in items if x["u"] is not None]
    above = [x for x in known if x["u"] >= threshold]
    unknown = measured - len(known)
    listed = above if only_above else items
    shown = listed[:top_n]
    top = known[0] if known else None
    if top is None:
        top_accent = "neutral"
    elif top["u"] >= threshold:
        top_accent = "danger"
    elif top["u"] >= threshold * 0.75:
        top_accent = "warning"
    else:
        top_accent = "success"

    kpis = [
        {"label": f"{_cap(noun)} measured", "value": _fmt_count(measured), "accent": "primary",
         "subtitle": f"{len(known):,} with a known speed"},
        {"label": f"At or above {threshold:g}%", "value": _fmt_count(len(above)),
         "accent": "danger" if above else "success", "subtitle": "95th-percentile utilization"},
        {"label": "Highest 95th percentile", "value": _fmt_util(top["u"]) if top else "—",
         "accent": top_accent, **({"subtitle": _label(top["meta"])} if top else {})},
        {"label": "Speed unknown", "value": _fmt_count(unknown), "accent": "neutral",
         "subtitle": "Utilization not measured"},
    ]
    charts = []
    ranked = [x for x in shown if x["u"] is not None][:15]
    if ranked:
        charts.append({"title": f"95th-percentile {_DIRECTION_WORDS[direction]}utilization",
                       "series": {"kind": "rank", "unit": "%", "color": "primary",
                                  "points": [{"label": _label(x["meta"]), "value": round(x["u"], 2)}
                                             for x in ranked]}})
    table_rows = [[x["meta"]["node"], x["meta"]["iface"], _speed_cell(x["meta"]), _fmt_rate(x["p_in"]),
                   _fmt_rate(x["p_out"]), _fmt_rate(x["peak"]), _fmt_util(x["a"]), _fmt_util(x["u"])]
                  for x in shown]
    table_title = (f"{_cap(noun)} at or above {threshold:g}%" if only_above
                   else f"{_cap(noun)} by 95th-percentile utilization")
    tables = [{"title": _truncated(table_title, len(shown), len(listed), noun),
               "headers": ["Node", "Interface", "Speed", "95th In", "95th Out", "Peak", "Avg Util.",
                           "95th Util."],
               "styles": ["text", "text", "num", "num", "num", "num", "pct", "pct-bar"],
               "rows": table_rows}] if shown else []
    if only_above and not above:
        notes.insert(0, f"No {noun} reached {threshold:g}% 95th-percentile utilization in this period.")
    notes.append(method + " " + SPEED_RULE)
    sources = Counter(x["meta"]["source"] for x in items)
    used = [f"{sources[s]:,} {label}" for s, label in ((SOURCE_CONFIGURED, "configured"),
                                                       (SOURCE_OVERRIDE, "NetFlow override"),
                                                       (SOURCE_SNMP, "SNMP")) if sources.get(s)]
    if used:
        notes.append("Speeds used: " + ", ".join(used) + ".")
    if unknown:
        notes.append(f"{unknown:,} of the {noun} have no known speed; their utilization shows —.")
    if links == "wan":
        notes.append(WAN_RULE)
    return _section(
        sid, title,
        description=(f"{'Every' if only_above else f'Top {top_n}'} {noun}"
                     f"{f' at or above {threshold:g}%' if only_above else ''} by 95th-percentile "
                     f"{_DIRECTION_WORDS[direction]}utilization of their effective speed over the period."),
        kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── interface_health ───────────────────────────────────────────────────────

_COUNTERS = (("in_errors", "ie"), ("out_errors", "oe"), ("in_discards", "id"),
             ("out_discards", "od"), ("in_ucast_pkts", "ip"), ("out_ucast_pkts", "op"))
_HEALTH_RANK = {"critical": 0, "warning": 1, "ok": 2}
_HEALTH_SORTS = {
    "severity": lambda h: (_HEALTH_RANK.get(h["health"], 3), -(h["error_ppm"] or 0.0),
                           -(h["errors"] + h["discards"]), -h["flaps"]),
    "errors": lambda h: (-h["errors"], -h["discards"]),
    "discards": lambda h: (-h["discards"], -h["errors"]),
    "error_rate": lambda h: (h["error_ppm"] is None, -(h["error_ppm"] or 0.0), -h["errors"]),
    "flaps": lambda h: (-h["flaps"], -h["errors"]),
}


def _health_cheap_sql(where: str) -> str:
    """Pass 1: max-min deltas (exact unless a counter went backwards) plus the
    two flags that send an interface to the precise pass."""
    deltas = ", ".join(f"max({c}) - min({c})" for c, _ in _COUNTERS)
    reset = " OR ".join(f"argMin({c}, timestamp) != min({c})" for c, _ in _COUNTERS)
    return (f"SELECT device_id, if_index, {deltas}, uniqExact(oper_status) > 1, ({reset}), "
            f"min(timestamp), max(timestamp) FROM {RAW_TABLE} WHERE {where} "
            "GROUP BY device_id, if_index")


def _health_precise_sql(where: str, pairs: str) -> str:
    """Pass 2 (flagged interfaces only): sum of positive steps between
    consecutive polls, so a reset or wrap is never counted as new events."""
    lags = ", ".join(f"lagInFrame({c}) OVER w AS p_{a}" for c, a in _COUNTERS)
    deltas = ", ".join(f"sum(if(rn > 1, greatest(toInt64({c}) - toInt64(p_{a}), 0), 0))"
                       for c, a in _COUNTERS)
    cols = ", ".join(c for c, _ in _COUNTERS)
    return (f"SELECT device_id, if_index, {deltas}, countIf(rn > 1 AND oper_status != p_os) "
            f"FROM (SELECT device_id, if_index, timestamp, oper_status, {cols}, {lags}, "
            "lagInFrame(oper_status) OVER w AS p_os, row_number() OVER w AS rn "
            f"FROM {RAW_TABLE} WHERE {where} AND (device_id, if_index) IN ({pairs}) "
            "WINDOW w AS (PARTITION BY device_id, if_index ORDER BY timestamp "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) "
            "GROUP BY device_id, if_index")


@register_section(
    "interface_health", title="Interface Errors, Discards and Flaps", category="Interfaces",
    description=("Interfaces with input/output errors, discards or operational status flaps, judged "
                 "as a rate per million frames with the Link Utilization thresholds."),
    scope="device", requires=("devices.view",),
    options=[
        top_n_option(25, 200),
        {"key": "sort", "label": "Sort by", "type": "select", "default": "severity",
         "choices": [["severity", "Health"], ["errors", "Errors"], ["discards", "Discards"],
                     ["error_rate", "Error rate"], ["flaps", "Flaps"]]},
        LINKS_OPTION,
    ],
)
async def interface_health(ctx: SectionCtx) -> dict:
    from app.api.v1.link_utilization import _health_payload

    sid, title = "interface_health", "Interface Errors, Discards and Flaps"
    top_n, sort, links = int(ctx.opt("top_n")), ctx.opt("sort"), ctx.opt("links")
    noun = _noun(links)
    if links == "wan":
        title = "WAN Link Errors, Discards and Flaps"
    frm, to, past = _raw_window(ctx)
    dev, dparams = ctx.ch_device_clause("device_id")
    params = {"f": frm, "t": to, **dparams}
    where = f"timestamp >= %(f)s AND timestamp < %(t)s{dev}"
    notes: list[str] = []
    if past:
        n = ctx.window_note(RAW_DAYS, "Per-poll interface counter data")
        if n:
            notes.append(n)

    rows = await ctx.ch_rows(_health_cheap_sql(where), params)
    stats: dict[tuple[str, int], dict] = {}
    flagged: list[tuple[str, int]] = []
    for r in rows:
        key = (str(r[0]), _i(r[1]))
        d = {f"d_{a}": _i(r[2 + n]) for n, (_, a) in enumerate(_COUNTERS)}
        d["flaps"] = 0
        first, last = r[10], r[11]
        d["span"] = (last - first).total_seconds() if first and last else 0.0
        stats[key] = d
        if r[8] or r[9]:
            flagged.append(key)
    inv = await _interfaces(ctx, (k[0] for k in stats))
    flagged = [k for k in flagged if _links_filter(inv.get(k), links)]
    try:
        for batch in _batches(flagged):
            for r in await ctx.ch_rows(_health_precise_sql(where, _pairs_sql(batch)), params):
                d = stats.get((str(r[0]), _i(r[1])))
                if d is None:
                    continue
                d.update({f"d_{a}": _i(r[2 + n]) for n, (_, a) in enumerate(_COUNTERS)})
                d["flaps"] = _i(r[8])
    except Exception:
        # Pass-1 numbers are right for every interface without a counter
        # reset; flap counts stay 0 rather than becoming wrong.
        logger.exception("precise interface health pass failed")
        notes.append("Reset-safe recount unavailable for this run; interfaces whose counters "
                     "reset may show inflated totals.")

    items = []
    for key, d in stats.items():
        meta = inv.get(key)
        if not _links_filter(meta, links):
            continue
        h = _health_payload(d, d["span"])
        if meta["admin"] in _ADMIN_DOWN:
            # An administratively shut port isn't a fault.
            h = {**h, "health": "ok", "issues": []}
        items.append({**h, "meta": meta})

    description = (f"Errors, discards and operational status flaps on {noun} over the period "
                   "(per-poll counters, last 30 days at most).")
    if not items:
        if past and not stats and to <= _now() - timedelta(days=RAW_DAYS):
            empty = ("Per-poll interface counters are kept for 30 days and this period ended "
                     "before that, so errors and discards cannot be reported.")
        else:
            empty = _no_samples(links, stats, noun, "counter")
        return _section(sid, title, description=description, notes=[empty] + notes)

    affected = [h for h in items if h["errors"] or h["discards"] or h["flaps"]]
    affected.sort(key=_HEALTH_SORTS.get(sort, _HEALTH_SORTS["severity"]))
    shown = affected[:top_n]
    counts = Counter(h["health"] for h in items)
    with_err = sum(1 for h in items if h["errors"])
    with_disc = sum(1 for h in items if h["discards"])
    flapping = sum(1 for h in items if h["flaps"])
    kpis = [
        {"label": f"{_cap(noun)} analysed", "value": _fmt_count(len(items)), "accent": "primary"},
        {"label": "With errors", "value": _fmt_count(with_err), "accent": "warning" if with_err else "success",
         "subtitle": f"{sum(h['errors'] for h in items):,} errors in total"},
        {"label": "With discards", "value": _fmt_count(with_disc), "accent": "warning" if with_disc else "success",
         "subtitle": f"{sum(h['discards'] for h in items):,} discards in total"},
        {"label": "Flapping", "value": _fmt_count(flapping), "accent": "warning" if flapping else "success",
         "subtitle": "Status changed at least once"},
        {"label": "Critical", "value": _fmt_count(counts.get("critical", 0)),
         "accent": "danger" if counts.get("critical") else "success",
         "subtitle": f"{counts.get('warning', 0):,} warning"},
    ]
    charts = [{"title": f"{_cap(noun)} by health",
               "series": {"kind": "donut", "unit": noun,
                          "points": [{"label": lbl, "value": counts[k]}
                                     for k, lbl in (("ok", "Healthy"), ("warning", "Warning"),
                                                    ("critical", "Critical")) if counts.get(k)]}}]
    tables = [{"title": _truncated(f"{_cap(noun)} with errors, discards or flaps", len(shown),
                                   len(affected), noun),
               "headers": ["Node", "Interface", "Errors", "Error rate (ppm)", "Discards",
                           "Discard rate (ppm)", "Flaps", "Health"],
               "styles": ["text", "text", "num", "num", "num", "num", "num", "status"],
               "rows": [[h["meta"]["node"], h["meta"]["iface"], _fmt_count(h["errors"]),
                         _fmt_ppm(h["error_ppm"]), _fmt_count(h["discards"]), _fmt_ppm(h["discard_ppm"]),
                         _fmt_count(h["flaps"]), h["health"]] for h in shown]}] if shown else []
    if not affected:
        notes.insert(0, "No interface errors, discards or status flaps in this period.")
    notes.append("Errors and discards are increases of the SNMP counters between consecutive polls, so a "
                 "counter reset or wrap is never counted as new events. Rates are per million frames "
                 "(unicast plus errored and discarded); without packet counters, 1,000 events is the "
                 "warning floor.")
    notes.append("Thresholds match Link Utilization: errors from 100 ppm warning and 1,000 ppm critical; "
                 "discards from 1,000 ppm warning and 10,000 ppm critical; any flap is a warning and 5 or "
                 "more critical. Administratively down ports are not judged.")
    if links == "wan":
        notes.append(WAN_RULE)
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── interface_traffic_trend ────────────────────────────────────────────────

def _rollup_rates_sql(dev: str, extra: str = "") -> str:
    return ("SELECT device_id, if_index, "
            "sum(avg_in_bps * sample_count) / sum(sample_count), "
            "sum(avg_out_bps * sample_count) / sum(sample_count), "
            "max(max_in_bps), max(max_out_bps), "
            "argMax(timestamp, max_in_bps), argMax(timestamp, max_out_bps) "
            f"FROM {ROLLUP_TABLE} WHERE timestamp >= %(f)s AND timestamp < %(t)s{dev}{extra} "
            "GROUP BY device_id, if_index")


@register_section(
    "interface_traffic_trend", title="Interface Traffic Trend", category="Interfaces",
    description="Inbound and outbound traffic over time for the busiest interfaces of the period.",
    scope="device", requires=("devices.view",),
    options=[top_n_option(5, 10), LINKS_OPTION],
)
async def interface_traffic_trend(ctx: SectionCtx) -> dict:
    sid = "interface_traffic_trend"
    top_n, links = int(ctx.opt("top_n")), ctx.opt("links")
    noun = _noun(links)
    title = f"Traffic Trend of the {top_n} Busiest {'WAN Links' if links == 'wan' else 'Interfaces'}"
    description = (f"Average inbound and outbound rate over time for the {top_n} {noun} that carried the "
                   "most traffic in the period.")
    dev, dparams = ctx.ch_device_clause("device_id")
    params = {"f": _naive(ctx.frm), "t": _naive(ctx.to), **dparams}
    rows = await ctx.ch_rows(_rollup_rates_sql(dev), params)
    stats = {(str(r[0]), _i(r[1])): r for r in rows}
    inv = await _interfaces(ctx, (k[0] for k in stats))
    notes: list[str] = []
    n = ctx.window_note(ROLLUP_DAYS, "The 5-minute interface rollup")
    if n:
        notes.append(n)
    cand = [(k, r) for k, r in stats.items() if _links_filter(inv.get(k), links)]
    if not cand:
        return _section(sid, title, description=description, notes=[_no_samples(links, stats, noun)] + notes)
    cand.sort(key=lambda kr: -((_f(kr[1][2]) or 0.0) + (_f(kr[1][3]) or 0.0)))
    top = cand[:top_n]
    keys = [k for k, _ in top]

    bucket, bparams = ctx.ch_bucket("timestamp")
    series_rows = await ctx.ch_rows(
        f"SELECT device_id, if_index, {bucket} AS slot, "
        "sum(avg_in_bps * sample_count) / sum(sample_count), "
        "sum(avg_out_bps * sample_count) / sum(sample_count) "
        f"FROM {ROLLUP_TABLE} WHERE timestamp >= %(f)s AND timestamp < %(t)s{dev} "
        f"AND (device_id, if_index) IN ({_pairs_sql(keys)}) "
        "GROUP BY device_id, if_index, slot ORDER BY slot", {**params, **bparams})
    by_key: dict[tuple[str, int], dict[str, tuple]] = {k: {} for k in keys}
    slots: dict[str, Any] = {}
    for r in series_rows:
        key = (str(r[0]), _i(r[1]))
        t = ctx.iso(r[2])
        if key not in by_key or t is None:
            continue
        slots[t] = r[2]
        by_key[key][t] = (_f(r[3]), _f(r[4]))
    order = sorted(slots)

    def lines(idx: int) -> list[dict]:
        return [{"name": _label(inv[k]),
                 "points": [{"t": t, "v": (round(by_key[k][t][idx], 1) if t in by_key[k]
                                           and by_key[k][t][idx] is not None else None)} for t in order]}
                for k in keys]

    charts = []
    if order:
        charts = [{"title": "Inbound traffic", "series": {"kind": "line", "unit": "bps", "series": lines(0)}},
                  {"title": "Outbound traffic", "series": {"kind": "line", "unit": "bps", "series": lines(1)}}]
    peak_in = max(top, key=lambda kr: _f(kr[1][4]) or 0.0)
    peak_out = max(top, key=lambda kr: _f(kr[1][5]) or 0.0)
    combined = sum((_f(r[2]) or 0.0) + (_f(r[3]) or 0.0) for _, r in top)
    kpis = [
        {"label": f"{_cap(noun)} charted", "value": f"{len(top)} of {len(cand):,}", "accent": "primary",
         "subtitle": "Busiest by average traffic"},
        {"label": "Peak inbound", "value": _fmt_bps(_f(peak_in[1][4]) or 0.0), "accent": "info",
         "subtitle": f"{_label(inv[peak_in[0]])} · {ctx.fmt_short(peak_in[1][6])}"},
        {"label": "Peak outbound", "value": _fmt_bps(_f(peak_out[1][5]) or 0.0), "accent": "info",
         "subtitle": f"{_label(inv[peak_out[0]])} · {ctx.fmt_short(peak_out[1][7])}"},
        {"label": "Combined average", "value": _fmt_bps(combined), "accent": "neutral",
         "subtitle": "In + out of the charted interfaces"},
    ]
    tables = [{"title": _truncated(f"Busiest {noun}", len(top), len(cand), noun),
               "headers": ["Node", "Interface", "Speed", "Avg In", "Avg Out", "Peak In", "Peak Out"],
               "styles": ["text", "text", "num", "num", "num", "num", "num"],
               "rows": [[inv[k]["node"], inv[k]["iface"], _speed_cell(inv[k]), _fmt_rate(_f(r[2])),
                         _fmt_rate(_f(r[3])), _fmt_rate(_f(r[4])), _fmt_rate(_f(r[5]))] for k, r in top]}]
    notes.append(f"Lines show the average rate per {_BUCKET_WORDS.get(ctx.bucket(), ctx.bucket().lower())} "
                 "from the 5-minute interface rollup; gaps mean no samples. Peaks are the highest single poll.")
    if links == "wan":
        notes.append(WAN_RULE)
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── interface_volume ───────────────────────────────────────────────────────

def _volume_sql(dev: str) -> str:
    """Bytes per interface: each 5-minute slot's weighted average rate × 300 s
    ÷ 8, summed over the slots that have samples (count() = slots seen)."""
    return ("SELECT device_id, if_index, sum(b_in), sum(b_out), count() "
            "FROM (SELECT device_id, if_index, timestamp AS slot, "
            "sum(avg_in_bps * sample_count) / sum(sample_count) AS b_in, "
            "sum(avg_out_bps * sample_count) / sum(sample_count) AS b_out "
            f"FROM {ROLLUP_TABLE} WHERE timestamp >= %(f)s AND timestamp < %(t)s{dev} "
            "GROUP BY device_id, if_index, slot) "
            "GROUP BY device_id, if_index")


def _bytes(rate_sum: Any) -> float:
    return (_f(rate_sum) or 0.0) * SLOT_SECONDS / 8.0


@register_section(
    "interface_volume", title="Total Traffic by Interface", category="Interfaces",
    description="Data transferred (inbound, outbound and total) per interface and per node over the period.",
    scope="device", requires=("devices.view",),
    options=[top_n_option(25, 200), LINKS_OPTION],
)
async def interface_volume(ctx: SectionCtx) -> dict:
    sid = "interface_volume"
    top_n, links = int(ctx.opt("top_n")), ctx.opt("links")
    noun = _noun(links)
    title = "Total Traffic by WAN Link" if links == "wan" else "Total Traffic by Interface"
    description = f"Data transferred per {noun[:-1]} and per node over the period, busiest first."
    dev, dparams = ctx.ch_device_clause("device_id")
    frm, to = _naive(ctx.frm), _naive(ctx.to)
    rows = await ctx.ch_rows(_volume_sql(dev), {"f": frm, "t": to, **dparams})
    stats = {(str(r[0]), _i(r[1])): r for r in rows}
    inv = await _interfaces(ctx, (k[0] for k in stats))
    notes: list[str] = []
    n = ctx.window_note(ROLLUP_DAYS, "The 5-minute interface rollup")
    if n:
        notes.append(n)
    keys = [k for k in stats if _links_filter(inv.get(k), links)]
    if not keys:
        return _section(sid, title, description=description, notes=[_no_samples(links, stats, noun)] + notes)

    floor = _now() - timedelta(days=ROLLUP_DAYS)
    expected = max(1, int((to - max(frm, floor)).total_seconds() // SLOT_SECONDS))
    items = []
    for k in keys:
        r = stats[k]
        b_in, b_out = _bytes(r[2]), _bytes(r[3])
        items.append({"key": k, "meta": inv[k], "in": b_in, "out": b_out, "total": b_in + b_out,
                      "coverage": min(100.0, _i(r[4]) / expected * 100.0)})
    items.sort(key=lambda x: -x["total"])
    total_in = sum(x["in"] for x in items)
    total_out = sum(x["out"] for x in items)
    grand = total_in + total_out

    prev_total: Optional[float] = None
    pf, pt = (_naive(x) for x in ctx.prior_window())
    if pf >= floor - _EDGE:
        prev_rows = await ctx.ch_rows(_volume_sql(dev), {"f": pf, "t": pt, **dparams})
        kept = set(keys)
        prev = [r for r in prev_rows if (str(r[0]), _i(r[1])) in kept]
        prev_total = sum(_bytes(r[2]) + _bytes(r[3]) for r in prev) if prev else None

    nodes: dict[str, dict] = {}
    for x in items:
        g = nodes.setdefault(x["meta"]["node"], {"n": 0, "in": 0.0, "out": 0.0})
        g["n"] += 1
        g["in"] += x["in"]
        g["out"] += x["out"]
    node_rows = sorted(nodes.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))

    def share(v: float) -> str:
        return _fmt_pct(v / grand * 100.0, 1) if grand > 0 else "—"

    busiest_node = node_rows[0]
    kpis = [
        {"label": "Total transferred", "value": _fmt_bytes(grand), "accent": "primary",
         "subtitle": _delta_subtitle(grand, prev_total)},
        {"label": "Inbound", "value": _fmt_bytes(total_in), "accent": "info"},
        {"label": "Outbound", "value": _fmt_bytes(total_out), "accent": "info"},
        {"label": f"Busiest {noun[:-1]}", "value": _fmt_bytes(items[0]["total"]), "accent": "neutral",
         "subtitle": _label(items[0]["meta"])},
        {"label": "Busiest node", "value": _fmt_bytes(busiest_node[1]["in"] + busiest_node[1]["out"]),
         "accent": "neutral", "subtitle": busiest_node[0]},
    ]
    shown = items[:top_n]
    charts = [{"title": f"Data transferred — top {min(len(shown), 15)} {noun}",
               "series": {"kind": "rank", "unit": "B", "color": "primary",
                          "points": [{"label": _label(x["meta"]), "value": round(x["total"])}
                                     for x in shown[:15]]}}]
    tables = [
        {"title": _truncated(f"{_cap(noun)} by data transferred", len(shown), len(items), noun),
         "headers": ["Node", "Interface", "Inbound", "Outbound", "Total", "Share", "Coverage"],
         "styles": ["text", "text", "num", "num", "num", "pct-bar", "pct"],
         "rows": [[x["meta"]["node"], x["meta"]["iface"], _fmt_bytes(x["in"]), _fmt_bytes(x["out"]),
                   _fmt_bytes(x["total"]), share(x["total"]), _fmt_pct(x["coverage"], 1)] for x in shown]},
        {"title": _truncated("Data transferred by node", min(len(node_rows), top_n), len(node_rows), "nodes"),
         "headers": ["Node", _cap(noun), "Inbound", "Outbound", "Total", "Share"],
         "styles": ["text", "num", "num", "num", "num", "pct-bar"],
         "rows": [[name, _fmt_count(g["n"]), _fmt_bytes(g["in"]), _fmt_bytes(g["out"]),
                   _fmt_bytes(g["in"] + g["out"]), share(g["in"] + g["out"])]
                  for name, g in node_rows[:top_n]]},
    ]
    notes.append("Volumes are estimated from 5-minute average rates (rate × 300 s ÷ 8); 5-minute slots "
                 "without samples are not counted (see Coverage).")
    notes.append("Node totals add every monitored interface on the node, so traffic that crosses a node is "
                 "counted on each interface it uses.")
    if links == "wan":
        notes.append(WAN_RULE)
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── interface_down_ports ───────────────────────────────────────────────────

@register_section(
    "interface_down_ports", title="Unused and Down Ports", category="Interfaces",
    description=("Switch ports that have not been operationally up for at least the chosen number of days, "
                 "as of report generation."),
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[
        top_n_option(100, 1000),
        {"key": "days", "label": "Not up for at least (days)", "type": "int", "default": 30,
         "min": 1, "max": 365},
        {"key": "include_disabled", "label": "Include administratively down ports", "type": "bool",
         "default": True},
        {"key": "port_types", "label": "Ports", "type": "select", "default": "ethernet",
         "choices": [["ethernet", "Ethernet ports"], ["all", "All interfaces"]]},
        {"key": "sort", "label": "Sort by", "type": "select", "default": "idle",
         "choices": [["idle", "Longest idle first"], ["node", "Node and port"]]},
    ],
)
async def interface_down_ports(ctx: SectionCtx) -> dict:
    sid = "interface_down_ports"
    top_n, days = int(ctx.opt("top_n")), int(ctx.opt("days"))
    include_disabled, ethernet = bool(ctx.opt("include_disabled")), ctx.opt("port_types") == "ethernet"
    port_word = "Ethernet ports" if ethernet else "interfaces"
    title = f"Ports Not Up for {days}+ Days" if ethernet else f"Interfaces Not Up for {days}+ Days"
    description = (f"{port_word.capitalize()} not operationally up for at least {days} days, as of report "
                   "generation.")
    now = datetime.now(timezone.utc)
    clause, params = ctx.pg_device_clause("di.device_id")
    params = {**params, "cutoff": now - timedelta(days=days)}
    types = ""
    if ethernet:
        types = " AND di.if_type = ANY(:types)"
        params["types"] = ETHERNET_TYPES
    unused = ("lower(COALESCE(di.oper_status, '')) NOT IN ('up', '1') "
              "AND (di.last_up_at < :cutoff OR (di.last_up_at IS NULL AND di.first_seen < :cutoff))")
    if not include_disabled:
        unused += " AND lower(COALESCE(di.admin_status, '')) NOT IN ('down', '2')"
    per_node = (await ctx.db.execute(text(
        "SELECT d.hostname, host(d.ip_address), count(*), "
        "count(*) FILTER (WHERE lower(COALESCE(di.oper_status, '')) IN ('up', '1')), "
        f"count(*) FILTER (WHERE {unused}), "
        f"count(*) FILTER (WHERE {unused} AND di.last_up_at IS NULL), "
        f"count(*) FILTER (WHERE {unused} AND lower(COALESCE(di.admin_status, '')) IN ('down', '2')) "
        "FROM device_interfaces di JOIN devices d ON d.id = di.device_id "
        f"WHERE TRUE{types}{clause} GROUP BY d.id, d.hostname, d.ip_address"), params)).fetchall()
    if not per_node:
        return _section(sid, title, description=description,
                        notes=[f"No {port_word} on record for the devices in scope."])
    ports = sum(_i(r[2]) for r in per_node)
    up = sum(_i(r[3]) for r in per_node)
    idle = sum(_i(r[4]) for r in per_node)
    never = sum(_i(r[5]) for r in per_node)
    disabled = sum(_i(r[6]) for r in per_node)
    kpis = [
        {"label": port_word.capitalize(), "value": _fmt_count(ports), "accent": "primary",
         "subtitle": f"{up:,} up now"},
        {"label": f"Not up for {days}+ days", "value": _fmt_count(idle), "accent": "warning" if idle else "success",
         "subtitle": f"{idle / ports * 100:.1f}% of {port_word}" if ports else "—"},
        {"label": "Never seen up", "value": _fmt_count(never), "accent": "neutral",
         "subtitle": "Since port tracking began"},
        {"label": "Administratively down", "value": _fmt_count(disabled), "accent": "neutral",
         "subtitle": "Of the idle ports" if include_disabled else "Excluded"},
    ]
    notes = ["Status is as of each port's last SNMP poll. “Last up” is when the poller last saw the port "
             "operationally up; a port never seen up is listed only when it was discovered at least "
             f"{days} days ago, and its idle time is a minimum."]
    if ethernet:
        notes.append("Ethernet ports are ifType 6 (ethernetCsmacd) and 117 (gigabitEthernet).")
    if not include_disabled:
        notes.append("Administratively down ports are excluded.")
    if not idle:
        notes.insert(0, f"No {port_word} have been down or unused for {days} days or more.")
        return _section(sid, title, description=description, kpis=kpis, notes=notes)

    order = ("COALESCE(di.last_up_at, di.first_seen) ASC, d.hostname, di.if_index"
             if ctx.opt("sort") == "idle" else "d.hostname, di.if_index")
    detail = (await ctx.db.execute(text(
        "SELECT d.hostname, host(d.ip_address), di.if_index, di.if_name, di.if_alias, di.if_descr, "
        "di.admin_status, di.oper_status, di.last_up_at, di.first_seen "
        "FROM device_interfaces di JOIN devices d ON d.id = di.device_id "
        f"WHERE {unused}{types}{clause} ORDER BY {order} LIMIT :lim"), {**params, "lim": top_n})).fetchall()
    rows = []
    for r in detail:
        last_up, first = _aware(r[8]), _aware(r[9])
        if last_up is not None:
            idle_days = f"{max(0, (now - last_up).days):,}"
        elif first is not None:
            idle_days = f"≥ {max(0, (now - first).days):,}"
        else:
            idle_days = "—"
        rows.append([_node_label(r[0], r[1]), _if_label(r[3], None, r[5], r[2]), str(r[4] or "").strip() or "—",
                     _admin_word(r[6]), _oper_word(r[7]),
                     ctx.fmt_date(last_up) if last_up else "Never seen up", idle_days])
    nodes = sorted(((_node_label(r[0], r[1]), r) for r in per_node if _i(r[4])),
                   key=lambda nr: (-_i(nr[1][4]), nr[0]))
    charts = [{"title": f"Idle {port_word} by node",
               "series": {"kind": "rank", "unit": "ports", "color": "warning",
                          "points": [{"label": name, "value": _i(r[4])} for name, r in nodes[:15]]}}]
    tables = [
        {"title": _truncated(f"{port_word.capitalize()} not up for {days}+ days", len(rows), idle, "ports"),
         "headers": ["Node", "Port", "Description", "Admin", "Oper", "Last up", "Days not up"],
         "styles": ["text", "mono", "text", "text", "status", "text", "num"], "rows": rows},
        {"title": _truncated("Idle ports by node", min(len(nodes), top_n), len(nodes), "nodes"),
         "headers": ["Node", port_word.capitalize(), "Up now", f"Not up {days}+ days", "Never seen up",
                     "Idle share"],
         "styles": ["text", "num", "num", "num", "num", "pct-bar"],
         "rows": [[name, _fmt_count(r[2]), _fmt_count(r[3]), _fmt_count(r[4]), _fmt_count(r[5]),
                   _fmt_pct(_i(r[4]) / _i(r[2]) * 100.0, 1) if _i(r[2]) else "—"]
                  for name, r in nodes[:top_n]]},
    ]
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── interface_inventory ────────────────────────────────────────────────────

@register_section(
    "interface_inventory", title="Interface Inventory", category="Interfaces",
    description=("Interfaces on record by type, speed, status, tag and node, as of report generation."),
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[
        top_n_option(25, 200),
        {"key": "breakdown", "label": "Breakdown", "type": "select", "default": "all",
         "choices": [["all", "All breakdowns"], ["type", "Type"], ["speed", "Speed"], ["state", "Status"],
                     ["tag", "Tag"], ["node", "Node"]]},
        {"key": "monitored_only", "label": "Monitored interfaces only", "type": "bool", "default": False},
    ],
)
async def interface_inventory(ctx: SectionCtx) -> dict:
    sid, title = "interface_inventory", "Interface Inventory"
    top_n, breakdown, monitored_only = int(ctx.opt("top_n")), ctx.opt("breakdown"), bool(ctx.opt("monitored_only"))
    if breakdown != "all":
        title = f"Interfaces by {dict(type='Type', speed='Speed', state='Status', tag='Tag', node='Node')[breakdown]}"
    description = ("Interfaces on record by type, speed, status, tag and node, as of report generation."
                   if breakdown == "all" else f"{title} as of report generation.")
    clause, params = ctx.pg_device_clause("di.device_id")
    mon = " AND di.monitored = TRUE" if monitored_only else ""
    speed = effective_speed_sql("di.configured_speed_bps", None, "di.if_speed")
    up_sql = "lower(COALESCE(di.oper_status, '')) IN ('up', '1')"
    admin_down_sql = "lower(COALESCE(di.admin_status, '')) IN ('down', '2')"
    grouped = (await ctx.db.execute(text(
        f"SELECT di.if_type, {speed}, lower(COALESCE(di.admin_status, '')), "
        "lower(COALESCE(di.oper_status, '')), di.monitored, count(*) "
        f"FROM device_interfaces di WHERE TRUE{clause}{mon} GROUP BY 1, 2, 3, 4, 5"), params)).fetchall()
    if not grouped:
        return _section(sid, title, description=description,
                        notes=["No interfaces on record for the devices in scope."])

    total = monitored = up = admin_down = unknown_speed = 0
    by_type: dict[str, Counter] = {}
    by_band: dict[str, Counter] = {}
    by_state: Counter = Counter()
    for r in grouped:
        n = _i(r[5])
        st = _state(r[2], r[3])
        total += n
        monitored += n if r[4] else 0
        up += n if st == "Up" else 0
        admin_down += n if st == "Admin down" else 0
        band = _band(r[1])
        unknown_speed += n if band == "Unknown" else 0
        by_state[st] += n
        for bucket, key in ((by_type, _if_type(r[0])), (by_band, band)):
            c = bucket.setdefault(key, Counter())
            c["n"] += n
            c[st] += n
            c["mon"] += n if r[4] else 0

    node_rows = (await ctx.db.execute(text(
        f"SELECT d.hostname, host(d.ip_address), count(*), count(*) FILTER (WHERE {up_sql}), "
        f"count(*) FILTER (WHERE {admin_down_sql}), count(*) FILTER (WHERE di.monitored) "
        "FROM device_interfaces di JOIN devices d ON d.id = di.device_id "
        f"WHERE TRUE{clause}{mon} GROUP BY d.id, d.hostname, d.ip_address "
        "ORDER BY count(*) DESC, d.hostname"), params)).fetchall()

    def share(n: int) -> str:
        return f"{n / total * 100:.1f}% of {total:,}" if total else "—"

    kpis = [
        {"label": "Interfaces", "value": _fmt_count(total), "accent": "primary",
         "subtitle": f"on {len(node_rows):,} node(s)"},
        {"label": "Monitored", "value": _fmt_count(monitored), "accent": "info", "subtitle": share(monitored)},
        {"label": "Up", "value": _fmt_count(up), "accent": "success", "subtitle": share(up)},
        {"label": "Admin down", "value": _fmt_count(admin_down), "accent": "neutral", "subtitle": share(admin_down)},
        {"label": "Speed unknown", "value": _fmt_count(unknown_speed), "accent": "neutral",
         "subtitle": "No configured or SNMP speed"},
    ]
    charts: list[dict] = []
    tables: list[dict] = []
    want = (lambda b: breakdown in ("all", b))
    types_sorted = sorted(by_type.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    if want("type"):
        head, rest = types_sorted[:7], sum(c["n"] for _, c in types_sorted[7:])
        charts.append({"title": "Interfaces by type",
                       "series": {"kind": "donut", "unit": "interfaces",
                                  "points": [{"label": k, "value": c["n"]} for k, c in head]
                                  + ([{"label": "Other types", "value": rest}] if rest else [])}})
        tables.append({"title": "Interfaces by type",
                       "headers": ["Type", "Interfaces", "Up", "Down", "Admin down", "Monitored"],
                       "styles": ["text", "num", "num", "num", "num", "num"],
                       "rows": [[k, _fmt_count(c["n"]), _fmt_count(c["Up"]), _fmt_count(c["Down"]),
                                 _fmt_count(c["Admin down"]), _fmt_count(c["mon"])] for k, c in types_sorted]})
    if want("state"):
        charts.append({"title": "Interfaces by status",
                       "series": {"kind": "donut", "unit": "interfaces",
                                  "points": [{"label": s, "value": by_state[s]} for s in _STATES if by_state[s]]}})
        if breakdown == "state":
            tables.append({"title": "Interfaces by status", "headers": ["Status", "Interfaces", "Share"],
                           "styles": ["text", "num", "pct-bar"],
                           "rows": [[s, _fmt_count(by_state[s]), _fmt_pct(by_state[s] / total * 100.0, 1)]
                                    for s in _STATES if by_state[s]]})
    if want("speed"):
        bands = [(b, by_band[b]) for b in _BANDS if b in by_band]
        if breakdown == "speed":
            charts.append({"title": "Interfaces by speed",
                           "series": {"kind": "rank", "unit": "interfaces", "color": "primary",
                                      "points": [{"label": b, "value": c["n"]} for b, c in bands]}})
        tables.append({"title": "Interfaces by speed", "headers": ["Speed", "Interfaces", "Up", "Monitored"],
                       "styles": ["text", "num", "num", "num"],
                       "rows": [[b, _fmt_count(c["n"]), _fmt_count(c["Up"]), _fmt_count(c["mon"])]
                                for b, c in bands]})
    tag_rows: list = []
    if want("tag"):
        tag_rows = (await ctx.db.execute(text(
            f"SELECT t.tag, count(*), count(*) FILTER (WHERE {up_sql}), count(*) FILTER (WHERE di.monitored), "
            f"sum({speed}) "
            "FROM device_interfaces di CROSS JOIN LATERAL jsonb_array_elements_text("
            "CASE WHEN jsonb_typeof(di.tags) = 'array' THEN di.tags ELSE '[]'::jsonb END) AS t(tag) "
            f"WHERE TRUE{clause}{mon} GROUP BY t.tag ORDER BY count(*) DESC, t.tag"), params)).fetchall()
        if tag_rows:
            shown = tag_rows[:top_n]
            tables.append({"title": _truncated("Interfaces by tag", len(shown), len(tag_rows), "tags"),
                           "headers": ["Tag", "Interfaces", "Up", "Monitored", "Total capacity"],
                           "styles": ["text", "num", "num", "num", "num"],
                           "rows": [[str(r[0]), _fmt_count(r[1]), _fmt_count(r[2]), _fmt_count(r[3]),
                                     _fmt_speed(r[4])] for r in shown]})
    if want("node") and node_rows:
        shown = node_rows[:top_n]
        tables.append({"title": _truncated("Interfaces by node", len(shown), len(node_rows), "nodes"),
                       "headers": ["Node", "Interfaces", "Up", "Admin down", "Monitored"],
                       "styles": ["text", "num", "num", "num", "num"],
                       "rows": [[_node_label(r[0], r[1]), _fmt_count(r[2]), _fmt_count(r[3]), _fmt_count(r[4]),
                                 _fmt_count(r[5])] for r in shown]})
    notes = ["Speed is the configured circuit speed, else SNMP ifSpeed. Status is as of each interface's "
             "last SNMP poll."]
    if want("tag") and not tag_rows:
        notes.append("No interfaces carry tags yet; tag links on Link Utilization to group them here.")
    if monitored_only:
        notes.append("Only monitored interfaces are counted.")
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── interface_status_changes ───────────────────────────────────────────────

@register_section(
    "interface_status_changes", title="Interface Status Changes", category="Interfaces",
    description="Operational status changes (up/down transitions) per interface over the period.",
    scope="device", requires=("devices.view",),
    options=[
        top_n_option(25, 200),
        {"key": "min_changes", "label": "At least this many changes", "type": "int", "default": 1,
         "min": 1, "max": 1000},
        LINKS_OPTION,
    ],
)
async def interface_status_changes(ctx: SectionCtx) -> dict:
    sid, title = "interface_status_changes", "Interface Status Changes"
    top_n, min_changes, links = int(ctx.opt("top_n")), int(ctx.opt("min_changes")), ctx.opt("links")
    noun = _noun(links)
    if links == "wan":
        title = "WAN Link Status Changes"
    description = (f"Operational status changes of {noun} over the period (per-poll data, last 30 days "
                   "at most)" + (f", {min_changes} or more changes." if min_changes > 1 else "."))
    frm, to, past = _raw_window(ctx)
    dev, dparams = ctx.ch_device_clause("device_id")
    params = {"f": frm, "t": to, **dparams}
    where = f"timestamp >= %(f)s AND timestamp < %(t)s{dev}"
    notes: list[str] = []
    if past:
        n = ctx.window_note(RAW_DAYS, "Per-poll interface status data")
        if n:
            notes.append(n)
    # Pass 1 (cheap): which interfaces saw more than one known state at all.
    rows = await ctx.ch_rows(
        "SELECT device_id, if_index, uniqExactIf(oper_status, oper_status != 0) "
        f"FROM {RAW_TABLE} WHERE {where} GROUP BY device_id, if_index", params)
    states = {(str(r[0]), _i(r[1])): _i(r[2]) for r in rows}
    inv = await _interfaces(ctx, (k[0] for k in states))
    measured = [k for k in states if _links_filter(inv.get(k), links)]
    if not measured:
        if past and not states and to <= _now() - timedelta(days=RAW_DAYS):
            empty = ("Per-poll interface status is kept for 30 days and this period ended before that, "
                     "so status changes cannot be reported.")
        else:
            empty = _no_samples(links, states, noun, "status")
        return _section(sid, title, description=description, notes=[empty] + notes)

    cand = [k for k in measured if states[k] > 1]
    changed: list[dict] = []
    buckets: Counter = Counter()
    bucket, bparams = ctx.ch_bucket("timestamp")
    precise: list = []
    for batch in _batches(cand):
        # Pass 2 (changed interfaces only): transitions between consecutive
        # polls that read a status (0 = not read is skipped).
        precise += await ctx.ch_rows(
            "SELECT device_id, if_index, "
            "countIf(rn > 1 AND oper_status != p_os), "
            "countIf(rn > 1 AND p_os = 1 AND oper_status != 1), "
            "countIf(rn > 1 AND p_os != 1 AND oper_status = 1), "
            "maxIf(timestamp, rn > 1 AND oper_status != p_os), "
            "countIf(oper_status = 1) / count(), "
            "argMax(oper_status, timestamp), "
            f"groupArrayIf({bucket}, rn > 1 AND oper_status != p_os) "
            "FROM (SELECT device_id, if_index, timestamp, oper_status, "
            "lagInFrame(oper_status) OVER w AS p_os, row_number() OVER w AS rn "
            f"FROM {RAW_TABLE} WHERE {where} AND oper_status != 0 "
            f"AND (device_id, if_index) IN ({_pairs_sql(batch)}) "
            "WINDOW w AS (PARTITION BY device_id, if_index ORDER BY timestamp "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) "
            "GROUP BY device_id, if_index", {**params, **bparams})
    for r in precise:
        key = (str(r[0]), _i(r[1]))
        meta = inv.get(key)
        if meta is None or _i(r[2]) < min_changes:
            continue
        last = r[5] if r[5] is not None and getattr(r[5], "year", 1970) > 1970 else None
        changed.append({"meta": meta, "changes": _i(r[2]), "down": _i(r[3]), "up": _i(r[4]),
                        "last": last, "up_share": _f(r[6]), "now": _OPER_CODES.get(_i(r[7]), "unknown")})
        for b in r[8] or ():
            t = ctx.iso(b)
            if t:
                buckets[t] += 1

    changed.sort(key=lambda c: (-c["changes"], -c["down"], c["meta"]["node"], c["meta"]["iface"]))
    total_changes = sum(c["changes"] for c in changed)
    went_down = sum(c["down"] for c in changed)
    top = changed[0] if changed else None
    kpis = [
        {"label": f"{_cap(noun)} with changes", "value": _fmt_count(len(changed)),
         "accent": "warning" if changed else "success", "subtitle": f"of {len(measured):,} measured"},
        {"label": "Status changes", "value": _fmt_count(total_changes), "accent": "primary"},
        {"label": "Went down", "value": _fmt_count(went_down), "accent": "danger" if went_down else "success",
         "subtitle": "Up → not up transitions"},
        {"label": "Most changes", "value": _fmt_count(top["changes"]) if top else "—", "accent": "neutral",
         **({"subtitle": _label(top["meta"])} if top else {})},
    ]
    notes.append("A change is a different ifOperStatus between consecutive polls that read a status. "
                 "“Time up” is the share of those polls that found the interface up.")
    if links == "wan":
        notes.append(WAN_RULE)
    if not changed:
        notes.insert(0, f"No {noun} changed operational status"
                        f"{f' {min_changes} or more times' if min_changes > 1 else ''} in this period.")
        return _section(sid, title, description=description, kpis=kpis, notes=notes)
    shown = changed[:top_n]
    charts = [{"title": f"Status changes per {_BUCKET_WORDS.get(ctx.bucket(), 'period')}",
               "series": {"kind": "bars", "unit": "changes", "color": "warning",
                          "points": [{"t": t, "v": buckets[t]} for t in sorted(buckets)]}}] if buckets else []
    tables = [{"title": _truncated(f"{_cap(noun)} by status changes", len(shown), len(changed), noun),
               "headers": ["Node", "Interface", "Changes", "Went down", "Came up", "Last change", "Time up",
                           "Status now"],
               "styles": ["text", "text", "num", "num", "num", "text", "pct", "status"],
               "rows": [[c["meta"]["node"], c["meta"]["iface"], _fmt_count(c["changes"]), _fmt_count(c["down"]),
                         _fmt_count(c["up"]), ctx.fmt_short(c["last"]),
                         _fmt_pct(c["up_share"] * 100.0 if c["up_share"] is not None else None),
                         c["now"]] for c in shown]}]
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=notes)


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "top_interfaces_utilization_p95", "Top 25 Interfaces by Utilization (95th Percentile)",
    "The 25 most utilized interfaces by 95th-percentile rate against their effective speed, with the "
    "traffic trend of the five busiest.",
    "interfaces",
    [S("interface_utilization_p95", None, top_n=25, direction="max", threshold=80),
     S("interface_traffic_trend", None, top_n=5)],
    audience=("Network engineer", "Capacity planner"),
    tags=("utilization", "95th percentile", "bandwidth", "top n"),
    default_period="last_7d", frequency="weekly", icon="Gauge", featured=True,
)
template(
    "interfaces_over_threshold", "Over-utilized Interfaces (above 80%)",
    "Every interface whose 95th-percentile utilization reached 80% of its effective speed, for headroom "
    "reviews and upgrade planning.",
    "capacity",
    [S("interface_utilization_p95", "Interfaces at or above 80% (95th Percentile)", top_n=100,
       threshold=80, only_above=True)],
    audience=("Capacity planner", "Network engineer"),
    tags=("utilization", "capacity", "headroom", "95th percentile"),
    default_period="last_30d", frequency="weekly", icon="TrendingUp",
)
template(
    "interface_errors_discards", "Interface Errors and Discards",
    "Interfaces with input/output errors, discards or flapping, ranked by health with error and discard "
    "rates per million frames, plus the interfaces that changed state most often.",
    "interfaces",
    [S("interface_health", None, top_n=25, sort="severity"),
     S("interface_status_changes", "Most Unstable Interfaces", top_n=10)],
    audience=("Network engineer", "NOC operator"),
    tags=("errors", "discards", "flapping", "health"),
    default_period="last_7d", frequency="weekly", icon="Activity", featured=True,
)
template(
    "interface_traffic_trend_report", "Interface Traffic Trend",
    "Inbound and outbound traffic over time for the ten busiest interfaces, with average and peak rates.",
    "interfaces",
    [S("interface_traffic_trend", None, top_n=10),
     S("interface_utilization_p95", "Top 10 Interfaces by Utilization (95th Percentile)", top_n=10)],
    audience=("Network engineer", "IT manager"),
    tags=("traffic", "bandwidth", "trend"),
    default_period="last_7d", frequency="weekly", icon="TrendingUp",
)
template(
    "interface_total_traffic", "Total Traffic by Interface",
    "Data transferred per interface and per node over the period, with the change against the previous "
    "period — the basis for chargeback and transit reviews.",
    "interfaces",
    [S("interface_volume", None, top_n=25)],
    audience=("Network engineer", "IT manager", "Capacity planner"),
    tags=("volume", "bytes transferred", "chargeback"),
    default_period="previous_month", frequency="monthly", icon="BarChart3",
)
template(
    "unused_switch_ports", "Unused and Down Switch Ports",
    "Switch ports not up for 30 days or more — candidates to reclaim before buying new switches — with "
    "port usage per switch.",
    "interfaces",
    [S("interface_down_ports", "Ports Not Up for 30+ Days", days=30, top_n=200),
     S("interface_inventory", "Ports by Node", breakdown="node", top_n=50)],
    audience=("Network engineer", "Capacity planner"),
    tags=("unused ports", "switch ports", "port capacity"),
    default_period="last_30d", frequency="monthly", icon="Network",
)
template(
    "interface_inventory_report", "Interface Inventory",
    "Every interface on record by type, speed band, status, tag and node.",
    "interfaces",
    [S("interface_inventory", None, breakdown="all", top_n=50)],
    audience=("IT manager", "Network engineer"),
    tags=("inventory", "interfaces", "ports"),
    default_period="last_30d", frequency="monthly", icon="Boxes",
)
template(
    "interface_status_changes_report", "Interface Status Changes",
    "Interfaces that went down or flapped in the period, with when they last changed and how much of the "
    "period they were up, plus ports that have stayed down for a week or more.",
    "interfaces",
    [S("interface_status_changes", None, top_n=25),
     S("interface_down_ports", "Ports Down for 7+ Days", days=7, top_n=50, include_disabled=False)],
    audience=("NOC operator", "Network engineer"),
    tags=("flapping", "port status", "link down"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)
template(
    "wan_link_capacity_review", "WAN Link Capacity Review",
    "Monthly review of WAN circuits: 95th-percentile utilization against the contracted speed, traffic "
    "trend, data transferred and link errors.",
    "capacity",
    [S("interface_utilization_p95", "WAN Links by 95th-Percentile Utilization", links="wan", top_n=50,
       threshold=70),
     S("interface_traffic_trend", "WAN Traffic Trend", links="wan", top_n=5),
     S("interface_volume", "Data Transferred per WAN Link", links="wan", top_n=25),
     S("interface_health", "WAN Link Errors and Discards", links="wan", top_n=25)],
    audience=("Capacity planner", "IT manager", "Network engineer"),
    tags=("wan", "circuits", "capacity", "95th percentile"),
    default_period="previous_month", frequency="monthly", icon="Route", featured=True,
)
