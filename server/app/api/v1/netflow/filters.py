"""The NetFlow filter model (contract §8): parsing, validation and SQL compilation.

``FlowFilter`` holds every canonical filter key with a typed value. It is parsed
from the query string by the ``flow_filter`` dependency; every value is validated
and anything unknown or out of range is a 400 whose ``detail`` names the key
(``fields`` maps each bad key to its message). Nothing is silently ignored: a
filter chip in the UI always means the data was filtered by it. Numeric zero is
a value, never "unset".

``compile_filter(f, table, vis, drop)`` turns a filter into a WHERE fragment for
one table. It always adds the caller's visibility scope (NF-04) and, on raw
records, the volume guard (§3.3). A key the table cannot evaluate is a
programming error (``UnsupportedFilter``): callers pick the table with
``sql.plan_source``/``table_supports`` first, so a rollup never answers a
question it cannot answer.

Every endpoint plans its tables with ``sql.plan_source`` (raw / rollup /
hybrid, half-open) and compiles this filter; the legacy scope-dict adapters
that predated the contract are gone.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from typing import Any, Iterable, Mapping

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import netflow_classify, netflow_groups

from .access import FlowVisibility, canonical_ip, flow_visibility, visibility_predicate
from .common import (
    BUDGET_BACKGROUND,
    CONVERSATIONS_1H,
    INTERFACE_5M,
    NetflowError,
    current_meta,
    invalid_filter,
    run_query,
    table_ref,
    validate_tz,
)
from .sql import (
    EXPORTER,
    IF_IN,
    IF_OUT,
    NET_CLASS_NAMES,
    NET_CLASSES,
    TABLES,
    VOLUME_GUARD,
    TableCaps,
    display,
    ip_param,
    netclass_member,
    normalize_net,
    side_in_network,
    table_caps,
)


# ── Keys ──────────────────────────────────────────────────────────────────────

# Canonical query/URL keys in contract order (tz is shared with the window).
FLOW_FILTER_KEYS: tuple[str, ...] = (
    "exporter", "iface", "iface_dir", "talker", "src", "dst", "not_talker", "not_src", "not_dst",
    "protocol", "port", "src_port", "dst_port", "dst_ports", "service_port", "app", "dscp",
    "netclass", "tcpflag", "hour", "dow", "tz", "group", "asn", "ip_version", "direction",
    "min_bytes", "max_bytes", "fw_event", "count",
)
# Read aliases (older links and saved views); the canonical key wins.
FILTER_ALIASES: dict[str, str] = {"proto": "protocol", "dscp_eq": "dscp"}

TCP_FLAGS = ("syn_only", "ack_only", "rst", "fin", "psh", "urg", "no_flags")
FW_EVENTS: dict[str, int] = {"created": 1, "teardown": 2, "denied": 3, "alert": 4, "update": 5}
# fw_event=flows: every record that carries traffic volume, i.e. everything
# except NSEL connection-created and denied events (the §3.3 volume guard's
# event part). Drill-downs from volume cards use it so Forensics returns the
# records the card counted.
FW_EVENT_FLOWS = "flows"
FW_EVENT_VALUES: tuple[str, ...] = (*FW_EVENTS, FW_EVENT_FLOWS)
DIRECTIONS: dict[str, int] = {"ingress": 0, "egress": 1}   # IE 61 flowDirection
IFACE_DIRS = ("in", "out", "any")
COUNT_MODES = ("all", "edge")
MAX_DST_PORTS = 32
UINT32_MAX = 4294967295
UINT64_MAX = 18446744073709551615

IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network


# ── Values ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AddrMatch:
    """An address or CIDR (IPv4-mapped IPv6 normalised to IPv4)."""

    network: IPNet

    @property
    def is_host(self) -> bool:
        return self.network.prefixlen == self.network.max_prefixlen

    @property
    def text(self) -> str:
        return str(self.network.network_address) if self.is_host else str(self.network)

    @classmethod
    def parse(cls, value: str) -> "AddrMatch":
        return cls(normalize_net(ipaddress.ip_network(value.strip(), strict=False)))


@dataclass(frozen=True)
class IfaceRef:
    """ifIndex N, optionally qualified by the exporter it belongs to (N@exporter)."""

    ifindex: int
    exporter: str | None = None

    @property
    def text(self) -> str:
        return f"{self.ifindex}@{self.exporter}" if self.exporter else str(self.ifindex)


@dataclass(frozen=True)
class CountingPolicy:
    """How ``count=edge`` counts (FE-12 / CR-16), resolved from
    netflow_exporter_settings plus the exporters' observed directions.

    Sets hold IPv6 bind text (``ip_param``) for comparison with EXPORTER.
    """

    excluded: tuple[str, ...] = ()     # role is not 'edge' (core/internal/excluded)
    ingress_only: tuple[str, ...] = ()  # keep ingress-monitored (and unknown) records
    egress_only: tuple[str, ...] = ()   # keep egress-monitored (and unknown) records
    direction_known: bool = True       # the auto rule could be evaluated

    @property
    def direction_free(self) -> bool:
        """No exporter needs per-record direction filtering."""
        return not (self.ingress_only or self.egress_only)


@dataclass(frozen=True)
class FlowFilter:
    """Every §8 filter dimension; ``None`` (or the default) means not filtered."""

    exporter: str | None = None
    iface: IfaceRef | None = None
    iface_dir: str = "any"
    talker: AddrMatch | None = None
    src: AddrMatch | None = None
    dst: AddrMatch | None = None
    not_talker: AddrMatch | None = None
    not_src: AddrMatch | None = None
    not_dst: AddrMatch | None = None
    protocol: int | None = None
    port: int | None = None
    src_port: int | None = None
    dst_port: int | None = None
    dst_ports: tuple[int, ...] = ()
    service_port: int | None = None
    app: str | None = None
    dscp: int | None = None
    netclass: str | None = None
    tcpflag: str | None = None
    hour: int | None = None
    dow: int | None = None
    tz: str = "UTC"
    group: str | None = None
    asn: int | None = None
    ip_version: int | None = None
    direction: str | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    fw_event: str | None = None
    count: str = "all"
    # Resolved context for count=edge (not a URL key).
    counting: CountingPolicy | None = field(default=None, compare=False)

    def active_keys(self) -> frozenset[str]:
        """Filter dimensions that restrict rows (tz alone restricts nothing)."""
        keys = set()
        for key in FLOW_FILTER_KEYS:
            if key in ("tz", "iface_dir"):
                continue
            value = getattr(self, key)
            if key == "count":
                if value == "edge":
                    keys.add(key)
            elif key == "dst_ports":
                if value:
                    keys.add(key)
            elif value is not None:
                keys.add(key)
        if self.iface is not None and self.iface_dir != "any":
            keys.add("iface_dir")
        return frozenset(keys)

    def canonical(self) -> dict[str, str]:
        """Canonical URL form of the active filters (plus tz when it matters)."""
        out: dict[str, str] = {}
        active = self.active_keys()
        for key in FLOW_FILTER_KEYS:
            if key == "tz":
                if self.tz != "UTC":
                    out["tz"] = self.tz
                continue
            if key not in active:
                continue
            value = getattr(self, key)
            if isinstance(value, (AddrMatch, IfaceRef)):
                out[key] = value.text
            elif key == "dst_ports":
                out[key] = ",".join(str(p) for p in value)
            else:
                out[key] = str(value)
        return out

    def without(self, *keys: str) -> "FlowFilter":
        """The filter with the given dimensions cleared."""
        changes: dict[str, Any] = {}
        for key in keys:
            if key == "iface":
                changes["iface"] = None
                changes["iface_dir"] = "any"
            elif key == "iface_dir":
                changes["iface_dir"] = "any"
            elif key == "dst_ports":
                changes["dst_ports"] = ()
            elif key == "count":
                changes["count"] = "all"
                changes["counting"] = None
            elif key == "tz":
                changes["tz"] = "UTC"
            elif key in _FIELD_NAMES:
                changes[key] = None
        return replace(self, **changes) if changes else self


_FIELD_NAMES = frozenset(f.name for f in fields(FlowFilter))


# ── Parsing ───────────────────────────────────────────────────────────────────

_UINT_RE = re.compile(r"^\d{1,20}$")
_IFACE_RE = re.compile(r"^(\d{1,10})(?:@(.+))?$")


def _uint(raw: str, lo: int, hi: int, what: str) -> int:
    if not _UINT_RE.match(raw):
        raise ValueError(f"{what} must be a whole number between {lo} and {hi}")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return value


def _enum(raw: str, allowed: Iterable[str], what: str) -> str:
    allowed = tuple(allowed)
    if raw not in allowed:
        raise ValueError(f"Unknown {what} '{raw[:64]}' (expected one of: {', '.join(allowed)})")
    return raw


def _addr(raw: str, what: str) -> AddrMatch:
    try:
        return AddrMatch.parse(raw)
    except ValueError:
        raise ValueError(f"{what} must be an IP address or CIDR") from None


def _ip(raw: str, what: str) -> str:
    # canonical_ip also accepts INET text (a.b.c.d/nn); a filter wants one address.
    ip = canonical_ip(raw) if "/" not in raw else None
    if ip is None:
        raise ValueError(f"{what} must be an IP address")
    return ip


def _iface(raw: str) -> IfaceRef:
    m = _IFACE_RE.match(raw)
    if not m:
        raise ValueError("iface must be an ifIndex (0..4294967295), optionally as N@exporter")
    index = int(m.group(1))
    if index > UINT32_MAX:
        raise ValueError("iface must be between 0 and 4294967295")
    exporter = _ip(m.group(2), "The exporter in iface=N@exporter") if m.group(2) else None
    return IfaceRef(index, exporter)


def _dst_ports(raw: str) -> tuple[int, ...]:
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if not items:
        raise ValueError("dst_ports needs at least one port")
    if len(items) > MAX_DST_PORTS:
        raise ValueError(f"dst_ports takes at most {MAX_DST_PORTS} ports")
    return tuple(sorted({_uint(p, 0, 65535, "Each dst_ports entry") for p in items}))


def _app(raw: str) -> str:
    # Case-insensitive, and an exporter (NBAR2/App-ID) name selects the
    # application it is classified as ("icmp" -> "ICMP"), so links written
    # before names were folded keep working.
    app = netflow_classify.resolve_application(raw)
    if app is None:
        raise ValueError(f"Unknown application '{raw[:80]}'")
    return app


def _group(raw: str) -> str:
    if raw not in netflow_groups.group_names():
        raise ValueError(f"Unknown IP group '{raw[:120]}'")
    return raw


_PARSERS: dict[str, Any] = {
    "exporter": lambda v: _ip(v, "exporter"),
    "iface": _iface,
    "iface_dir": lambda v: _enum(v, IFACE_DIRS, "iface_dir"),
    "talker": lambda v: _addr(v, "talker"),
    "src": lambda v: _addr(v, "src"),
    "dst": lambda v: _addr(v, "dst"),
    "not_talker": lambda v: _addr(v, "not_talker"),
    "not_src": lambda v: _addr(v, "not_src"),
    "not_dst": lambda v: _addr(v, "not_dst"),
    "protocol": lambda v: _uint(v, 0, 255, "protocol"),
    "port": lambda v: _uint(v, 0, 65535, "port"),
    "src_port": lambda v: _uint(v, 0, 65535, "src_port"),
    "dst_port": lambda v: _uint(v, 0, 65535, "dst_port"),
    "dst_ports": _dst_ports,
    "service_port": lambda v: _uint(v, 0, 65535, "service_port"),
    "app": _app,
    "dscp": lambda v: _uint(v, 0, 63, "dscp"),
    "netclass": lambda v: _enum(v, NET_CLASS_NAMES, "network class"),
    "tcpflag": lambda v: _enum(v, TCP_FLAGS, "tcpflag"),
    "hour": lambda v: _uint(v, 0, 23, "hour"),
    "dow": lambda v: _uint(v, 1, 7, "dow"),
    "group": _group,
    "asn": lambda v: _uint(v, 0, UINT32_MAX, "asn"),
    "ip_version": lambda v: int(_enum(v, ("4", "6"), "ip_version")),
    "direction": lambda v: _enum(v, tuple(DIRECTIONS), "direction"),
    "min_bytes": lambda v: _uint(v, 0, UINT64_MAX, "min_bytes"),
    "max_bytes": lambda v: _uint(v, 0, UINT64_MAX, "max_bytes"),
    "fw_event": lambda v: _enum(v, FW_EVENT_VALUES, "fw_event"),
    "count": lambda v: _enum(v, COUNT_MODES, "count"),
}


def _values(query: Any, key: str) -> list[str]:
    if hasattr(query, "getlist"):
        return list(query.getlist(key))
    value = query.get(key) if isinstance(query, Mapping) else None
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def parse_flow_filter(query: Any) -> FlowFilter:
    """FlowFilter from query parameters (Starlette QueryParams or a mapping).

    Empty values count as absent. A key given twice with different values, or
    a canonical key and its alias disagreeing, is an error — a link must mean
    one thing. All problems are reported together.
    """
    errors: dict[str, str] = {}
    raw: dict[str, str] = {}
    for key in FLOW_FILTER_KEYS:
        spellings = [key] + [alias for alias, canon in FILTER_ALIASES.items() if canon == key]
        seen: dict[str, str] = {}
        for spelling in spellings:
            for value in _values(query, spelling):
                value = str(value).strip()
                if value:
                    seen.setdefault(value, spelling)
        if len(seen) > 1:
            errors[key] = f"{key} was given more than once with different values"
        elif seen:
            raw[key] = next(iter(seen))

    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "tz":
            continue
        try:
            values[key] = _PARSERS[key](value)
        except ValueError as exc:
            errors[key] = str(exc)
    tz = "UTC"
    if "tz" in raw:
        try:
            tz = validate_tz(raw["tz"])
        except NetflowError as exc:
            errors["tz"] = exc.detail
    if values.get("min_bytes") is not None and values.get("max_bytes") is not None \
            and values["min_bytes"] > values["max_bytes"]:
        errors["max_bytes"] = "max_bytes must not be below min_bytes"
    if errors:
        message = "; ".join(errors.values())
        if set(errors) == {"tz"}:
            raise NetflowError(400, message, code="invalid_window", fields=errors)
        raise invalid_filter(message, errors)
    return FlowFilter(tz=tz, **values)


# ── count=edge policy ─────────────────────────────────────────────────────────

_COUNTING_TTL_S = 30.0
_counting_cache: dict[str, Any] = {"value": None, "at": 0.0}
# Exporters that emitted both ingress- and egress-monitored records recently
# are the ones whose 'auto' counting keeps only ingress records.
_BIDIRECTIONAL_LOOKBACK = timedelta(days=7)


async def load_counting_policy(db: AsyncSession) -> CountingPolicy:
    """Resolve ``count=edge`` from netflow_exporter_settings (no row = edge,
    count_direction 'auto') and the directions each exporter emits (cached 30 s)."""
    cached = _counting_cache["value"]
    if cached is not None and time.monotonic() - _counting_cache["at"] < _COUNTING_TTL_S:
        return cached
    settings: dict[str, tuple[str, str]] = {}
    exists = (await db.execute(text("SELECT to_regclass('public.netflow_exporter_settings') IS NOT NULL"))).scalar()
    if exists:
        rows = (await db.execute(text(
            "SELECT host(exporter_ip) AS ip, role, count_direction FROM netflow_exporter_settings"
        ))).all()
        for r in rows:
            ip = canonical_ip(r.ip)
            if ip:
                settings[ip] = (r.role, r.count_direction)
    direction_known = True
    try:
        res = await run_query(
            f"""
            SELECT {EXPORTER} AS e
            FROM {table_ref(INTERFACE_5M)}
            WHERE timestamp >= now() - toIntervalSecond(%(lookback)s) AND flow_direction IN (0, 1)
            GROUP BY e
            HAVING uniqExact(flow_direction) = 2
            """,
            {"lookback": int(_BIDIRECTIONAL_LOOKBACK.total_seconds())},
            budget_s=BUDGET_BACKGROUND,
            label="counting-policy",
        )
        bidirectional = {canonical_ip(r[0]) for r in res.result_rows}
    except NetflowError:
        # Unknown directions: count every record of 'auto' exporters rather
        # than guess, and say so.
        bidirectional = set()
        direction_known = False
    excluded, ingress, egress = [], set(), set()
    for ip, (role, direction) in settings.items():
        if role != "edge":
            excluded.append(ip)
        elif direction == "ingress":
            ingress.add(ip)
        elif direction == "egress":
            egress.add(ip)
    for ip in bidirectional:
        if ip and settings.get(ip, ("edge", "auto"))[1] == "auto" and ip not in excluded:
            ingress.add(ip)
    policy = CountingPolicy(
        excluded=tuple(sorted(ip_param(ip) for ip in excluded)),
        ingress_only=tuple(sorted(ip_param(ip) for ip in ingress)),
        egress_only=tuple(sorted(ip_param(ip) for ip in egress)),
        direction_known=direction_known,
    )
    _counting_cache.update(value=policy, at=time.monotonic())
    return policy


def invalidate_counting_cache() -> None:
    """Call after writing netflow_exporter_settings."""
    _counting_cache.update(value=None, at=0.0)
    _double_cache.clear()


# ── Exporter notes the answer must carry (X-28, X-08) ────────────────────────
# Two per-exporter facts change how a total must be read, and a page that
# shows the total must say so:
# * An exporter that monitors both ingress and egress reports each flow twice
#   (flowDirection 0 and 1). count=edge (and Capacity) keep one of the two;
#   count=all, the default, adds both. That stays the definition of 'all'
#   (every observation), but the answer says so instead of disagreeing with
#   Capacity in silence (X-28).
# * An exporter whose samplers have different rates and whose records name
#   none of them: the collector leaves those records unscaled (X-08), so their
#   volume is not a sampling estimate.
# Each is looked up per visibility scope (the query binds the caller's
# exporters), cached for a minute; a failure is "unknown" -- no notice -- and
# retried after 30 s.

_NOTES_TTL_S = 60.0
_NOTES_RETRY_S = 30.0
_NOTES_BUDGET_S = 3.0
_NOTES_CACHE_MAX = 256
_NOTES_LOOKBACK = timedelta(days=7)
_double_cache: dict[Any, tuple[float, frozenset[str] | None]] = {}


async def _count_direction_settings(db: AsyncSession) -> dict[str, str]:
    """{canonical exporter -> count_direction}; {} when unavailable."""
    try:
        async with db.begin_nested():
            exists = (await db.execute(text(
                "SELECT to_regclass('public.netflow_exporter_settings') IS NOT NULL"))).scalar()
            if not exists:
                return {}
            rows = (await db.execute(text(
                "SELECT host(exporter_ip) AS ip, count_direction FROM netflow_exporter_settings"))).all()
    except Exception:  # noqa: BLE001 - a notice must never fail the request
        return {}
    return {ip: r.count_direction for r in rows if (ip := canonical_ip(r.ip))}


async def _scoped_exporter_set(name: str, table: str, vis: FlowVisibility, load) -> frozenset[str] | None:
    """Cached ``await load()`` (a set of canonical exporters) per scope."""
    if vis.exporters is not None and not vis.exporters:
        return frozenset()
    key = (name, table, tuple(sorted(vis.exporters)) if vis.exporters is not None else None)
    now = time.monotonic()
    hit = _double_cache.get(key)
    if hit is not None and now - hit[0] < (_NOTES_TTL_S if hit[1] is not None else _NOTES_RETRY_S):
        return hit[1]
    meta = current_meta()
    unanswered = meta.ch_unanswered
    value: frozenset[str] | None
    try:
        value = frozenset(await load())
    except Exception:  # noqa: BLE001 - unknown, not "none"
        value = None
        # A slow answer to this side lookup must not make the request's own
        # queries fail fast (NF-31 short-circuit): only they decide that.
        meta.ch_unanswered = unanswered
    if len(_double_cache) >= _NOTES_CACHE_MAX:
        _double_cache.clear()
    _double_cache[key] = (now, value)
    return value


async def double_counted_exporters(db: AsyncSession, vis: FlowVisibility) -> frozenset[str] | None:
    """Visible exporters whose records count=all adds twice: they emitted
    both ingress- and egress-monitored records (IE 61) within the counting
    lookback and are not set to count_direction 'both' (the operator's word
    that the two directions are different traffic). Canonical IP text; None
    when ClickHouse cannot say."""
    table = table_ref(INTERFACE_5M)

    async def load() -> set[str]:
        vis_sql, params = visibility_predicate(vis, param="nf_vis")
        params["lookback"] = int(_BIDIRECTIONAL_LOOKBACK.total_seconds())
        res = await run_query(
            f"""
            SELECT {display(EXPORTER)} AS e
            FROM {table}
            WHERE timestamp >= now() - toIntervalSecond(%(lookback)s) AND flow_direction IN (0, 1)
              {f"AND {vis_sql}" if vis_sql else ""}
            GROUP BY e
            HAVING uniqExact(flow_direction) = 2
            """,
            params, budget_s=_NOTES_BUDGET_S, label="double-counted-exporters",
        )
        both = {ip for ip in (canonical_ip(r[0]) for r in res.result_rows) if ip}
        if both:
            settings = await _count_direction_settings(db)
            both = {ip for ip in both if settings.get(ip, "auto") != "both"}
        return both

    return await _scoped_exporter_set("double", table, vis, load)


async def ambiguous_sampling_exporters(vis: FlowVisibility) -> frozenset[str] | None:
    """Visible exporters with records stored unscaled because their sampling
    rate was ambiguous (collector X-08, flow_collector_stats
    .sampling_ambiguous_records) within the lookback; None when unknown."""
    table = table_ref("flow_collector_stats")

    async def load() -> set[str]:
        params: dict = {"lookback": int(_NOTES_LOOKBACK.total_seconds())}
        pred = "exporter != ''"
        if vis.exporters is not None:
            pred += " AND exporter IN %(nf_vis_exporters)s"
            params["nf_vis_exporters"] = sorted(vis.exporters)
        res = await run_query(
            f"""
            SELECT exporter FROM {table}
            WHERE ts >= now() - toIntervalSecond(%(lookback)s) AND {pred}
            GROUP BY exporter
            HAVING sum(sampling_ambiguous_records) > 0
            """,
            params, budget_s=_NOTES_BUDGET_S, label="ambiguous-sampling-exporters",
        )
        return {ip for ip in (canonical_ip(r[0]) for r in res.result_rows) if ip}

    return await _scoped_exporter_set("ambiguous", table, vis, load)


def _named_exporters(f: "FlowFilter") -> set[str]:
    named = {f.exporter, f.iface.exporter if f.iface else None} - {None}
    return {ip for ip in (canonical_ip(e) for e in named) if ip}


def _affected(f: "FlowFilter", exporters: Iterable[str] | None) -> list[str]:
    """``exporters`` the answer for ``f`` can include, sorted for display."""
    affected = set(exporters or ())
    named = _named_exporters(f)
    if named:
        affected &= named
    return sorted(affected, key=lambda ip: (":" in ip, ip))


def _exporter_list(names: list[str]) -> str:
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"exporter {shown}" if len(names) == 1 else f"exporters {shown}"


def ambiguous_sampling_warning(f: "FlowFilter", exporters: Iterable[str] | None) -> str | None:
    """The ``sampling_ambiguous`` notice for ``f``, or None (X-08)."""
    names = _affected(f, exporters)
    if not names:
        return None
    return (f"Some volumes from {_exporter_list(names)} are not scaled for sampling: the exporter announced "
            "samplers with different rates and its records name none of them, so they are counted 1 in 1")


def double_count_warning(f: "FlowFilter", exporters: Iterable[str] | None) -> str | None:
    """The ``counting_both_directions`` notice for ``f`` (count=all), or None.

    Only exporters the answer can include count: a filter naming another
    exporter, or a single monitored direction, has no duplicates."""
    if f.count != "all" or f.direction is not None:
        return None
    names = _affected(f, exporters)
    if not names:
        return None
    who = _exporter_list(names) + (" reports" if len(names) == 1 else " report")
    return (f"Totals count these flows twice: {who} each flow at both ingress and egress. "
            "Counting 'Edge exporters only' (count=edge) counts each flow once, as Capacity does")


# ── Dependency ────────────────────────────────────────────────────────────────

def reject_unknown_filter_keys(keys: Iterable[str], *, prefix: str = "") -> None:
    """400 naming every key that is not a filter key or a read alias.

    For filters given as a whole object (a JSON body, a report scope, a
    stored schedule) -- query strings also carry window and paging keys, so
    ``parse_flow_filter`` cannot judge them. An unknown key is never
    silently ignored (contract §8): a typo such as ``talkr`` would otherwise
    widen the answer to the whole scope."""
    known = set(FLOW_FILTER_KEYS) | set(FILTER_ALIASES)
    unknown = sorted({str(k) for k in keys} - known)
    if unknown:
        names = ", ".join(f"'{k[:40]}'" for k in unknown[:5])
        raise invalid_filter(f"Unknown filter key {names} (expected one of: {', '.join(FLOW_FILTER_KEYS)})",
                             {f"{prefix}{k[:40]}": "Unknown filter key" for k in unknown[:20]})


def parse_flow_filter_strict(values: Mapping[str, Any], *, prefix: str = "") -> FlowFilter:
    """``parse_flow_filter`` for a complete filter object: unknown keys are a
    400 (``reject_unknown_filter_keys``); values may be strings, numbers or
    lists (``dst_ports``)."""
    reject_unknown_filter_keys(values.keys(), prefix=prefix)
    flat: dict[str, str] = {}
    for key, value in values.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool) or isinstance(value, dict):
            raise invalid_filter(f"Invalid value for filter '{key}'", {f"{prefix}{key}": "Invalid value"})
        if isinstance(value, (list, tuple)):
            flat[str(key)] = ",".join(str(v) for v in value)
        else:
            flat[str(key)] = str(value)
    return parse_flow_filter(flat)


async def flow_filter(
    request: Request,
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
) -> FlowFilter:
    """FastAPI dependency: the request's validated FlowFilter.

    Refreshes the classifier rules and IP groups first (cheap while cached) so
    ``app``/``group`` are validated against — and compiled with — current data.
    Adds the exporter notices the answer must carry (``warn_exporter_notes``:
    double counting under count=all, unscaled ambiguous sampling).
    """
    await netflow_classify.load_rules()
    await netflow_groups.load_groups()
    f = parse_flow_filter(request.query_params)
    if f.count == "edge":
        f = replace(f, counting=await load_counting_policy(db))
        if not f.counting.direction_known:
            current_meta().warn("counting_direction_unknown",
                                "Exporter flow directions could not be read; edge counting keeps every record "
                                "of exporters set to automatic direction")
    await warn_exporter_notes(f, vis, db)
    current_meta().filter = f
    return f


async def warn_exporter_notes(f: FlowFilter, vis: FlowVisibility, db: AsyncSession) -> None:
    """Add the per-exporter notices that change how ``f``'s totals read: the
    ``counting_both_directions`` warning when count=all includes exporters
    that report each flow twice (X-28), and ``sampling_ambiguous`` when it
    includes exporters with records left unscaled (X-08)."""
    meta = current_meta()
    want_double = f.count == "all" and f.direction is None

    async def no_lookup() -> None:
        return None

    # Independent lookups, run together: a ClickHouse that does not answer
    # costs one notes budget per request, not two (NF-31).
    double, ambiguous = await asyncio.gather(
        double_counted_exporters(db, vis) if want_double else no_lookup(),
        ambiguous_sampling_exporters(vis),
    )
    if want_double:
        message = double_count_warning(f, double)
        if message:
            meta.warn("counting_both_directions", message)
    message = ambiguous_sampling_warning(f, ambiguous)
    if message:
        meta.warn("sampling_ambiguous", message)


# ── Compilation ───────────────────────────────────────────────────────────────

class UnsupportedFilter(ValueError):
    """compile_filter was asked for a table that cannot evaluate a key."""

    def __init__(self, table: str, keys: Iterable[str]):
        self.table, self.keys = table, tuple(sorted(keys))
        super().__init__(f"{table} cannot evaluate filter keys {', '.join(self.keys)}")


def table_supports(f: FlowFilter, table: str, drop: Iterable[str] = ()) -> bool:
    """Whether ``table`` can evaluate every active key of ``f`` (minus ``drop``)."""
    return table_caps(table).can_serve(f, frozenset(drop))


def _exporter_match(caps: TableCaps, ip: str, param: str, params: dict) -> str:
    """Rows from one exporter; IPv4 compares the native column (primary key)."""
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv4Address):
        params[param] = str(addr)
        if caps.exporter == EXPORTER:
            return f"(exporter_ip6 = toIPv6('::') AND exporter_ip = toIPv4(%({param})s))"
        return f"exporter_ip = toIPv4(%({param})s)"
    params[param] = ip_param(addr)
    return f"({caps.exporter} = toIPv6(%({param})s))"


def _side(caps: TableCaps, side: str, m: AddrMatch, param: str, params: dict) -> str:
    return side_in_network(side, m.network, param, params)


def _tcpflag_sql(caps: TableCaps, flag: str) -> str:
    bits = {"fin": 1, "syn": 2, "rst": 4, "psh": 8, "ack": 16, "urg": 32}
    if flag == "syn_only":
        cond = f"bitAnd(tcp_flags, {bits['syn']}) != 0 AND bitAnd(tcp_flags, {bits['ack']}) = 0"
    elif flag == "ack_only":
        cond = f"bitAnd(tcp_flags, {bits['ack']}) != 0 AND bitAnd(tcp_flags, {bits['syn']}) = 0"
    elif flag == "no_flags":
        # Exported and zero; a legacy or flag-less record's zero proves nothing.
        cond = f"tcp_flags = 0 AND {caps.flags_known}"
    else:
        cond = f"bitAnd(tcp_flags, {bits[flag]}) != 0"
    return f"(protocol = 6 AND {cond})"


def _classifier_sql(caps: TableCaps, element: str) -> str:
    """The classifier's ``service_port``/``application`` expression for a table."""
    src_port = netflow_classify.ROLLUP_SRC_PORT_SQL if caps.name == CONVERSATIONS_1H else "src_port"
    exprs = netflow_classify.classification_sql(
        src_port, "dst_port", "protocol", app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr",
    )
    return exprs[element]


def compile_filter(
    f: FlowFilter,
    table: str,
    vis: FlowVisibility,
    drop: Iterable[str] = frozenset(),
    *,
    volume_guard: bool = True,
) -> tuple[str, dict]:
    """``(predicate, params)`` for ``table``: visibility scope, the volume guard
    on raw records (unless ``volume_guard=False`` — forensics shows every row),
    then every active key of ``f`` except ``drop``.

    The predicate is a conjunction ready for ``WHERE … AND {predicate}``
    (``"1"`` when nothing applies). Parameter names start with ``nf_`` and
    depend only on the filter, so fragments compiled for different tables of
    one query can share a parameter dict.
    """
    if vis is None:
        raise TypeError("compile_filter needs the caller's FlowVisibility")
    caps = table_caps(table)
    drop = frozenset(drop)
    missing = caps.unsupported(f, drop)
    if missing:
        raise UnsupportedFilter(caps.name, missing)
    meta = current_meta()
    if vis.restricted:
        meta.restricted = True
    if drop:
        meta.dropped |= drop & f.active_keys()

    parts: list[str] = []
    params: dict = {}
    vis_sql, vis_params = visibility_predicate(
        vis, v6_col="exporter_ip6" if caps.exporter == EXPORTER else None, param="nf_vis",
    )
    if vis_sql:
        parts.append(vis_sql)
        params.update(vis_params)
    if volume_guard and caps.volume_guard:
        parts.append(VOLUME_GUARD)

    active = f.active_keys() - drop
    if "exporter" in active:
        parts.append(_exporter_match(caps, f.exporter, "nf_exporter", params))
    if "iface" in active:
        params["nf_iface"] = f.iface.ifindex
        direction = f.iface_dir if "iface_dir" in active else "any"
        if caps.kind == "interface_rollup":
            cond = "ifindex = %(nf_iface)s"
            if direction != "any":
                cond += f" AND direction = {0 if direction == 'in' else 1}"
            parts.append(f"({cond})")
        else:
            parts.append({
                "in": f"{IF_IN} = %(nf_iface)s",
                "out": f"{IF_OUT} = %(nf_iface)s",
                "any": f"({IF_IN} = %(nf_iface)s OR {IF_OUT} = %(nf_iface)s)",
            }[direction])
        if f.iface.exporter:
            parts.append(_exporter_match(caps, f.iface.exporter, "nf_iface_exporter", params))
    if "talker" in active:
        parts.append(f"({_side(caps, 'src', f.talker, 'nf_talker', params)} OR "
                     f"{_side(caps, 'dst', f.talker, 'nf_talker', params)})")
    if "src" in active:
        parts.append(_side(caps, "src", f.src, "nf_src", params))
    if "dst" in active:
        parts.append(_side(caps, "dst", f.dst, "nf_dst", params))
    if "not_talker" in active:
        parts.append(f"NOT ({_side(caps, 'src', f.not_talker, 'nf_not_talker', params)} OR "
                     f"{_side(caps, 'dst', f.not_talker, 'nf_not_talker', params)})")
    if "not_src" in active:
        parts.append(f"NOT {_side(caps, 'src', f.not_src, 'nf_not_src', params)}")
    if "not_dst" in active:
        parts.append(f"NOT {_side(caps, 'dst', f.not_dst, 'nf_not_dst', params)}")
    if "protocol" in active:
        parts.append("protocol = %(nf_protocol)s")
        params["nf_protocol"] = f.protocol
    if "port" in active:
        parts.append("(src_port = %(nf_port)s OR dst_port = %(nf_port)s)")
        params["nf_port"] = f.port
    if "src_port" in active:
        parts.append("src_port = %(nf_src_port)s")
        params["nf_src_port"] = f.src_port
    if "dst_port" in active:
        parts.append("dst_port = %(nf_dst_port)s")
        params["nf_dst_port"] = f.dst_port
    if "dst_ports" in active:
        parts.append("dst_port IN %(nf_dst_ports)s")
        params["nf_dst_ports"] = tuple(f.dst_ports)
    if "service_port" in active:
        parts.append(f"{_classifier_sql(caps, 'service_port')} = %(nf_service_port)s")
        params["nf_service_port"] = f.service_port
    if "app" in active:
        parts.append(f"{_classifier_sql(caps, 'application')} = %(nf_app)s")
        params["nf_app"] = f.app
    if "dscp" in active:
        # Only records whose ToS was exported (or legacy rows, presence
        # unknown): a template without ToS stores 0, which is not DSCP 0.
        dscp = "bitShiftRight(tos, 2) = %(nf_dscp)s"
        parts.append(f"({dscp} AND {caps.tos_known})" if caps.tos_known else dscp)
        params["nf_dscp"] = f.dscp
    if "netclass" in active:
        parts.append(f"({netclass_member('src', f.netclass)} OR {netclass_member('dst', f.netclass)})")
    if "tcpflag" in active:
        parts.append(_tcpflag_sql(caps, f.tcpflag))
    if "hour" in active or "dow" in active:
        params["nf_tz"] = f.tz
    if "hour" in active:
        parts.append(f"toHour({caps.time_col}, %(nf_tz)s) = %(nf_hour)s")
        params["nf_hour"] = f.hour
    if "dow" in active:
        parts.append(f"toDayOfWeek({caps.time_col}, 0, %(nf_tz)s) = %(nf_dow)s")
        params["nf_dow"] = f.dow
    if "group" in active:
        pred = netflow_groups.group_predicate(f.group, "src_addr", "dst_addr")
        if pred is None:
            # Deleted between validation and compilation: still never unfiltered.
            raise invalid_filter(f"Unknown IP group '{f.group}'", {"group": "Unknown IP group"})
        parts.append(pred)
    if "asn" in active:
        parts.append("(src_as = %(nf_asn)s OR dst_as = %(nf_asn)s)")
        params["nf_asn"] = f.asn
    if "ip_version" in active:
        parts.append(f"{caps.ip_version} = %(nf_ip_version)s")
        params["nf_ip_version"] = f.ip_version
    if "direction" in active:
        parts.append("flow_direction = %(nf_direction)s")
        params["nf_direction"] = DIRECTIONS[f.direction]
    if "min_bytes" in active:
        parts.append(f"{caps.name}.bytes >= %(nf_min_bytes)s")
        params["nf_min_bytes"] = f.min_bytes
    if "max_bytes" in active:
        parts.append(f"{caps.name}.bytes <= %(nf_max_bytes)s")
        params["nf_max_bytes"] = f.max_bytes
    if "fw_event" in active:
        if f.fw_event == FW_EVENT_FLOWS:
            # Rollups hold flow records only (their MVs apply the volume
            # guard, events excluded at insert): nothing left to filter.
            if caps.kind == "raw":
                parts.append("fw_event NOT IN (1, 3)")
        else:
            parts.append("fw_event = %(nf_fw_event)s")
            params["nf_fw_event"] = FW_EVENTS[f.fw_event]
    if "count" in active:
        policy = f.counting
        if policy is None:
            raise ValueError("count=edge needs the counting policy (flow_filter or load_counting_policy)")
        if policy.excluded:
            parts.append(f"{caps.exporter} NOT IN %(nf_count_excluded)s")
            params["nf_count_excluded"] = policy.excluded
        if caps.has_direction:
            if policy.ingress_only:
                parts.append(f"NOT ({caps.exporter} IN %(nf_count_ingress)s AND flow_direction = 1)")
                params["nf_count_ingress"] = policy.ingress_only
            if policy.egress_only:
                parts.append(f"NOT ({caps.exporter} IN %(nf_count_egress)s AND flow_direction = 0)")
                params["nf_count_egress"] = policy.egress_only
    return (" AND ".join(parts) if parts else "1"), params


# ── Shared helpers ────────────────────────────────────────────────────────────

UNRESTRICTED = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset())


def require_filter_exporters_visible(f: FlowFilter, vis: FlowVisibility) -> None:
    """404 when the filter names an exporter the caller cannot see (a scoped
    user cannot confirm an out-of-scope exporter exists by probing)."""
    for exporter in (f.exporter, f.iface.exporter if f.iface else None):
        if exporter and not vis.allows(exporter):
            raise HTTPException(status_code=404, detail="Exporter not found")


__all__ = [
    "FLOW_FILTER_KEYS", "FILTER_ALIASES", "TCP_FLAGS", "FW_EVENTS", "FW_EVENT_FLOWS", "FW_EVENT_VALUES",
    "DIRECTIONS", "NET_CLASSES",
    "NET_CLASS_NAMES", "AddrMatch", "IfaceRef", "CountingPolicy", "FlowFilter", "parse_flow_filter",
    "flow_filter", "load_counting_policy", "invalidate_counting_cache", "compile_filter",
    "double_counted_exporters", "double_count_warning", "ambiguous_sampling_exporters",
    "ambiguous_sampling_warning", "warn_exporter_notes",
    "table_supports", "UnsupportedFilter", "TABLES", "UNRESTRICTED",
    "require_filter_exporters_visible", "reject_unknown_filter_keys", "parse_flow_filter_strict",
]
