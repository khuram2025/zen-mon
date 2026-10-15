"""Confirmed device status: the reference state machine shared with the Go poller.

One check (one poll-cycle result) at a time, ``step`` turns the persisted
``State`` plus a ``Check`` into the next state and the events to publish:

* soft/hard states: DOWN needs ``down_confirm_checks`` consecutive failures
  (``sensor_down_confirm_checks`` for remote-sensor checks), DEGRADED needs
  ``degraded_confirm_checks`` bad checks (slow, lossy or unanswered) out of the
  last ``degraded_window_checks``;
* hysteresis: DEGRADED clears after ``degraded_clear_checks`` consecutive
  checks within the recovery limits, or after a full window with no bad check;
* flap detection over a time window of confirmed changes, per class
  (reachability = into/out of down, performance = up<->degraded), with a
  count stop and a stability stop. Flapping is a flag, never a status.

``step`` is pure (no I/O, no wall clock: only ``check.ts``) and never mutates
its input. The Go package ``poller/internal/devicestatus`` implements the same
contract; ``scripts/dev/device-status-contract/vectors.json`` (built from this
module by ``build_vectors.py``) holds both languages to identical states,
events and reason strings.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

OWNER_CENTRAL = "central"
HARD_STATUSES = ("up", "degraded", "down", "unknown")
SEEDED_STATUSES = ("up", "degraded", "down")

STREAK_CAP = 100          # streak counters saturate here (columns are SMALLINT)
CHANGES_CAP = 64          # newest change times kept per class
GAP_FLOOR_SECONDS = 180   # a silence longer than max(3 x interval, this) resets confirmation

MS_EPSILON = 1e-6         # exceeds_ms tolerance
PCT_EPSILON = 1e-4        # exceeds_pct tolerance (absorbs float32 loss ratios)

REASON_MAINTENANCE = "Maintenance window active"

# Stored 'monitoring' document defaults. None for the recovery limits means
# "derive from the degraded limit": round3(0.8 * limit).
DEFAULTS: dict[str, Any] = {
    "degraded_rtt_ms": 100.0,
    "degraded_loss_pct": 10.0,
    "degraded_rtt_clear_ms": None,
    "degraded_loss_clear_pct": None,
    "degraded_confirm_checks": 3,
    "degraded_window_checks": 5,
    "degraded_clear_checks": 3,
    "down_confirm_checks": 3,
    "sensor_down_confirm_checks": 1,
    "up_confirm_checks": 1,
    "flap_detection_enabled": True,
    "flap_window_minutes": 30,
    "flap_start_changes": 4,
    "flap_stop_changes": 1,
    "flap_stable_minutes": 10,
}

# Integer fields: (minimum, maximum) applied before the cross-field clamps.
INT_RANGES: dict[str, tuple[int, int]] = {
    "degraded_confirm_checks": (1, 20),
    "degraded_window_checks": (1, 20),
    "degraded_clear_checks": (1, 20),
    "down_confirm_checks": (1, 10),
    "sensor_down_confirm_checks": (1, 10),
    "up_confirm_checks": (1, 10),
    "flap_window_minutes": (5, 1440),
    "flap_start_changes": (3, 64),
    "flap_stop_changes": (0, 63),
    "flap_stable_minutes": (1, 1440),
}
RTT_MAX_MS = 10000.0
LOSS_MIN_PCT, LOSS_MAX_PCT = 1.0, 100.0

# Single-check behaviour of the releases before confirmation (offered in the UI).
LEGACY_PRESET_KEYS = {
    "degraded_confirm_checks": 1,
    "degraded_window_checks": 1,
    "degraded_clear_checks": 1,
    "flap_detection_enabled": False,
}


# --------------------------------------------------------------------------- policy

@dataclass(frozen=True)
class Policy:
    degraded_rtt_ms: float = 100.0
    degraded_loss_pct: float = 10.0
    degraded_rtt_clear_ms: float = 80.0
    degraded_loss_clear_pct: float = 8.0
    degraded_confirm_checks: int = 3
    degraded_window_checks: int = 5
    degraded_clear_checks: int = 3
    down_confirm_checks: int = 3
    sensor_down_confirm_checks: int = 1
    up_confirm_checks: int = 1
    flap_detection_enabled: bool = True
    flap_window_minutes: int = 30
    flap_start_changes: int = 4
    flap_stop_changes: int = 1
    flap_stable_minutes: int = 10


def round3(value: float) -> float:
    """Round to 3 decimals: the correctly rounded decimal of the exact binary
    value, ties to even, like ``fmt3`` (Go: ParseFloat(FormatFloat(v, 'f', 3))).
    Scaling by 1000 first would add its own rounding error (369.4935 -> .494)."""
    return round(float(value), 3)


def _number(value: Any) -> float | int | None:
    """A JSON number (bool excluded), finite; anything else means 'use the default'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        as_float = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(as_float) else None


def _int_field(raw: Mapping[str, Any], key: str) -> int:
    lo, hi = INT_RANGES[key]
    value = _number(raw.get(key))
    if value is None or (isinstance(value, float) and not value.is_integer()):
        return DEFAULTS[key]
    return int(min(max(value, lo), hi))


def policy_from_settings(raw: Mapping[str, Any] | None) -> Policy:
    """Effective policy from the stored 'monitoring' JSON document.

    Every field is parsed on its own: a missing, null, non-numeric (bool and
    strings included) or non-finite value takes the default, integer fields
    accept only integral numbers (3.0 is 3, 3.5 is the default), out-of-range
    values clamp to their range. Then, in this order: rtt, loss,
    rtt_clear = min(rtt_clear, rtt), loss_clear = min(loss_clear, loss), confirm,
    window = max(window, confirm), clear, down, sensor_down, up, flap_window,
    start, stop = min(stop, start-1), stable = min(stable, flap_window).
    The exclusive lower bounds take the default instead of clamping:
    degraded_rtt_ms <= 0 is 100 (0 has always meant "unset"), a recovery
    latency limit <= 0 is the derived one.
    """
    raw = raw if isinstance(raw, Mapping) else {}

    rtt = _number(raw.get("degraded_rtt_ms"))
    rtt = DEFAULTS["degraded_rtt_ms"] if rtt is None or rtt <= 0 else min(float(rtt), RTT_MAX_MS)
    loss = _number(raw.get("degraded_loss_pct"))
    loss = DEFAULTS["degraded_loss_pct"] if loss is None else min(max(float(loss), LOSS_MIN_PCT), LOSS_MAX_PCT)

    rtt_clear = _number(raw.get("degraded_rtt_clear_ms"))
    rtt_clear = round3(0.8 * rtt) if rtt_clear is None or rtt_clear <= 0 else min(float(rtt_clear), RTT_MAX_MS)
    rtt_clear = min(rtt_clear, rtt)
    loss_clear = _number(raw.get("degraded_loss_clear_pct"))
    loss_clear = round3(0.8 * loss) if loss_clear is None else min(max(float(loss_clear), 0.0), LOSS_MAX_PCT)
    loss_clear = min(loss_clear, loss)

    confirm = _int_field(raw, "degraded_confirm_checks")
    window = max(_int_field(raw, "degraded_window_checks"), confirm)
    flap_window = _int_field(raw, "flap_window_minutes")
    start = _int_field(raw, "flap_start_changes")
    enabled = raw.get("flap_detection_enabled")
    return Policy(
        degraded_rtt_ms=rtt,
        degraded_loss_pct=loss,
        degraded_rtt_clear_ms=rtt_clear,
        degraded_loss_clear_pct=loss_clear,
        degraded_confirm_checks=confirm,
        degraded_window_checks=window,
        degraded_clear_checks=_int_field(raw, "degraded_clear_checks"),
        down_confirm_checks=_int_field(raw, "down_confirm_checks"),
        sensor_down_confirm_checks=_int_field(raw, "sensor_down_confirm_checks"),
        up_confirm_checks=_int_field(raw, "up_confirm_checks"),
        flap_detection_enabled=enabled if isinstance(enabled, bool) else DEFAULTS["flap_detection_enabled"],
        flap_window_minutes=flap_window,
        flap_start_changes=start,
        flap_stop_changes=min(_int_field(raw, "flap_stop_changes"), start - 1),
        flap_stable_minutes=min(_int_field(raw, "flap_stable_minutes"), flap_window),
    )


def effective_policy_dict(policy: Policy) -> dict[str, Any]:
    """The loader output as a plain dict (the API's ``effective`` object)."""
    return asdict(policy)


def legacy_preset(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``raw`` with the single-check (legacy) preset applied.

    Recovery limits equal the effective degraded limits, so the hysteresis band
    is empty and one clean check clears DEGRADED as before confirmation existed.
    """
    doc = dict(raw) if isinstance(raw, Mapping) else {}
    base = policy_from_settings(doc)
    doc.update(LEGACY_PRESET_KEYS)
    doc["degraded_rtt_clear_ms"] = base.degraded_rtt_ms
    doc["degraded_loss_clear_pct"] = base.degraded_loss_pct
    return doc


def down_confirm_for(policy: Policy, source: str) -> int:
    return policy.sensor_down_confirm_checks if source == "sensor" else policy.down_confirm_checks


# --------------------------------------------------------------------------- predicates and text

def fmt3(value: float) -> str:
    """Fixed 3 decimals, trailing zeros and dot trimmed: 500, 5.6, 1234.568."""
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def exceeds_ms(value: float, limit: float) -> bool:
    return float(value) - float(limit) > MS_EPSILON


def exceeds_pct(loss_ratio: float, limit_pct: float) -> bool:
    return float(loss_ratio) * 100.0 - float(limit_pct) > PCT_EPSILON


def classify(rtt_ms: float | None, loss: float | None, policy: Policy) -> str:
    """'breach', 'clear' or 'marginal' (inside the hysteresis band) for a responding check."""
    rtt = float(rtt_ms or 0.0)
    ratio = float(loss or 0.0)
    if exceeds_ms(rtt, policy.degraded_rtt_ms) or exceeds_pct(ratio, policy.degraded_loss_pct):
        return "breach"
    if not exceeds_ms(rtt, policy.degraded_rtt_clear_ms) and not exceeds_pct(ratio, policy.degraded_loss_clear_pct):
        return "clear"
    return "marginal"


def degraded_reason(rtt_ms: float | None, loss_ratio: float | None, rtt_limit: float, loss_limit_pct: float) -> str:
    """Per-sample text for a check over the degraded limits (canonical predicates)."""
    parts = []
    loss_pct = float(loss_ratio or 0.0) * 100.0
    if rtt_ms is not None and exceeds_ms(rtt_ms, rtt_limit):
        rtt = float(rtt_ms)
        parts.append(f"Latency {rtt:.3f} ms exceeds {fmt3(rtt_limit)} ms by {rtt - float(rtt_limit):.3f} ms")
    if exceeds_pct(loss_ratio or 0.0, loss_limit_pct):
        parts.append(f"Packet loss {loss_pct:.2f}% exceeds {fmt3(loss_limit_pct)}% "
                     f"by {loss_pct - float(loss_limit_pct):.2f} percentage points")
    return "; ".join(parts) or "High latency or packet loss"


# --------------------------------------------------------------------------- state, check, event

def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class State:
    hard_status: str = "unknown"
    in_maintenance: bool = False
    status_since: datetime | None = None
    fail_streak: int = 0
    ok_streak: int = 0
    clear_streak: int = 0
    window_bits: int = 0
    window_len: int = 0
    reach_changes: list[datetime] = field(default_factory=list)
    perf_changes: list[datetime] = field(default_factory=list)
    reach_flapping: bool = False
    perf_flapping: bool = False
    flapping_since: datetime | None = None
    last_sample_at: datetime | None = None
    owner: str = OWNER_CENTRAL

    @property
    def displayed(self) -> str:
        return "maintenance" if self.in_maintenance else self.hard_status

    @property
    def is_flapping(self) -> bool:
        return self.reach_flapping or self.perf_flapping

    @property
    def flap_kind(self) -> str | None:
        return _kind(self.reach_flapping, self.perf_flapping)


@dataclass(frozen=True)
class Check:
    """One poll-cycle result. ``ordered`` is True for the central poller (single
    sequential writer) and False for remote-sensor samples."""
    ts: datetime
    responding: bool
    rtt_ms: float | None = None
    loss: float = 0.0
    interval_s: float = 0
    maintenance: bool = False
    ordered: bool = True
    source: str = "central"

    def __post_init__(self):
        object.__setattr__(self, "ts", _utc(self.ts))


@dataclass(frozen=True)
class Event:
    """type 'transition' (old/new/reason/alert/flapping), 'flap_start' (kind) or
    'flap_stop' (end_reason). ``flapping`` is the transition's class flapping
    state after this check; for flap events it is the device flag afterwards."""
    type: str
    old: str | None = None
    new: str | None = None
    reason: str | None = None
    alert: bool = False
    flapping: bool = False
    kind: str | None = None
    end_reason: str | None = None


def _kind(reach: bool, perf: bool) -> str | None:
    if reach and perf:
        return "both"
    if reach:
        return "reachability"
    if perf:
        return "performance"
    return None


def copy_state(state: State) -> State:
    return replace(state, reach_changes=list(state.reach_changes), perf_changes=list(state.perf_changes))


def window_counts(state: State, policy: Policy) -> tuple[int, int]:
    """(bad_count, eff) of the window ring read with the policy's window size."""
    size = policy.degraded_window_checks
    eff = min(state.window_len, size)
    return bin(state.window_bits & ((1 << eff) - 1)).count("1"), eff


def _push(state: State, bad: bool, size: int) -> None:
    state.window_bits = ((state.window_bits << 1) | int(bad)) & ((1 << size) - 1)
    state.window_len = min(state.window_len + 1, size)


def _clear_window(state: State) -> None:
    state.window_bits = 0
    state.window_len = 0


def _change_class(prev: str, new: str) -> str | None:
    if prev == "unknown" or new == "unknown":
        return None
    return "reach" if "down" in (prev, new) else "perf"


_EXIT_REASONS = {
    "up": "Maintenance ended; device responding normally",
    "degraded": "Maintenance ended; latency or loss above limits",
}


def _exit_reason(s: State, policy: Policy) -> str:
    """Maintenance exit text. Still down while answering (up_confirm_checks > 1,
    recovery not confirmed yet): the failure count is 0, so report the progress."""
    if s.hard_status in _EXIT_REASONS:
        return _EXIT_REASONS[s.hard_status]
    if s.fail_streak > 0:
        return f"Maintenance ended; no response for {s.fail_streak} consecutive checks"
    return (f"Maintenance ended; responding for {s.ok_streak} of {policy.up_confirm_checks} "
            "checks needed to confirm recovery")


# --------------------------------------------------------------------------- step

def step(state: State, check: Check, policy: Policy) -> tuple[State, list[Event]]:
    """Apply one check. Returns a new state and the events in publish order:
    [maintenance entry?, transition?, flap_start | flap_stop?]."""
    ts = check.ts
    last = state.last_sample_at
    s = copy_state(state)

    # 0. Ordering and gaps.
    if last is not None:
        if ts <= last:
            if not check.ordered:
                return s, []  # duplicate or late remote-sensor sample: ignored entirely
            # Central clock stepped back: accept, forget changes from the "future".
            s.reach_changes = [c for c in s.reach_changes if c <= ts]
            s.perf_changes = [c for c in s.perf_changes if c <= ts]
        elif ts - last > timedelta(seconds=max(3 * float(check.interval_s or 0), GAP_FLOOR_SECONDS)):
            s.fail_streak = s.ok_streak = s.clear_streak = 0
            _clear_window(s)
    s.last_sample_at = ts
    if s.hard_status not in HARD_STATUSES:
        s.hard_status = "unknown"  # a status outside the contract is unconfirmed

    # 1. Maintenance entry.
    entry_event = None
    maintenance_stop = False
    if check.maintenance and not s.in_maintenance:
        entry_event = Event("transition", old=s.hard_status, new="maintenance",
                            reason=REASON_MAINTENANCE, alert=False, flapping=False)
        s.in_maintenance = True
        maintenance_stop = s.is_flapping
        s.reach_changes, s.perf_changes = [], []
        s.reach_flapping = s.perf_flapping = False
        s.flapping_since = None

    # 2. Hard-status machine.
    prev = s.hard_status
    nxt, reason, cause, cls = prev, None, None, None
    size = policy.degraded_window_checks
    confirm = policy.degraded_confirm_checks
    if not check.responding:
        s.fail_streak = min(s.fail_streak + 1, STREAK_CAP)
        s.ok_streak = s.clear_streak = 0
        if prev != "down":
            _push(s, True, size)
            if s.fail_streak >= down_confirm_for(policy, check.source):
                nxt, cause = "down", "fail"
                reason = f"No response for {s.fail_streak} consecutive checks"
    else:
        rtt = float(check.rtt_ms or 0.0)
        loss = float(check.loss or 0.0)
        cls = classify(rtt, loss, policy)
        breach = cls == "breach"
        s.fail_streak = 0
        if prev in ("down", "unknown"):
            reached = True
            if prev == "down":
                s.ok_streak = min(s.ok_streak + 1, STREAK_CAP)
                reached = s.ok_streak >= policy.up_confirm_checks
                cause = "ok"
            if reached:
                # The first accepted answer is a fresh baseline: degraded must be
                # confirmed on its own (only confirm == 1 can land in degraded here).
                _clear_window(s)
                _push(s, breach, size)
                bad, _ = window_counts(s, policy)
                nxt = "degraded" if breach and bad >= confirm else "up"
                if prev == "unknown":
                    reason = (degraded_reason(rtt, loss, policy.degraded_rtt_ms, policy.degraded_loss_pct)
                              if nxt == "degraded" else "Device responding normally")
                elif nxt == "degraded":
                    reason = "Responding again; " + degraded_reason(rtt, loss, policy.degraded_rtt_ms,
                                                                    policy.degraded_loss_pct)
                elif policy.up_confirm_checks == 1:
                    reason = "Responding again"
                else:
                    reason = f"Responding for {s.ok_streak} consecutive checks"
        elif prev == "up":
            _push(s, breach, size)
            bad, eff = window_counts(s, policy)
            if bad >= confirm:
                nxt = "degraded"
                window_text = f"{bad} of the last {eff} checks over limits or unanswered"
                if breach:
                    reason = (degraded_reason(rtt, loss, policy.degraded_rtt_ms, policy.degraded_loss_pct)
                              + "; " + window_text)
                else:
                    reason = window_text + "; latest check within limits"
        elif prev == "degraded":
            _push(s, breach, size)
            s.clear_streak = min(s.clear_streak + 1, STREAK_CAP) if cls == "clear" else 0
            bad, eff = window_counts(s, policy)
            if s.clear_streak >= policy.degraded_clear_checks:
                nxt, cause = "up", "clear"
                reason = (f"Within recovery limits ({fmt3(policy.degraded_rtt_clear_ms)} ms, "
                          f"{fmt3(policy.degraded_loss_clear_pct)}%) for {s.clear_streak} consecutive checks")
            elif s.window_len >= size and bad == 0:
                nxt = "up"
                reason = f"No slow, lossy or unanswered checks in the last {eff} checks"

    change_class = None
    transition = None
    if nxt != prev:
        if cause != "fail":
            s.fail_streak = 0
        if cause != "ok":
            s.ok_streak = 0
        if cause != "clear":
            s.clear_streak = 0
        s.hard_status = nxt
        s.status_since = ts
        _clear_window(s)
        if check.responding:
            _push(s, cls == "breach", size)
        change_class = _change_class(prev, nxt)
        if not s.in_maintenance:
            if change_class == "reach":
                s.reach_changes.append(ts)
            elif change_class == "perf":
                s.perf_changes.append(ts)
            transition = (prev, nxt, reason)

    # 3. Maintenance exit (an unconfirmed 'unknown' keeps the overlay).
    exit_event = None
    if not check.maintenance and s.in_maintenance and s.hard_status != "unknown":
        s.in_maintenance = False
        exit_event = Event("transition", old="maintenance", new=s.hard_status,
                           reason=_exit_reason(s, policy), alert=True, flapping=False)

    # 4. Flap detection (not while the maintenance overlay is shown).
    flap_event = None
    if not s.in_maintenance:
        flap_event = _flap_step(s, ts, policy)

    events: list[Event] = []
    if entry_event:
        events.append(entry_event)
    if transition:
        old, new, text = transition
        class_flapping = (s.reach_flapping if change_class == "reach"
                          else s.perf_flapping if change_class == "perf" else False)
        events.append(Event("transition", old=old, new=new, reason=text, alert=True, flapping=class_flapping))
    if exit_event:
        events.append(exit_event)
    if maintenance_stop:
        events.append(Event("flap_stop", alert=False, flapping=False, end_reason="maintenance"))
    elif flap_event:
        events.append(flap_event)
    return s, events


def _flap_step(s: State, ts: datetime, policy: Policy) -> Event | None:
    was = s.is_flapping
    lo = ts - timedelta(minutes=policy.flap_window_minutes)
    stable_lo = ts - timedelta(minutes=policy.flap_stable_minutes)
    stop_reason = None
    for name in ("reach", "perf"):
        changes = [c for c in getattr(s, f"{name}_changes") if c > lo][-CHANGES_CAP:]
        flapping = getattr(s, f"{name}_flapping")
        if not policy.flap_detection_enabled:
            if flapping:
                flapping, stop_reason = False, "disabled"
        else:
            if not flapping and len(changes) >= policy.flap_start_changes:
                flapping = True
            if flapping:
                stable = bool(changes) and changes[-1] <= stable_lo
                if stable or len(changes) <= policy.flap_stop_changes:
                    flapping, stop_reason = False, "stable"
                    if stable:
                        changes = []  # a restart needs flap_start_changes fresh changes
        setattr(s, f"{name}_changes", changes)
        setattr(s, f"{name}_flapping", flapping)

    now = s.is_flapping
    if now and not was:
        s.flapping_since = ts
        return Event("flap_start", alert=False, flapping=True, kind=s.flap_kind)
    if was and not now:
        s.flapping_since = None
        return Event("flap_stop", alert=False, flapping=False, end_reason=stop_reason)
    return None


# --------------------------------------------------------------------------- progress, seeding

def pending(state: State, policy: Policy, source: str = "central") -> dict[str, Any] | None:
    """Unconfirmed progress towards the next hard status, or None.

    A failure streak (towards DOWN) outranks window evidence (towards DEGRADED).
    """
    hard = state.hard_status
    if hard != "down" and state.fail_streak > 0:
        return {"target": "down", "count": state.fail_streak, "required": down_confirm_for(policy, source)}
    if hard == "down":
        if state.ok_streak > 0:
            return {"target": "up", "count": state.ok_streak, "required": policy.up_confirm_checks}
        return None
    if hard == "up":
        bad, _ = window_counts(state, policy)
        if bad > 0:
            return {"target": "degraded", "count": bad, "required": policy.degraded_confirm_checks}
    if hard == "degraded" and state.clear_streak > 0:
        return {"target": "up", "count": state.clear_streak, "required": policy.degraded_clear_checks}
    return None


def seed_state(devices_status: str | None, status_changed_at: datetime | None, owner: str) -> State:
    """Silent (re)seed from the devices row: no events, no counters, no flapping."""
    return State(
        hard_status=devices_status if devices_status in SEEDED_STATUSES else "unknown",
        in_maintenance=devices_status == "maintenance",
        status_since=_utc(status_changed_at),
        owner=str(owner),
    )


def _field(row: Any, key: str) -> Any:
    if isinstance(row, State):
        return getattr(row, key)
    if isinstance(row, Mapping):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    return mapping.get(key) if mapping is not None else getattr(row, key, None)


def reseed_reason(row_state: Any, owner: str, devices_status: str | None) -> str | None:
    """Why the stored state must be reseeded: 'owner_change', 'reseed' or None.

    Reseed when the row is missing, belongs to another owner, or another writer
    changed devices.status behind the machine (maintenance is an overlay and is
    not compared). The reason is the end_reason for an open flap episode.
    """
    if row_state is None:
        return "reseed"
    if str(_field(row_state, "owner")) != str(owner):
        return "owner_change"
    if devices_status != "maintenance" and _field(row_state, "hard_status") != devices_status:
        return "reseed"
    return None


def needs_reseed(row_state: Any, owner: str, devices_status: str | None) -> bool:
    return reseed_reason(row_state, owner, devices_status) is not None


def device_fields(state: State) -> dict[str, Any]:
    """The devices columns mirrored from the state (status is the displayed one)."""
    return {"status": state.displayed, "is_flapping": state.is_flapping,
            "flapping_since": state.flapping_since, "flap_kind": state.flap_kind}


def significant_change(before: State | None, after: State) -> bool:
    """True when anything but last_sample_at differs (the state must be persisted)."""
    if before is None:
        return True
    return replace(before, last_sample_at=None) != replace(after, last_sample_at=None)


# --------------------------------------------------------------------------- serialisation

# device_status_state columns written from a State (device_id and updated_at are the writer's).
STATE_COLUMNS = tuple(f.name for f in fields(State))
_TS_FIELDS = ("status_since", "flapping_since", "last_sample_at")
_TS_LIST_FIELDS = ("reach_changes", "perf_changes")


def format_ts(value: datetime | None) -> str | None:
    """Canonical JSON timestamp: RFC 3339 UTC with 'Z', fraction only when non-zero
    and without trailing zeros (identical to Go's time.RFC3339Nano for UTC)."""
    if value is None:
        return None
    value = _utc(value)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += "." + f"{value.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse an RFC 3339 timestamp ('Z' or an offset); naive values are UTC."""
    if value is None or isinstance(value, datetime):
        return _utc(value)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return _utc(datetime.fromisoformat(text))


def state_to_row(state: State, device_id: Any = None) -> dict[str, Any]:
    """Bind parameters for a device_status_state row (Python values)."""
    row = {name: getattr(state, name) for name in STATE_COLUMNS}
    row["reach_changes"] = list(state.reach_changes)
    row["perf_changes"] = list(state.perf_changes)
    if device_id is not None:
        row = {"device_id": device_id, **row}
    return row


def state_from_row(row: Any) -> State:
    """State from a device_status_state row (mapping, SQLAlchemy Row or State)."""
    values = {name: _field(row, name) for name in STATE_COLUMNS}
    return State(
        hard_status=values["hard_status"] or "unknown",
        in_maintenance=bool(values["in_maintenance"]),
        status_since=_utc(values["status_since"]),
        fail_streak=int(values["fail_streak"] or 0),
        ok_streak=int(values["ok_streak"] or 0),
        clear_streak=int(values["clear_streak"] or 0),
        window_bits=int(values["window_bits"] or 0),
        window_len=int(values["window_len"] or 0),
        reach_changes=[_utc(c) for c in values["reach_changes"] or []],
        perf_changes=[_utc(c) for c in values["perf_changes"] or []],
        reach_flapping=bool(values["reach_flapping"]),
        perf_flapping=bool(values["perf_flapping"]),
        flapping_since=_utc(values["flapping_since"]),
        last_sample_at=_utc(values["last_sample_at"]),
        owner=str(values["owner"] or OWNER_CENTRAL),
    )


def state_to_json(state: State) -> dict[str, Any]:
    """JSON form (column names, canonical timestamps); also fits jsonb_to_recordset."""
    data = state_to_row(state)
    for name in _TS_FIELDS:
        data[name] = format_ts(data[name])
    for name in _TS_LIST_FIELDS:
        data[name] = [format_ts(c) for c in data[name]]
    return data


def state_from_json(data: Mapping[str, Any]) -> State:
    values = {name: data[name] for name in STATE_COLUMNS if name in data}
    for name in _TS_FIELDS:
        if name in values:
            values[name] = parse_ts(values[name])
    for name in _TS_LIST_FIELDS:
        if name in values:
            values[name] = [parse_ts(c) for c in values[name] or []]
    return State(**values)


def check_to_json(check: Check) -> dict[str, Any]:
    return {"ts": format_ts(check.ts), "responding": check.responding, "rtt_ms": check.rtt_ms,
            "loss": check.loss, "interval_s": check.interval_s, "maintenance": check.maintenance,
            "ordered": check.ordered, "source": check.source}


def check_from_json(data: Mapping[str, Any]) -> Check:
    return Check(ts=parse_ts(data["ts"]), responding=bool(data["responding"]), rtt_ms=data.get("rtt_ms"),
                 loss=data.get("loss") or 0.0, interval_s=data.get("interval_s") or 0,
                 maintenance=bool(data.get("maintenance")), ordered=bool(data.get("ordered", True)),
                 source=data.get("source") or "central")


def event_to_json(event: Event) -> dict[str, Any]:
    return asdict(event)


def event_from_json(data: Mapping[str, Any]) -> Event:
    return Event(**{f.name: data.get(f.name, f.default) for f in fields(Event)})
