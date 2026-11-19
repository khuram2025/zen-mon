"""Name a device status alert after the condition that triggered it.

Status alerts used to be titled from the rule name and the new status: every
alert from a rule called "High Latency" read "High Latency: fw-01 (10.0.0.1) is
DEGRADED", even when the device was degraded by packet loss (field review
D-09). The title now says what was measured against which limit:

    Packet loss 33% (limit 10%)
    Latency 412 ms (limit 100 ms)
    Packet loss 33% (limit 10%) · latency 412 ms (limit 100 ms)
    No response
    Responding again

Only the wording changes. Which rules match, their thresholds and the
confirmed status (docs/device-status-policy.md) are untouched: the limits
quoted are the status policy's own Degraded limits, and the over-limit test is
the status machine's canonical predicate. The check that confirmed a window
degradation can itself be within limits ("3 of the last 5 checks over
limits"); then the title says so instead of quoting a reading that was fine.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.services import device_status_machine as dsm


def fmt_number(value: float) -> str:
    """Readable figure: whole numbers from 10 up, one decimal below (33, 412, 2.5, 0.4)."""
    v = float(value)
    if abs(v) >= 10:
        whole = math.floor(abs(v) + 0.5)  # half up: 12.5 -> 13, not to even
        return str(-whole if v < 0 else whole)
    return f"{v:.1f}".rstrip("0").rstrip(".") or "0"


def status_condition(new_status: str, old_status: str | None, rtt_ms: float | None,
                     loss_pct: float | None, policy: dsm.Policy | None) -> dict[str, Any]:
    """Title and structured reading for one confirmed transition.

    ``loss_pct`` is a percentage (0-100); ``rtt_ms`` is None for a check
    without a reply. Returns ``title`` plus ``metric``, ``value`` and ``limit``
    (the first over-limit reading, or None)."""
    new = (new_status or "").lower()
    old = (old_status or "").lower()
    if new == "down":
        return {"title": "No response", "metric": "reachability", "value": None, "limit": None}
    if new == "up":
        if old == "down":
            return {"title": "Responding again", "metric": "reachability", "value": None, "limit": None}
        if old == "degraded":
            return {"title": "Latency and packet loss back within limits", "metric": "performance",
                    "value": None, "limit": None}
        return {"title": "Up", "metric": "status", "value": None, "limit": None}
    if new == "degraded":
        policy = policy or dsm.Policy()
        parts: list[tuple[str, str, float, float]] = []
        if loss_pct is not None and dsm.exceeds_pct(float(loss_pct) / 100.0, policy.degraded_loss_pct):
            parts.append(("packet_loss", f"packet loss {fmt_number(loss_pct)}% (limit {dsm.fmt3(policy.degraded_loss_pct)}%)",
                          float(loss_pct), float(policy.degraded_loss_pct)))
        if rtt_ms is not None and dsm.exceeds_ms(rtt_ms, policy.degraded_rtt_ms):
            parts.append(("rtt", f"latency {fmt_number(rtt_ms)} ms (limit {dsm.fmt3(policy.degraded_rtt_ms)} ms)",
                          float(rtt_ms), float(policy.degraded_rtt_ms)))
        if not parts:
            return {"title": "Latency or packet loss over limits on recent checks", "metric": "performance",
                    "value": None, "limit": None}
        title = " · ".join(p[1] for p in parts)
        metric, _, value, limit = parts[0]
        return {"title": title[0].upper() + title[1:], "metric": metric, "value": value, "limit": limit}
    return {"title": f"Status {new or 'unknown'}", "metric": "status", "value": None, "limit": None}


def alert_message(condition_title: str, hostname: str, ip_address: str | None) -> str:
    """The stored alert message: the condition first, then the device."""
    where = f"{hostname} ({ip_address})" if ip_address else hostname
    return f"{condition_title} — {where}"


# ── Rules on the device's own latency / packet loss ──────────────────────────
#
# A rule on rtt or packet_loss fired because of its own reading against its
# own limit; the status policy's cause may be something else (a "High
# latency" rule on a device the policy degraded for packet loss). Its message
# leads with the rule's reading and adds the status cause only when it
# differs (audit T-04):
#
#     Round-trip time 150 ms (limit 100 ms) — fw-01 (10.0.0.1); status degraded: packet loss 33% (limit 10%)

RULE_READING_METRICS = frozenset({"rtt", "packet_loss"})
_PART_METRIC = re.compile(r"^(packet loss|latency) [0-9]", re.IGNORECASE)


def _part_metric(part: str) -> str | None:
    m = _PART_METRIC.match(part.strip())
    if not m:
        return None
    return "packet_loss" if m.group(1).lower() == "packet loss" else "rtt"


def status_cause(rule_metric: str | None, new_status: str | None, title: str | None) -> str:
    """"status degraded: packet loss 33% (limit 10%)" — the parts of the
    status condition ``title`` that are not the rule's own metric; "" when
    there are none, or the rule is not an rtt/packet-loss rule."""
    if rule_metric not in RULE_READING_METRICS or not title:
        return ""
    new = (new_status or "").lower()
    # Down is "no response": a loss rule at 100% already says that.
    if new != "degraded":
        return ""
    parts = [p.strip() for p in str(title).split(" · ") if p.strip()]
    others = [p for p in parts if _part_metric(p) != rule_metric]
    if not others:
        return ""
    text_ = " and ".join(p[:1].lower() + p[1:] for p in others)
    return f"status {new}: {text_}"


def rule_reading_title(rule_metric: str, value, operator, threshold) -> str:
    """"Round-trip time 150 ms (limit 100 ms)" — the rule's own reading."""
    from app.services import alert_phrasing as ap

    noun = ap.metric_noun(rule_metric)
    if value is None:
        reading = "unavailable (no reply)" if rule_metric == "rtt" else ap.UNAVAILABLE
    else:
        reading = ap.format_reading(rule_metric, value, operator, threshold)
    limit = ap.format_threshold(rule_metric, threshold, operator) if threshold is not None else ""
    return f"{noun} {reading}" + (f" (limit {limit})" if limit else "")


def rule_alert_message(rule_metric: str, value, operator, threshold, hostname: str,
                       ip_address: str | None, cause: str = "") -> str:
    """The stored message of an rtt/packet-loss rule's alert."""
    msg = alert_message(rule_reading_title(rule_metric, value, operator, threshold),
                        hostname, ip_address)
    return f"{msg}; {cause}" if cause else msg
