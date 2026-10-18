"""Human phrasing for alert notifications.

Every alert path — device status, SNMP metrics, service checks, host agents,
APM, UDT — sends mail and SMS through here so an operator reads the same
English regardless of which evaluator fired.

The rule that shapes this module: a notification is read by a person, often on
a phone, often at 3am. ``if_util_pct > 80.0`` is the rule's storage format, not
a sentence. What belongs in a message is "Interface utilisation is above 80%",
and on the way back down, how long it stayed that way.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

# ── Metric catalogue ─────────────────────────────────────────────────────────
#
# ``noun``  — subject of the sentence, sentence-cased ("Interface utilisation").
# ``unit``  — appended to threshold and reading values.
# ``scale`` — divide raw values by this before formatting (bps -> Mbps).
# ``state`` — a metric that is a condition, not a quantity: phrased directly
#             instead of "<noun> is above <threshold>".

_PERCENT = {"unit": "%", "decimals": 0}

METRICS: dict[str, dict] = {
    # Reachability / ICMP
    "ping_status": {"noun": "ICMP reachability", "healthy": 1.0, "subject": "the device",
                    "narrative": {"down": "is not responding to ping", "up": "is responding to ping again"},
                    "state": {"down": "stops responding to ping",
                              "up": "responds to ping again"},
                    "state_label": {"down": "Device unreachable (no ICMP response)",
                                    "up": "Device reachable"}},
    "rtt": {"noun": "Round-trip time", "unit": " ms", "decimals": 1},
    "packet_loss": {"noun": "Packet loss", **_PERCENT},
    "jitter": {"noun": "Jitter", "unit": " ms", "decimals": 1},

    # Device health (SNMP)
    "cpu": {"noun": "Device CPU", **_PERCENT},
    "memory": {"noun": "Device memory", **_PERCENT},
    "temperature": {"noun": "Device temperature", "unit": "°C", "decimals": 0},
    "fan_state": {"noun": "Fan state", "subject": "the device",
                  "narrative": {"down": "is reporting a failed fan", "up": "is reporting all fans healthy"},
                  "state": {"down": "reports a failed fan",
                            "up": "reports every fan healthy"},
                  "state_label": {"down": "Fan failure reported", "up": "All fans healthy"}},
    "psu_state": {"noun": "Power supply state", "subject": "the device",
                  "narrative": {"down": "is reporting a failed power supply", "up": "is reporting all power supplies healthy"},
                  "state": {"down": "reports a failed power supply",
                            "up": "reports every power supply healthy"},
                  "state_label": {"down": "Power supply failure reported",
                                  "up": "All power supplies healthy"}},
    "uptime_reset": {"noun": "Device uptime", "subject": "the device",
                     "narrative": {"down": "has rebooted", "up": "has stayed up since the last check"},
                     "state": {"down": "reports a reboot", "up": "stays up"},
                     "state_label": {"down": "Device reboot detected",
                                     "up": "No reboot detected"}},
    "session_count": {"noun": "Session count", "unit": " sessions", "decimals": 0},

    # Interfaces
    "if_util_pct": {"noun": "Interface utilisation", **_PERCENT},
    "if_in_bps": {"noun": "Inbound traffic", "unit": " Mbps", "scale": 1e6, "decimals": 1},
    "if_out_bps": {"noun": "Outbound traffic", "unit": " Mbps", "scale": 1e6, "decimals": 1},
    "if_errors": {"noun": "Interface errors", "unit": " errors", "decimals": 0},
    "if_discards": {"noun": "Interface discards", "unit": " discards", "decimals": 0},
    "if_oper_status": {"noun": "Interface state", "healthy": 1.0, "subject": "the interface",
                       "narrative": {"down": "is operationally down", "up": "is back up"},
                       "state": {"down": "goes operationally down",
                                 "up": "comes back up"},
                       "state_label": {"down": "Interface operationally down",
                                       "up": "Interface up"}},

    # Connectivity services
    "vpn_tunnel_state": {"noun": "VPN tunnel state", "subject": "the device",
                         "narrative": {"down": "has dropped a VPN tunnel", "up": "has restored its VPN tunnels"},
                         "state": {"down": "drops a VPN tunnel",
                                   "up": "restores its VPN tunnels"},
                         "state_label": {"down": "VPN tunnel down", "up": "VPN tunnels up"}},
    "ha_state": {"noun": "HA state", "subject": "the device",
                 "narrative": {"down": "has left its expected HA role", "up": "has returned to its expected HA role"},
                 "state": {"down": "leaves its expected HA role",
                           "up": "returns to its expected HA role"},
                 "state_label": {"down": "HA role changed unexpectedly",
                                 "up": "HA role restored"}},
    "bgp_neighbor_down": {"noun": "BGP neighbour state", "subject": "the device",
                          "narrative": {"down": "has lost a BGP neighbour", "up": "has re-established its BGP neighbours"},
                          "state": {"down": "loses a BGP neighbour",
                                    "up": "re-establishes its BGP neighbours"},
                          "state_label": {"down": "BGP neighbour down",
                                          "up": "BGP neighbours established"}},

    # Service checks and traps
    "service_status": {"noun": "Service check", "healthy": 1.0, "subject": "the service",
                       "narrative": {"down": "is failing its check", "up": "is passing its check again"},
                       "state": {"down": "fails its check",
                                 "up": "passes its check again"},
                       "state_label": {"down": "Service check failing",
                                       "up": "Service check passing"}},
    # `event`: fires on arrival, not on a threshold. A trap rule matches an OID
    # and stores a placeholder `== 0` because the wizard needs *something* in
    # the operator/threshold columns — probing that placeholder concluded the
    # rule watched the healthy state, so every trap alert announced itself as
    # "has cleared the trap condition", the opposite of what happened.
    "trap": {"noun": "SNMP trap", "subject": "the device", "event": True,
             "narrative": {"down": "has sent a matching SNMP trap", "up": "has cleared the trap condition"},
             "state": {"down": "sends a matching SNMP trap",
                       "up": "clears the trap condition"},
             "state_label": {"down": "Matching SNMP trap received",
                             "up": "Trap condition cleared"}},

    # Server agent (host) metrics
    "host_cpu_pct": {"noun": "Host CPU", **_PERCENT},
    "host_memory_pct": {"noun": "Host memory", **_PERCENT},
    "host_filesystem_pct": {"noun": "Filesystem usage", **_PERCENT},
    "host_disk_util_pct": {"noun": "Disk utilisation", **_PERCENT},
    "host_service_down": {"noun": "Monitored service", "subject": "the host",
                          "narrative": {"down": "has a stopped monitored service", "up": "has restarted the monitored service"},
                          "state": {"down": "stops a monitored service",
                                    "up": "restarts the monitored service"},
                          "state_label": {"down": "Monitored service stopped",
                                          "up": "Monitored service running"}},
    "host_process_down": {"noun": "Monitored process", "subject": "the host",
                          "narrative": {"down": "is missing a monitored process", "up": "has restarted the monitored process"},
                          "state": {"down": "loses a monitored process",
                                    "up": "restarts the monitored process"},
                          "state_label": {"down": "Monitored process not running",
                                          "up": "Monitored process running"}},

    # Application monitoring
    "apm_latency_p50": {"noun": "Median (p50) latency", "unit": " ms", "decimals": 0},
    "apm_latency_p95": {"noun": "p95 latency", "unit": " ms", "decimals": 0},
    "apm_latency_p99": {"noun": "p99 latency", "unit": " ms", "decimals": 0},
    "apm_error_rate": {"noun": "Error rate", "unit": "%", "scale": 0.01, "decimals": 2},
    "apm_throughput": {"noun": "Throughput", "unit": " req/s", "decimals": 1},
    "apm_apdex": {"noun": "Apdex score", "unit": "", "decimals": 2},
    "apm_rum_lcp_p75": {"noun": "Field LCP p75", "unit": " ms", "decimals": 0},
    "apm_rum_inp_p75": {"noun": "Field INP p75", "unit": " ms", "decimals": 0},
    "apm_rum_cls_p75": {"noun": "Field CLS p75", "unit": "", "decimals": 3},
    "apm_rum_error_session_rate": {"noun": "Error-affected sessions", "unit": "%", "scale": 0.01, "decimals": 2},
    "apm_rum_resource_failure_rate": {"noun": "Failed browser resources", "unit": "%", "scale": 0.01, "decimals": 2},
    "apm_rum_new_error_groups": {"noun": "New browser error groups", "unit": "", "decimals": 0},
    "apm_slo_burn": {"noun": "SLO burn rate", "unit": "x", "decimals": 1},
    "apm_synthetic_down": {"noun": "Synthetic journey", "subject": "the service",
                           "narrative": {"down": "is failing its synthetic journey", "up": "is passing its synthetic journey again"},
                           "state": {"down": "fails its synthetic journey",
                                     "up": "passes its synthetic journey again"},
                           "state_label": {"down": "Synthetic journey failing",
                                           "up": "Synthetic journey passing"}},
    "apm_anomaly": {"noun": "Traffic anomaly", "subject": "the service",
                    "narrative": {"down": "is deviating from its learned baseline", "up": "is back within its learned baseline"},
                    "state": {"down": "deviates from its learned baseline",
                              "up": "returns to its learned baseline"},
                    "state_label": {"down": "Deviating from learned baseline",
                                    "up": "Back within learned baseline"}},

    # User device tracker
    "udt_new_endpoint": {"noun": "New endpoint",
                         "narrative": {"down": "has seen a previously unknown endpoint", "up": "has stopped seeing new endpoints"},
                         "subject": "the tracker",
                         "state_label": {"down": "Previously unseen endpoint detected", "up": "No new endpoints"},
                         "state": {"down": "sees an endpoint it has never seen before",
                                   "up": "stops seeing new endpoints"}},
    "udt_rogue_endpoint": {"noun": "Rogue endpoint",
                           "narrative": {"down": "has seen an unauthorised endpoint", "up": "no longer sees the unauthorised endpoint"},
                         "subject": "the tracker",
                         "state_label": {"down": "Unauthorised endpoint detected", "up": "No unauthorised endpoints"},
                           "state": {"down": "sees an unauthorised endpoint",
                                     "up": "no longer sees the unauthorised endpoint"}},
    "udt_watch_endpoint": {"noun": "Watched endpoint",
                           "narrative": {"down": "has seen a watch-listed endpoint", "up": "no longer sees the watch-listed endpoint"},
                         "subject": "the tracker",
                         "state_label": {"down": "Watch-listed endpoint seen", "up": "Watch-listed endpoint gone"},
                           "state": {"down": "sees a watch-listed endpoint",
                                     "up": "no longer sees the watch-listed endpoint"}},
    "udt_endpoint_moved": {"noun": "Endpoint move",
                           "narrative": {"down": "has moved an endpoint to a different port", "up": "has settled the endpoint"},
                         "subject": "the tracker",
                         "state_label": {"down": "Endpoint moved to another port", "up": "Endpoint settled"},
                           "state": {"down": "moves an endpoint to a different port",
                                     "up": "settles the endpoint back"}},
    "udt_port_capacity_pct": {"noun": "Switch port capacity", **_PERCENT},

    # NetPath (WAN/Internet path)
    "netpath_rtt": {"noun": "Path latency", "unit": "ms", "decimals": 0, "subject": "the path"},
    "netpath_loss": {"noun": "Path packet loss", **_PERCENT, "subject": "the path"},
    "netpath_hop_count": {"noun": "Path hop count", "unit": " hops", "decimals": 0, "subject": "the path"},
    "netpath_path_change": {"noun": "Network path change", "subject": "the path",
                            "narrative": {"down": "changed the route it takes to the target",
                                          "up": "returned to a stable route"},
                            "state": {"down": "takes a different route to the target",
                                      "up": "is back on a stable route"},
                            "state_label": {"down": "Route changed", "up": "Route stable"}},
    "netpath_unreachable": {"noun": "Path unreachable", "subject": "the path",
                            "narrative": {"down": "can no longer reach the target",
                                          "up": "can reach the target again"},
                            "state": {"down": "cannot reach the target",
                                      "up": "can reach the target again"},
                            "state_label": {"down": "Target unreachable", "up": "Target reachable"}},
}

# Rules are stored with either spelling depending on which screen created them
# — the wizard writes ">", the link-utilization quick-rule writes "gt" — so
# everything here normalises first. Missing this silently described every
# `eq`/`lt` rule as "is above".
_OP_ALIASES = {
    "gt": ">", "gte": ">=", "ge": ">=", "lt": "<", "lte": "<=", "le": "<=",
    "eq": "==", "neq": "!=", "ne": "!=",
}


def normalize_operator(operator: Optional[str]) -> str:
    op = (operator or "").strip()
    return _OP_ALIASES.get(op.lower(), op or ">")


# Present / past phrasing per comparison operator. Present tense describes the
# rule ("is above 80%"); past tense narrates what happened ("rose above 80%").
_OPERATORS: dict[str, dict[str, str]] = {
    ">":  {"present": "is above",        "past": "rose above",       "cleared": "is back below"},
    ">=": {"present": "is at or above",  "past": "reached",          "cleared": "is back below"},
    "<":  {"present": "is below",        "past": "fell below",       "cleared": "is back above"},
    "<=": {"present": "is at or below",  "past": "fell to",          "cleared": "is back above"},
    "==": {"present": "is",              "past": "reached",          "cleared": "is no longer"},
    "!=": {"present": "is other than",   "past": "moved away from",  "cleared": "is back to"},
}


def metric_noun(metric: Optional[str]) -> str:
    """Sentence-cased subject for a metric key, or the key itself if unknown."""
    info = METRICS.get((metric or "").strip())
    if info:
        return info["noun"]
    key = (metric or "").strip()
    if key.startswith("tpl_"):
        # Monitoring-template series: `tpl_ups_load_pct` -> "Ups load pct".
        return key[4:].replace("_", " ").strip().capitalize() or "Template metric"
    return key.replace("_", " ").strip().capitalize() or "Metric"


def _display(metric: Optional[str], num: float, *, extra: int = 0,
             strip: bool = True) -> tuple[str, str]:
    """(number text, unit) for a numeric value in the metric's display units.

    ``extra`` adds decimals to the catalogue precision; ``strip`` drops
    trailing zeros ("80%" rather than "80.0%").
    """
    info = METRICS.get((metric or "").strip(), {})
    unit = info.get("unit", "")
    num /= float(info.get("scale") or 1.0)
    decimals = int(info.get("decimals", 1))
    # A link rule written in bits reads as "1.5 Gbps", never "1500 Mbps".
    if unit == " Mbps" and abs(num) >= 1000:
        num /= 1000.0
        unit, decimals = " Gbps", 2
    text_ = f"{num:.{decimals + max(0, extra)}f}"
    # Drop a trailing ".0" — "80%" reads better than "80.0%".
    if strip and "." in text_:
        text_ = text_.rstrip("0").rstrip(".")
    return text_, unit


def format_value(metric: Optional[str], value) -> str:
    """Render a reading or threshold with its unit — 80 -> "80%", 200 -> "200 ms"."""
    if value in (None, ""):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    text_, unit = _display(metric, num)
    return f"{text_}{unit}"


# ── Readings next to their threshold ─────────────────────────────────────────
#
# The catalogue rounds percentages to whole numbers, which is right for a rule
# description ("above 90%") and wrong for a reading that sits just over it: a
# 90.3% reading printed as "currently 90%" next to "rose above 90%" reads like
# a false alarm. A reading therefore gets as many extra decimals (up to
# READING_MAX_EXTRA_DECIMALS) as it needs to differ visibly from the threshold,
# and the threshold is shown with the same precision.

READING_MAX_EXTRA_DECIMALS = 3
UNAVAILABLE = "unavailable"


def _finite(value) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num == num and num not in (float("inf"), float("-inf")) else None


def reading_precision(metric: Optional[str], value, operator: Optional[str] = None,
                      threshold=None) -> int:
    """Extra decimals a reading and its threshold are both shown with.

    Near its threshold (within ``NEAR_THRESHOLD_POINTS``) a whole-percent
    reading keeps one decimal: 91.5% against 90% is "91.5%", never "92%"
    (field report: readings of 90.1-91.5% read as "90%"/"91%"). Readings far
    from the threshold keep the catalogue format ("100%", "95%").
    """
    extra = _reading_precision(metric, value, operator, threshold)
    info = METRICS.get((metric or "").strip(), {})
    num, thr = _finite(value), _finite(threshold)
    if (num is not None and thr is not None and num != thr and abs(num - thr) < NEAR_THRESHOLD_POINTS
            and info.get("unit") == "%" and int(info.get("decimals", 1)) == 0):
        extra = max(extra, 1)
    return extra


NEAR_THRESHOLD_POINTS = 5.0


def _reading_precision(metric: Optional[str], value, operator: Optional[str] = None,
                       threshold=None) -> int:
    """Extra decimals (0..3) a reading and its threshold are both shown with.

    The threshold first gets enough decimals to print exactly (a 90.5% rule is
    never shown as "90%"); the reading then gets more only while it would
    still print identical to a threshold it differs from. A non-zero reading
    with no threshold is never shown as zero ("0.4%", not "0%").
    ``operator`` is accepted for symmetry with the rule columns: rounding is
    monotonic, so a reading on the breaching side of an exactly printed
    threshold can only collide with it, never print on the wrong side.
    """
    num = _finite(value)
    thr = _finite(threshold)
    cap = READING_MAX_EXTRA_DECIMALS
    if num is None:
        return 0
    if thr is None:
        for extra in range(cap + 1):
            if num == 0 or float(_display(metric, num, extra=extra, strip=False)[0]) != 0:
                return extra
        return cap
    base = 0
    for extra in range(cap + 1):
        base = extra
        shown = float(_display(metric, thr, extra=extra, strip=False)[0])
        exact = float(_display(metric, thr, extra=cap + 6, strip=False)[0])
        if abs(shown - exact) <= 1e-9 * max(1.0, abs(exact)):
            break
    if num == thr:
        return base
    for extra in range(base, cap + 1):
        if (_display(metric, num, extra=extra, strip=False)
                != _display(metric, thr, extra=extra, strip=False)):
            return extra
    return cap


def format_reading(metric: Optional[str], value: float | None, operator: Optional[str],
                   threshold: float | None) -> str:
    """A reading formatted against its rule's threshold.

    The catalogue format, with as many extra decimals (max 3) as needed so the
    reading never prints equal to a threshold it is on the other side of:
    90.3 against 90 -> "90.3%", 90.04 -> "90.04%", None -> "unavailable".
    Use :func:`format_threshold` for the threshold next to it.
    """
    num = _finite(value)
    if num is None:
        return UNAVAILABLE
    extra = reading_precision(metric, num, operator, threshold)
    text_, unit = _display(metric, num, extra=extra, strip=extra == 0)
    return f"{text_}{unit}"


def format_threshold(metric: Optional[str], threshold, operator: Optional[str] = None,
                     value=None) -> str:
    """The threshold with the same decimals :func:`format_reading` gave ``value``.

    "" for a missing threshold, as format_value does.
    """
    thr = _finite(threshold)
    if thr is None:
        return format_value(metric, threshold)
    num = _finite(value)
    extra = reading_precision(metric, thr if num is None else num, operator, thr)
    text_, unit = _display(metric, thr, extra=extra, strip=extra == 0)
    return f"{text_}{unit}"


# Per-poll counter metrics: a delta between two polls, so the reading names
# the interval it covers and which direction counted it.
COUNTER_METRICS = frozenset({"if_errors", "if_discards"})


def compact_interval(seconds) -> str:
    """"60 s", "5 min", "2 hours 10 minutes" — the span a counter delta covers."""
    secs = _finite(seconds)
    if secs is None or secs <= 0:
        return ""
    if secs < 120:
        return f"{secs:.0f} s"
    minutes = secs / 60.0
    if abs(minutes - round(minutes)) < 1e-6 and minutes < 120:
        return f"{round(minutes)} min"
    return humanize_duration(secs)


def counter_detail(metric: Optional[str], context: Optional[dict]) -> str:
    """"in the last 60 s (150 in, 2 out)" for a per-poll counter delta.

    ``context`` carries ``interval_s`` and the per-direction deltas ``in`` /
    ``out`` (None = not known for that direction). "" without an interval.
    """
    if not context:
        return ""
    span = compact_interval(context.get("interval_s"))
    if not span:
        return ""
    parts = []
    for key, word in (("in", "in"), ("out", "out")):
        if key not in context:
            continue
        num = _finite(context.get(key))
        parts.append(f"{num:.0f} {word}" if num is not None else f"{word} unknown")
    tail = f" ({', '.join(parts)})" if parts else ""
    return f"in the last {span}{tail}"


def reading_text(metric: Optional[str], value, operator: Optional[str] = None,
                 threshold=None, context: Optional[dict] = None) -> str:
    """The reading as a notification states it.

    :func:`format_reading`, plus, for per-poll counters with a value context,
    the interval and direction: "152 errors in the last 60 s (150 in, 2 out)".
    """
    shown = format_reading(metric, value, operator, threshold)
    key = (metric or "").strip()
    if key in COUNTER_METRICS and shown != UNAVAILABLE:
        detail = counter_detail(key, context)
        if detail:
            return f"{shown} {detail}"
    return shown


def headline_metric(metric: Optional[str], value, operator: Optional[str] = None,
                    threshold=None, *, context: Optional[dict] = None,
                    label: Optional[str] = None) -> dict:
    """The ``headline_metric`` block for an alert email.

    Reading and threshold share one precision; a counter's interval and
    direction go in ``detail`` so the large value stays short.
    """
    key = (metric or "").strip()
    out = {
        "label": label or metric_noun(key),
        "value": format_reading(key, value, operator, threshold),
        "secondary_label": "Threshold" if threshold is not None else None,
        "secondary_value": (format_threshold(key, threshold, operator, value)
                            if threshold is not None else None),
    }
    detail = counter_detail(key, context) if key in COUNTER_METRICS else ""
    if detail:
        out["detail"] = detail[0].upper() + detail[1:]
    return out


def _cmp(value: float, operator: str, threshold: float) -> bool:
    ops = {
        ">": value > threshold, ">=": value >= threshold,
        "<": value < threshold, "<=": value <= threshold,
        "==": value == threshold, "!=": value != threshold,
    }
    return ops.get(normalize_operator(operator), False)


def _alerts_on_unhealthy(metric: str, operator: str, threshold) -> bool:
    """Does this condition fire on the metric's *bad* state?

    Flag metrics disagree about which number is healthy: ping_status and
    if_oper_status are 1/2-style "up" values, while `*_down` counters are 0 when
    all is well. Rather than special-case each one, test the rule against the
    known-healthy reading — if a healthy device would satisfy the condition, the
    rule is watching for the healthy state (a trigger_on='up' rule).
    """
    info = METRICS[metric]
    if info.get("event"):
        # No condition to probe: the rule fires when the event arrives, so it
        # always describes the side where it happened.
        return True
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        thr = 0.0
    healthy = float(info.get("healthy", 0.0))
    return not _cmp(healthy, operator or "==", thr)


def _state_phrase(metric: str, operator: str, threshold, *, cleared: bool,
                  narrative: bool = False) -> str:
    """Phrasing for on/off metrics, where a threshold number means nothing.

    Two voices: the rule-description one ("stops responding to ping", which
    completes "this rule fires when the device …") and the narrative one ("is
    not responding to ping", which completes "core-router-01 …").
    """
    info = METRICS[metric]
    states = (info.get("narrative") if narrative else None) or info["state"]
    wants_bad = _alerts_on_unhealthy(metric, operator, threshold)
    key = "up" if (wants_bad == cleared) else "down"
    return states[key]


def condition_phrase(metric: Optional[str], operator: Optional[str], threshold,
                     *, tense: str = "present", subject: Optional[str] = None,
                     threshold_text: Optional[str] = None) -> str:
    """One condition as a clause: "Interface utilisation is above 80%".

    ``tense`` is "present" (describing the rule), "past" (it just happened) or
    "cleared" (it just stopped happening). ``subject`` names who the sentence is
    about — a hostname reads better than "the device" — and applies only to
    state metrics; a quantity is always described by its own noun, never by the
    host ("core-router-01 rose above 80%" is not a sentence anyone wants).
    ``threshold_text`` replaces the formatted threshold (see format_threshold).
    """
    key = (metric or "").strip()
    info = METRICS.get(key, {})
    if "state" in info:
        who = subject or info.get("subject") or "the device"
        return f"{who} {_state_phrase(key, operator or '==', threshold, cleared=(tense == 'cleared'), narrative=(tense != 'present'))}"
    noun = metric_noun(key)
    verb = _OPERATORS.get(normalize_operator(operator), _OPERATORS[">"])[
        tense if tense in ("present", "past", "cleared") else "present"
    ]
    value = threshold_text if threshold_text is not None else format_value(key, threshold)
    return " ".join(p for p in (noun, verb, value) if p)


def condition_label(metric: Optional[str], operator: Optional[str], threshold,
                    *, threshold_text: Optional[str] = None) -> str:
    """Compact form for the "Condition" row of the details table."""
    key = (metric or "").strip()
    info = METRICS.get(key, {})
    if "state" in info:
        # Name the state the rule actually watches — a trigger_on='up' rule
        # labelled "Device unreachable" would be exactly backwards.
        side = "down" if _alerts_on_unhealthy(key, operator or "==", threshold) else "up"
        labels = info.get("state_label") or {}
        return labels.get(side) or f"{info['noun']}: {info['state'][side]}"
    verb = _OPERATORS.get(normalize_operator(operator), _OPERATORS[">"])["present"]
    # "Interface utilisation above 80%" — the "is" is noise in a table cell.
    verb = verb[3:] if verb.startswith("is ") else verb
    value = threshold_text if threshold_text is not None else format_value(key, threshold)
    return " ".join(p for p in (metric_noun(key), verb, value) if p)


def _same_threshold(a, b) -> bool:
    x, y = _finite(a), _finite(b)
    return x is not None and x == y


def conditions_label(conditions: Optional[Iterable[dict]], logic: str = "AND",
                     *, metric: Optional[str] = None, operator: Optional[str] = None,
                     threshold=None, threshold_text: Optional[str] = None) -> str:
    """Label for a rule that may carry several conditions.

    Falls back to the rule's single metric/operator/threshold columns when the
    ``conditions`` array is empty, which is how most rules are still stored.
    ``threshold_text`` is applied to the rule's own metric/threshold only.
    """
    def own(c) -> Optional[str]:
        if (threshold_text is not None and c.get("metric") == metric
                and _same_threshold(c.get("threshold"), threshold)):
            return threshold_text
        return None

    parts = [
        condition_label(c.get("metric"), c.get("operator"), c.get("threshold"),
                        threshold_text=own(c))
        for c in (conditions or [])
        if isinstance(c, dict) and c.get("metric")
    ]
    if not parts:
        if not metric:
            return ""
        return condition_label(metric, operator, threshold, threshold_text=threshold_text)
    joiner = " or " if (logic or "AND").upper() == "OR" else " and "
    return joiner.join(parts)


# ── Durations ────────────────────────────────────────────────────────────────

def humanize_duration(seconds: Optional[float]) -> str:
    """"3 minutes 25 seconds", "2 hours 14 minutes", "4 days 3 hours".

    Two units at most: the third never changes a decision, and a notification
    that reads like a stopwatch readout is harder to skim, not easier.
    """
    if seconds is None:
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    if total < 5:
        return "a few seconds"
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"

    minutes, secs = divmod(total, 60)
    if minutes < 60:
        # Seconds stop mattering once the outage is measured in tens of minutes.
        if minutes >= 10 or secs == 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return (f"{minutes} minute{'s' if minutes != 1 else ''} "
                f"{secs} second{'s' if secs != 1 else ''}")

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes == 0:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        return (f"{hours} hour{'s' if hours != 1 else ''} "
                f"{minutes} minute{'s' if minutes != 1 else ''}")

    days, hours = divmod(hours, 24)
    if hours == 0:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''} {hours} hour{'s' if hours != 1 else ''}"


def duration_between(started, ended=None) -> str:
    """Humanised gap between two timestamps; "" when the start is unknown."""
    if not started:
        return ""
    if isinstance(started, str):
        try:
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return ""
    ended = ended or datetime.now(timezone.utc)
    if isinstance(ended, str):
        try:
            ended = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        except ValueError:
            ended = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return humanize_duration((ended - started).total_seconds())


def host_label(hostname: Optional[str], ip_address: Optional[str] = None) -> str:
    """"core-sw-01 (10.0.0.1)" — the same form the device status alerts use.

    The IP is dropped when it is missing or is already the name, so an
    unnamed device reads "10.0.0.1" rather than "10.0.0.1 (10.0.0.1)".
    """
    name = (hostname or "").strip()
    ip = (ip_address or "").strip()
    if not ip or ip == name:
        return name or ip
    return f"{name} ({ip})" if name else ip


# ── Default message bodies ───────────────────────────────────────────────────
#
# These are `{placeholder}` templates, not finished prose: a rule may override
# any of them, and the wizard shows the effective template for editing. The
# variables they lean on are built by `message_variables()` below.

# `{host_suffix}` names the object as " — core-sw-01 (10.0.0.1)": two switches
# sharing a sysName are otherwise indistinguishable in an inbox.
DEFAULT_EMAIL_SUBJECT = "[{severity}] {status}: {rule_name}{host_suffix}"
DEFAULT_RECOVERY_EMAIL_SUBJECT = "[{severity}] Resolved: {rule_name}{host_suffix}"

# The email body is the highlighted callout above a table that already lists
# the rule, the condition, the host and the time. So it says the one thing the
# table cannot: what happened, in a sentence.
DEFAULT_EMAIL_BODY = "{event_sentence}"
DEFAULT_RECOVERY_EMAIL_BODY = "{event_sentence}{duration_sentence}"

# SMS has no table to lean on, so it names the rule as well.
DEFAULT_SMS = "ZenPlus {severity} — {rule_name}: {event_sentence}"
DEFAULT_RECOVERY_SMS = "ZenPlus resolved — {rule_name}: {event_sentence}{duration_suffix}"


# Templates the alert-rule wizard used to seed into every new rule. Nobody
# typed these — they were the pre-filled default — so a rule still carrying one
# byte-for-byte is using the default, not a customisation, and gets the current
# wording instead. Anything else the user wrote is left exactly as written.
LEGACY_DEFAULT_TEMPLATES = frozenset({
    "[{severity}] {status}: {rule_name}",
    "[{severity}] RESOLVED: {rule_name}",
    "[{severity}] Resolved: {rule_name}",
    "{status_intro}\n\nRule: {rule_name}\nSeverity: {severity}\nDevice: {hostname} "
    "({ip_address})\nStatus: {status}\nMetric: {metric} {operator} {threshold}\n"
    "Time: {timestamp}\n\n--\nZenPlus Network Monitoring",
    "[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. Rule: {rule_name}",
    "[ZenPlus {severity}] {hostname} ({ip_address}) RECOVERED. Rule: {rule_name}",
    "[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. RESOLVED: {rule_name}",
    "[ZenPlus {severity}] {hostname} is {status}. RESOLVED: {rule_name}",
    "An alert has been triggered:",
    "The following alert has been resolved:",
})


def is_default_template(stored: Optional[str]) -> bool:
    """True when a stored template is blank or one the wizard pre-filled."""
    return not (stored or "").strip() or stored.strip() in LEGACY_DEFAULT_TEMPLATES


def effective_template(stored: Optional[str], default: str) -> str:
    """The template to render: the user's if they wrote one, else the default."""
    return default if is_default_template(stored) else stored


# ── Per-source default template sets ─────────────────────────────────────────
#
# A service check reports its own failure reason, and a trap carries its own
# message, so those paths lead with different sentences than a device does.
# The senders in alert_engine read these, and so do the rule editors — showing
# a device-shaped default on a service rule advertises text that will never be
# sent.

_SERVICE_TEMPLATES = {
    "email_subject": "[{severity}] {status}: {check_name}",
    "email_body": "The {check_type} check “{check_name}” on {target} is {status}."
                  "{error_sentence}",
    "sms_template": "ZenPlus {severity} — {rule_name}: {check_name} is {status} ({target}).",
    # The service path has no {host_suffix}; its subject stays name-only.
    "recovery_email_subject": "[{severity}] Resolved: {rule_name}",
    "recovery_email_body": "The {check_type} check “{check_name}” on {target} is "
                           "passing again.{duration_sentence}",
    "recovery_sms_template": "ZenPlus resolved — {rule_name}: {check_name} is "
                             "passing again.{duration_suffix}",
}

_TRAP_TEMPLATES = {
    "email_subject": DEFAULT_EMAIL_SUBJECT,
    "email_body": "{event_sentence} {trap_message}",
    "sms_template": DEFAULT_SMS,
    # A trap is an event, not a state: nothing ever "clears" it, so the trap
    # path has no recovery notification to describe.
    "recovery_email_subject": None,
    "recovery_email_body": None,
    "recovery_sms_template": None,
}

_SYSLOG_TEMPLATES = {
    **_TRAP_TEMPLATES,
    'email_subject': '{rule_name}: syslog from {hostname}',
    'email_body': '{hostname} sent a matching syslog event (severity {syslog_severity}). {event_message}',
    'sms_template': '{rule_name}: {hostname}: {event_message}',
}

_DEVICE_TEMPLATES = {
    "email_subject": DEFAULT_EMAIL_SUBJECT,
    "email_body": DEFAULT_EMAIL_BODY,
    "sms_template": DEFAULT_SMS,
    "recovery_email_subject": DEFAULT_RECOVERY_EMAIL_SUBJECT,
    "recovery_email_body": DEFAULT_RECOVERY_EMAIL_BODY,
    "recovery_sms_template": DEFAULT_RECOVERY_SMS,
}

TEMPLATE_FIELDS = (
    "email_subject", "email_body", "sms_template",
    "recovery_email_subject", "recovery_email_body", "recovery_sms_template",
)


def template_kind(rule) -> str:
    """Which default set a rule's notifications are built from.

    Mirrors the routing in alert_engine: metric='trap' goes to the trap path,
    anything bound to a service check goes to the service path, and everything
    else (device ping, SNMP, host, network, APM) uses the device wording.
    """
    def field(name):
        if isinstance(rule, dict):
            return rule.get(name)
        return getattr(rule, name, None)

    if (field("metric") or "") == "syslog":
        return "syslog"
    if (field("metric") or "") == "trap":
        return "trap"
    if (field("service_check_id") or field("service_check_group_id")
            or (field("metric") or "") == "service_status"):
        return "service"
    return "device"


def default_templates(kind: str = "device") -> dict:
    """The six default message templates for a source kind.

    Values may be None (a trap has no recovery notification); callers rendering
    an editor should hide those fields rather than offer text nobody sends.
    """
    if kind == "service":
        return dict(_SERVICE_TEMPLATES)
    if kind == "trap":
        return dict(_TRAP_TEMPLATES)
    if kind == "syslog":
        return dict(_SYSLOG_TEMPLATES)
    return dict(_DEVICE_TEMPLATES)


def effective_templates(rule, kind: Optional[str] = None) -> dict:
    """What this rule will actually send, per field.

    Resolves each stored template through ``effective_template`` against its
    own default, so a rule storing NULL (the common case) reports the built-in
    wording rather than a blank the editor cannot show.
    """
    def field(name):
        if isinstance(rule, dict):
            return rule.get(name)
        return getattr(rule, name, None)

    defaults = default_templates(kind or template_kind(rule))
    out = {}
    for name in TEMPLATE_FIELDS:
        default = defaults.get(name)
        out[name] = None if default is None else effective_template(field(name), default)
    return out


def normalize_stored_template(value: Optional[str], default: Optional[str]) -> Optional[str]:
    """What to persist for a submitted template.

    An editor that shows the default as real, editable text posts it back
    unchanged whenever the user did not touch it. Storing that would bake a
    copy onto the rule and freeze it at today's wording, so text equal to the
    default (or blank, or a legacy pre-filled default) is stored as NULL and
    keeps tracking the built-in.
    """
    if value is None:
        return None
    text_value = value.strip()
    if not text_value or is_default_template(text_value):
        return None
    if default is not None and text_value == default.strip():
        return None
    return value


# ── Device flapping notices ──────────────────────────────────────────────────
#
# One notice when a device starts flapping and one when it stops
# (device_flap_notice_service). They go once per channel to the union of the
# channels of every rule whose status notifications the episode holds, so no
# single rule renders them: these defaults are the whole text. Variables:
# {hostname} {ip} {changes} {window} {kind_label} {stable} {held_label}
# {held_sms_label} {unaffected_label} (start; see flap_held_labels) and
# {hostname} {ip} {end_reason_label} {STATUS} {since} (stop).

FLAP_START_EMAIL_SUBJECT = "[ZenPlus] {hostname} is flapping"
FLAP_START_EMAIL_BODY = (
    "{hostname} ({ip}) changed status {changes} times in the last {window} minutes "
    "({kind_label}). {held_label} for this device are held: a status that "
    "lasts {stable} minutes still notifies. Flapping ends after {stable} minutes "
    "without a change. {unaffected_label} are not affected."
)
FLAP_START_SMS = ("ZenPlus — {hostname} ({ip}) is flapping: {changes} status changes in "
                  "{window} min. {held_sms_label} held until a status lasts {stable} min.")

FLAP_STOP_EMAIL_SUBJECT = "[ZenPlus] {hostname} stopped flapping — {STATUS}"
FLAP_STOP_EMAIL_BODY = (
    "{hostname} ({ip}) has stopped flapping ({end_reason_label}). Current status: "
    "{STATUS} since {since}. Status notifications have resumed."
)
FLAP_STOP_SMS = "ZenPlus — {hostname} ({ip}) stopped flapping: {STATUS} since {since}."

# Episode kind (the union of the classes that flapped) -> what changed.
FLAP_KIND_LABELS = {
    "reachability": "up/down",
    "performance": "up/degraded",
    "both": "up/down and up/degraded",
}

# Episode kind -> the status notifications it holds. Only the flapping class
# holds its own triggers: latency/loss flapping holds Degraded, never Down
# (a move into down is reachability), and recoveries to Up are never held.
FLAP_HELD_LABELS = {
    "reachability": "Up/down notifications",
    "performance": "Degraded (latency/loss) notifications",
    "both": "Status notifications",
}
FLAP_HELD_SMS_LABELS = {
    "reachability": "Up/down alerts",
    "performance": "Degraded alerts",
    "both": "Status alerts",
}
# ... and what it leaves alone.
_FLAP_OTHER_ALERTS = "Other alerts (interfaces, services, metrics)"
FLAP_UNAFFECTED_LABELS = {
    "performance": "Down notifications and other alerts (interfaces, services, metrics)",
}

# device_flap_episodes.end_reason -> why the episode ended.
FLAP_END_REASON_LABELS = {
    "stable": "status stable",
    "disabled": "flap detection turned off",
    "maintenance": "maintenance window started",
    "owner_change": "polling owner changed or went offline",
    "reseed": "status re-synchronised",
    "not_polled": "no longer polled",
}


def flap_notice_templates(flap_event: str) -> dict:
    """{email_subject, email_body, sms_template} for 'start' or 'stop'."""
    if flap_event == "stop":
        return {"email_subject": FLAP_STOP_EMAIL_SUBJECT, "email_body": FLAP_STOP_EMAIL_BODY,
                "sms_template": FLAP_STOP_SMS}
    return {"email_subject": FLAP_START_EMAIL_SUBJECT, "email_body": FLAP_START_EMAIL_BODY,
            "sms_template": FLAP_START_SMS}


def flap_kind_label(kind: Optional[str]) -> str:
    return FLAP_KIND_LABELS.get(kind or "", "status changes")


def flap_held_labels(kind: Optional[str]) -> dict:
    """{held_label, held_sms_label, unaffected_label} for a start notice of
    an episode of ``kind`` (an unknown kind reads as every status)."""
    k = kind or ""
    return {
        "held_label": FLAP_HELD_LABELS.get(k, "Status notifications"),
        "held_sms_label": FLAP_HELD_SMS_LABELS.get(k, "Status alerts"),
        "unaffected_label": FLAP_UNAFFECTED_LABELS.get(k, _FLAP_OTHER_ALERTS),
    }


def flap_held_notice(kind: Optional[str], stable_minutes) -> str:
    """The start notice's callout line: what is held, until when."""
    text_ = (f"{flap_held_labels(kind)['held_label']} for this device are held until a "
             f"status lasts {stable_minutes} minutes.")
    if kind == "performance":
        text_ += " Down notifications are not affected."
    return text_


def flap_end_reason_label(reason: Optional[str]) -> str:
    return FLAP_END_REASON_LABELS.get(reason or "", "ended")


def _reading_extra(metric: Optional[str], reading: Optional[str]) -> int:
    """Extra decimals a pre-formatted reading ("90.3%") carries over the
    catalogue precision, so the threshold beside it can match. 0 when the
    reading is not in the metric's plain unit (a "1.25 Gbps" reading against
    a Mbps catalogue entry, "unavailable", a counter sentence's tail)."""
    import re as _re
    if not reading:
        return 0
    m = _re.match(r"-?\d+(?:\.(\d+))?", str(reading).strip())
    if not m:
        return 0
    info = METRICS.get((metric or "").strip(), {})
    unit = info.get("unit", "")
    rest = str(reading).strip()[m.end():]
    if unit and not rest.startswith(unit):
        return 0
    shown = len(m.group(1) or "")
    return max(0, min(READING_MAX_EXTRA_DECIMALS, shown - int(info.get("decimals", 1))))


def _threshold_text(metric: Optional[str], operator: Optional[str], threshold,
                    value, reading: Optional[str]) -> str:
    """The threshold as every part of one message shows it."""
    thr = _finite(threshold)
    if thr is None:
        return format_value(metric, threshold) if threshold is not None else ""
    if _finite(value) is not None:
        return format_threshold(metric, thr, operator, value)
    extra = max(_reading_extra(metric, reading), reading_precision(metric, thr, operator, thr))
    text_, unit = _display(metric, thr, extra=extra, strip=extra == 0)
    return f"{text_}{unit}"


def phrasing_variables(*, metric: Optional[str] = None, operator: Optional[str] = None,
                       threshold=None, conditions: Optional[Iterable[dict]] = None,
                       condition_logic: str = "AND", reading: Optional[str] = None,
                       duration: str = "", is_recovery: bool = False,
                       subject_noun: Optional[str] = None, value=None,
                       value_context: Optional[dict] = None) -> dict:
    """The condition/duration half of the template variables.

    Kept separate from the identity half (host, severity, timestamps) because
    every evaluator already knows who it is talking about, but each phrases the
    condition the same way. ``reading`` is the current value already formatted
    for display ("87%"), which the evaluators know and the rule columns do not.

    ``value`` is the raw reading: when given (and ``reading`` is not), the
    reading is formatted here with :func:`reading_text`, so it never prints
    equal to a threshold it is on the other side of, and a per-poll counter
    states its interval and direction (``value_context``: ``interval_s``,
    ``in``, ``out``). The threshold is shown with the reading's precision in
    the sentence, the condition label and ``{threshold_value}``.
    """
    key = (metric or "").strip()
    is_state = "state" in METRICS.get(key, {})
    if reading is None and value is not None and not is_state:
        reading = reading_text(key, value, operator, threshold, value_context)
    thr_text = None if is_state else _threshold_text(key, operator, threshold, value, reading)

    tense = "cleared" if is_recovery else "past"
    phrase = condition_phrase(metric, operator, threshold, tense=tense, threshold_text=thr_text)
    present = condition_phrase(metric, operator, threshold, tense="present",
                               threshold_text=thr_text)
    label = conditions_label(conditions, condition_logic, metric=metric,
                             operator=operator, threshold=threshold,
                             threshold_text=thr_text)

    noun_ = metric_noun(key)

    def _finish(text_: str) -> str:
        """Sentence-case and punctuate, then hang the reading off the end."""
        if not text_:
            return ""
        # Openings that already carry their own casing are left alone: a
        # hostname ("core-router-01 is not responding", never "Core-router-01")
        # and a metric noun the catalogue deliberately lower-cases ("p95").
        keeps_case = (subject_noun and text_.startswith(subject_noun)) or text_.startswith(noun_)
        if not keeps_case:
            text_ = text_[0].upper() + text_[1:]
        if reading:
            shown = str(reading)
            # "152 errors in the last 60 s (150 in, 2 out)" inside the
            # sentence's own parentheses reads as "...60 s: 150 in, 2 out".
            if shown.endswith(")") and shown.count(" (") == 1:
                shown = shown[:-1].replace(" (", ": ")
            text_ += f" ({'now' if is_recovery else 'currently'} {shown})"
        return text_ + "."

    # One sentence that names who it is about, so a template needs nothing else:
    #   "core-router-01 is not responding to ping."
    #   "Interface utilisation on core-router-01 rose above 80% (currently 87%)."
    if is_state:
        event = condition_phrase(metric, operator, threshold, tense=tense, subject=subject_noun)
    elif subject_noun:
        noun = metric_noun(key)
        rest = phrase[len(noun):].lstrip() if phrase.startswith(noun) else phrase
        event = f"{noun} on {subject_noun} {rest}".strip()
    else:
        event = phrase

    sentence = _finish(phrase)
    reading_value = (format_reading(key, value, operator, threshold)
                     if value is not None and not is_state else (reading or ""))

    return {
        "event_sentence": _finish(event),
        "metric": metric or "",
        "metric_label": metric_noun(metric),
        "operator": operator or "",
        # `{threshold}` stays the raw number: templates written before this
        # module existed say "{threshold}%", and a formatted value would double
        # the unit. `{threshold_value}` is the formatted one.
        "threshold": "" if threshold is None else str(threshold),
        "threshold_value": (thr_text if thr_text is not None
                            else format_value(metric, threshold)) if threshold is not None else "",
        "condition": label or present,
        "condition_label": label or present,
        "condition_sentence": sentence,
        "recovery_sentence": sentence,
        "reading": reading or "",
        # The bare value ("152 errors") and, for counters, the window it
        # covers ("in the last 60 s (150 in, 2 out)").
        "reading_value": reading_value,
        "reading_detail": (counter_detail(key, value_context)
                           if key in COUNTER_METRICS else ""),
        "duration": duration,
        # Both read as a clause appended after a finished sentence, so a
        # template can end with one without gluing words together.
        "duration_sentence": f" The condition was active for {duration}." if duration else "",
        "duration_suffix": f" Active for {duration}." if duration else "",
        "host_suffix": f" — {subject_noun}" if subject_noun else "",
    }


def sample_reading(metric: Optional[str], operator: Optional[str], threshold,
                   *, recovered: bool = False) -> str:
    """A plausible reading for previews — "87%" against a threshold of 80%.

    Preview mail has to show what a real one looks like, and a real one leads
    with a number. Inventing one from the threshold beats leaving a gap.
    """
    key = (metric or "").strip()
    if "state" in METRICS.get(key, {}):
        return ""
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        return ""
    if thr == 0:
        return format_value(key, 1.0 if not recovered else 0.0)
    op = normalize_operator(operator)
    breaching = op in (">", ">=", "!=")
    # Overshoot when the rule watches for "too high", undershoot when it watches
    # for "too low"; the recovered sample lands comfortably the other side.
    if breaching:
        factor = 1.09 if not recovered else 0.52
    else:
        factor = 0.61 if not recovered else 1.24
    value = thr * factor
    if METRICS.get(key, {}).get("unit") == "%" and not key.startswith("apm_"):
        value = max(0.0, min(100.0, value))
        # A 95% threshold overshoots the ceiling, and "currently 100%" next to
        # "above 95%" reads like a rounding bug. Sit it between the two instead.
        if breaching and not recovered and value >= 100.0:
            value = min(99.0, thr + (100.0 - thr) * 0.6)
    return format_value(key, value)


def rule_phrasing(rule, *, hostname: str = "", is_recovery: bool = False,
                  reading: Optional[str] = None, duration: str = "",
                  subject_noun: Optional[str] = None, value=None,
                  value_context: Optional[dict] = None) -> dict:
    """`phrasing_variables` for an alert_rules row (ORM row, mapping or dict)."""
    def field(name, default=None):
        if isinstance(rule, dict):
            return rule.get(name, default)
        return getattr(rule, name, default)

    conditions = field("conditions") or None
    if isinstance(conditions, str):
        try:
            import json as _json
            conditions = _json.loads(conditions)
        except Exception:
            conditions = None

    return phrasing_variables(
        metric=field("metric"), operator=field("operator"), threshold=field("threshold"),
        conditions=conditions, condition_logic=field("condition_logic") or "AND",
        reading=reading, duration=duration, is_recovery=is_recovery,
        subject_noun=subject_noun or (hostname or None),
        value=value, value_context=value_context,
    )


def message_variables(*, rule_name: str, hostname: str, ip_address: str = "",
                      severity: str = "warning", status: str = "",
                      timestamp: Optional[datetime] = None, **phrasing) -> dict:
    """Full template variable set: identity plus phrasing."""
    now = timestamp or datetime.now(timezone.utc)
    is_recovery = bool(phrasing.get("is_recovery"))
    return {
        "rule_name": rule_name or "Alert",
        "hostname": hostname or "",
        "ip_address": ip_address or "",
        "severity": (severity or "warning").upper(),
        "status": (status or ("RESOLVED" if is_recovery else "ALERT")).upper(),
        "timestamp": now.strftime("%H:%M UTC on %d %b %Y"),
        **phrasing_variables(**phrasing),
    }


# ── Incident lifecycle context ───────────────────────────────────────────────
#
# Every alert delivery says which incident it is about and where that incident
# is in its life: the first mail, a reminder of a continuing fault, an
# escalation, a material change, or the recovery. Without it a recipient cannot
# tell the fourth mail about one memory incident from four separate incidents.

LIFECYCLES = ("new", "reminder", "escalated", "changed", "recovered")
# The ctx keys incident_context returns (copy these into an email ctx).
INCIDENT_KEYS = ("incident_ref", "incident_id", "first_seen", "active_for",
                 "lifecycle", "lifecycle_label", "lifecycle_tag")
_LIFECYCLE_ALIASES = {
    "new": "new", "trigger": "new", "triggered": "new", "late_trigger": "new",
    "reminder": "reminder", "repeat": "reminder",
    "escalated": "escalated", "escalation": "escalated",
    "changed": "changed", "change": "changed",
    "recovered": "recovered", "recovery": "recovered", "resolved": "recovered",
    "all_clear": "recovered",
}


def normalize_lifecycle(lifecycle: Optional[str]) -> str:
    """One of :data:`LIFECYCLES`; ValueError for anything else."""
    key = _LIFECYCLE_ALIASES.get(str(lifecycle or "").strip().lower())
    if key is None:
        raise ValueError(f"unknown alert lifecycle {lifecycle!r}; expected one of {LIFECYCLES}")
    return key


def incident_ref(alert_id) -> str:
    """Short stable incident reference: "INC-3f2a9c1d" for an alert uuid.

    The first 8 hex digits of the uuid (no dashes), so the same incident
    carries the same reference in every mail, the UI and the delivery log.
    "" for a missing id; a non-uuid id is used as it is.
    """
    if alert_id in (None, ""):
        return ""
    raw = str(alert_id).strip()
    try:
        from uuid import UUID
        return f"INC-{UUID(raw).hex[:8]}"
    except (ValueError, AttributeError, TypeError):
        return f"INC-{raw}" if raw else ""


def _as_utc(ts) -> Optional[datetime]:
    if ts in (None, ""):
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def lifecycle_label(lifecycle: str, *, reminder_index: Optional[int] = None,
                    reminder_total: Optional[int] = None,
                    escalation_level: Optional[int] = None) -> str:
    """"New", "Reminder 2 of 3", "Escalated to level 2", "Changed", "Recovered"."""
    kind = normalize_lifecycle(lifecycle)
    if kind == "reminder":
        if reminder_index and reminder_total:
            return f"Reminder {int(reminder_index)} of {int(reminder_total)}"
        if reminder_index:
            return f"Reminder {int(reminder_index)}"
        return "Reminder"
    if kind == "escalated":
        return f"Escalated to level {int(escalation_level)}" if escalation_level else "Escalated"
    return {"new": "New", "changed": "Changed", "recovered": "Recovered"}[kind]


def lifecycle_tag(lifecycle: str, *, reminder_index: Optional[int] = None,
                  reminder_total: Optional[int] = None,
                  escalation_level: Optional[int] = None) -> str:
    """The compact subject form: "New", "Reminder 2/3", "Escalated L2"."""
    kind = normalize_lifecycle(lifecycle)
    if kind == "reminder":
        if reminder_index and reminder_total:
            return f"Reminder {int(reminder_index)}/{int(reminder_total)}"
        return f"Reminder {int(reminder_index)}" if reminder_index else "Reminder"
    if kind == "escalated":
        return f"Escalated L{int(escalation_level)}" if escalation_level else "Escalated"
    return {"new": "New", "changed": "Changed", "recovered": "Recovered"}[kind]


def incident_context(*, alert_id, first_seen, now, lifecycle: str,
                     reminder_index: int | None = None, reminder_total: int | None = None,
                     escalation_level: int | None = None) -> dict:
    """Incident identity and lifecycle keys for a notification ctx.

    Returns ``incident_ref`` ("INC-3f2a9c1d"), ``incident_id`` (the alert id
    as a string), ``first_seen`` (ISO 8601 UTC, "" when unknown),
    ``active_for`` (human duration from first seen to ``now``; on a recovery
    this is the total duration), ``lifecycle`` (one of :data:`LIFECYCLES`),
    ``lifecycle_label`` ("New", "Reminder 2 of 3", "Escalated to level 2",
    "Recovered") and ``lifecycle_tag`` (the subject form). email_render
    shows them in every alert email that carries them; the same keys are
    usable as template variables ({incident_ref}, {lifecycle_label}, ...).
    Aliases such as "trigger"/"recovery"/"escalation" are accepted;
    an unknown lifecycle raises ValueError.
    """
    kind = normalize_lifecycle(lifecycle)
    seen = _as_utc(first_seen)
    at = _as_utc(now) or datetime.now(timezone.utc)
    extras = dict(reminder_index=reminder_index, reminder_total=reminder_total,
                  escalation_level=escalation_level)
    return {
        "incident_ref": incident_ref(alert_id),
        "incident_id": "" if alert_id in (None, "") else str(alert_id),
        "first_seen": seen.isoformat() if seen else "",
        "active_for": duration_between(seen, at) if seen else "",
        "lifecycle": kind,
        "lifecycle_label": lifecycle_label(kind, **extras),
        "lifecycle_tag": lifecycle_tag(kind, **extras),
    }


# Status words a lifecycle tag makes redundant right after "[SEVERITY]":
# "[WARNING] [New] ALERT: ..." says the same thing twice.
_SUBJECT_REDUNDANT = {
    "new": ("ALERT: ",),
    "reminder": ("ALERT: ",),
    "escalated": ("ALERT: ",),
    "changed": ("ALERT: ",),
    "recovered": ("Resolved: ", "RESOLVED: "),
}


def incident_subject(subject: str, ctx: Optional[dict]) -> str:
    """Add the lifecycle tag and incident ref to an email subject.

    "[WARNING] ALERT: Memory high — srv-a" becomes
    "[WARNING] [New] Memory high — srv-a [INC-3f2a9c1d]": the severity stays
    first so existing inbox rules keep matching, the tag follows it, and the
    ref goes last. Idempotent; a ctx without incident keys leaves the subject
    unchanged. A subject the rule's author wrote keeps all of its words.
    """
    subject = subject or ""
    if not ctx:
        return subject
    tag = str(ctx.get("lifecycle_tag") or "").strip()
    ref = str(ctx.get("incident_ref") or "").strip()
    kind = str(ctx.get("lifecycle") or "").strip().lower()
    if not tag and not ref:
        return subject
    head, rest = "", subject
    if rest.startswith("[") and "]" in rest:
        close = rest.index("]") + 1
        head, rest = rest[:close], rest[close:].lstrip()
    if tag and f"[{tag}]" not in (head, rest[:len(tag) + 2]):
        for word in _SUBJECT_REDUNDANT.get(kind, ()):
            if rest.startswith(word):
                rest = rest[len(word):]
                break
        rest = f"[{tag}] {rest}".rstrip()
    out = f"{head} {rest}".strip() if head else rest
    if ref and f"[{ref}]" not in out:
        out = f"{out} [{ref}]".strip()
    return out


# ── Priority in subjects, incident links ─────────────────────────────────────

def priority_subject(subject: str, priority) -> str:
    """Put the priority tag after the leading severity tag:
    "[WARNING] [New] Memory high [INC-3f2a9c1d]" becomes
    "[WARNING] [P3] [New] Memory high [INC-3f2a9c1d]". Idempotent; a subject
    without a leading tag gets the priority first; no priority = unchanged."""
    from app.services.alert_priority import subject_tag

    subject = subject or ""
    tag = subject_tag(priority)
    if not tag or tag in subject:
        return subject
    if subject.startswith("[") and "]" in subject:
        close = subject.index("]") + 1
        return f"{subject[:close]} {tag} {subject[close:].lstrip()}".rstrip()
    return f"{tag} {subject}".rstrip()


# Top-level dashboard sections an alert action_url may point into; what
# precedes the first of them is the dashboard base URL.
_DASHBOARD_SECTIONS = ("/alerts", "/alert-rules", "/services", "/devices", "/servers", "/apm",
                       "/netflow", "/netpath", "/udt", "/sensors", "/network", "/maps")


def dashboard_base(action_url: Optional[str]) -> str:
    """The dashboard base of an absolute action URL ("" when it is not one)."""
    from urllib.parse import urlsplit

    url = str(action_url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path or ""
    cut = len(path)
    for section in _DASHBOARD_SECTIONS:
        i = path.find(section)
        while i >= 0:
            end = i + len(section)
            if end == len(path) or path[end] in "/?#":
                cut = min(cut, i)
                break
            i = path.find(section, i + 1)
    return f"{parts.scheme}://{parts.netloc}{path[:cut].rstrip('/')}"


def incident_links(ctx: Optional[dict]) -> tuple[str, str]:
    """("View incident" URL, "Acknowledge" URL) for an alert email.

    Explicit ``incident_url`` / ``ack_url`` win; otherwise they are built
    from the dashboard base of ``action_url`` and ``incident_id``:
    ``/alerts/<id>`` and ``/alerts/<id>?ack=1``. The acknowledge page needs a
    signed-in user and asks for an explicit confirmation — the link itself
    never changes anything (mail scanners follow links). ("", "") when
    neither can be known.
    """
    c = ctx or {}
    view = str(c.get("incident_url") or "").strip()
    ack = str(c.get("ack_url") or "").strip()
    if not view:
        base = dashboard_base(c.get("action_url"))
        aid = str(c.get("incident_id") or c.get("alert_id") or "").strip()
        if base and aid:
            view = f"{base}/alerts/{aid}"
    if view and not ack:
        ack = f"{view}{'&' if '?' in view else '?'}ack=1"
    return view, ack
