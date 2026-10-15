"""Device status machine: shared contract vectors plus readable scenarios.

The vectors (scripts/dev/device-status-contract/vectors.json) are replayed in
full here and in the Go package poller/internal/devicestatus. The scenarios
below are written from the design rules, independently of the vectors.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.device_status_machine import (
    DEFAULTS, Check, Event, Policy, State, check_from_json, check_to_json, classify, degraded_reason,
    effective_policy_dict, event_to_json, exceeds_ms, exceeds_pct, fmt3, format_ts, legacy_preset, needs_reseed,
    parse_ts, pending, policy_from_settings, reseed_reason, round3, seed_state, significant_change, state_from_json,
    state_from_row, state_to_json, state_to_row, step,
)

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "scripts" / "dev" / "device-status-contract"
VECTORS = json.loads((CONTRACT / "vectors.json").read_text(encoding="utf-8"))

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
LEGACY = {"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1,
          "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10, "flap_detection_enabled": False}
PER_CHECK = {"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1,
             "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10}


# --------------------------------------------------------------------------- vectors

def _record(st, events, policy, source):
    return {
        "hard_status": st.hard_status, "displayed": st.displayed, "in_maintenance": st.in_maintenance,
        "status_since": format_ts(st.status_since), "fail_streak": st.fail_streak, "ok_streak": st.ok_streak,
        "clear_streak": st.clear_streak, "window_bits": st.window_bits, "window_len": st.window_len,
        "reach_changes": [format_ts(c) for c in st.reach_changes],
        "perf_changes": [format_ts(c) for c in st.perf_changes],
        "reach_flapping": st.reach_flapping, "perf_flapping": st.perf_flapping,
        "flapping_since": format_ts(st.flapping_since), "last_sample_at": format_ts(st.last_sample_at),
        "pending": pending(st, policy, source),
        "events": [event_to_json(e) for e in events],
    }


@pytest.mark.parametrize("vector", VECTORS["policy_cases"], ids=lambda v: v["name"])
def test_policy_vectors(vector):
    assert effective_policy_dict(policy_from_settings(vector["raw"])) == vector["expect_effective"]


@pytest.mark.parametrize("vector", VECTORS["reseed_cases"], ids=lambda v: v["name"])
def test_reseed_vectors(vector):
    row = state_from_json(vector["row"]) if vector["row"] is not None else None
    reason = reseed_reason(row, vector["owner"], vector["devices_status"])
    assert reason == vector["expect_reason"]
    assert needs_reseed(row, vector["owner"], vector["devices_status"]) is (reason is not None)
    if reason:
        seeded = seed_state(vector["devices_status"], parse_ts(vector["status_changed_at"]), vector["owner"])
        assert state_to_json(seeded) == vector["expect_state"]


@pytest.mark.parametrize("vector", VECTORS["cases"], ids=lambda v: v["name"])
def test_case_vectors(vector):
    policy = policy_from_settings(vector["policy_raw"])
    if vector["initial"] is not None:
        st = state_from_json(vector["initial"])
    else:
        sd = vector["seed"]
        st = seed_state(sd["devices_status"], parse_ts(sd["status_changed_at"]), sd["owner"])
    assert len(vector["checks"]) == len(vector["expect"])
    for index, (item, expected) in enumerate(zip(vector["checks"], vector["expect"])):
        if "policy_raw" in item:
            policy = policy_from_settings(item["policy_raw"])
        check = check_from_json(item)
        st, events = step(st, check, policy)
        assert _record(st, events, policy, check.source) == expected, f"check {index} ({item['ts']})"


def test_vectors_are_current():
    result = subprocess.run([sys.executable, str(CONTRACT / "build_vectors.py"), "--check"],
                            cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_vectors_cover_every_reason_and_end_reason():
    reasons, end_reasons = set(), set()
    for vector in VECTORS["cases"]:
        for expected in vector["expect"]:
            for event in expected["events"]:
                if event["type"] == "transition":
                    reasons.add(event["reason"])
                elif event["type"] == "flap_stop":
                    end_reasons.add(event["end_reason"])
    patterns = [
        r"No response for \d+ consecutive checks",
        r"Responding again",
        r"Responding for \d+ consecutive checks",
        r"Responding again; Latency .*",
        r"Device responding normally",
        r"(Latency|Packet loss) [^;]*",  # unknown -> degraded: the degraded reason alone
        r"(Latency|Packet loss) .*; \d+ of the last \d+ checks over limits or unanswered",
        r"\d+ of the last \d+ checks over limits or unanswered; latest check within limits",
        r"Within recovery limits \([\d.]+ ms, [\d.]+%\) for \d+ consecutive checks",
        r"No slow, lossy or unanswered checks in the last \d+ checks",
        r"Maintenance window active",
        r"Maintenance ended; device responding normally",
        r"Maintenance ended; latency or loss above limits",
        r"Maintenance ended; no response for \d+ consecutive checks",
        r"Maintenance ended; responding for \d+ of \d+ checks needed to confirm recovery",
    ]
    for pattern in patterns:
        assert any(re.fullmatch(pattern, r) for r in reasons), pattern
    assert end_reasons == {"stable", "disabled", "maintenance"}
    assert len(VECTORS["cases"]) >= 45


# --------------------------------------------------------------------------- scenario helpers

SAMPLE = {
    "C": dict(responding=True, rtt_ms=4.0, loss=0.0),
    "S": dict(responding=True, rtt_ms=180.0, loss=0.0),
    "M": dict(responding=True, rtt_ms=95.0, loss=0.0),
    "L": dict(responding=True, rtt_ms=4.0, loss=1 / 3),
    "F": dict(responding=False, rtt_ms=None, loss=1.0),
}


def run(pattern, policy=None, st=None, start=T0, interval=60, **check):
    """Step through `pattern`; returns [(state, events)] per check."""
    pol = policy if isinstance(policy, Policy) else policy_from_settings(policy or {})
    st = st or seed_state("up", start - timedelta(hours=2), "central")
    out = []
    for i, letter in enumerate(pattern.replace(" ", "")):
        args = {**SAMPLE[letter], "interval_s": interval, **check}
        st, events = step(st, Check(ts=start + timedelta(seconds=i * interval), **args), pol)
        out.append((st, events))
    return out


def transitions(results):
    return [(i, e.old, e.new) for i, (_, events) in enumerate(results) for e in events if e.type == "transition"]


# --------------------------------------------------------------------------- scenarios

def test_single_slow_lossy_or_failed_checks_are_blips():
    for pattern in ("C S C C", "C L C C", "C F C C", "C F F C", "S C C S C C"):
        assert transitions(run(pattern)) == [], pattern


def test_three_of_five_bad_checks_confirm_degraded_with_window_reason():
    results = run("S C S C S")
    assert transitions(results) == [(4, "up", "degraded")]
    reason = results[4][1][0].reason
    assert reason == ("Latency 180.000 ms exceeds 100 ms by 80.000 ms; "
                      "3 of the last 5 checks over limits or unanswered")
    st = results[4][0]
    assert st.status_since == T0 + timedelta(minutes=4)
    assert st.perf_changes == [T0 + timedelta(minutes=4)]
    # Window restarted with the breaching check that confirmed the change.
    assert (st.window_bits, st.window_len) == (1, 1)


def test_failures_count_as_bad_evidence_but_never_enter_degraded_themselves():
    results = run("F F C F F C")
    # Checks 0-4: failures never confirm degraded (and never three in a row).
    assert transitions(results[:5]) == []
    assert transitions(results) == [(5, "up", "degraded")]
    assert results[5][1][0].reason == ("3 of the last 5 checks over limits or unanswered; "
                                       "latest check within limits")
    # The repeating pattern then holds degraded and never confirms down.
    longer = run("F F C" * 6)
    assert [t[2] for t in transitions(longer)] == ["degraded"]
    assert all(st.hard_status == "degraded" for st, _ in longer[5:])


def test_hysteresis_band_holds_degraded_until_clear_streak_or_clean_window():
    # Recovery limits default to 80 ms / 8 %; 95 ms is between them and the degraded limit.
    results = run("S S S M M M M")
    assert transitions(results) == [(2, "up", "degraded")]
    assert [st.clear_streak for st, _ in results[3:]] == [0, 0, 0, 0]
    # The window still holds the breach that confirmed degraded; once five checks with no
    # breach and no failure fill it, the window path recovers.
    results = run("S S S M M M M M")
    assert transitions(results) == [(2, "up", "degraded"), (7, "degraded", "up")]
    assert results[7][1][0].reason == "No slow, lossy or unanswered checks in the last 5 checks"
    # Three consecutive checks within the recovery limits: the streak path.
    results = run("S S S C C C")
    assert transitions(results) == [(2, "up", "degraded"), (5, "degraded", "up")]
    assert results[5][1][0].reason == "Within recovery limits (80 ms, 8%) for 3 consecutive checks"
    assert results[5][0].clear_streak == 3


def test_down_needs_consecutive_failures_and_first_answer_recovers():
    results = run("C F F F F C")
    assert transitions(results) == [(3, "up", "down"), (5, "down", "up")]
    assert results[3][1][0].reason == "No response for 3 consecutive checks"
    assert results[5][1][0].reason == "Responding again"
    assert results[4][0].fail_streak == 4
    # A remote-sensor check confirms down on its own (sensor_down_confirm_checks=1).
    sensor = run("C F", source="sensor", ordered=False)
    assert transitions(sensor) == [(1, "up", "down")]


def test_recovering_device_goes_up_first_and_degraded_is_confirmed_separately():
    results = run("F F F S S S S", {"up_confirm_checks": 2})
    assert transitions(results) == [(2, "up", "down"), (4, "down", "up"), (6, "up", "degraded")]
    assert results[4][1][0].reason == "Responding for 2 consecutive checks"
    # The window restarted at the recovery, so degraded needs three breaches of its own.
    assert results[6][1][0].reason.endswith("; 3 of the last 3 checks over limits or unanswered")
    assert pending(results[3][0], policy_from_settings({"up_confirm_checks": 2})) == \
        {"target": "up", "count": 1, "required": 2}


def test_legacy_preset_reproduces_single_check_behaviour():
    preset = legacy_preset({})
    assert policy_from_settings(preset) == policy_from_settings(LEGACY)
    results = run("C S C", preset)
    assert transitions(results) == [(1, "up", "degraded"), (2, "degraded", "up")]
    results = run("F F F S C", preset)
    assert transitions(results) == [(2, "up", "down"), (3, "down", "degraded"), (4, "degraded", "up")]
    assert results[3][1][0].reason == "Responding again; Latency 180.000 ms exceeds 100 ms by 80.000 ms"
    unknown = seed_state("unknown", None, "central")
    results = run("S", preset, st=unknown)
    assert transitions(results) == [(0, "unknown", "degraded")]
    assert results[0][1][0].reason == "Latency 180.000 ms exceeds 100 ms by 80.000 ms"
    # A failed check still never enters degraded.
    assert transitions(run("F C F C", preset)) == []


def test_flapping_starts_at_the_threshold_and_holds_the_transition():
    results = run("C S C S C C", PER_CHECK)
    # Changes at checks 1..4; the 4th change starts flapping on that same check.
    st, events = results[4]
    assert [e.type for e in events] == ["transition", "flap_start"]
    assert events[0].flapping is True
    assert events[1].kind == "performance"
    assert st.perf_flapping and st.flapping_since == T0 + timedelta(minutes=4)
    for _, earlier in results[1:4]:
        assert earlier[0].flapping is False


def test_flapping_stops_after_stable_minutes_and_needs_fresh_changes():
    results = run("C S C S C" + "C" * 10 + "S C S C", PER_CHECK)
    # Last change at minute 4; ten minutes later the stability stop fires.
    stop_at = [i for i, (_, ev) in enumerate(results) if any(e.type == "flap_stop" for e in ev)]
    assert stop_at == [14]
    stopped = results[14][0]
    assert stopped.perf_changes == [] and not stopped.is_flapping and stopped.flapping_since is None
    assert results[14][1][-1].end_reason == "stable"
    # Three new changes are not enough; the fourth restarts flapping.
    starts = [i for i, (_, ev) in enumerate(results) if any(e.type == "flap_start" for e in ev)]
    assert starts == [4, 18]


def test_flap_count_stop_and_disabled_stop():
    pol = {**PER_CHECK, "flap_window_minutes": 5, "flap_stop_changes": 2, "flap_stable_minutes": 5}
    # Changes at minutes 1-4 (start), then quiet: at minute 7 only the changes of minutes 3 and 4 are
    # newer than ts-5min (exclusive), 2 <= stop -> count stop; the last change is not 5 minutes old.
    results = run("C S C S C C C C", pol)
    st, events = results[7]
    assert [e.end_reason for e in events if e.type == "flap_stop"] == ["stable"]
    assert st.perf_changes == [T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)]
    # Disabling detection stops an episode with reason 'disabled' and keeps recording changes.
    flapping = run("C S C S C", PER_CHECK)[-1][0]
    st, events = run("S", {**PER_CHECK, "flap_detection_enabled": False}, st=flapping,
                     start=T0 + timedelta(minutes=5))[0]
    assert [(e.type, e.flapping, e.end_reason) for e in events] == [
        ("transition", False, None), ("flap_stop", False, "disabled")]
    assert len(st.perf_changes) == 5


def test_reachability_and_performance_flapping_are_separate_classes():
    pol = {**PER_CHECK, "down_confirm_checks": 1}
    results = run("C S C S C F", pol)
    st, events = results[5]
    assert st.perf_flapping and not st.reach_flapping
    assert (events[0].new, events[0].flapping) == ("down", False)
    assert st.flap_kind == "performance"


def test_maintenance_overlay_keeps_running_the_machine():
    st = run("C F F")[-1][0]
    results = run("F F", st=st, start=T0 + timedelta(minutes=3), maintenance=True)
    entry = results[0][1]
    assert [(e.old, e.new, e.alert) for e in entry] == [("up", "maintenance", False)]
    # Down is confirmed silently during the window.
    assert results[0][0].hard_status == "down" and results[0][0].displayed == "maintenance"
    assert results[1][1] == []
    st, events = run("F", st=results[1][0], start=T0 + timedelta(minutes=5))[0]
    assert [(e.old, e.new, e.reason, e.alert) for e in events] == [
        ("maintenance", "down", "Maintenance ended; no response for 5 consecutive checks", True)]
    # Seeded maintenance with an unconfirmed (unknown) status keeps the overlay while failing.
    seeded = seed_state("maintenance", None, "central")
    results = run("F F F", st=seeded)
    assert [r[0].displayed for r in results] == ["maintenance", "maintenance", "down"]
    # Still down but answering when the window ends (up_confirm=2): report the recovery progress,
    # never "no response for 0 consecutive checks".
    two = policy_from_settings({"up_confirm_checks": 2})
    st = run("F", two, st=seed_state("down", None, "central"), maintenance=True)[-1][0]
    results = run("C C", two, st=st, start=T0 + timedelta(minutes=1))
    assert [(e.old, e.new, e.reason) for e in results[0][1]] == [
        ("maintenance", "down", "Maintenance ended; responding for 1 of 2 checks needed to confirm recovery")]
    assert [(e.old, e.new) for e in results[1][1]] == [("down", "up")]


def test_changes_from_unknown_have_no_class_and_odd_statuses_are_unknown():
    # A hand-built unknown state with a flapping reachability history: the change from unknown
    # is neither recorded nor reported as flapping.
    st = State(hard_status="unknown", reach_flapping=True,
               reach_changes=[T0 - timedelta(minutes=m) for m in (5, 4, 3, 2)])
    st, events = run("F F F", st=st, interval=30)[-1]
    assert [(e.old, e.new, e.flapping) for e in events] == [("unknown", "down", False)]
    assert st.reach_flapping and len(st.reach_changes) == 4
    # A stored status outside the contract is unconfirmed: the first answer is the baseline.
    for status in ("", "pending", "maintenance"):
        st, events = run("C", st=State(hard_status=status))[0]
        assert (st.hard_status, [(e.old, e.new) for e in events]) == ("up", [("unknown", "up")]), status


def test_sensor_duplicates_are_ignored_and_central_clock_steps_are_accepted():
    pol = policy_from_settings({"sensor_down_confirm_checks": 3})
    st = seed_state("up", None, "sensor-1")
    st, _ = step(st, Check(ts=T0, responding=False, ordered=False, source="sensor"), pol)
    for ts in (T0, T0 - timedelta(seconds=5)):
        again, events = step(st, Check(ts=ts, responding=False, ordered=False, source="sensor"), pol)
        assert again == st and events == []
    assert st.fail_streak == 1

    three_changes = run("C S C S", PER_CHECK)[-1][0]   # changes at minutes 1, 2, 3
    st, _ = step(three_changes, Check(ts=T0 + timedelta(seconds=90), responding=True, rtt_ms=4.0),
                 policy_from_settings(PER_CHECK))
    assert st.last_sample_at == T0 + timedelta(seconds=90)
    assert st.perf_changes == [T0 + timedelta(minutes=1), T0 + timedelta(seconds=90)]


def test_gap_resets_confirmation_but_not_the_hard_status():
    st = run("F F")[-1][0]
    st, events = step(st, Check(ts=T0 + timedelta(seconds=60 + 181), responding=False, interval_s=30),
                      Policy())
    assert events == [] and st.fail_streak == 1 and st.window_len == 1 and st.hard_status == "up"


def test_streaks_saturate():
    results = run("F" * 130, st=seed_state("down", None, "central"))
    assert results[-1][0].fail_streak == 100
    assert all(not events for _, events in results)


def test_step_never_mutates_its_input():
    st = run("C S C S", PER_CHECK)[-1][0]
    before = state_to_json(st)
    step(st, Check(ts=T0 + timedelta(minutes=10), responding=True, rtt_ms=4.0, maintenance=True),
         policy_from_settings(PER_CHECK))
    step(st, Check(ts=T0 + timedelta(minutes=10), responding=False), policy_from_settings(PER_CHECK))
    assert state_to_json(st) == before


def test_pending_progress():
    pol = Policy()
    assert pending(State(hard_status="up", fail_streak=2), pol) == {"target": "down", "count": 2, "required": 3}
    assert pending(State(hard_status="up", fail_streak=1), pol, "sensor") == \
        {"target": "down", "count": 1, "required": 1}
    assert pending(State(hard_status="up", window_bits=0b101, window_len=3), pol) == \
        {"target": "degraded", "count": 2, "required": 3}
    assert pending(State(hard_status="degraded", clear_streak=1), pol) == {"target": "up", "count": 1, "required": 3}
    assert pending(State(hard_status="down", ok_streak=1), Policy(up_confirm_checks=2)) == \
        {"target": "up", "count": 1, "required": 2}
    assert pending(State(hard_status="down", fail_streak=40), pol) is None
    assert pending(State(hard_status="up"), pol) is None
    # Bits beyond the policy window are not counted.
    assert pending(State(hard_status="up", window_bits=0b11000, window_len=5), Policy(degraded_window_checks=3)) is None


def test_canonical_predicates_and_formatting():
    assert not exceeds_ms(100.0, 100) and not exceeds_ms(100.0000001, 100) and exceeds_ms(100.00001, 100)
    assert not exceeds_pct(0.1, 10) and not exceeds_pct(0.05000000074505806, 5) and not exceeds_pct(0.07, 7)
    assert not exceeds_pct(1 / 3, 33.3333) and exceeds_pct(1 / 3, 33.333)
    assert [fmt3(v) for v in (500, 5.6000000000000005, 1234.5678, 0.0004, 80.0)] == ["500", "5.6", "1234.568", "0", "80"]
    # Correctly rounded like fmt3: an exact binary tie goes to even, a value just below a
    # tie goes down even when scaling by 1000 would round it up (0.8 * 1.005625 = 0.80449..).
    assert round3(0.0625) == 0.062 and round3(0.1875) == 0.188 and round3(0.8 * 7) == 5.6
    assert round3(0.8 * 1.005625) == 0.804 and round3(1.0005) == 1.0 and round3(2.675) == 2.675
    pol = Policy()
    assert classify(150, 0, pol) == "breach" and classify(90, 0, pol) == "marginal"
    assert classify(80, 0.08, pol) == "clear" and classify(5, 0.09, pol) == "marginal"
    assert classify(None, None, pol) == "clear"


def test_degraded_reason_text():
    assert degraded_reason(501, .06, 500, 5) == ("Latency 501.000 ms exceeds 500 ms by 1.000 ms; Packet loss 6.00% "
                                                 "exceeds 5% by 1.00 percentage points")
    assert degraded_reason(.839, .5, 500, 5) == "Packet loss 50.00% exceeds 5% by 45.00 percentage points"
    # At-limit values (float32 5 % included) are not breaches; nothing breached gives the fallback.
    assert degraded_reason(500, 0.05000000074505806, 500, 5) == "High latency or packet loss"
    assert degraded_reason(None, 0, 100, 10) == "High latency or packet loss"
    assert "exceeds 1234.568 ms" in degraded_reason(1300, 0, 1234.5678, 10)


def test_policy_loader_rules():
    assert effective_policy_dict(policy_from_settings(None))["degraded_rtt_clear_ms"] == 80.0
    assert set(effective_policy_dict(Policy())) == set(DEFAULTS)
    pol = policy_from_settings({"degraded_confirm_checks": 6, "degraded_window_checks": 2,
                                "degraded_rtt_ms": 50, "degraded_rtt_clear_ms": 60,
                                "flap_start_changes": 3, "flap_stop_changes": 9,
                                "flap_window_minutes": 6, "flap_stable_minutes": 20,
                                "down_confirm_checks": "4", "up_confirm_checks": 2.5})
    assert (pol.degraded_confirm_checks, pol.degraded_window_checks) == (6, 6)
    assert pol.degraded_rtt_clear_ms == 50
    assert (pol.flap_stop_changes, pol.flap_stable_minutes) == (2, 6)
    assert (pol.down_confirm_checks, pol.up_confirm_checks) == (3, 1)


def test_seed_and_reseed_rules():
    changed = T0 - timedelta(days=1)
    for status, hard, maint in (("up", "up", False), ("degraded", "degraded", False), ("down", "down", False),
                                ("unknown", "unknown", False), ("maintenance", "unknown", True),
                                (None, "unknown", False)):
        st = seed_state(status, changed, "central")
        assert (st.hard_status, st.in_maintenance, st.status_since) == (hard, maint, changed)
        assert (st.fail_streak, st.window_len, st.reach_changes, st.is_flapping) == (0, 0, [], False)
    row = State(hard_status="up", owner="central")
    assert reseed_reason(None, "central", "up") == "reseed"
    assert reseed_reason(row, "sensor-1", "up") == "owner_change"
    assert reseed_reason(row, "central", "unknown") == "reseed"
    assert reseed_reason(row, "central", "maintenance") is None
    assert reseed_reason({"owner": "central", "hard_status": "up"}, "central", "up") is None


def test_row_and_json_round_trips():
    st = run("C S C S C", PER_CHECK)[-1][0]
    assert st.is_flapping
    assert state_from_json(state_to_json(st)) == st
    row = state_to_row(st, device_id="d1")
    assert row["device_id"] == "d1" and row["perf_changes"] == st.perf_changes
    naive = {**row, "last_sample_at": st.last_sample_at.replace(tzinfo=None)}
    assert state_from_row(naive) == st
    assert format_ts(datetime(2026, 1, 2, 3, 4, 5, 120000, tzinfo=timezone.utc)) == "2026-01-02T03:04:05.12Z"
    assert parse_ts("2026-01-02T03:04:05+00:00") == parse_ts("2026-01-02T03:04:05Z")
    check = Check(ts=T0, responding=True, rtt_ms=1.5, loss=0.0, interval_s=30, source="sensor", ordered=False)
    assert check_from_json(check_to_json(check)) == check
    assert event_to_json(Event("flap_stop", end_reason="stable"))["end_reason"] == "stable"
    assert significant_change(st, step(st, Check(ts=T0 + timedelta(minutes=6), responding=True, rtt_ms=4.0),
                                       policy_from_settings(PER_CHECK))[0]) is False
