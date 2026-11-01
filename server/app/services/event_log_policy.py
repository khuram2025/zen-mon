"""Windows Event Log filters in agent policies.

The agent treats ``event_log_filters`` from /agents/config as the complete
list of channel/level queries to run: a non-null list replaces its built-in
defaults, so an empty list means "query nothing". Every policy stored ``[]``
(the column default, and the policy editor never set it), so each agent sent
event-log summaries only between enrolling and its first config fetch, and
the Events tab stayed empty from then on.

An empty policy list now means "the default filters", which the appliance
sends explicitly. Turning collection off is the ``event_log`` feature flag,
which the agent applies as an on/off switch.

``validated_filters`` mirrors the agent's own checks
(collectors.validatedEventLogFilters): the agent drops the whole list when
one entry is invalid, so a policy is refused at save time instead.
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_LEVELS = ["critical", "error", "warning"]
DEFAULT_FILTERS: list[dict[str, Any]] = [
    {"channel": "System", "levels": DEFAULT_LEVELS},
    {"channel": "Application", "levels": DEFAULT_LEVELS},
]

# Part of every agent's config ETag. Changing what /agents/config sends for an
# unchanged policy must change the ETag too, or agents holding the old one get
# 304 Not Modified and never see it. Bump this whenever that happens.
CONFIG_REVISION = "event-details-role-profiles-2"

LEVELS = ("critical", "error", "warning", "information", "verbose", "audit")

# collectors.go limits.
MAX_FILTERS = 32
MAX_LEVELS_PER_FILTER = 5
MAX_IDS_PER_FILTER = 64
MAX_TOTAL_LEVELS = 128
MAX_TOTAL_IDS = 512
MAX_CHANNEL_BYTES = 256
MAX_EVENT_ID = 2_147_483_647


def validated_filters(raw: Iterable[Any] | None) -> list[dict[str, Any]]:
    """The policy's filters in stored form (channel trimmed, levels lower-case
    and de-duplicated, IDs as ints). ValueError names the first problem."""
    filters = list(raw or [])
    if len(filters) > MAX_FILTERS:
        raise ValueError(f"At most {MAX_FILTERS} event-log channels")
    out: list[dict[str, Any]] = []
    total_levels = total_ids = 0
    seen: set[str] = set()
    for i, f in enumerate(filters, 1):
        if not isinstance(f, dict):
            raise ValueError(f"Event-log filter {i} must be an object")
        channel = str(f.get("channel") or "").strip()
        if not channel:
            raise ValueError(f"Event-log filter {i} has no channel")
        if len(channel.encode()) > MAX_CHANNEL_BYTES:
            raise ValueError(f"Event-log channel {channel[:40]}… is too long")
        if channel.lower() in seen:
            raise ValueError(f"Event-log channel {channel} is listed twice")
        seen.add(channel.lower())
        levels: list[str] = []
        for lv in f.get("levels") or []:
            lv = str(lv).strip().lower()
            if lv == "info":
                lv = "information"
            if lv not in LEVELS:
                raise ValueError(f"Event-log level {lv!r} for {channel} is not one of {', '.join(LEVELS)}")
            if lv not in levels:
                levels.append(lv)
        if not levels:
            raise ValueError(f"Event-log channel {channel} has no levels")
        if len(levels) > MAX_LEVELS_PER_FILTER:
            raise ValueError(f"Event-log channel {channel} has more than {MAX_LEVELS_PER_FILTER} levels")
        ids: list[int] = []
        for v in f.get("ids") or []:
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"Event ID {v!r} for {channel} is not a number") from None
            if not 1 <= n <= MAX_EVENT_ID:
                raise ValueError(f"Event ID {n} for {channel} is out of range")
            if n not in ids:
                ids.append(n)
        if len(ids) > MAX_IDS_PER_FILTER:
            raise ValueError(f"Event-log channel {channel} has more than {MAX_IDS_PER_FILTER} event IDs")
        total_levels += len(levels)
        total_ids += len(ids)
        if total_levels > MAX_TOTAL_LEVELS:
            raise ValueError(f"At most {MAX_TOTAL_LEVELS} event-log levels in all")
        if total_ids > MAX_TOTAL_IDS:
            raise ValueError(f"At most {MAX_TOTAL_IDS} event IDs in all")
        item: dict[str, Any] = {"channel": channel, "levels": levels}
        if ids:
            item["ids"] = ids
        out.append(item)
    return out


def effective_filters(stored: Iterable[Any] | None, profile: int | None = None) -> list[dict[str, Any]]:
    """What /agents/config sends: the policy's own filters, or the defaults when
    it has none. A stored list the agent would reject (written before
    validation existed) also falls back to the defaults rather than silencing
    the collector."""
    try:
        filters = validated_filters(stored)
    except ValueError:
        filters = []
    if filters:
        return filters
    if profile is not None:
        return role_filters(profile)
    return [dict(f, levels=list(f["levels"])) for f in DEFAULT_FILTERS]


def role_filters(profile: int) -> list[dict[str, Any]]:
    """Explicitly assigned server roles; never infer a role from its hostname.

    Details-capable agents always collect OS lifecycle evidence separately.
    Busy audit success/process creation streams are intentionally opt-in.
    """
    base = [{"channel": "System", "levels": ["critical", "error"]},
            {"channel": "Application", "levels": ["critical", "error"]}]
    if profile == 1:  # Domain controller
        base += [{"channel": "Security", "levels": ["audit"],
                  "ids": [4625, 4740, 4720, 4726, 4728, 4732, 4756, 1102]},
                 {"channel": "Directory Service", "levels": ["critical", "error", "warning"]},
                 {"channel": "DNS Server", "levels": ["critical", "error", "warning"]},
                 {"channel": "DFS Replication", "levels": ["critical", "error"]}]
    elif profile == 2:  # IIS
        base += [{"channel": "Microsoft-Windows-IIS-Configuration/Operational", "levels": ["critical", "error", "warning"]}]
    elif profile == 3:  # SQL Server; selected warnings in addition to errors
        base += [{"channel": "Application", "levels": ["warning"], "ids": [825, 17883, 17884, 17887, 17888]}]
    return base


def validated_limits(raw: dict) -> dict:
    bounds = {"event_profile": (0, 3), "event_interval_s": (60, 3600),
              "event_max_events": (50, 1000), "event_max_bytes": (16384, 1048576),
              "event_message_bytes": (256, 8192)}
    for key, (lo, hi) in bounds.items():
        if key in raw and (isinstance(raw[key], bool) or not isinstance(raw[key], int) or not lo <= raw[key] <= hi):
            raise ValueError(f"{key} must be an integer between {lo} and {hi}")
    return raw
