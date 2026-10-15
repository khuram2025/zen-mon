"""Explain ping status using the owning poller's observations, not chart averages.

The rules themselves live in device_status_machine (shared with the Go poller
through the contract vectors); this module only reads them back for the
status-explanation API: per-sample measurements judged with the canonical
predicates, the writer's persisted confirmation state and the flapping block.
"""
from datetime import datetime, timedelta, timezone
from math import ceil

from app.core.database import get_clickhouse_client
from app.services import device_status_machine as dsm
# Re-exported: the per-sample reason text is the machine's (one wording, one
# set of predicates for both writers and the explanation).
from app.services.device_status_machine import degraded_reason  # noqa: F401

PACKETS_PER_CHECK = 3   # ICMP echo requests per check (the legacy confirmation key)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def thresholds_for(policy: dsm.Policy) -> dict:
    """The limits a sample is judged against: degraded limits plus the recovery (clear) limits."""
    return {"degraded_rtt_ms": policy.degraded_rtt_ms, "degraded_loss_pct": policy.degraded_loss_pct,
            "degraded_rtt_clear_ms": policy.degraded_rtt_clear_ms,
            "degraded_loss_clear_pct": policy.degraded_loss_clear_pct}


# Key of the stored 'monitoring' document that PUT /settings/monitoring stamps (UTC, ISO 8601) when the
# limits a sample is judged against change. The policy loaders ignore it. null: the limits have not changed
# since the document was created.
THRESHOLDS_CHANGED_AT = "thresholds_changed_at"
_THRESHOLD_KEYS = ("degraded_rtt_ms", "degraded_loss_pct", "degraded_rtt_clear_ms", "degraded_loss_clear_pct")


def thresholds_changed(before: dict, after: dict) -> bool:
    """Whether two stored 'monitoring' documents judge samples differently.

    Compares the effective limits (derived recovery limits included) and the stored limit values
    themselves: a limit the document leaves unset follows the central poller's configured fallback,
    which may differ from the built-in default, so setting it explicitly counts as a change. The
    confirmation counts, the window and the flap settings never change a sample's judgement."""
    if thresholds_for(dsm.policy_from_settings(before)) != thresholds_for(dsm.policy_from_settings(after)):
        return True
    return any(before.get(key) != after.get(key) for key in _THRESHOLD_KEYS)


def thresholds_changed_at(document: dict, updated_at) -> datetime | None:
    """When the judging limits last changed (None: not since the document was created).

    The settings PUT stamps THRESHOLDS_CHANGED_AT. A document it has not stamped yet (written before
    the stamp existed) falls back to the row's ``updated_at``, as the explanation read it until then.
    So does a malformed stamp: every PUT now moves ``updated_at``, so it is never earlier than the last
    change and can only hide evidence, never present the current limits as historical ones."""
    if THRESHOLDS_CHANGED_AT not in document:
        return utc(updated_at)
    value = document[THRESHOLDS_CHANGED_AT]
    if value is None:
        return None
    try:
        return utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return utc(updated_at)


def _limits(thresholds: dict) -> dsm.Policy:
    """The thresholds as the machine's policy for classify(). Thresholds without recovery limits
    (older callers) get an empty hysteresis band: a check is then 'breach' or 'clear'."""
    rtt, loss = float(thresholds["degraded_rtt_ms"]), float(thresholds["degraded_loss_pct"])
    rtt_clear, loss_clear = thresholds.get("degraded_rtt_clear_ms"), thresholds.get("degraded_loss_clear_pct")
    return dsm.Policy(degraded_rtt_ms=rtt, degraded_loss_pct=loss,
                      degraded_rtt_clear_ms=rtt if rtt_clear is None else float(rtt_clear),
                      degraded_loss_clear_pct=loss if loss_clear is None else float(loss_clear))


def measurement(sample, thresholds):
    """One ping sample against the limits, with the canonical predicates of the state machine.

    ``classification`` is 'fail' for an unanswered check, else the machine's 'breach' / 'marginal'
    (inside the hysteresis band: within the degraded limits, above a recovery limit) / 'clear'.
    Float32 packet loss stored at exactly a limit (5% as 0.0500000007) is not a breach.
    """
    if not sample:
        return None
    timestamp, rtt_ms, packet_loss, is_up = sample
    rtt = float(rtt_ms or 0.0)
    ratio = float(packet_loss or 0.0)
    loss_pct = ratio * 100
    breaches = []
    classification = "fail"
    if is_up:
        for name, value, limit, unit, over in (
            ("Packet loss", loss_pct, thresholds["degraded_loss_pct"], "percentage points",
             dsm.exceeds_pct(ratio, thresholds["degraded_loss_pct"])),
            ("Latency", rtt, thresholds["degraded_rtt_ms"], "ms", dsm.exceeds_ms(rtt, thresholds["degraded_rtt_ms"])),
        ):
            if over:
                # Round display arithmetic, never average samples here.
                breaches.append({"metric": name, "value": value, "threshold": limit,
                                 "exceeded_by": round(value - limit, 6), "excess_unit": unit})
        classification = dsm.classify(rtt, ratio, _limits(thresholds))
    return {"timestamp": utc(timestamp), "rtt_ms": rtt, "packet_loss_pct": loss_pct,
            "is_up": bool(is_up), "breaches": breaches, "classification": classification}


# --------------------------------------------------------------------------- confirmation state

def newest(*times: datetime | None) -> datetime | None:
    """The latest of the given times (naive ones are UTC), None when there is none."""
    known = [utc(t) for t in times if t is not None]
    return max(known) if known else None


def stale_after_seconds(interval_s) -> float:
    """Silence after which the writer's next check resets the streaks and the window (the machine's gap rule)."""
    return max(3 * float(interval_s or 0), float(dsm.GAP_FLOOR_SECONDS))


def current_state(row, owner: str, devices_status: str | None, status_changed_at) -> tuple[dsm.State, bool]:
    """The writer's state for this device and whether it is a fresh seed.

    A missing row, a row of another owner, or one that another writer has overtaken (the
    devices status changed behind it) is what the writer reseeds silently at its next check,
    so the explanation shows that seed (zero counters, no flapping) rather than the stale row.
    """
    if row is not None and not dsm.needs_reseed(row, owner, devices_status):
        return dsm.state_from_row(row), False
    return dsm.seed_state(devices_status, status_changed_at, owner), True


def state_block(state: dsm.State, policy: dsm.Policy, source: str, interval_s, seeded: bool,
                now: datetime | None = None, last_check_at: datetime | None = None) -> dict:
    """``state`` of the status explanation.

    Streaks linger after the change they caused, so each is reported only where it means
    something: ``fail_streak`` while not down, ``ok_streak`` while down, ``clear_streak``
    while degraded (None otherwise). ``pending`` is the unconfirmed progress towards the
    next status (device_status_machine.pending), None when nothing is pending or when the
    state is ``stale`` (silent longer than the gap rule, so the next check starts over).

    ``last_sample_at`` is the owner's latest check: the row's own value or the newer
    ``last_check_at`` the caller knows. The central poller rewrites a row whose counters
    did not change only every 10 minutes (its heartbeat), so the row alone would make a
    steady, regularly checked device look silent.
    """
    now = now or datetime.now(timezone.utc)
    hard = state.hard_status if state.hard_status in dsm.HARD_STATUSES else "unknown"
    bad, eff = dsm.window_counts(state, policy)
    last = newest(state.last_sample_at, last_check_at)
    stale = last is not None and (now - last).total_seconds() > stale_after_seconds(interval_s)
    return {
        "hard_status": hard,
        "in_maintenance": bool(state.in_maintenance),
        "pending": None if stale else dsm.pending(state, policy, source),
        "fail_streak": state.fail_streak if hard != "down" else None,
        "ok_streak": state.ok_streak if hard == "down" else None,
        "clear_streak": state.clear_streak if hard == "degraded" else None,
        "bad_in_window": bad,
        "window_len": eff,
        "window_size": policy.degraded_window_checks,
        "status_since": utc(state.status_since),
        "last_sample_at": last,
        "stale": stale,
        "seeded": seeded,
    }


def _span_checks(changes: int, into: int, out: int) -> int:
    """Fewest consecutive checks that can hold ``changes`` alternating changes of one class
    when entering the bad state needs ``into`` checks and leaving it ``out``."""
    pairs, odd = divmod(changes - 1, 2)
    return pairs * (into + out) + (min(into, out) if odd else 0)


def max_detectable_interval_s(policy: dsm.Policy, source: str = "central") -> int:
    """Longest ping interval (whole seconds) at which flapping can still start.

    flap_start_changes confirmed changes of one class must fall inside flap_window_minutes
    (the window excludes its lower bound). Each change needs its own confirmation checks:
    into/out of down = down confirmation (for the source) / up_confirm_checks; up<->degraded =
    degraded_confirm_checks / the faster of degraded_clear_checks and a clean window. The
    faster class decides.
    """
    reach = _span_checks(policy.flap_start_changes, dsm.down_confirm_for(policy, source), policy.up_confirm_checks)
    perf = _span_checks(policy.flap_start_changes, policy.degraded_confirm_checks,
                        min(policy.degraded_clear_checks, policy.degraded_window_checks))
    return ceil(policy.flap_window_minutes * 60 / min(reach, perf)) - 1


def earliest_stop_at(state: dsm.State, policy: dsm.Policy) -> datetime | None:
    """Earliest time flapping can end if no further change happens: per flapping class, the
    stability stop (last change + flap_stable_minutes) or the count stop (enough changes aged
    out of flap_window_minutes to leave at most flap_stop_changes), whichever comes first; the
    device stops when its last flapping class does. The stop itself happens on the first check
    at or after that time. None when not flapping or when detection is disabled (the next
    check ends it)."""
    if not policy.flap_detection_enabled:
        return None
    ends = []
    for flapping, changes in ((state.reach_flapping, state.reach_changes), (state.perf_flapping, state.perf_changes)):
        if not flapping or not changes:
            continue
        changes = sorted(utc(c) for c in changes)
        stable_at = changes[-1] + timedelta(minutes=policy.flap_stable_minutes)
        aged_out = len(changes) - policy.flap_stop_changes
        count_at = changes[aged_out - 1] + timedelta(minutes=policy.flap_window_minutes) if aged_out >= 1 else stable_at
        ends.append(min(stable_at, count_at))
    return max(ends) if ends else None


def flapping_block(device, state: dsm.State, policy: dsm.Policy, source: str, interval_s,
                   now: datetime | None = None) -> dict:
    """``flapping`` of the status explanation. ``active``/``kind``/``since`` are the device's
    mirrored flags (what the Devices list shows); the change counts are the writer's recorded
    confirmed changes still inside the flap window now."""
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(minutes=policy.flap_window_minutes)
    active = bool(getattr(device, "is_flapping", False))
    max_interval = max_detectable_interval_s(policy, source)
    return {
        "active": active,
        "kind": getattr(device, "flap_kind", None) if active else None,
        "since": utc(getattr(device, "flapping_since", None)) if active else None,
        "reach_changes_in_window": sum(1 for c in state.reach_changes if utc(c) > lo),
        "perf_changes_in_window": sum(1 for c in state.perf_changes if utc(c) > lo),
        "window_minutes": policy.flap_window_minutes,
        "start_changes": policy.flap_start_changes,
        "stop_changes": policy.flap_stop_changes,
        "stable_minutes": policy.flap_stable_minutes,
        "enabled": policy.flap_detection_enabled,
        "earliest_stop_at": earliest_stop_at(state, policy) if active else None,
        "max_detectable_interval_s": max_interval,
        # False: at this device's interval the window cannot hold enough confirmed changes.
        "detectable": bool(policy.flap_detection_enabled and int(interval_s or 60) <= max_interval),
    }


def confirmation_block(policy: dsm.Policy, source: str, interval_s) -> dict:
    """The legacy ``confirmation`` keys, now from the effective policy (central and sensor owners)."""
    return {"failed_checks": dsm.down_confirm_for(policy, source), "packets_per_check": PACKETS_PER_CHECK,
            "interval_seconds": int(interval_s or 60), "recovery_checks": policy.up_confirm_checks}


# --------------------------------------------------------------------------- ClickHouse evidence

def evidence(device_id, poller_id, thresholds, changed_at, interval):
    """The owning poller's latest sample and the most recent degraded transition with its sample.

    Transitions are stamped with the timestamp of the check that confirmed them, so the
    confirming sample is found at the transition time (the -5 s..+1 s tolerance also covers
    rows written before confirmation existed, stamped at write time).

    ``changed_at`` is when the limits last changed (thresholds_changed_at; None when they
    have not): a sample taken before it, or within the minute the writers take to reload the
    policy, is not judged against the current limits.
    """
    changed_at = utc(changed_at)
    client = get_clickhouse_client()
    params = {"id": str(device_id), "poller": poller_id}
    rows = client.query("""SELECT timestamp, rtt_ms, packet_loss, is_up
        FROM ping_metrics WHERE device_id = {id:UUID} AND poller_id = {poller:String}
        AND timestamp >= now() - INTERVAL 24 HOUR ORDER BY timestamp DESC LIMIT 1""",
        parameters=params).result_rows
    latest = measurement(rows[0] if rows else None, thresholds)
    now = datetime.now(timezone.utc)
    if latest:
        latest["stale"] = now - latest["timestamp"] > timedelta(seconds=max(60, interval * 2))
        latest["settings_pending"] = bool(changed_at and latest["timestamp"] < changed_at + timedelta(seconds=60))

    events = client.query("""SELECT timestamp, reason FROM device_status_log
        WHERE device_id = {id:UUID} AND new_status = 'degraded'
        AND timestamp >= now() - INTERVAL 30 DAY ORDER BY timestamp DESC LIMIT 1""",
        parameters=params).result_rows
    recent = None
    if events:
        at, reason = events[0]
        params["at"] = utc(at)
        samples = client.query("""SELECT timestamp, rtt_ms, packet_loss, is_up
            FROM ping_metrics WHERE device_id = {id:UUID} AND poller_id = {poller:String}
            AND timestamp >= {at:DateTime} - INTERVAL 5 SECOND
            AND timestamp <= {at:DateTime} + INTERVAL 1 SECOND
            ORDER BY abs(dateDiff('millisecond', timestamp, {at:DateTime})) LIMIT 1""",
            parameters=params).result_rows
        recovered = client.query("""SELECT timestamp FROM device_status_log
            WHERE device_id = {id:UUID} AND new_status = 'up' AND timestamp > {at:DateTime}
            ORDER BY timestamp LIMIT 1""", parameters=params).result_rows
        unchanged = not changed_at or changed_at + timedelta(seconds=60) <= utc(at)
        recent = {"timestamp": utc(at), "reason": reason,
                  "sample": measurement(samples[0] if samples else None, thresholds),
                  "thresholds_unchanged": unchanged,
                  "recovered_at": utc(recovered[0][0]) if recovered else None}
        if recent["sample"] and not unchanged:
            # Current thresholds must not be represented as historical limits.
            recent["sample"]["breaches"] = []
            if recent["sample"]["is_up"]:
                recent["sample"]["classification"] = None
    return {"latest": latest, "recent_degraded": recent}
