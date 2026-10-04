"""NetFlow SQL building blocks: address expressions, table capabilities and the
raw / rollup / hybrid source planner (contract §3.2, §8, NF-17).

Addresses (§3.2). IPv4 and IPv6 flows share one representation: the unified
expressions below map IPv4 columns into IPv6 space (``::ffff:a.b.c.d``), so a
predicate, GROUP BY or IN-list works for both families. IP parameters are bound
as IPv6 text (``ip_param``); results are shown with ``display()``, which strips
the ``::ffff:`` prefix again. Columns are never ``toString()``-ed inside
WHERE/GROUP BY.

Tables. ``TABLES`` describes what each flow table can answer: which expressions
exist there and which filter keys (§8) it supports. The filter compiler and the
planner consult it, so a filter the rollup cannot evaluate always routes the
query to raw records instead of being silently ignored.

Planner (NF-17). Hour rollups only hold whole buckets, so a window that does not
start and end on a bucket boundary used to over-count up to an hour at each end.
``plan_source`` splits a window into rollup buckets for the complete interior and
raw records for the partial edges, all half-open. Parts that fall outside the
retention of the table that would serve them are left out, and the plan reports
the effective bounds and why (``meta.window.effective_from/to`` + warnings).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .common import (
    CONVERSATIONS_1H,
    EXPORTER_MINUTES,
    INTERFACE_5M,
    QOS_1H,
    RAW,
    TRAFFIC_5M,
    ROLLUP_MIN_SECONDS,
    QueryMeta,
    Retention,
    Window,
    current_meta,
    get_retention,
    now_utc,
    table_ref,
)

# ── Unified address expressions (§3.2) ───────────────────────────────────────

ZERO6 = "toIPv6('::')"
EXPORTER = f"if(exporter_ip6 = {ZERO6}, IPv4ToIPv6(exporter_ip), exporter_ip6)"
# Address family of a flow row (NF-32). An IPv4 flow stores both IPv6 twins as
# '::'; an IPv6 flow always has at least one endpoint other than '::' (no
# packet is sent to the unspecified address). Testing each side on its own
# (``src_addr6 = '::'``) read an IPv6 flow from or to '::' -- DAD neighbour
# solicitations, MLD reports -- as the IPv4 address 0.0.0.0. The pair test
# works on every table that keeps both address pairs: raw records (where it
# agrees with ip_version) and the conversation rollup (which has no
# ip_version column), so one SRC/DST serves raw, rollup and hybrid plans.
IS_V4_FLOW = f"(src_addr6 = {ZERO6} AND dst_addr6 = {ZERO6})"
SRC = f"if({IS_V4_FLOW}, IPv4ToIPv6(src_addr), src_addr6)"
DST = f"if({IS_V4_FLOW}, IPv4ToIPv6(dst_addr), dst_addr6)"
# A next hop is IPv4 when its IPv6 twin is empty and either the flow is IPv4
# or an IPv4 next hop was exported (6PE); an IPv6 flow without one shows '::'.
NEXT_HOP = (f"if(next_hop6 = {ZERO6} AND ({IS_V4_FLOW} OR next_hop != toIPv4('0.0.0.0')), "
            f"IPv4ToIPv6(next_hop), next_hop6)")
# Tables that predate IPv6 exporters (flow_traffic_5m) only have exporter_ip.
EXPORTER_V4 = "IPv4ToIPv6(exporter_ip)"

# Full-width ifIndexes on flow_records (input_snmp/output_snmp are the legacy
# UInt16 columns; the collector writes both, readers use these).
IF_IN = "input_if"
IF_OUT = "output_if"

# Volume guard (§3.3) for raw aggregates. Qualified as flow_records.bytes so it
# resolves to the column, never to a ``sum(bytes) AS bytes`` SELECT alias
# (which would raise ILLEGAL_AGGREGATION). Stored bytes are sampling-adjusted,
# so the guard divides by the sampling interval to test the unsampled value;
# NSEL create/deny events (fw_event 1, 3) carry no volume of their own.
VOLUME_GUARD = (
    "flow_records.bytes / greatest(flow_records.sampling_interval, 1) <= 1000000000000"
    " AND flow_records.fw_event NOT IN (1, 3)"
)


def display(expr: str) -> str:
    """Address expression -> display text (IPv4 without the ::ffff: prefix).
    Only for result columns; never inside WHERE/GROUP BY."""
    return f"replaceRegexpOne(toString({expr}), '^::ffff:', '')"


IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network


def normalize_ip(value: IPAddr) -> IPAddr:
    """IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is the IPv4 address it maps."""
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        return value.ipv4_mapped
    return value


def normalize_net(net: IPNet) -> IPNet:
    """An IPv6 network inside ::ffff:0:0/96 is the IPv4 network it maps."""
    if isinstance(net, ipaddress.IPv6Network) and net.prefixlen >= 96:
        mapped = net.network_address.ipv4_mapped
        if mapped is not None:
            return ipaddress.IPv4Network((mapped, net.prefixlen - 96))
    return net


def ip_param(ip: str | IPAddr) -> str:
    """Bind value for an address compared against a unified expression."""
    addr = normalize_ip(ipaddress.ip_address(ip) if isinstance(ip, str) else ip)
    if isinstance(addr, ipaddress.IPv4Address):
        return f"::ffff:{addr}"
    return str(addr)


def side_in_network(side: str, net: IPNet, param: str | None, params: dict | None) -> str:
    """Predicate: the ``side`` ('src' | 'dst') address of a row lies in ``net``.

    IPv4 networks compare the native IPv4 column (and require the row to be an
    IPv4 flow); IPv6 networks compare the unified expression. With ``param``
    the bounds are bound as parameters (user input); without, they are inlined
    (constants such as network classes, validated by ipaddress)."""
    net = normalize_net(net)
    v4col, v6col, uni = f"{side}_addr", f"{side}_addr6", (SRC if side == "src" else DST)

    def lit(value: str, fn: str, suffix: str) -> str:
        if param is None:
            return f"{fn}('{value}')"
        params[f"{param}{suffix}"] = value
        return f"{fn}(%({param}{suffix})s)"

    if isinstance(net, ipaddress.IPv4Network):
        # The row must be an IPv4 flow (both twins '::'), not merely have an
        # empty IPv6 column on this side: an IPv6 flow from '::' is not in
        # 0.0.0.0/8 (NF-32).
        guard = IS_V4_FLOW
        if net.prefixlen == 32:
            return f"({guard} AND {v4col} = {lit(str(net.network_address), 'toIPv4', '')})"
        lo = lit(str(net.network_address), "toIPv4", "_lo")
        hi = lit(str(net.broadcast_address), "toIPv4", "_hi")
        return f"({guard} AND {v4col} BETWEEN {lo} AND {hi})"
    if net.prefixlen == 128:
        return f"({uni} = {lit(str(net.network_address), 'toIPv6', '')})"
    lo = lit(str(net.network_address), "toIPv6", "_lo")
    hi = lit(str(net.broadcast_address), "toIPv6", "_hi")
    return f"({uni} BETWEEN {lo} AND {hi})"


# ── Network classes (one definition for the filter and the breakdown) ────────

NET_CLASSES: dict[str, tuple[str, ...]] = {
    "Private 10/8": ("10.0.0.0/8",),
    "Private 172.16/12": ("172.16.0.0/12",),
    "Private 192.168/16": ("192.168.0.0/16",),
    "CGNAT": ("100.64.0.0/10",),
    "Loopback": ("127.0.0.0/8", "::1/128"),
    "Link-local": ("169.254.0.0/16", "fe80::/10"),
    "Multicast": ("224.0.0.0/4", "ff00::/8"),
    "Unique local (IPv6)": ("fc00::/7",),
    # Special-purpose space that is neither private nor globally routed
    # (RFC 6890 / IANA special-purpose registries): "this network", IETF
    # protocol assignments, documentation (TEST-NET-1/2/3, 2001:db8::/32,
    # 3fff::/20), benchmarking, reserved 240/4 incl. the limited broadcast, and
    # the IPv6 discard prefix. Counting these as Public overstated public
    # traffic in lab and simulator data.
    "Documentation / special-use": (
        "0.0.0.0/8", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24",
        "203.0.113.0/24", "240.0.0.0/4",
        "2001:db8::/32", "3fff::/20", "2001:2::/48", "100::/64",
        # The IPv6 unspecified address (DAD / MLD sources) is not public.
        "::/128",
    ),
}
SPECIAL_USE_CLASS = "Documentation / special-use"
PUBLIC_CLASS = "Public"
NET_CLASS_NAMES: tuple[str, ...] = (*NET_CLASSES, PUBLIC_CLASS)


def netclass_member(side: str, name: str) -> str:
    """Predicate: the ``side`` address is in network class ``name`` (Public =
    in none of the listed classes)."""
    if name == PUBLIC_CLASS:
        inside = " OR ".join(netclass_member(side, n) for n in NET_CLASSES)
        return f"NOT ({inside})"
    terms = [side_in_network(side, ipaddress.ip_network(c), None, None) for c in NET_CLASSES[name]]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def netclass_expr(side: str) -> str:
    """Expression naming the network class of the ``side`` address (for
    breakdowns; agrees with the ``netclass`` filter by construction)."""
    branches = ", ".join(f"{netclass_member(side, n)}, '{n}'" for n in NET_CLASSES)
    return f"multiIf({branches}, '{PUBLIC_CLASS}')"


# ── Table capabilities ────────────────────────────────────────────────────────

# A record's ToS (DSCP) is an observation when the exporter sent the field
# (fields_present bit 1) or the row predates field tracking (fields_present =
# 0: presence unknown, read as before). Otherwise the stored 0 means "not
# exported", never DSCP 0 / Best Effort. flow_qos_1h keeps the same fact in
# its tos_known key (1 exported, 2 legacy, 0 not exported).
TOS_KNOWN_RAW = "(fields_present = 0 OR bitTest(fields_present, 1))"

# Filter keys (§8) that only narrow by exporter; every table can evaluate them.
_EXPORTER_KEYS = frozenset({"exporter"})
_TIME_KEYS = frozenset({"hour", "dow"})
_ADDRESS_KEYS = frozenset({"talker", "src", "dst", "not_talker", "not_src", "not_dst", "netclass", "group"})
ALL_FILTER_KEYS = frozenset({
    "exporter", "iface", "iface_dir", "talker", "src", "dst", "not_talker", "not_src", "not_dst",
    "protocol", "port", "src_port", "dst_port", "dst_ports", "service_port", "app", "dscp",
    "netclass", "tcpflag", "hour", "dow", "group", "asn", "ip_version", "direction",
    "min_bytes", "max_bytes", "fw_event", "count",
})


@dataclass(frozen=True)
class TableCaps:
    """What one flow table can answer."""

    name: str                       # bare table name
    kind: str                       # 'raw' | 'rollup' | 'interface_rollup' (= meta.window.source)
    supports: frozenset[str]        # §8 filter keys it can evaluate
    exporter: str = EXPORTER        # exporter expression
    bucket_s: int = 0               # time bucket (0 = per record)
    time_col: str = "timestamp"
    volume_guard: bool = False      # raw volume guard applies (MVs apply it at insert)
    flags_known: str | None = None  # "the TCP-flags field was exported" for this row
    tos_known: str | None = None    # "the ToS field was exported (or legacy row)": DSCP is an observation
    ip_version: str | None = None   # 4 / 6 expression
    has_direction: bool = False     # flow_direction column (count=edge direction rules)

    @property
    def ref(self) -> str:
        return table_ref(self.name)

    def unsupported(self, f: Any, drop: frozenset[str] = frozenset()) -> frozenset[str]:
        """Active filter keys of ``f`` this table cannot evaluate (empty = all good)."""
        active = f.active_keys() - drop
        missing = set(active - self.supports)
        # fw_event=flows ("flow records, not NSEL create/deny events") holds
        # for every rollup row: rollups are built through the volume guard.
        if "fw_event" in missing and self.kind != "raw" and getattr(f, "fw_event", None) == "flows":
            missing.discard("fw_event")
        # Hour buckets only line up with local hours in whole-hour time zones.
        if self.bucket_s >= 3600 and active & _TIME_KEYS and not _whole_hour_tz(f.tz):
            missing |= active & _TIME_KEYS
        # Per-exporter counting directions need flow_direction.
        if "count" in active and "count" not in missing and not self.has_direction:
            counting = getattr(f, "counting", None)
            if counting is None or not counting.direction_free:
                missing.add("count")
        return frozenset(missing)

    def can_serve(self, f: Any, drop: frozenset[str] = frozenset()) -> bool:
        return not self.unsupported(f, drop)


def _whole_hour_tz(tz: str) -> bool:
    if tz == "UTC":
        return True
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    year = datetime.now(timezone.utc).year
    for month in (1, 4, 7, 10):
        offset = datetime(year, month, 1, tzinfo=zone).utcoffset() or timedelta(0)
        if offset.total_seconds() % 3600:
            return False
    return True


# The classifier reads the rollup's source port from its src_ports state (a key
# seen with exactly one source port classifies as raw would; see
# netflow_classify.ROLLUP_SRC_PORT_SQL), so app/service_port work there too.
_ROLLUP_ADDR_KEYS = _ADDRESS_KEYS | {"protocol", "dst_port", "dst_ports", "ip_version", "app", "service_port"}

TABLES: dict[str, TableCaps] = {
    RAW: TableCaps(
        name=RAW, kind="raw", supports=ALL_FILTER_KEYS, volume_guard=True,
        flags_known="bitTest(fields_present, 0)", ip_version="ip_version", has_direction=True,
        tos_known=TOS_KNOWN_RAW,
    ),
    # Pair-keyed hour rollup: addresses, protocol and destination port survive;
    # per-record columns (ifIndex, ToS, flags, source port, AS, sizes) do not.
    CONVERSATIONS_1H: TableCaps(
        name=CONVERSATIONS_1H, kind="rollup", bucket_s=3600,
        supports=_EXPORTER_KEYS | _TIME_KEYS | _ROLLUP_ADDR_KEYS | {"count"},
        ip_version=f"if({IS_V4_FLOW}, 4, 6)",
    ),
    # ToS/flag-keyed hour rollup: exporter, protocol, DSCP and exact flag values.
    QOS_1H: TableCaps(
        name=QOS_1H, kind="rollup", bucket_s=3600,
        supports=_EXPORTER_KEYS | _TIME_KEYS | {"protocol", "dscp", "tcpflag", "count"},
        flags_known="flags_known = 1",
        tos_known="tos_known != 0",
    ),
    # Per-interface, per-direction 5-minute capacity rollup.
    INTERFACE_5M: TableCaps(
        name=INTERFACE_5M, kind="interface_rollup", bucket_s=300,
        supports=_EXPORTER_KEYS | _TIME_KEYS | {"iface", "iface_dir", "direction", "count"},
        has_direction=True,
    ),
    TRAFFIC_5M: TableCaps(
        name=TRAFFIC_5M, kind="rollup", bucket_s=300, exporter=EXPORTER_V4,
        supports=_EXPORTER_KEYS | _TIME_KEYS | {"protocol", "dst_port", "dst_ports"},
    ),
    EXPORTER_MINUTES: TableCaps(
        name=EXPORTER_MINUTES, kind="rollup", bucket_s=60, time_col="minute",
        supports=_EXPORTER_KEYS | {"count"},
    ),
}


def table_caps(table: str) -> TableCaps:
    """Capabilities for a bare or ``db.``-qualified table name."""
    try:
        return TABLES[table.split(".")[-1]]
    except KeyError:
        raise ValueError(f"no NetFlow capability entry for table {table!r}") from None


# ── Time buckets ──────────────────────────────────────────────────────────────

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def floor_to(dt: datetime, seconds: int) -> datetime:
    """``dt`` rounded down to an epoch-aligned multiple of ``seconds``."""
    off = int((dt - _EPOCH).total_seconds()) // seconds * seconds
    return _EPOCH + timedelta(seconds=off)


def ceil_to(dt: datetime, seconds: int) -> datetime:
    floored = floor_to(dt, seconds)
    return floored if floored == dt else floored + timedelta(seconds=seconds)


# ── Source planner (NF-17) ────────────────────────────────────────────────────

ROLLUP_MIN_SPAN = timedelta(seconds=ROLLUP_MIN_SECONDS)


@dataclass(frozen=True)
class Segment:
    table: str       # bare table name
    start: datetime  # inclusive
    end: datetime    # exclusive


@dataclass(frozen=True)
class SourcePlan:
    """Which tables serve which parts of a window.

    ``segments`` are disjoint and half-open; their union is
    ``[effective_from, effective_to)`` (the requested window minus parts no
    retained table can answer exactly). ``source`` is 'raw', 'rollup' (or
    'interface_rollup') or 'hybrid' (rollup interior + raw edges).
    """

    window: Window
    source: str
    rollup: str | None
    segments: tuple[Segment, ...]
    effective_from: datetime
    effective_to: datetime
    granularity_s: int
    warnings: tuple[tuple[str, str], ...] = ()
    retention_limited: bool = False

    @property
    def empty(self) -> bool:
        return not self.segments

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.table for s in self.segments))

    def record(self, meta: QueryMeta | None = None, *, force: bool = False) -> "SourcePlan":
        """Publish this plan in the response meta (first plan wins unless force)."""
        meta = meta or current_meta()
        meta.tables.update(self.tables)
        if meta.source is not None and not force:
            return self
        meta.set_source(self.source, self.effective_from, self.effective_to, self.granularity_s,
                        force=True, retention_checked=True)
        meta.retention_limited = meta.retention_limited or self.retention_limited
        for code, message in self.warnings:
            meta.warn(code, message)
        return self

    def from_sql(
        self,
        columns: Mapping[str, str | tuple[str, str]],
        f: Any,
        vis: Any,
        drop: frozenset[str] = frozenset(),
        *,
        group_by: str | tuple[str | None, str | None] | None = None,
        volume_guard: bool = True,
    ) -> tuple[str, dict]:
        """A parenthesised ``UNION ALL`` subquery over the plan's segments.

        ``columns`` maps output alias -> expression, or -> ``(rollup_expr,
        raw_expr)`` where the two tables need different expressions (e.g.
        ``"flows": ("flows", "toUInt64(1)")``). The two sides of a column must
        have a common ClickHouse supertype: UInt64 sums mix freely, but the
        Float64 columns of flow_interface_5m need ``toFloat64(...)`` on the raw
        side, and raw edges of an interface plan must be shaped per direction
        (one row per input_if / output_if) to match that rollup's rows.
        ``group_by`` is applied to each part (a ``(rollup, raw)`` pair when they
        differ), for parts that emit aggregate states. Every part carries the
        compiled filter (with the visibility scope) for its own table. Use as
        ``FROM {sql} AS src``.
        """
        # Imported here: filters builds on this module.
        from .filters import compile_filter

        by_table: dict[str, list[Segment]] = {}
        for seg in self.segments:
            by_table.setdefault(seg.table, []).append(seg)
        if not by_table:
            # Nothing retained covers the window: an empty result with the
            # expected columns, never an unbounded scan.
            by_table = {RAW: []}
        params: dict = {}
        parts: list[str] = []
        n = 0
        for table, segs in by_table.items():
            caps = table_caps(table)
            is_raw = caps.kind == "raw"
            exprs = []
            for alias, expr in columns.items():
                if isinstance(expr, tuple):
                    expr = expr[1] if is_raw else expr[0]
                exprs.append(f"{expr} AS {alias}")
            ranges = []
            for seg in segs:
                ranges.append(f"({caps.time_col} >= %(nf_t{n}_from)s AND {caps.time_col} < %(nf_t{n}_to)s)")
                params[f"nf_t{n}_from"] = seg.start
                params[f"nf_t{n}_to"] = seg.end
                n += 1
            time_pred = " OR ".join(ranges) if ranges else "0"
            where, fparams = compile_filter(f, table, vis, drop, volume_guard=volume_guard)
            params.update(fparams)
            sql = f"SELECT {', '.join(exprs)} FROM {caps.ref} WHERE ({time_pred}) AND {where}"
            grp = group_by[1 if is_raw else 0] if isinstance(group_by, tuple) else group_by
            if grp:
                sql += f" GROUP BY {grp}"
            parts.append(sql)
        return "(\n" + "\nUNION ALL\n".join(parts) + "\n)", params


# Display names for filter keys that can force a raw-only plan (raw_retention warning).
_RAW_ONLY_NAMES = {
    "dscp": "DSCP", "tcpflag": "TCP flag", "port": "port", "src_port": "source port",
    "iface": "interface", "iface_dir": "interface direction", "asn": "AS number",
    "min_bytes": "record size", "max_bytes": "record size", "direction": "flow direction",
    "fw_event": "firewall event", "count": "edge counting", "hour": "hour", "dow": "day of week",
    "ip_version": "IP version", "netclass": "network class", "group": "IP group",
}


def build_plan(
    window: Window,
    f: Any,
    *,
    retention: Retention,
    rollup: str | None = CONVERSATIONS_1H,
    drop: frozenset[str] = frozenset(),
    now: datetime | None = None,
    min_rollup_span: timedelta = ROLLUP_MIN_SPAN,
) -> SourcePlan:
    """Plan the tables for ``window`` (pure; ``plan_source`` adds retention).

    * Raw only when the rollup cannot evaluate the filter, or the window is
      short (below ``min_rollup_span``) and entirely within raw retention.
    * Otherwise the rollup serves the complete buckets inside the window and
      raw records serve the partial buckets at either end (hybrid).
    * An edge older than raw retention cannot be answered exactly, so it is
      left out and the effective bound moves to the bucket boundary; the part
      of a raw-only window beyond raw retention, and rollup buckets beyond
      rollup retention, are left out the same way. Each case is reported.
    """
    now = now or now_utc()
    start, end = window.start, window.end
    raw_h = retention.horizon(RAW, now)
    warnings: list[tuple[str, str]] = []
    raw_note = (f"Raw flow records are kept for {retention.raw_days} days; "
                "the part of the range before that is not included")

    caps = table_caps(rollup) if rollup else None
    use_rollup = caps is not None and caps.can_serve(f, drop) and (
        end - start >= min_rollup_span or (raw_h is not None and start < raw_h)
    )
    if caps is not None and not caps.can_serve(f, drop):
        # Say which filter forces raw records: beyond raw retention its
        # traffic is unknown, while unfiltered panels still read the rollups.
        keys = sorted(caps.unsupported(f, drop))
        names = ", ".join(dict.fromkeys(_RAW_ONLY_NAMES.get(k, k) for k in keys))
        raw_note = (f"The {names} filter needs raw flow records, which are kept for {retention.raw_days} days; "
                    "the part of the range before that is unknown for this filter, not zero")

    if not use_rollup:
        eff_from = max(start, raw_h) if raw_h is not None else start
        limited = eff_from > start
        if limited:
            warnings.append(("raw_retention", raw_note))
        segments = (Segment(RAW, eff_from, end),) if eff_from < end else ()
        if not segments:
            eff_from = end
        return SourcePlan(window, "raw", None, segments, eff_from, end, 1, tuple(warnings), limited)

    bucket = caps.bucket_s
    b_start, b_end = ceil_to(start, bucket), floor_to(end, bucket)
    roll_h = retention.horizon(caps.name, now)
    body_from = max(b_start, ceil_to(roll_h, bucket)) if roll_h is not None else b_start
    segments: list[Segment] = []
    limited = False
    if b_start >= b_end:
        # No complete bucket inside the window: raw only, exactly.
        eff_from = max(start, raw_h) if raw_h is not None else start
        if eff_from > start:
            warnings.append(("raw_retention", raw_note))
            limited = True
        if eff_from < end:
            segments.append(Segment(RAW, eff_from, end))
        else:
            eff_from = end
        return SourcePlan(window, "raw", None, tuple(segments), eff_from, end, 1, tuple(warnings), limited)

    eff_from, eff_to = start, end
    # Head edge [start, b_start): raw when it is entirely within raw retention.
    if start < b_start:
        if body_from == b_start and (raw_h is None or start >= raw_h):
            segments.append(Segment(RAW, start, b_start))
        else:
            eff_from = b_start
            limited = True
            warnings.append(("raw_retention", "The start of the range is older than raw flow retention; "
                             f"it begins at the first complete {bucket // 60}-minute rollup bucket instead"))
    if body_from > b_start:
        eff_from = min(body_from, b_end)
        limited = True
        warnings.append(("rollup_retention", f"Rollups are kept for {retention.days.get(caps.name)} days; "
                         "the part of the range before that is not included"))
    if body_from < b_end:
        segments.append(Segment(caps.name, body_from, b_end))
    # Tail edge [b_end, end): raw when it is entirely within raw retention.
    if b_end < end:
        if raw_h is None or b_end >= raw_h:
            segments.append(Segment(RAW, b_end, end))
        else:
            eff_to = b_end
            limited = True
            warnings.append(("raw_retention", "The end of the range is older than raw flow retention; "
                             f"it stops at the last complete {bucket // 60}-minute rollup bucket instead"))
    if not segments:
        eff_from = eff_to
    tables = {s.table for s in segments}
    if tables == {RAW}:
        source = "raw"
    elif RAW in tables:
        source = "hybrid"
    else:
        source = caps.kind
    granularity = 1 if source == "raw" else bucket
    return SourcePlan(window, source, caps.name, tuple(segments), eff_from, eff_to, granularity,
                      tuple(dict.fromkeys(warnings)), limited)


async def plan_source(
    window: Window,
    f: Any,
    *,
    rollup: str | None = CONVERSATIONS_1H,
    drop: frozenset[str] = frozenset(),
    record: bool = True,
    min_rollup_span: timedelta = ROLLUP_MIN_SPAN,
) -> SourcePlan:
    """Plan ``window`` for filter ``f`` against current retention and (by
    default) record the plan as the response's source metadata.

    ``rollup`` names the rollup that could serve the query
    (``flow_conversations_1h`` for address/port aggregates, ``flow_qos_1h`` for
    DSCP/flags/protocol, ``flow_interface_5m`` for per-interface volume), or
    None for raw-only data.
    """
    plan = build_plan(window, f, retention=await get_retention(), rollup=rollup, drop=drop,
                      min_rollup_span=min_rollup_span)
    if record:
        plan.record()
    return plan


__all__ = [
    "ZERO6", "IS_V4_FLOW", "EXPORTER", "SRC", "DST", "NEXT_HOP", "EXPORTER_V4", "IF_IN", "IF_OUT", "VOLUME_GUARD",
    "display", "normalize_ip", "normalize_net", "ip_param", "side_in_network",
    "NET_CLASSES", "NET_CLASS_NAMES", "PUBLIC_CLASS", "SPECIAL_USE_CLASS", "netclass_member", "netclass_expr",
    "ALL_FILTER_KEYS", "TableCaps", "TABLES", "table_caps", "floor_to", "ceil_to", "TOS_KNOWN_RAW",
    "Segment", "SourcePlan", "build_plan", "plan_source", "ROLLUP_MIN_SPAN",
]
