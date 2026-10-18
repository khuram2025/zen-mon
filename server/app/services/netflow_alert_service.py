"""NetFlow alert rules in the platform alert engine (CR-07, NF-23).

Five metric keys make up the ``netflow`` rule family (``alert_rules.metric``,
widened by migrate-143):

``netflow_traffic_bps``
    Average bit rate of the traffic matching the rule's FlowFilter scope
    (``alert_rules.flow_scope``: exporter, iface, group, app, protocol,
    dst_port, talker ... -- every contract §8 key) over the last closed
    ``window_min`` minutes. Above, below, or outside a band; optionally split
    per exporter so each exporter is its own alert.
``netflow_endpoint_bytes``
    One host (source, destination or either side) moving more than N bytes in
    the last ``window_min`` minutes. One alert per host.
``netflow_exporter_silent``
    Seconds since an exporter last delivered flow records to the appliance
    (``flow_exporter_minutes``, appliance receive clock -- the same evidence as
    ``/netflow/exporters/status``). It says the *flow export* stopped; it never
    claims the device is down (NF-23).
``netflow_interface_util_pct``
    Per-direction interface utilisation from the flow interface rollup
    (``flow_interface_5m``) against the one speed rule
    (``interface_speed.effective_speed``: configured, override, SNMP).
    An interface without a known speed is "unknown", never a guess.
``netflow_finding``
    Raises a platform alert for each persistent detection finding
    (``netflow_findings``) of the selected kinds, severity and confidence;
    resolves it when the finding is resolved or suppressed.

Evaluation (``netflow_alert_loop``) runs every ``EVAL_INTERVAL_S`` under a
PostgreSQL advisory lock so the two uvicorn workers never double-evaluate.
Each (rule, subject) keeps a small state row (``netflow_alert_state``):

* a breach must hold for ``hold`` consecutive evaluations before an alert is
  raised, and a clear for ``clear_hold`` evaluations before it resolves;
* an unknown reading (no coverage, unknown speed, ClickHouse unavailable) is
  neither a breach nor a clear -- unknown is not zero;
* flap suppression: when a subject has already changed state ``flap_max``
  times within ``flap_window_min``, a resolve is deferred (the alert stays
  open, marked ``flapping``) until the history calms down, so a flapping
  condition pages once instead of every few minutes;
* the rule's cooldown suppresses a repeat *notification* for the same subject
  (the alert row is still recorded), like every other platform evaluator.

Alerts go through the existing machinery: ``alerts`` rows with ``rule_id`` (so
escalation tiers, acknowledgement and the Alert Center work unchanged), the
exporter owner's ``device_id`` when known (so tag scoping via
``alert_visible`` applies -- alerts about an unowned exporter are system-level
and hidden from scoped users), device maintenance and silences honoured,
quiet hours through ``alert_schedule``, delivery through
``host_alert_service.dispatch_to_channels`` with the house phrasing, and the
``notified`` stamp that keeps recovery notices consistent with triggers.
Traffic alerts carry a top-talker / top-application snapshot of the window
and a frozen deep link into NetFlow.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow.access import (
    UNRESTRICTED_SYSTEM,
    FlowVisibility,
    canonical_ip,
    resolve_exporter_owners,
)
from app.api.v1.netflow.common import (
    BUDGET_BACKGROUND,
    CONVERSATIONS_1H,
    EXPORTER_MINUTES,
    INTERFACE_5M,
    RAW,
    NetflowError,
    Window,
    run_query,
    table_ref,
)
from app.api.v1.netflow.filters import (
    FLOW_FILTER_KEYS,
    FlowFilter,
    compile_filter,
    load_counting_policy,
    parse_flow_filter,
)
from app.api.v1.netflow.sql import DST, EXPORTER, SRC, display, floor_to, plan_source
from app.services import alert_phrasing as ap

logger = logging.getLogger("zenplus.netflow.alerts")

FAMILY = "netflow"
EVAL_INTERVAL_S = 60
ADVISORY_LOCK = 0x4E46_414C  # "NFAL"
RULE_SUBJECT = "__rule__"     # rule-level status row in netflow_alert_state

TRAFFIC = "netflow_traffic_bps"
ENDPOINT = "netflow_endpoint_bytes"
SILENT = "netflow_exporter_silent"
UTIL = "netflow_interface_util_pct"
FINDING = "netflow_finding"
METRICS: tuple[str, ...] = (TRAFFIC, ENDPOINT, SILENT, UTIL, FINDING)

# Alert-rule vocabulary regex fragment (alert_rules._CONDITION_METRICS).
METRIC_PATTERN = "|".join(METRICS)

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "warning": 2, "high": 3, "critical": 4}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
FINDING_SEVERITIES = ("info", "low", "medium", "warning", "high", "critical")
FINDING_CONFIDENCES = ("low", "medium", "high")

# ── Metric catalogue (API + wizard) ──────────────────────────────────────────

METRIC_INFO: dict[str, dict[str, Any]] = {
    TRAFFIC: {
        "label": "Flow traffic rate",
        "unit": "bps",
        "operators": [">", ">=", "<", "<=", "!="],
        "description": "Average bit rate of the traffic matching the flow scope over the window. "
                       "Above, below or outside a band (operator != with band_high).",
        "subject": "scope or exporter",
        "defaults": {"window_min": 5, "settle_s": 120, "hold": 2, "clear_hold": 2,
                     "flap_window_min": 60, "flap_max": 4, "split": "none"},
        "window_range": [1, 60],
    },
    ENDPOINT: {
        "label": "Host transfer volume",
        "unit": "bytes",
        "operators": [">", ">="],
        "description": "A single host moving more than the threshold in the window "
                       "(sent, received or either side).",
        "subject": "host",
        "defaults": {"window_min": 15, "settle_s": 120, "hold": 1, "clear_hold": 2,
                     "flap_window_min": 60, "flap_max": 4, "side": "any", "max_subjects": 25},
        "window_range": [1, 1440],
    },
    SILENT: {
        "label": "Flow export silence",
        "unit": "s",
        "operators": [">", ">="],
        "description": "Seconds since the exporter last delivered flow records to this appliance. "
                       "Flow-export silence, not device reachability.",
        "subject": "exporter",
        "defaults": {"hold": 1, "clear_hold": 1, "flap_window_min": 60, "flap_max": 4,
                     "lookback_h": 168},
        "window_range": None,
    },
    UTIL: {
        "label": "Interface utilisation (flow)",
        "unit": "%",
        "operators": [">", ">=", "<", "<=", "!="],
        "description": "Per-direction utilisation of an exporter interface from flow data against "
                       "its configured, override or SNMP speed. Unknown speed = no evaluation.",
        "subject": "interface direction",
        "defaults": {"window_min": 15, "settle_s": 300, "hold": 2, "clear_hold": 2,
                     "flap_window_min": 60, "flap_max": 4, "direction": "either"},
        "window_range": [5, 60],
    },
    FINDING: {
        "label": "NetFlow detection finding",
        "unit": "",
        "operators": ["=="],
        "description": "Raises an alert for each open detection finding of the selected kinds, "
                       "minimum severity and confidence; resolves it with the finding.",
        "subject": "finding",
        "defaults": {"hold": 1, "clear_hold": 1, "flap_window_min": 60, "flap_max": 0,
                     "kinds": [], "min_severity": "warning", "min_confidence": None, "max_age_h": 24},
        "window_range": None,
    },
}

# Phrasing for the alert mail (alert_phrasing.METRICS). Registered at import:
# alert_phrasing is shared platform code, and these entries only add keys.
PHRASING: dict[str, dict] = {
    TRAFFIC: {"noun": "Flow traffic", "unit": " Mbps", "scale": 1e6, "decimals": 1},
    ENDPOINT: {"noun": "Host transfer volume", "unit": " MB", "scale": 1e6, "decimals": 1},
    SILENT: {"noun": "Flow export silence", "unit": " s", "decimals": 0},
    UTIL: {"noun": "Interface utilisation (flow)", "unit": "%", "decimals": 1},
    FINDING: {"noun": "NetFlow finding", "subject": "the detector",
              "narrative": {"down": "raised a detection finding", "up": "has no open finding"},
              "state": {"down": "raises a detection finding", "up": "has no open finding"},
              "state_label": {"down": "Detection finding open", "up": "Finding closed"}},
}


def register_phrasing() -> None:
    for key, info in PHRASING.items():
        ap.METRICS.setdefault(key, info)


register_phrasing()


def is_netflow_metric(metric: str | None) -> bool:
    return (metric or "") in METRICS


# ── Rule definition ──────────────────────────────────────────────────────────

class RuleDefinitionError(ValueError):
    """A NetFlow rule's scope or parameters are invalid (API: 422)."""


@dataclass(frozen=True)
class RuleParams:
    window_min: int = 5
    settle_s: int = 120
    hold: int = 1
    clear_hold: int = 1
    flap_window_min: int = 60
    flap_max: int = 4
    band_high: float | None = None
    split: str = "none"            # traffic: none | exporter
    side: str = "any"              # endpoint: src | dst | any
    max_subjects: int = 25         # endpoint
    direction: str = "either"      # util: in | out | either
    lookback_h: int = 168          # silent
    kinds: tuple[str, ...] = ()    # finding
    min_severity: str | None = None
    min_confidence: str | None = None
    max_age_h: int = 24            # finding

    def as_dict(self, metric: str) -> dict:
        """The stored form: only the keys that mean something for ``metric``."""
        keys = {"hold", "clear_hold", "flap_window_min", "flap_max"}
        if metric in (TRAFFIC, ENDPOINT, UTIL):
            keys |= {"window_min", "settle_s"}
        if metric in (TRAFFIC, UTIL):
            keys.add("band_high")
        if metric == TRAFFIC:
            keys.add("split")
        if metric == ENDPOINT:
            keys |= {"side", "max_subjects"}
        if metric == UTIL:
            keys.add("direction")
        if metric == SILENT:
            keys.add("lookback_h")
        if metric == FINDING:
            keys |= {"kinds", "min_severity", "min_confidence", "max_age_h"}
        out = {}
        for k in sorted(keys):
            v = getattr(self, k)
            out[k] = list(v) if isinstance(v, tuple) else v
        return out


def _int_param(raw: Mapping, key: str, default: int, lo: int, hi: int) -> int:
    value = raw.get(key, default)
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuleDefinitionError(f"{key} must be a whole number between {lo} and {hi}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise RuleDefinitionError(f"{key} must be a whole number between {lo} and {hi}") from None
    if not math.isfinite(f) or f != int(f) or not lo <= int(f) <= hi:
        raise RuleDefinitionError(f"{key} must be a whole number between {lo} and {hi}")
    return int(f)


def _enum_param(raw: Mapping, key: str, default: str, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    value = raw.get(key, default)
    if value is None or value == "":
        return default
    if value not in allowed:
        raise RuleDefinitionError(f"{key} must be one of: {', '.join(allowed)}")
    return value


def parse_params(metric: str, operator: str | None, threshold, raw: Mapping | None) -> RuleParams:
    """Validated parameters for ``metric`` (defaults filled in)."""
    if metric not in METRICS:
        raise RuleDefinitionError(f"Unknown NetFlow metric {metric!r}")
    raw = dict(raw or {})
    unknown = set(raw) - set(RuleParams.__dataclass_fields__)
    if unknown:
        raise RuleDefinitionError(f"Unknown NetFlow rule parameter(s): {', '.join(sorted(unknown))}")
    info = METRIC_INFO[metric]
    d = info["defaults"]
    op = ap.normalize_operator(operator)
    if op not in info["operators"]:
        raise RuleDefinitionError(
            f"{info['label']} rules support the operators {', '.join(info['operators'])}")
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        raise RuleDefinitionError("threshold must be a number") from None
    if not math.isfinite(thr):
        raise RuleDefinitionError("threshold must be a finite number")
    if metric in (TRAFFIC, ENDPOINT, SILENT) and thr < 0:
        raise RuleDefinitionError("threshold must not be negative")
    if metric == UTIL and not 0 <= thr <= 1000:
        raise RuleDefinitionError("Utilisation threshold must be between 0 and 1000 %")

    wr = info.get("window_range")
    kw: dict[str, Any] = {
        "hold": _int_param(raw, "hold", d["hold"], 1, 60),
        "clear_hold": _int_param(raw, "clear_hold", d["clear_hold"], 1, 60),
        "flap_window_min": _int_param(raw, "flap_window_min", d["flap_window_min"], 5, 1440),
        "flap_max": _int_param(raw, "flap_max", d["flap_max"], 0, 50),
    }
    if kw["flap_max"] == 1:
        raise RuleDefinitionError("flap_max must be 0 (off) or at least 2 state changes")
    if wr:
        kw["window_min"] = _int_param(raw, "window_min", d["window_min"], wr[0], wr[1])
        kw["settle_s"] = _int_param(raw, "settle_s", d["settle_s"], 0, 1800)
    if metric == UTIL and kw["window_min"] % 5:
        raise RuleDefinitionError("window_min must be a multiple of 5 for interface utilisation "
                                  "(the interface rollup has 5-minute slots)")
    if op == "!=":
        band_high = raw.get("band_high")
        try:
            band_high = float(band_high)
        except (TypeError, ValueError):
            raise RuleDefinitionError("An 'outside band' rule needs band_high (the upper bound); "
                                      "threshold is the lower bound") from None
        if not math.isfinite(band_high) or band_high <= thr:
            raise RuleDefinitionError("band_high must be above the threshold (the lower bound)")
        kw["band_high"] = band_high
    elif raw.get("band_high") not in (None, ""):
        raise RuleDefinitionError("band_high only applies to 'outside band' rules (operator !=)")
    if metric == TRAFFIC:
        kw["split"] = _enum_param(raw, "split", d["split"], ("none", "exporter"))
    if metric == ENDPOINT:
        kw["side"] = _enum_param(raw, "side", d["side"], ("src", "dst", "any"))
        kw["max_subjects"] = _int_param(raw, "max_subjects", d["max_subjects"], 1, 100)
    if metric == UTIL:
        kw["direction"] = _enum_param(raw, "direction", d["direction"], ("in", "out", "either"))
    if metric == SILENT:
        kw["lookback_h"] = _int_param(raw, "lookback_h", d["lookback_h"], 1, 2160)
        if thr < 60:
            raise RuleDefinitionError("Silence threshold must be at least 60 seconds")
    if metric == FINDING:
        kinds = raw.get("kinds") or []
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",")]
        if not isinstance(kinds, (list, tuple)) or len(kinds) > 32 or not all(
                isinstance(k, str) and 0 < len(k.strip()) <= 48 for k in kinds):
            raise RuleDefinitionError("kinds must be a list of up to 32 finding kinds")
        kw["kinds"] = tuple(sorted({k.strip() for k in kinds}))
        sev = raw.get("min_severity", d["min_severity"])
        if sev not in (None, "") and sev not in FINDING_SEVERITIES:
            raise RuleDefinitionError(f"min_severity must be one of: {', '.join(FINDING_SEVERITIES)}")
        kw["min_severity"] = sev or None
        conf = raw.get("min_confidence", d["min_confidence"])
        if conf not in (None, "") and conf not in FINDING_CONFIDENCES:
            raise RuleDefinitionError(f"min_confidence must be one of: {', '.join(FINDING_CONFIDENCES)}")
        kw["min_confidence"] = conf or None
        kw["max_age_h"] = _int_param(raw, "max_age_h", d["max_age_h"], 1, 720)
    return RuleParams(**kw)


_SCOPE_KEYS_BY_METRIC: dict[str, frozenset[str]] = {
    TRAFFIC: frozenset(FLOW_FILTER_KEYS) - {"fw_event"},
    ENDPOINT: frozenset(FLOW_FILTER_KEYS) - {"fw_event"},
    SILENT: frozenset({"exporter"}),
    UTIL: frozenset({"exporter", "iface", "iface_dir"}),
    FINDING: frozenset({"exporter", "talker"}),
}


def parse_scope(metric: str, raw: Mapping | None) -> FlowFilter:
    """The rule's FlowFilter. Raises RuleDefinitionError with the offending
    keys; keys the metric cannot use are rejected, never silently ignored."""
    raw = {str(k): v for k, v in (raw or {}).items() if v not in (None, "", [])}
    allowed = _SCOPE_KEYS_BY_METRIC[metric]
    extra = set(raw) - allowed
    if extra:
        raise RuleDefinitionError(
            f"{METRIC_INFO[metric]['label']} rules cannot be scoped by: {', '.join(sorted(extra))}")
    query = {k: (",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v))
             for k, v in raw.items()}
    try:
        return parse_flow_filter(query)
    except NetflowError as exc:
        raise RuleDefinitionError(f"Invalid flow scope: {exc.detail}") from None


def scope_canonical(f: FlowFilter) -> dict[str, str]:
    """Stored form of a scope: canonical URL keys (the same the NetFlow pages use)."""
    return f.canonical()


async def validate_definition(metric: str, operator, threshold, flow_scope, flow_params) -> tuple[dict, dict]:
    """(canonical scope, stored params) for a NetFlow rule; RuleDefinitionError
    otherwise. Refreshes the application rules and IP groups first so ``app`` /
    ``group`` are checked against current data."""
    from app.services import netflow_classify, netflow_groups

    try:
        await netflow_classify.load_rules()
        await netflow_groups.load_groups()
    except Exception:  # pragma: no cover - cached data is used
        logger.debug("netflow alert: classifier/group refresh failed", exc_info=True)
    if flow_scope is not None and not isinstance(flow_scope, Mapping):
        raise RuleDefinitionError("flow_scope must be an object of filter keys")
    if flow_params is not None and not isinstance(flow_params, Mapping):
        raise RuleDefinitionError("flow_params must be an object")
    f = parse_scope(metric, flow_scope)
    params = parse_params(metric, operator, threshold, flow_params)
    if metric == UTIL and f.iface is not None and not (f.iface.exporter or f.exporter):
        raise RuleDefinitionError("An interface scope needs its exporter (iface=N@exporter, or exporter=...)")
    return scope_canonical(f), params.as_dict(metric)


@dataclass(frozen=True)
class NetflowRule:
    id: str
    name: str
    metric: str
    operator: str
    threshold: float
    severity: str
    filter: FlowFilter
    params: RuleParams
    scope: dict[str, str]
    notify_channels: tuple[str, ...] = ()
    cooldown: int = 0
    recovery_alert: bool = True
    schedule_start: Any = None
    schedule_end: Any = None
    schedule_days: Any = None
    templates: dict = field(default_factory=dict)
    created_by: str | None = None


def _jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def rule_from_row(row: Mapping) -> NetflowRule:
    """NetflowRule from an alert_rules row; RuleDefinitionError when broken."""
    metric = row["metric"]
    scope = _jsonish(row.get("flow_scope")) or {}
    params_raw = _jsonish(row.get("flow_params")) or {}
    f = parse_scope(metric, scope)
    params = parse_params(metric, row.get("operator"), row.get("threshold"), params_raw)
    channels = _jsonish(row.get("notify_channels")) or []
    return NetflowRule(
        id=str(row["id"]), name=row.get("name") or "NetFlow rule", metric=metric,
        operator=ap.normalize_operator(row.get("operator")), threshold=float(row.get("threshold") or 0),
        severity=row.get("severity") or "warning", filter=f, params=params, scope=scope_canonical(f),
        notify_channels=tuple(str(c) for c in channels),
        cooldown=int(row.get("cooldown") or 0),
        recovery_alert=bool(row.get("recovery_alert", True)),
        schedule_start=row.get("schedule_start"), schedule_end=row.get("schedule_end"),
        schedule_days=row.get("schedule_days"),
        templates={k: row.get(k) for k in ap.TEMPLATE_FIELDS},
        created_by=str(row["created_by"]) if row.get("created_by") else None,
    )


# ── Formatting ───────────────────────────────────────────────────────────────

def _si(value: float, unit: str, base: float = 1000.0) -> str:
    prefixes = ("", "k", "M", "G", "T", "P")
    v = float(value)
    i = 0
    while abs(v) >= base and i < len(prefixes) - 1:
        v /= base
        i += 1
    digits = 0 if abs(v) >= 100 else 1 if abs(v) >= 10 else 2
    text_ = f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}"
    return f"{text_} {prefixes[i]}{unit}"


def fmt_bps(v: float | None) -> str:
    if v is None:
        return "—"
    if 0 < v < 1:
        return "<1 bps"
    return _si(v, "bps")


def fmt_bytes(v: float | None) -> str:
    return "—" if v is None else _si(v, "B")


def fmt_duration_s(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return ap.humanize_duration(seconds) or f"{int(seconds)} s"


def format_reading(metric: str, value: float | None) -> str:
    if value is None:
        return "unknown"
    if metric == TRAFFIC:
        return fmt_bps(value)
    if metric == ENDPOINT:
        return fmt_bytes(value)
    if metric == SILENT:
        return fmt_duration_s(value)
    if metric == UTIL:
        return f"{value:.1f}%"
    return "open" if value else "closed"


_KEY_LABELS = {
    "exporter": "exporter", "iface": "interface", "iface_dir": "direction", "talker": "host",
    "src": "source", "dst": "destination", "not_talker": "excluding host", "not_src": "excluding source",
    "not_dst": "excluding destination", "protocol": "protocol", "port": "port", "src_port": "source port",
    "dst_port": "destination port", "dst_ports": "destination ports", "service_port": "service port",
    "app": "application", "dscp": "DSCP", "netclass": "network class", "tcpflag": "TCP flag",
    "hour": "hour", "dow": "weekday", "group": "IP group", "asn": "AS", "ip_version": "IPv",
    "direction": "flow direction", "min_bytes": "min bytes", "max_bytes": "max bytes", "count": "counting",
}


def describe_scope(scope: Mapping | None) -> str:
    """"exporter 10.0.0.1 · application HTTPS" -- or "all visible flow traffic"."""
    parts = []
    for key in FLOW_FILTER_KEYS:
        if key == "tz":
            continue
        value = (scope or {}).get(key)
        if value in (None, ""):
            continue
        label = _KEY_LABELS.get(key, key)
        if key == "ip_version":
            parts.append(f"IPv{value}")
        elif key == "count":
            parts.append("edge counting" if value == "edge" else f"{label} {value}")
        else:
            parts.append(f"{label} {value}")
    return " · ".join(parts) if parts else "all flow traffic"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def netflow_link(path: str, scope: Mapping | None, start: datetime, end: datetime, **extra) -> str:
    """Frozen deep link into NetFlow: canonical filter keys + exact from/to."""
    q = {k: v for k, v in (scope or {}).items() if v not in (None, "")}
    q["from"] = _iso(start)
    q["to"] = _iso(end)
    q.update({k: v for k, v in extra.items() if v not in (None, "")})
    return f"{path}?{urlencode(q)}"


# ── Readings and the state machine ───────────────────────────────────────────

@dataclass
class Reading:
    """One evaluated subject. ``value`` None = unknown (with ``reason``)."""

    subject: str
    label: str
    value: float | None
    reason: str | None = None
    exporter: str | None = None
    if_index: int | None = None
    direction: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Evaluation:
    """The result of evaluating one rule once."""

    rule_id: str
    metric: str
    status: str                       # ok | no_data | failed | skipped
    readings: list[Reading] = field(default_factory=list)
    window_from: datetime | None = None
    window_to: datetime | None = None
    reason: str | None = None
    error_id: str | None = None
    truncated: bool = False
    tracked_only: bool = False        # unknown for untracked subjects


def breaches(rule_or_op, threshold: float | None = None, value: float | None = None, *,
             band_high: float | None = None) -> bool | None:
    """Whether ``value`` breaches the condition; None when the value is unknown.

    ``!=`` is "outside the band [threshold, band_high]"."""
    if isinstance(rule_or_op, NetflowRule):
        op, thr, band_high = rule_or_op.operator, rule_or_op.threshold, rule_or_op.params.band_high
    else:
        op, thr = ap.normalize_operator(rule_or_op), float(threshold or 0)
    if value is None:
        return None
    if isinstance(rule_or_op, NetflowRule) and rule_or_op.metric == FINDING:
        return value >= 1
    if op == ">":
        return value > thr
    if op == ">=":
        return value >= thr
    if op == "<":
        return value < thr
    if op == "<=":
        return value <= thr
    if op == "!=":
        high = band_high if band_high is not None else thr
        return value < thr or value > high
    if op == "==":
        return value == thr
    return value > thr


@dataclass
class SubjectState:
    state: str = "unknown"
    breach_streak: int = 0
    clear_streak: int = 0
    transitions: list[str] = field(default_factory=list)   # ISO times of raise/resolve
    flapping: bool = False
    last_value: float | None = None
    last_reason: str | None = None
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    action: str          # raise | resolve | flap_hold | none
    state: SubjectState


def _recent(transitions: Iterable[str], now: datetime, window: timedelta) -> list[str]:
    out = []
    for t in transitions:
        try:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if now - dt <= window:
            out.append(_iso(dt))
    return out


def step(prev: SubjectState, breach: bool | None, *, alert_open: bool, params: RuleParams,
         now: datetime) -> Decision:
    """Advance one subject by one evaluation (pure; no I/O).

    * unknown: streaks and state are kept; nothing is raised or resolved;
    * breach: the breach streak grows; an alert is raised once it reaches
      ``hold`` (and none is open);
    * clear: the clear streak grows; an open alert resolves once it reaches
      ``clear_hold`` -- unless the subject is flapping (``flap_max``
      transitions within ``flap_window_min``, counting this one), in which
      case the alert is held open.
    """
    window = timedelta(minutes=params.flap_window_min)
    s = replace(prev, transitions=_recent(prev.transitions, now, window), context=dict(prev.context))
    if breach is None:
        return Decision("none", s)
    if breach:
        s.breach_streak += 1
        s.clear_streak = 0
        s.state = "breach"
        if not alert_open and s.breach_streak >= params.hold:
            s.transitions.append(_iso(now))
            s.flapping = params.flap_max > 0 and len(s.transitions) >= params.flap_max
            return Decision("raise", s)
        s.flapping = params.flap_max > 0 and len(s.transitions) >= params.flap_max
        return Decision("none", s)
    s.clear_streak += 1
    s.breach_streak = 0
    s.state = "ok"
    if alert_open and s.clear_streak >= params.clear_hold:
        if params.flap_max > 0 and len(s.transitions) + 1 >= params.flap_max:
            s.flapping = True
            return Decision("flap_hold", s)
        s.transitions.append(_iso(now))
        s.flapping = False
        return Decision("resolve", s)
    s.flapping = params.flap_max > 0 and len(s.transitions) >= params.flap_max
    return Decision("none", s)


# ── ClickHouse evaluation ────────────────────────────────────────────────────

def _eval_end(now: datetime, settle_s: int, slot_s: int = 60) -> datetime:
    return floor_to(now - timedelta(seconds=settle_s), slot_s)


def _exporter_only(f: FlowFilter) -> FlowFilter:
    exporter = f.exporter or (f.iface.exporter if f.iface is not None else None)
    return FlowFilter(exporter=exporter, count=f.count, counting=f.counting)


async def _with_counting(f: FlowFilter) -> FlowFilter:
    if f.count == "edge" and f.counting is None:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            f = replace(f, counting=await load_counting_policy(db))
    return f


async def exporter_receipts(f: FlowFilter, vis: FlowVisibility, start: datetime, end: datetime,
                            *, label: str = "nf-alert-receipts") -> dict[str, dict]:
    """{exporter: {last_received_at, records}} for exporters that delivered
    anything in ``[start, end)`` by the appliance receive clock (half-open)."""
    ef = _exporter_only(f)
    where, params = compile_filter(ef, EXPORTER_MINUTES, vis)
    params.update({"nf_rc_from": floor_to(start, 60), "nf_rc_to": end})
    res = await run_query(
        f"SELECT {display(EXPORTER)} AS e, max(last_received_at) AS last, sum(records) AS n "
        f"FROM {table_ref(EXPORTER_MINUTES)} "
        f"WHERE minute >= %(nf_rc_from)s AND minute < %(nf_rc_to)s AND {where} GROUP BY e",
        params, budget_s=BUDGET_BACKGROUND, label=label)
    out = {}
    for e, last, n in res.result_rows:
        ip = canonical_ip(e)
        if ip:
            out[ip] = {"last_received_at": last, "records": int(n or 0)}
    return out


async def traffic_snapshot(f: FlowFilter, vis: FlowVisibility, start: datetime, end: datetime,
                           *, limit: int = 10) -> dict:
    """Top conversations and applications in the window, with whole-scope
    shares (the evidence attached to a traffic alert)."""
    from app.services import netflow_classify

    where, params = compile_filter(f, RAW, vis)
    params.update({"nf_s_from": start, "nf_s_to": end, "nf_s_limit": limit})
    base = (f"FROM {table_ref(RAW)} WHERE timestamp >= %(nf_s_from)s AND timestamp < %(nf_s_to)s "
            f"AND {where}")
    app_expr = netflow_classify.application_sql(
        "src_port", "dst_port", "protocol", app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    total_q = run_query(f"SELECT sum(bytes), count() {base}", params,
                        budget_s=BUDGET_BACKGROUND, label="nf-alert-snap-total")
    talk_q = run_query(
        f"SELECT {display(SRC)} AS s, {display(DST)} AS d, protocol, dst_port, sum(bytes) AS b, count() AS n "
        f"{base} GROUP BY {SRC}, {DST}, protocol, dst_port ORDER BY b DESC LIMIT %(nf_s_limit)s",
        params, budget_s=BUDGET_BACKGROUND, label="nf-alert-snap-talkers")
    app_q = run_query(
        f"SELECT {app_expr} AS a, sum(bytes) AS b {base} GROUP BY a ORDER BY b DESC LIMIT 5",
        params, budget_s=BUDGET_BACKGROUND, label="nf-alert-snap-apps")
    total_r, talk_r, app_r = await asyncio.gather(total_q, talk_q, app_q)
    total = int(total_r.result_rows[0][0] or 0) if total_r.result_rows else 0

    def share(b):
        return round(b / total * 100, 1) if total else None

    talkers = [{"src": s, "dst": d, "protocol": int(p), "dst_port": int(dp), "bytes": int(b),
                "flows": int(n), "share_pct": share(int(b))} for s, d, p, dp, b, n in talk_r.result_rows]
    apps = [{"application": a or "Unclassified", "bytes": int(b), "share_pct": share(int(b))}
            for a, b in app_r.result_rows]
    return {"window": {"from": _iso(start), "to": _iso(end)}, "total_bytes": total,
            "total_flows": int(total_r.result_rows[0][1] or 0) if total_r.result_rows else 0,
            "talkers": talkers, "applications": apps,
            "denominator": "all traffic in the alert scope and window"}


async def _eval_traffic(rule: NetflowRule, vis: FlowVisibility, now: datetime,
                        tracked: Iterable[str]) -> Evaluation:
    p = rule.params
    end = _eval_end(now, p.settle_s)
    start = end - timedelta(minutes=p.window_min)
    f = await _with_counting(rule.filter)
    ev = Evaluation(rule.id, rule.metric, "ok", window_from=start, window_to=end)
    receipts = await exporter_receipts(f, vis, start, now)
    where, params = compile_filter(f, RAW, vis)
    params.update({"nf_a_from": start, "nf_a_to": end})
    split = p.split == "exporter"
    key = f"{display(EXPORTER)}" if split else "''"
    group = f" GROUP BY {EXPORTER}" if split else ""
    res = await run_query(
        f"SELECT {key} AS k, sum(bytes) AS b, count() AS n FROM {table_ref(RAW)} "
        f"WHERE timestamp >= %(nf_a_from)s AND timestamp < %(nf_a_to)s AND {where}{group}",
        params, budget_s=BUDGET_BACKGROUND, label="nf-alert-traffic")
    seconds = p.window_min * 60
    scope_label = describe_scope(rule.scope)
    if not split:
        row = res.result_rows[0] if res.result_rows else ("", 0, 0)
        if not receipts:
            ev.readings.append(Reading("all", scope_label, None, "no_coverage",
                                       exporter=rule.filter.exporter))
            ev.status = "no_data"
        else:
            b = int(row[1] or 0)
            ev.readings.append(Reading("all", scope_label, b * 8 / seconds, exporter=rule.filter.exporter,
                                       detail={"bytes": b, "flows": int(row[2] or 0),
                                               "exporters_receiving": len(receipts)}))
        return ev
    by_exporter = {canonical_ip(k): (int(b or 0), int(n or 0)) for k, b, n in res.result_rows}
    subjects = set(receipts) | {s.split(":", 1)[1] for s in tracked if s.startswith("exporter:")}
    for exp in sorted(x for x in subjects if x):
        subj = f"exporter:{exp}"
        if exp not in receipts:
            ev.readings.append(Reading(subj, exp, None, "no_coverage", exporter=exp))
            continue
        b, n = by_exporter.get(exp, (0, 0))
        ev.readings.append(Reading(subj, exp, b * 8 / seconds, exporter=exp,
                                   detail={"bytes": b, "flows": n}))
    if not receipts:
        ev.status = "no_data"
    return ev


def _endpoint_host_expr(side: str) -> str:
    if side == "src":
        return SRC
    if side == "dst":
        return DST
    return f"arrayJoin([{SRC}, {DST}])"


async def _eval_endpoint(rule: NetflowRule, vis: FlowVisibility, now: datetime,
                         tracked: Iterable[str]) -> Evaluation:
    p = rule.params
    end = _eval_end(now, p.settle_s)
    start = end - timedelta(minutes=p.window_min)
    f = await _with_counting(rule.filter)
    ev = Evaluation(rule.id, rule.metric, "ok", window_from=start, window_to=end)
    receipts = await exporter_receipts(f, vis, start, now)
    win = Window(start=start, end=end, mode="fixed")
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H, record=False,
                             min_rollup_span=timedelta(hours=3))
    host = _endpoint_host_expr(p.side)
    sub, params = plan.from_sql({"h": host, "e": EXPORTER, "b": "bytes"}, f, vis)
    fetch = max(200, p.max_subjects * 4)
    params["nf_ep_limit"] = fetch
    res = await run_query(
        "SELECT " + display("h") + " AS host, sum(eb) AS total, argMax(" + display("e") + ", eb) AS top_e "
        f"FROM (SELECT h, e, sum(b) AS eb FROM {sub} AS src GROUP BY h, e) "
        "GROUP BY h ORDER BY total DESC LIMIT %(nf_ep_limit)s",
        params, budget_s=BUDGET_BACKGROUND, label="nf-alert-endpoint")
    rows = [(canonical_ip(h), int(t or 0), canonical_ip(e)) for h, t, e in res.result_rows]
    rows = [r for r in rows if r[0]]
    if not receipts:
        ev.status = "no_data"
        for subj in tracked:
            if subj.startswith("host:"):
                ev.readings.append(Reading(subj, subj[5:], None, "no_coverage"))
        return ev
    complete = len(res.result_rows) < fetch
    floor_value = rows[-1][1] if rows else 0
    seen = set()
    breaching = 0
    for h, total, e in rows:
        subj = f"host:{h}"
        is_breach = breaches(rule, value=float(total))
        if is_breach:
            breaching += 1
            if breaching > p.max_subjects:
                ev.truncated = True
                continue
        elif subj not in tracked:
            continue
        seen.add(subj)
        ev.readings.append(Reading(subj, h, float(total), exporter=e,
                                   detail={"bytes": total, "side": p.side,
                                           "window_min": p.window_min}))
    for subj in tracked:
        if not subj.startswith("host:") or subj in seen:
            continue
        if complete:
            # Every host with traffic was fetched: this one moved nothing.
            ev.readings.append(Reading(subj, subj[5:], 0.0, detail={"bytes": 0}))
        elif not breaches(rule, value=float(floor_value)):
            # Not in the ranked list, whose smallest entry is already below the
            # threshold: this host is below it too (value is an upper bound).
            ev.readings.append(Reading(subj, subj[5:], float(floor_value),
                                       detail={"upper_bound": True, "bytes_at_most": floor_value}))
        else:
            ev.readings.append(Reading(subj, subj[5:], None, "not_ranked"))
    return ev


async def _excluded_exporters(db: AsyncSession) -> set[str]:
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT host(exporter_ip) FROM netflow_exporter_settings WHERE role = 'excluded'"))).all()
    except Exception:
        return set()
    return {canonical_ip(r[0]) for r in rows if canonical_ip(r[0])}


async def exporter_silence(f: FlowFilter, vis: FlowVisibility, now: datetime, lookback_h: int,
                           *, label: str = "nf-alert-silent") -> dict[str, dict]:
    """{exporter: {last_received_at, receipt_age_s}} for every exporter that
    delivered anything in the lookback (appliance receive clock)."""
    start = now - timedelta(hours=lookback_h)
    rec = await exporter_receipts(f, vis, start, now + timedelta(minutes=1), label=label)
    out = {}
    for exp, r in rec.items():
        last = r["last_received_at"]
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - last).total_seconds())
        else:
            age = None
        out[exp] = {"last_received_at": last, "receipt_age_s": age, "records": r["records"]}
    return out


async def _eval_silent(rule: NetflowRule, vis: FlowVisibility, now: datetime,
                       tracked: Iterable[str], db: AsyncSession | None = None) -> Evaluation:
    p = rule.params
    ev = Evaluation(rule.id, rule.metric, "ok", window_from=now - timedelta(hours=p.lookback_h), window_to=now)
    status = await exporter_silence(rule.filter, vis, now, p.lookback_h)
    excluded = await _excluded_exporters(db) if db is not None else set()
    names = await _exporter_names(db, list(status)) if db is not None else {}
    subjects = set(status) | {s.split(":", 1)[1] for s in tracked if s.startswith("exporter:")}
    silent = 0
    for exp in sorted(x for x in subjects if x):
        if exp in excluded:
            continue
        subj = f"exporter:{exp}"
        info = status.get(exp)
        label = f"{names[exp]} ({exp})" if names.get(exp) else exp
        if info is None:
            # Tracked but nothing received in the whole lookback: silent at least that long.
            age = float(p.lookback_h * 3600)
            ev.readings.append(Reading(subj, label, age, exporter=exp,
                                       detail={"last_received_at": None, "beyond_lookback": True}))
            silent += 1
            continue
        age = info["receipt_age_s"]
        ev.readings.append(Reading(subj, label, age, "unknown_clock" if age is None else None, exporter=exp,
                                   detail={"last_received_at": _iso(info["last_received_at"])}))
        if age is not None and breaches(rule, value=age):
            silent += 1
    if ev.readings and silent == len(ev.readings) and len(ev.readings) > 1:
        for r in ev.readings:
            r.detail["all_silent"] = True
    if not ev.readings:
        ev.status = "no_data"
    return ev


async def _exporter_names(db: AsyncSession | None, exporters: list[str]) -> dict[str, str]:
    if db is None or not exporters:
        return {}
    try:
        owners = await resolve_exporter_owners(db, exporters)
    except Exception:
        return {}
    return {ip: o.hostname for ip, o in owners.items() if o.hostname}


async def interface_speeds(db: AsyncSession, keys: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """{(exporter, ifindex): {in_bps, out_bps, source_in, source_out, name}} by
    the one speed rule (configured, else NetFlow override, else SNMP)."""
    from app.services.interface_speed import effective_speed_source

    keys = list({(canonical_ip(e), int(i)) for e, i in keys if canonical_ip(e)})
    if not keys:
        return {}
    owners = await resolve_exporter_owners(db, [k[0] for k in keys])
    out: dict[tuple[str, int], dict] = {}
    polled: dict[tuple[str, int], tuple] = {}
    by_device: dict[str, list[tuple[str, int]]] = {}
    for exp, idx in keys:
        o = owners.get(exp)
        if o:
            by_device.setdefault(o.device_id, []).append((exp, idx))
    for did, items in by_device.items():
        rows = (await db.execute(text(
            "SELECT if_index, configured_speed_bps, if_speed, COALESCE(NULLIF(if_alias, ''), if_name, if_descr) "
            "FROM device_interfaces WHERE device_id = :d AND if_index = ANY(:idx)"
        ), {"d": did, "idx": [i for _, i in items]})).all()
        byidx = {int(r[0]): r for r in rows}
        for exp, idx in items:
            if idx in byidx:
                polled[(exp, idx)] = byidx[idx]
    overrides: dict[tuple[str, int], tuple] = {}
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT host(exporter_ip), ifindex, speed_in_bps, speed_out_bps, display_name, role "
                "FROM netflow_interface_overrides WHERE host(exporter_ip) = ANY(:e)"
            ), {"e": sorted({k[0] for k in keys})})).all()
        overrides = {(canonical_ip(r[0]), int(r[1])): r for r in rows}
    except Exception:
        overrides = {}
    for k in keys:
        pol = polled.get(k)
        ov = overrides.get(k)
        configured = pol[1] if pol else None
        snmp = pol[2] if pol else None
        in_bps, src_in = effective_speed_source(configured, ov[2] if ov else None, snmp)
        out_bps, src_out = effective_speed_source(configured, ov[3] if ov else None, snmp)
        out[k] = {"in_bps": in_bps, "out_bps": out_bps, "source_in": src_in, "source_out": src_out,
                  "name": (ov[4] if ov and ov[4] else None) or (pol[3] if pol else None),
                  "role": ov[5] if ov else None}
    return out


async def _capacity_utilisation(rule: NetflowRule, vis: FlowVisibility, start: datetime, end: datetime,
                                db: AsyncSession) -> list[dict] | None:
    """Per-interface, per-direction average rate and speed over the window
    from the capacity service (B4 ``compute_capacity``: the one capacity
    definition -- direction de-duplication, collection gaps, the speed rule).
    None when it is unavailable (the rollup read below is used instead)."""
    try:
        from app.services import netflow_capacity as cap
    except ImportError:
        return None
    fn = getattr(cap, "compute_capacity", None)
    if fn is None:
        return None
    f = rule.filter
    try:
        unsupported = cap.unsupported_keys(f) if hasattr(cap, "unsupported_keys") else frozenset()
        if unsupported:
            f = f.without(*unsupported)
        result = await fn(Window(start=start, end=end, mode="fixed"), f, vis, db, record_meta=False)
    except Exception:
        logger.warning("netflow alert: capacity service failed; reading the interface rollup instead",
                       exc_info=True)
        return None
    out = []
    for row in result.rows:
        speed = row.get("speed") or {}
        for d in ("in", "out"):
            dd = row.get(d) or {}
            out.append({
                "exporter": row.get("exporter"), "ifindex": row.get("ifindex"), "direction": d,
                "bps": dd.get("avg_bps"), "speed_bps": dd.get("speed_bps"),
                "speed_source": speed.get(f"source_{d}"),
                "name": row.get("display_name") or row.get("if_name") or row.get("if_descr"),
                "device": row.get("device_hostname"), "samples": row.get("samples"),
                "expected": row.get("expected_slots"), "role": row.get("role"),
            })
    return out


def _dirs(direction: str) -> tuple[int, ...]:
    return (0, 1) if direction == "either" else ((0,) if direction == "in" else (1,))


async def _eval_util(rule: NetflowRule, vis: FlowVisibility, now: datetime, tracked: Iterable[str],
                     db: AsyncSession) -> Evaluation:
    p = rule.params
    end = _eval_end(now, p.settle_s, 300)
    start = end - timedelta(minutes=p.window_min)
    ev = Evaluation(rule.id, rule.metric, "ok", window_from=start, window_to=end)
    receipts = await exporter_receipts(rule.filter, vis, start, now)
    wanted = _dirs(p.direction)
    seconds = p.window_min * 60
    # (exporter, ifindex, direction 0/1) -> {bps, speed_bps, speed_source, name, device, role, samples}
    found: dict[tuple[str, int, int], dict] = {}
    rows = await _capacity_utilisation(rule, vis, start, end, db)
    if rows is not None:
        ev.reason = None
        for r in rows:
            exp = canonical_ip(r.get("exporter"))
            d = 0 if r.get("direction") == "in" else 1
            if exp is None or d not in wanted:
                continue
            if rule.filter.iface is not None and int(r["ifindex"]) != rule.filter.iface.ifindex:
                continue
            found[(exp, int(r["ifindex"]), d)] = r
        source = "capacity"
    else:
        f = rule.filter
        where, params = compile_filter(f, INTERFACE_5M, vis)
        params.update({"nf_u_from": start, "nf_u_to": end})
        res = await run_query(
            f"SELECT {display(EXPORTER)} AS e, ifindex, direction, sum(bytes) AS b "
            f"FROM {table_ref(INTERFACE_5M)} WHERE timestamp >= %(nf_u_from)s AND timestamp < %(nf_u_to)s "
            f"AND ifindex != 0 AND {where} GROUP BY {EXPORTER}, ifindex, direction",
            params, budget_s=BUDGET_BACKGROUND, label="nf-alert-util")
        vols = {}
        for e, idx, d, b in res.result_rows:
            exp = canonical_ip(e)
            if exp and int(d) in wanted:
                vols[(exp, int(idx), int(d))] = float(b or 0) * 8 / seconds
        speeds = await interface_speeds(db, [(k[0], k[1]) for k in vols])
        for (exp, idx, d), bps in vols.items():
            sp = speeds.get((exp, idx), {})
            found[(exp, idx, d)] = {"bps": bps, "speed_bps": sp.get("in_bps" if d == 0 else "out_bps"),
                                    "speed_source": sp.get("source_in" if d == 0 else "source_out"),
                                    "name": sp.get("name"), "role": sp.get("role")}
        source = "interface_rollup"
    keys = set(found)
    for subj in tracked:
        if subj.startswith("if:"):
            try:
                exp, idx, dname = subj[3:].rsplit(":", 2)
                keys.add((canonical_ip(exp), int(idx), 0 if dname == "in" else 1))
            except (ValueError, TypeError):
                continue
    if rule.filter.iface is not None:
        exp = rule.filter.iface.exporter or rule.filter.exporter
        if exp:
            for d in wanted:
                keys.add((canonical_ip(exp), rule.filter.iface.ifindex, d))
    missing = [k for k in keys if k[0] and k not in found]
    extra_speeds = await interface_speeds(db, [(k[0], k[1]) for k in missing]) if missing else {}
    names = await _exporter_names(db, sorted({k[0] for k in keys if k[0]}))
    for exp, idx, d in sorted(k for k in keys if k[0]):
        dname = "in" if d == 0 else "out"
        subj = f"if:{exp}:{idx}:{dname}"
        info = found.get((exp, idx, d))
        if info is None:
            sp = extra_speeds.get((exp, idx), {})
            info = {"bps": 0.0, "speed_bps": sp.get("in_bps" if d == 0 else "out_bps"),
                    "speed_source": sp.get("source_in" if d == 0 else "source_out"),
                    "name": sp.get("name"), "role": sp.get("role"), "idle": True}
        if info.get("role") == "ignore":
            continue
        ifname = info.get("name") or f"ifIndex {idx}"
        dev = info.get("device") or names.get(exp) or exp
        label = f"{dev} / {ifname} ({dname})"
        detail = {"speed_bps": info.get("speed_bps"), "speed_source": info.get("speed_source"),
                  "if_name": ifname, "bps": info.get("bps"), "source": source}
        common = dict(exporter=exp, if_index=idx, direction=dname, detail=detail)
        if exp not in receipts or (info.get("samples") == 0):
            ev.readings.append(Reading(subj, label, None, "no_coverage", **common))
        elif not info.get("speed_bps"):
            ev.readings.append(Reading(subj, label, None, "speed_unknown", **common))
        elif info.get("bps") is None:
            ev.readings.append(Reading(subj, label, None, "no_samples", **common))
        else:
            ev.readings.append(Reading(subj, label, float(info["bps"]) / float(info["speed_bps"]) * 100,
                                       **common))
    if not receipts:
        ev.status = "no_data"
    return ev


_FINDING_SQL = """
    SELECT id::text, kind, severity, confidence, status, title, description,
           host(src_ip), host(dst_ip), host(exporter_ip), first_seen, last_seen, occurrences,
           alert_id::text
    FROM netflow_findings
    WHERE (status = 'open' AND last_seen >= :since) OR id::text = ANY(:tracked)
"""


def finding_matches(rule: NetflowRule, kind: str | None, severity: str | None, confidence: str | None) -> bool:
    p = rule.params
    if p.kinds and (kind or "") not in p.kinds:
        return False
    if p.min_severity and _SEVERITY_RANK.get((severity or "").lower(), -1) < _SEVERITY_RANK[p.min_severity]:
        return False
    if p.min_confidence and _CONFIDENCE_RANK.get((confidence or "").lower(), -1) < _CONFIDENCE_RANK[p.min_confidence]:
        return False
    return True


def _in_net(ip: str | None, value) -> bool:
    if not ip or value is None:
        return False
    try:
        return ipaddress.ip_address(ip) in value.network
    except (ValueError, AttributeError):
        return False


async def _eval_finding(rule: NetflowRule, vis: FlowVisibility, now: datetime, tracked: Iterable[str],
                        db: AsyncSession) -> Evaluation:
    p = rule.params
    tracked_ids = [s.split(":", 1)[1] for s in tracked if s.startswith("finding:")]
    ev = Evaluation(rule.id, rule.metric, "ok", window_from=now - timedelta(hours=p.max_age_h), window_to=now)
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(_FINDING_SQL), {
                "since": now - timedelta(hours=p.max_age_h), "tracked": tracked_ids})).all()
    except Exception:
        ev.status = "failed"
        ev.reason = "findings_unavailable"
        return ev
    f = rule.filter
    for r in rows:
        (fid, kind, sev, conf, status, title, desc, src, dst, exp, first, last, occ, alert_id) = r
        exp = canonical_ip(exp)
        subj = f"finding:{fid}"
        if exp and not vis.allows(exp):
            continue
        if exp is None and vis.restricted:
            continue
        in_scope = finding_matches(rule, kind, sev, conf)
        if f.exporter and exp != canonical_ip(f.exporter):
            in_scope = False
        if f.talker is not None and not (_in_net(src, f.talker) or _in_net(dst, f.talker)):
            in_scope = False
        if not in_scope and subj not in tracked:
            continue
        # open (and still in scope) = breach; resolved / suppressed = clear;
        # acknowledged keeps the platform alert open (people are on it).
        if status == "open" and in_scope:
            value = 1.0
        elif status == "acknowledged":
            value = 1.0
        else:
            value = 0.0
        label = title or f"{kind or 'finding'} {src or ''}".strip()
        ev.readings.append(Reading(subj, label, value, exporter=exp, detail={
            "finding_id": fid, "kind": kind, "severity": sev, "confidence": conf, "status": status,
            "description": desc, "src_ip": src, "dst_ip": dst, "first_seen": _iso(first),
            "last_seen": _iso(last), "occurrences": occ, "finding_alert_id": alert_id}))
    return ev


async def evaluate(rule: NetflowRule, *, vis: FlowVisibility = UNRESTRICTED_SYSTEM,
                   now: datetime | None = None, tracked: Iterable[str] = (),
                   db: AsyncSession | None = None) -> Evaluation:
    """Evaluate one rule once (no state, no alerts). Failures come back as
    ``status='failed'`` with the error id; nothing raises."""
    now = now or datetime.now(timezone.utc)
    tracked = set(tracked)
    try:
        if rule.metric == TRAFFIC:
            return await _eval_traffic(rule, vis, now, tracked)
        if rule.metric == ENDPOINT:
            return await _eval_endpoint(rule, vis, now, tracked)
        if rule.metric == SILENT:
            return await _eval_silent(rule, vis, now, tracked, db)
        if rule.metric == UTIL:
            if db is None:
                from app.core.database import AsyncSessionLocal

                async with AsyncSessionLocal() as own:
                    return await _eval_util(rule, vis, now, tracked, own)
            return await _eval_util(rule, vis, now, tracked, db)
        if rule.metric == FINDING:
            if db is None:
                from app.core.database import AsyncSessionLocal

                async with AsyncSessionLocal() as own:
                    return await _eval_finding(rule, vis, now, tracked, own)
            return await _eval_finding(rule, vis, now, tracked, db)
    except NetflowError as exc:
        logger.warning("netflow alert rule %s (%s) evaluation failed: %s", rule.id, rule.metric, exc.detail)
        return Evaluation(rule.id, rule.metric, "failed", reason=exc.code, error_id=exc.error_id)
    except Exception:
        logger.exception("netflow alert rule %s (%s) evaluation failed", rule.id, rule.metric)
        return Evaluation(rule.id, rule.metric, "failed", reason="evaluation_error")
    return Evaluation(rule.id, rule.metric, "skipped", reason="unknown_metric")


# ── PostgreSQL state ─────────────────────────────────────────────────────────

async def load_states(db: AsyncSession, rule_id: str) -> dict[str, SubjectState]:
    rows = (await db.execute(text(
        "SELECT subject, state, breach_streak, clear_streak, transitions, flapping, last_value, "
        "last_reason, context FROM netflow_alert_state WHERE rule_id = :r"), {"r": rule_id})).all()
    out = {}
    for r in rows:
        out[r[0]] = SubjectState(
            state=r[1], breach_streak=int(r[2] or 0), clear_streak=int(r[3] or 0),
            transitions=list(_jsonish(r[4]) or []), flapping=bool(r[5]),
            last_value=r[6], last_reason=r[7], context=dict(_jsonish(r[8]) or {}))
    return out


async def save_state(db: AsyncSession, rule_id: str, subject: str, s: SubjectState, now: datetime) -> None:
    await db.execute(text("""
        INSERT INTO netflow_alert_state
            (rule_id, subject, state, breach_streak, clear_streak, last_value, last_reason,
             last_eval_at, transitions, flapping, context, updated_at)
        VALUES (:r, :s, :st, :bs, :cs, :v, :why, :now, CAST(:tr AS jsonb), :fl, CAST(:ctx AS jsonb), :now)
        ON CONFLICT (rule_id, subject) DO UPDATE SET
            state = EXCLUDED.state, breach_streak = EXCLUDED.breach_streak,
            clear_streak = EXCLUDED.clear_streak, last_value = EXCLUDED.last_value,
            last_reason = EXCLUDED.last_reason, last_eval_at = EXCLUDED.last_eval_at,
            transitions = EXCLUDED.transitions, flapping = EXCLUDED.flapping,
            context = EXCLUDED.context, updated_at = EXCLUDED.updated_at
    """), {"r": rule_id, "s": subject[:200], "st": s.state, "bs": s.breach_streak, "cs": s.clear_streak,
           "v": s.last_value, "why": (s.last_reason or None) and s.last_reason[:64], "now": now,
           "tr": json.dumps(s.transitions[-50:]), "fl": s.flapping, "ctx": json.dumps(s.context, default=str)})


async def open_alerts(db: AsyncSession, rule_id: str) -> dict[str, dict]:
    rows = (await db.execute(text(
        "SELECT id, metadata->>'netflow_subject', triggered_at, metadata->>'notified', "
        "COALESCE(metadata->>'flapping', 'false'), device_id, "
        "COALESCE(metadata->>'cooldown_suppressed', 'false') FROM alerts "
        "WHERE rule_id = :r AND status IN ('pending', 'active', 'acknowledged') "
        "AND metadata->>'netflow_dedupe' IS NOT NULL"), {"r": rule_id})).all()
    return {r[1]: {"id": r[0], "triggered_at": r[2], "notified": r[3], "flapping": r[4] == "true",
                   "device_id": str(r[5]) if r[5] else None, "cooldown_suppressed": r[6] == "true"}
            for r in rows if r[1]}


async def reset_rule(db: AsyncSession, rule_id: str, *, reason: str = "rule_changed") -> int:
    """Forget a rule's evaluation state and close its open NetFlow alerts
    (administratively) -- after its scope, metric or parameters changed, the
    old subjects no longer mean anything. Returns how many alerts closed."""
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW(), "
        "metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object("
        "'resolved_by', 'migration', 'resolution', CAST(:why AS text)) "
        "WHERE rule_id = :r AND status IN ('pending', 'active', 'acknowledged') "
        "AND metadata->>'netflow_dedupe' IS NOT NULL"), {"r": rule_id, "why": reason})
    await db.execute(text("DELETE FROM netflow_alert_state WHERE rule_id = :r"), {"r": rule_id})
    return res.rowcount or 0


# ── Alerts and notifications ─────────────────────────────────────────────────

def condition_text(rule: NetflowRule) -> str:
    """"above 1 Gbps", "outside 20–80%", "silent for more than 10 min"."""
    thr = rule.threshold
    if rule.metric == FINDING:
        parts = []
        if rule.params.kinds:
            parts.append(", ".join(rule.params.kinds))
        if rule.params.min_severity:
            parts.append(f"severity ≥ {rule.params.min_severity}")
        if rule.params.min_confidence:
            parts.append(f"confidence ≥ {rule.params.min_confidence}")
        return "open finding" + (f" ({'; '.join(parts)})" if parts else "")
    fmt = lambda v: format_reading(rule.metric, v)  # noqa: E731
    op = rule.operator
    if op == "!=":
        return f"outside {fmt(thr)} – {fmt(rule.params.band_high)}"
    words = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below", "==": "equal to"}
    if rule.metric == SILENT:
        return f"silent for more than {fmt(thr)}"
    return f"{words.get(op, 'above')} {fmt(thr)}"


def event_sentence(rule: NetflowRule, reading: Reading, *, recovered: bool = False,
                   duration: str = "") -> str:
    """The one sentence an alert (or its all-clear) says. How long it lasted
    is added by the templates' ``{duration_sentence}``, not here."""
    val = format_reading(rule.metric, reading.value)
    cond = condition_text(rule)
    tail = f"no longer {cond}" if recovered else cond
    who = reading.label
    p = rule.params
    if rule.metric == TRAFFIC:
        return f"Flow traffic for {who} is {val} averaged over {p.window_min} min, {tail}."
    if rule.metric == ENDPOINT:
        side = {"src": "sent", "dst": "received", "any": "moved"}[p.side]
        bound = "at most " if reading.detail.get("upper_bound") else ""
        return f"Host {who} {side} {bound}{val} in {p.window_min} min, {tail}."
    if rule.metric == SILENT:
        if recovered:
            return f"Exporter {who} is delivering flow records again."
        text_ = (f"Exporter {who} has delivered no flow records for {val} "
                 f"(threshold {format_reading(SILENT, rule.threshold)}). "
                 "This is flow-export silence; it does not mean the device is down.")
        if reading.detail.get("all_silent"):
            text_ += " Every exporter in scope is silent at once: check the flow collector."
        return text_
    if rule.metric == UTIL:
        return (f"Interface {who} is at {val} of {fmt_bps(reading.detail.get('speed_bps'))} "
                f"({fmt_bps(reading.detail.get('bps'))} over {p.window_min} min), {tail}.")
    d = reading.detail
    if recovered:
        return f"NetFlow finding “{who}” is {d.get('status') or 'closed'}."
    return (f"NetFlow finding “{who}” ({d.get('kind')}, severity {d.get('severity')}, "
            f"confidence {d.get('confidence')}) is open.")


def _scope_subject(rule: NetflowRule, reading: Reading) -> str:
    return reading.label or describe_scope(rule.scope)


async def _owner_device(db: AsyncSession, exporter: str | None) -> tuple[str | None, str | None]:
    if not exporter:
        return None, None
    try:
        owners = await resolve_exporter_owners(db, [exporter])
    except Exception:
        return None, None
    o = owners.get(canonical_ip(exporter) or "")
    return (o.device_id, o.hostname) if o else (None, None)


async def _silenced(db: AsyncSession, rule_id: str, device_id: str | None, if_index: int | None) -> bool:
    if not device_id:
        return False
    dedupe = f"rule:{rule_id}" + (f":if:{if_index}" if if_index is not None else "")
    row = (await db.execute(text(
        "SELECT 1 FROM alert_silences WHERE device_id = :d AND dedupe = :k "
        "AND (until IS NULL OR until > NOW()) LIMIT 1"), {"d": device_id, "k": dedupe})).first()
    return row is not None


async def _in_maintenance(db: AsyncSession, device_id: str | None) -> bool:
    if not device_id:
        return False
    from app.api.v1.alert_engine import _device_in_maintenance

    return await _device_in_maintenance(db, device_id)


async def _within_cooldown(db: AsyncSession, rule: NetflowRule, dedupe: str, exclude_id) -> bool:
    if rule.cooldown <= 0:
        return False
    row = (await db.execute(text(
        "SELECT 1 FROM alerts WHERE rule_id = :r AND metadata->>'netflow_dedupe' = :k AND id <> :x "
        "AND COALESCE(metadata->>'notified', 'true') = 'true' "
        "AND triggered_at > now() - make_interval(secs => :cd) LIMIT 1"),
        {"r": rule.id, "k": dedupe, "x": exclude_id, "cd": rule.cooldown})).first()
    return row is not None


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


async def notify(db: AsyncSession, rule: NetflowRule, reading: Reading, *, alert_id, is_recovery: bool,
                 duration: str = "", link: str | None = None, snapshot: dict | None = None) -> bool:
    """Send a trigger (or all-clear) through the rule's channels in the house
    style. True when it was dispatched (quiet hours say no)."""
    from app.services.alert_schedule import get_configured_timezone, notifications_allowed
    from app.services import alert_funnel
    from app.services.host_alert_service import dispatch_to_channels

    if not is_recovery:
        tz = await get_configured_timezone(db)
        if not notifications_allowed(rule.schedule_start, rule.schedule_end, rule.schedule_days, tz):
            return False
    if not rule.notify_channels:
        return True
    sev = rule.severity or "warning"
    reading_text = format_reading(rule.metric, reading.value)
    subject_noun = _scope_subject(rule, reading)
    v = ap.rule_phrasing({"metric": rule.metric, "operator": rule.operator, "threshold": rule.threshold},
                         hostname=subject_noun, is_recovery=is_recovery,
                         reading=None if rule.metric == FINDING else reading_text, duration=duration)
    sentence = event_sentence(rule, reading, recovered=is_recovery, duration=duration)
    v.update({"event_sentence": sentence, "condition": condition_text(rule),
              "condition_label": condition_text(rule), "rule_name": rule.name, "hostname": subject_noun,
              "ip_address": reading.exporter or "", "severity": sev.upper(),
              "status": "RESOLVED" if is_recovery else "ALERT",
              "timestamp": datetime.now(timezone.utc).strftime("%H:%M UTC on %d %b %Y")})
    if is_recovery:
        subject_tpl = ap.effective_template(rule.templates.get("recovery_email_subject"),
                                            ap.DEFAULT_RECOVERY_EMAIL_SUBJECT)
        body_tpl = ap.effective_template(rule.templates.get("recovery_email_body"), ap.DEFAULT_RECOVERY_EMAIL_BODY)
        sms_tpl = ap.effective_template(rule.templates.get("recovery_sms_template"), ap.DEFAULT_RECOVERY_SMS)
    else:
        subject_tpl = ap.effective_template(rule.templates.get("email_subject"), ap.DEFAULT_EMAIL_SUBJECT)
        body_tpl = ap.effective_template(rule.templates.get("email_body"), ap.DEFAULT_EMAIL_BODY)
        sms_tpl = ap.effective_template(rule.templates.get("sms_template"), ap.DEFAULT_SMS)
    details: list = [("Alert rule", rule.name), ("Condition", condition_text(rule)),
                     ("Flow scope", describe_scope(rule.scope)), ("Measured", reading_text),
                     ("Active for", duration if is_recovery else None)]
    if rule.metric in (TRAFFIC, ENDPOINT, UTIL):
        details.append(("Window", f"{rule.params.window_min} min, closed minutes"))
    if snapshot and snapshot.get("talkers") and not is_recovery:
        top = snapshot["talkers"][:3]
        details.append(("Top talkers", "; ".join(
            f"{t['src']} → {t['dst']}:{t['dst_port']} {fmt_bytes(t['bytes'])}"
            + (f" ({t['share_pct']}%)" if t.get("share_pct") is not None else "") for t in top)))
    action_url = ""
    try:
        from app.api.v1.alert_engine import _dashboard_url

        action_url = await _dashboard_url(db, link or "/alerts")
    except Exception:
        action_url = ""
    headline = None
    if rule.metric != FINDING and reading.value is not None:
        headline = {"label": METRIC_INFO[rule.metric]["label"], "value": reading_text,
                    "secondary_label": "Condition", "secondary_value": condition_text(rule)}
    sent = await dispatch_to_channels(db, list(rule.notify_channels), {
        "subject": _render(subject_tpl, v), "body": _render(body_tpl, v), "message": _render(sms_tpl, v),
        "hostname": subject_noun, "ip_address": reading.exporter or "",
        "status": "RESOLVED" if is_recovery else "ALERT", "severity": sev, "resolved": is_recovery,
        "headline_metric": headline, "details": details,
        "triggered_at": datetime.now(timezone.utc).isoformat(), "rule_id": rule.id, "rule_name": rule.name,
        "alert_id": str(alert_id) if alert_id else None, "is_recovery": is_recovery,
        # Keyed: one send per incident event, channel and recipient.
        "delivery_event": "recovery" if is_recovery else "trigger",
        **alert_funnel.notification_fields(sev, None, trigger=not is_recovery),
        "action_url": action_url,
    })
    logger.debug("netflow alert %s: %s channel(s) accepted", alert_id, sent)
    return True


def _metadata(rule: NetflowRule, reading: Reading, ev: Evaluation, *, device_hostname: str | None,
              link: str | None, snapshot: dict | None) -> dict:
    dedupe = f"{rule.metric}:{reading.subject}"
    meta = {
        "source": "netflow", "rule_id": rule.id, "metric": rule.metric,
        "netflow_dedupe": dedupe, "netflow_subject": reading.subject,
        "dedupe": f"rule:{rule.id}" + (f":if:{reading.if_index}" if reading.if_index is not None else ""),
        "subject_label": reading.label, "value": None if reading.value is None else round(reading.value, 4),
        "reading": format_reading(rule.metric, reading.value), "threshold": rule.threshold,
        "operator": rule.operator, "band_high": rule.params.band_high, "condition": condition_text(rule),
        "flow_scope": rule.scope, "flow_scope_label": describe_scope(rule.scope),
        "window": {"from": _iso(ev.window_from), "to": _iso(ev.window_to)},
        "exporter": reading.exporter, "device_hostname": device_hostname,
        "detail": reading.detail,
    }
    if reading.if_index is not None:
        meta["if_index"] = str(reading.if_index)
        meta["direction"] = reading.direction
    if link:
        meta["netflow_link"] = link
    if snapshot:
        meta["snapshot"] = snapshot
    return meta


# ── Redaction for callers without netflow.view (X-01) ───────────────────────
# A NetFlow alert's message and metadata carry flow data: rates, byte and
# flow counts, the top-talker snapshot (addresses, ports), host subjects and
# the flow scope. The Alert Center serves alerts on ``alerts.view``; a caller
# without ``netflow.view`` still sees that a NetFlow rule fired on the device
# (so it can be triaged), but none of the flow-derived values.

REDACTED_MESSAGE = "NetFlow alert — the details need the NetFlow view permission"
# Metadata keys that describe the alert's lifecycle or rule, not flows.
_REDACTION_KEEP = frozenset({
    "source", "rule_id", "metric", "dedupe", "operator", "threshold", "band_high", "condition",
    "notified", "flapping", "cooldown_suppressed", "resolved_by", "escalation", "suppression",
    "policy_snapshot", "hold_until", "hold_confirmed_at", "old_status", "new_status", "notice",
})


def is_netflow_alert(meta: Mapping | None) -> bool:
    return isinstance(meta, Mapping) and meta.get("source") == "netflow"


def redact_alert_metadata(meta: Mapping) -> dict:
    """NetFlow alert metadata without flow-derived values."""
    out = {k: v for k, v in meta.items() if k in _REDACTION_KEEP}
    out["netflow_redacted"] = True
    return out


def redact_alert_payload(payload: dict) -> dict:
    """An Alert Center alert payload (``alerts.serialize``) as a caller
    without netflow.view may see it; non-NetFlow alerts are unchanged.
    Also used for report alert rows (``report_service._fetch_alerts``),
    which carry ``message`` and the metadata ``source``."""
    meta = payload.get("metadata")
    if not is_netflow_alert(meta):
        return payload
    return {**payload, "message": REDACTED_MESSAGE, "metadata": redact_alert_metadata(meta)}


async def netflow_details_withheld(db: AsyncSession, user) -> bool:
    """Whether NetFlow alert details must be withheld from this user: it lacks
    netflow.view (X-01). No user (the appliance itself) sees everything. One
    rule for the Alert Center and every report surface that shows alert
    messages."""
    if user is None:
        return False
    from app.core.permissions import has_permission
    from app.core.security import get_role_permissions

    return not has_permission(await get_role_permissions(db, user.role), "netflow.view")


def _evidence_link(rule: NetflowRule, reading: Reading, ev: Evaluation) -> str | None:
    if ev.window_from is None or ev.window_to is None:
        return None
    scope = dict(rule.scope)
    if rule.metric == TRAFFIC and reading.exporter and "exporter" not in scope:
        scope["exporter"] = reading.exporter
    if rule.metric == ENDPOINT:
        return netflow_link(f"/netflow/hosts/{reading.label}", scope, ev.window_from, ev.window_to)
    if rule.metric == UTIL and reading.exporter and reading.if_index is not None:
        return netflow_link("/netflow/capacity", {"exporter": reading.exporter,
                                                 "iface": f"{reading.if_index}@{reading.exporter}"},
                            ev.window_from, ev.window_to)
    if rule.metric == SILENT:
        return "/netflow/health"
    if rule.metric == FINDING:
        return f"/netflow/anomalies?finding={reading.detail.get('finding_id')}"
    return netflow_link("/netflow", scope, ev.window_from, ev.window_to)


async def _raise(db: AsyncSession, rule: NetflowRule, reading: Reading, ev: Evaluation,
                 state: SubjectState, vis: FlowVisibility) -> tuple[str | None, str]:
    """Insert the alert; returns (alert id or None, outcome)."""
    device_id, hostname = await _owner_device(db, reading.exporter)
    if await _in_maintenance(db, device_id):
        return None, "maintenance"
    if await _silenced(db, rule.id, device_id, reading.if_index):
        return None, "silenced"
    link = _evidence_link(rule, reading, ev)
    snapshot = None
    if rule.metric == TRAFFIC and ev.window_from is not None:
        f = rule.filter
        if reading.exporter and not f.exporter:
            f = replace(f, exporter=reading.exporter)
        try:
            snapshot = await traffic_snapshot(await _with_counting(f), vis, ev.window_from, ev.window_to)
        except Exception:
            logger.warning("netflow alert: top-talker snapshot failed for rule %s", rule.id, exc_info=True)
            snapshot = {"error": "Top-talker snapshot unavailable"}
    meta = _metadata(rule, reading, ev, device_hostname=hostname, link=link, snapshot=snapshot)
    if state.flapping:
        meta["flapping"] = True
    message = event_sentence(rule, reading)
    row = (await db.execute(text(
        "INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata) "
        "VALUES (:d, :r, 'active', :sev, :msg, NOW(), CAST(:meta AS jsonb)) RETURNING id"),
        {"d": device_id, "r": rule.id, "sev": rule.severity if rule.severity in ("info", "warning", "critical")
         else "warning", "msg": message[:2000], "meta": json.dumps(meta, default=str)})).first()
    alert_id = row[0]
    if rule.metric == FINDING and reading.detail.get("finding_id") and not reading.detail.get("finding_alert_id"):
        try:
            async with db.begin_nested():
                await db.execute(text(
                    "UPDATE netflow_findings SET alert_id = :a WHERE id = CAST(:f AS uuid) AND alert_id IS NULL"),
                    {"a": alert_id, "f": reading.detail["finding_id"]})
        except Exception:
            logger.debug("netflow alert: could not link finding %s", reading.detail.get("finding_id"))
    state.context["last_alert_id"] = str(alert_id)
    state.context["link"] = link
    return alert_id, "raised"


async def _resolve(db: AsyncSession, alert_id) -> None:
    await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW(), "
        "metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('resolved_by', 'recovered') "
        "WHERE id = :id AND status IN ('pending', 'active', 'acknowledged')"), {"id": alert_id})


async def _mark_flapping(db: AsyncSession, alert_id) -> None:
    await db.execute(text(
        "UPDATE alerts SET metadata = COALESCE(metadata, '{}'::jsonb) || '{\"flapping\": true}'::jsonb "
        "WHERE id = :id"), {"id": alert_id})


@dataclass
class ApplyResult:
    raised: int = 0
    resolved: int = 0
    suppressed: int = 0
    evaluated: int = 0


async def apply_evaluation(db: AsyncSession, rule: NetflowRule, ev: Evaluation, *,
                           now: datetime, vis: FlowVisibility = UNRESTRICTED_SYSTEM) -> ApplyResult:
    """Advance the state machine for every reading and raise/resolve/notify.
    Commits after each alert transition (before dispatching) so a failed
    delivery can never lose the alert row."""
    from app.services import alert_notify_state as ns

    result = ApplyResult()
    states = await load_states(db, rule.id)
    open_by_subject = await open_alerts(db, rule.id)
    rule_state = states.get(RULE_SUBJECT, SubjectState())
    rule_state.context.update({
        "eval_key": eval_key(rule, now),
        "status": ev.status, "reason": ev.reason, "error_id": ev.error_id,
        "window_from": _iso(ev.window_from), "window_to": _iso(ev.window_to),
        "truncated": ev.truncated, "subjects": len(ev.readings)})
    rule_state.state = "unknown" if ev.status in ("failed", "no_data") else "ok"
    rule_state.last_reason = ev.reason or (ev.status if ev.status != "ok" else None)
    if ev.status == "failed":
        await save_state(db, rule.id, RULE_SUBJECT, rule_state, now)
        await db.commit()
        return result
    for reading in ev.readings:
        prev = states.get(reading.subject, SubjectState())
        alert = open_by_subject.get(reading.subject)
        breach = breaches(rule, value=reading.value)
        decision = step(prev, breach, alert_open=alert is not None, params=rule.params, now=now)
        s = decision.state
        s.last_value = reading.value
        s.last_reason = reading.reason
        s.context.update({"label": reading.label, "exporter": reading.exporter,
                          "if_index": reading.if_index, "direction": reading.direction})
        result.evaluated += 1
        if decision.action == "raise":
            alert_id, outcome = await _raise(db, rule, reading, ev, s, vis)
            if alert_id is None:
                # Maintenance or a silence: remember the breach but record no
                # transition, so the flap history is not polluted.
                s.transitions = s.transitions[:-1]
                s.context["suppressed"] = outcome
                result.suppressed += 1
                await save_state(db, rule.id, reading.subject, s, now)
                continue
            s.context.pop("suppressed", None)
            await save_state(db, rule.id, reading.subject, s, now)
            await db.commit()
            result.raised += 1
            dedupe = f"{rule.metric}:{reading.subject}"
            if await _within_cooldown(db, rule, dedupe, alert_id):
                # Recorded, not announced: the same subject paged moments ago.
                await ns.stamp(db, alert_id, False)
                await db.execute(text(
                    "UPDATE alerts SET metadata = metadata || '{\"cooldown_suppressed\": true}'::jsonb "
                    "WHERE id = :id"), {"id": alert_id})
                await db.commit()
                continue
            meta_row = (await db.execute(text("SELECT metadata->'snapshot', metadata->>'netflow_link' "
                                              "FROM alerts WHERE id = :id"), {"id": alert_id})).first()
            sent = await notify(db, rule, reading, alert_id=alert_id, is_recovery=False,
                                link=meta_row[1] if meta_row else None,
                                snapshot=_jsonish(meta_row[0]) if meta_row else None)
            await ns.stamp(db, alert_id, sent)
            await db.commit()
        elif decision.action == "resolve" and alert is not None:
            notified = await ns.was_notified(db, alert["id"])
            await _resolve(db, alert["id"])
            await save_state(db, rule.id, reading.subject, s, now)
            await db.commit()
            result.resolved += 1
            if notified and rule.recovery_alert:
                await notify(db, rule, reading, alert_id=alert["id"], is_recovery=True,
                             duration=ap.duration_between(alert["triggered_at"]))
                await db.commit()
        else:
            if decision.action == "flap_hold" and alert is not None and not alert["flapping"]:
                await _mark_flapping(db, alert["id"])
            elif (alert is not None and breach and not alert["cooldown_suppressed"]
                  and await ns.is_pending(db, alert["id"])
                  and not await _silenced(db, rule.id, alert["device_id"], reading.if_index)):
                # Raised during quiet hours and still breaching: announce it
                # now that notifications are allowed (unless snoozed).
                sent = await notify(db, rule, reading, alert_id=alert["id"], is_recovery=False)
                if sent:
                    await ns.stamp(db, alert["id"], True)
            # Only remember subjects worth remembering: breaching, open, or
            # previously tracked (endpoint rules see thousands of hosts).
            if reading.subject in states or breach or alert is not None:
                await save_state(db, rule.id, reading.subject, s, now)
    await save_state(db, rule.id, RULE_SUBJECT, rule_state, now)
    # Forget quiet subjects: no open alert, not breaching, idle for a day.
    await db.execute(text(
        "DELETE FROM netflow_alert_state WHERE rule_id = :r AND subject <> :rs AND state <> 'breach' "
        "AND updated_at < :cut AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.rule_id = :r2 "
        "AND a.status IN ('pending','active','acknowledged') "
        "AND a.metadata->>'netflow_subject' = netflow_alert_state.subject)"),
        {"r": rule.id, "rs": RULE_SUBJECT, "cut": now - timedelta(days=1), "r2": rule.id})
    await db.commit()
    return result


# ── Loop ─────────────────────────────────────────────────────────────────────

_RULE_SQL = """
    SELECT id, name, metric, operator, threshold, severity, notify_channels, cooldown,
           recovery_alert, schedule_start, schedule_end, schedule_days, flow_scope, flow_params,
           email_subject, email_body, sms_template, recovery_email_subject, recovery_email_body,
           recovery_sms_template, created_by, updated_at
    FROM alert_rules
    WHERE enabled AND metric LIKE 'netflow\\_%' ESCAPE '\\'
"""


def eval_key(rule: NetflowRule, now: datetime) -> str:
    """The evaluation slot ``now`` falls in. A rule is evaluated at most once
    per slot however many workers (or passes) run, so ``hold`` counts
    distinct windows -- traffic/endpoint: one closed minute; utilisation: one
    closed 5-minute slot; silence/findings: one wall-clock minute."""
    if rule.metric in (TRAFFIC, ENDPOINT):
        return _iso(_eval_end(now, rule.params.settle_s))
    if rule.metric == UTIL:
        return _iso(_eval_end(now, rule.params.settle_s, 300))
    return _iso(floor_to(now, 60))


def _due(rule: NetflowRule, last_key: str | None, now: datetime) -> bool:
    return last_key != eval_key(rule, now)


async def run_eval_once(now: datetime | None = None) -> dict:
    """One evaluation pass over every enabled NetFlow rule (under the lock)."""
    from app.core.database import AsyncSessionLocal

    now = now or datetime.now(timezone.utc)
    totals = {"rules": 0, "raised": 0, "resolved": 0, "suppressed": 0, "failed": 0, "locked": False}
    async with AsyncSessionLocal() as lock_db:
        got = (await lock_db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                     {"k": ADVISORY_LOCK})).scalar()
        if not got:
            totals["locked"] = True
            return totals
        async with AsyncSessionLocal() as db:
            try:
                rows = (await db.execute(text(_RULE_SQL))).mappings().all()
            except Exception:
                await db.rollback()
                logger.warning("netflow alert: rules unavailable (migrate-143 not applied?)", exc_info=True)
                return totals
            for row in rows:
                try:
                    rule = rule_from_row(dict(row))
                except RuleDefinitionError as exc:
                    logger.warning("netflow alert rule %s is invalid: %s", row["id"], exc)
                    totals["failed"] += 1
                    continue
                try:
                    states = await load_states(db, rule.id)
                    rs = states.get(RULE_SUBJECT)
                    if not _due(rule, (rs.context.get("eval_key") if rs else None), now):
                        continue
                    tracked = [s for s in states if s != RULE_SUBJECT]
                    ev = await evaluate(rule, now=now, tracked=tracked, db=db)
                    res = await apply_evaluation(db, rule, ev, now=now)
                    totals["rules"] += 1
                    totals["raised"] += res.raised
                    totals["resolved"] += res.resolved
                    totals["suppressed"] += res.suppressed
                    totals["failed"] += ev.status == "failed"
                except Exception:
                    await db.rollback()
                    totals["failed"] += 1
                    logger.exception("netflow alert rule %s failed", rule.id)
        await lock_db.commit()
    if totals["raised"] or totals["resolved"]:
        logger.info("netflow alerts: %(rules)s rules, %(raised)s raised, %(resolved)s resolved", totals)
    return totals


async def netflow_alert_loop() -> None:
    """Evaluate NetFlow alert rules every EVAL_INTERVAL_S (netflow_tasks)."""
    await asyncio.sleep(25)
    logger.info("NetFlow alert evaluator started (interval %ss)", EVAL_INTERVAL_S)
    while True:
        try:
            await run_eval_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow alert pass failed")
        await asyncio.sleep(EVAL_INTERVAL_S)


# ── Read helpers for the API (rule status, dry-run, preview) ─────────────────

def _sample_value(rule: NetflowRule, recovered: bool) -> float:
    thr, op, high = rule.threshold, rule.operator, rule.params.band_high
    if rule.metric == FINDING:
        return 0.0 if recovered else 1.0
    if op == "!=":
        return (thr + (high or thr)) / 2 if recovered else (high or thr) * 1.15 + 1
    if op in ("<", "<="):
        return thr * 1.24 + 1 if recovered else thr * 0.61
    value = thr * 0.52 if recovered else thr * 1.09 + (1 if thr == 0 else 0)
    if rule.metric == UTIL:
        value = min(value, 99.0 if not recovered else value)
    return value


def preview_phrasing(rule_dict: Mapping, *, is_recovery: bool, duration: str = "") -> dict | None:
    """Template variables for a NetFlow rule's preview mail, from a sample
    reading -- the same sentence the evaluator sends. None when the draft is
    not a valid NetFlow rule yet (the generic preview is used then)."""
    if not is_netflow_metric(rule_dict.get("metric")):
        return None
    row = dict(rule_dict)
    row.setdefault("id", "00000000-0000-0000-0000-000000000000")
    if row.get("metric") == FINDING:
        row["operator"], row["threshold"] = "==", 1
    try:
        rule = rule_from_row(row)
    except (RuleDefinitionError, TypeError, ValueError, KeyError):
        return None
    value = _sample_value(rule, is_recovery)
    samples = {
        TRAFFIC: ("edge-router-01 (10.0.0.1)" if rule.params.split == "exporter" else describe_scope(rule.scope),
                  {}),
        ENDPOINT: ("10.20.30.40", {"bytes": value}),
        SILENT: ("edge-router-01 (10.0.0.1)", {"last_received_at": None}),
        UTIL: ("edge-router-01 / Gi0/1 (out)", {"speed_bps": 1e9, "bps": value / 100 * 1e9}),
        FINDING: ("Horizontal scan from 10.20.30.40",
                  {"kind": (rule.params.kinds or ("scan",))[0], "severity": rule.params.min_severity or "warning",
                   "confidence": rule.params.min_confidence or "high", "status": "resolved" if is_recovery else "open"}),
    }
    label, detail = samples[rule.metric]
    reading = Reading("preview", label, value, exporter="10.0.0.1", detail=detail)
    return {
        "event_sentence": event_sentence(rule, reading, recovered=is_recovery, duration=duration),
        "condition": condition_text(rule), "condition_label": condition_text(rule),
        "reading": "" if rule.metric == FINDING else format_reading(rule.metric, value),
        "threshold_value": condition_text(rule), "hostname": label,
        "host_suffix": f" — {label}", "flow_scope_label": describe_scope(rule.scope),
    }


def reading_payload(rule: NetflowRule, r: Reading) -> dict:
    return {
        "subject": r.subject, "label": r.label, "value": r.value, "reading": format_reading(rule.metric, r.value),
        "breach": breaches(rule, value=r.value), "reason": r.reason, "exporter": r.exporter,
        "if_index": r.if_index, "direction": r.direction, "detail": r.detail,
    }


def evaluation_payload(rule: NetflowRule, ev: Evaluation) -> dict:
    return {
        "metric": rule.metric, "status": ev.status, "reason": ev.reason, "error_id": ev.error_id,
        "window": {"from": _iso(ev.window_from), "to": _iso(ev.window_to)},
        "condition": condition_text(rule), "scope_label": describe_scope(rule.scope),
        "truncated": ev.truncated, "readings": [reading_payload(rule, r) for r in ev.readings],
        "breaching": sum(1 for r in ev.readings if breaches(rule, value=r.value)),
        "unknown": sum(1 for r in ev.readings if r.value is None),
    }


async def rule_status(db: AsyncSession, rule_id: str, vis: FlowVisibility) -> dict:
    """Stored evaluation state of one rule, restricted to what ``vis`` sees."""
    rows = (await db.execute(text(
        "SELECT subject, state, breach_streak, clear_streak, last_value, last_reason, last_eval_at, "
        "flapping, context FROM netflow_alert_state WHERE rule_id = :r ORDER BY subject"),
        {"r": rule_id})).all()
    out = {"rule": None, "subjects": []}
    for r in rows:
        ctx = dict(_jsonish(r[8]) or {})
        item = {"subject": r[0], "state": r[1], "breach_streak": r[2], "clear_streak": r[3],
                "value": r[4], "reason": r[5], "evaluated_at": _iso(r[6]), "flapping": r[7], "context": ctx}
        if r[0] == RULE_SUBJECT:
            out["rule"] = item
            continue
        exp = ctx.get("exporter")
        if exp and not vis.allows(exp):
            continue
        if not exp and vis.restricted:
            continue
        out["subjects"].append(item)
    return out


__all__ = [
    "METRICS", "METRIC_INFO", "METRIC_PATTERN", "FAMILY", "TRAFFIC", "ENDPOINT", "SILENT", "UTIL", "FINDING",
    "RuleDefinitionError", "RuleParams", "NetflowRule", "Reading", "Evaluation", "SubjectState", "Decision",
    "parse_params", "parse_scope", "validate_definition", "rule_from_row", "describe_scope", "breaches",
    "step", "evaluate", "apply_evaluation", "run_eval_once", "netflow_alert_loop", "evaluation_payload",
    "rule_status", "is_netflow_metric", "format_reading", "condition_text", "event_sentence",
    "exporter_receipts", "exporter_silence", "traffic_snapshot", "interface_speeds", "netflow_link",
    "reset_rule", "reading_payload", "eval_key", "preview_phrasing",
]
