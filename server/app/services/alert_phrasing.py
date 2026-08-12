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
    "trap": {"noun": "SNMP trap", "subject": "the device",
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


def format_value(metric: Optional[str], value) -> str:
    """Render a reading or threshold with its unit — 80 -> "80%", 200 -> "200 ms"."""
    if value in (None, ""):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    info = METRICS.get((metric or "").strip(), {})
    unit = info.get("unit", "")
    num /= float(info.get("scale") or 1.0)
    decimals = int(info.get("decimals", 1))
    # A link rule written in bits reads as "1.5 Gbps", never "1500 Mbps".
    if unit == " Mbps" and abs(num) >= 1000:
        num /= 1000.0
        unit, decimals = " Gbps", 2
    text_ = f"{num:.{decimals}f}"
    # Drop a trailing ".0" — "80%" reads better than "80.0%".
    if "." in text_:
        text_ = text_.rstrip("0").rstrip(".")
    return f"{text_}{unit}"


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
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        thr = 0.0
    healthy = float(METRICS[metric].get("healthy", 0.0))
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
                     *, tense: str = "present", subject: Optional[str] = None) -> str:
    """One condition as a clause: "Interface utilisation is above 80%".

    ``tense`` is "present" (describing the rule), "past" (it just happened) or
    "cleared" (it just stopped happening). ``subject`` names who the sentence is
    about — a hostname reads better than "the device" — and applies only to
    state metrics; a quantity is always described by its own noun, never by the
    host ("core-router-01 rose above 80%" is not a sentence anyone wants).
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
    value = format_value(key, threshold)
    return " ".join(p for p in (noun, verb, value) if p)


def condition_label(metric: Optional[str], operator: Optional[str], threshold) -> str:
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
    return " ".join(p for p in (metric_noun(key), verb, format_value(key, threshold)) if p)


def conditions_label(conditions: Optional[Iterable[dict]], logic: str = "AND",
                     *, metric: Optional[str] = None, operator: Optional[str] = None,
                     threshold=None) -> str:
    """Label for a rule that may carry several conditions.

    Falls back to the rule's single metric/operator/threshold columns when the
    ``conditions`` array is empty, which is how most rules are still stored.
    """
    parts = [
        condition_label(c.get("metric"), c.get("operator"), c.get("threshold"))
        for c in (conditions or [])
        if isinstance(c, dict) and c.get("metric")
    ]
    if not parts:
        if not metric:
            return ""
        return condition_label(metric, operator, threshold)
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


# ── Default message bodies ───────────────────────────────────────────────────
#
# These are `{placeholder}` templates, not finished prose: a rule may override
# any of them, and the wizard shows the effective template for editing. The
# variables they lean on are built by `message_variables()` below.

DEFAULT_EMAIL_SUBJECT = "[{severity}] {status}: {rule_name}"
DEFAULT_RECOVERY_EMAIL_SUBJECT = "[{severity}] Resolved: {rule_name}"

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


def phrasing_variables(*, metric: Optional[str] = None, operator: Optional[str] = None,
                       threshold=None, conditions: Optional[Iterable[dict]] = None,
                       condition_logic: str = "AND", reading: Optional[str] = None,
                       duration: str = "", is_recovery: bool = False,
                       subject_noun: Optional[str] = None) -> dict:
    """The condition/duration half of the template variables.

    Kept separate from the identity half (host, severity, timestamps) because
    every evaluator already knows who it is talking about, but each phrases the
    condition the same way. ``reading`` is the current value already formatted
    for display ("87%"), which the evaluators know and the rule columns do not.
    """
    tense = "cleared" if is_recovery else "past"
    phrase = condition_phrase(metric, operator, threshold, tense=tense)
    present = condition_phrase(metric, operator, threshold, tense="present")
    label = conditions_label(conditions, condition_logic, metric=metric,
                             operator=operator, threshold=threshold)

    key = (metric or "").strip()
    is_state = "state" in METRICS.get(key, {})

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
            text_ += f" ({'now' if is_recovery else 'currently'} {reading})"
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

    return {
        "event_sentence": _finish(event),
        "metric": metric or "",
        "metric_label": metric_noun(metric),
        "operator": operator or "",
        # `{threshold}` stays the raw number: templates written before this
        # module existed say "{threshold}%", and a formatted value would double
        # the unit. `{threshold_value}` is the formatted one.
        "threshold": "" if threshold is None else str(threshold),
        "threshold_value": format_value(metric, threshold) if threshold is not None else "",
        "condition": label or present,
        "condition_label": label or present,
        "condition_sentence": sentence,
        "recovery_sentence": sentence,
        "reading": reading or "",
        "duration": duration,
        # Both read as a clause appended after a finished sentence, so a
        # template can end with one without gluing words together.
        "duration_sentence": f" The condition was active for {duration}." if duration else "",
        "duration_suffix": f" Active for {duration}." if duration else "",
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
                  subject_noun: Optional[str] = None) -> dict:
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
