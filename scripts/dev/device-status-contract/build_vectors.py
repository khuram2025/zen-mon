#!/usr/bin/env python3
"""Build scripts/dev/device-status-contract/vectors.json, the device status contract.

The case INPUTS live in this file (readable Python); the expected states and
events are produced by the reference implementation
``server/app/services/device_status_machine.py`` and written next to the
inputs. ``server/tests/test_device_status_machine.py`` and the Go package
``poller/internal/devicestatus`` replay every case and must reproduce the
expectations exactly (reason strings included).

Usage (from the repository root, with the server's Python requirements installed)::

    python scripts/dev/device-status-contract/build_vectors.py           # rewrite vectors.json
    python scripts/dev/device-status-contract/build_vectors.py --check   # exit 1 when vectors.json is stale
    python scripts/dev/device-status-contract/build_vectors.py --out X   # write somewhere else

No database, no network, no environment. The output is deterministic.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "vectors.json"

sys.path.insert(0, str(REPO / "server"))

from app.services.device_status_machine import (  # noqa: E402  (after the path tweak)
    State, check_from_json, effective_policy_dict, event_to_json, format_ts, pending, policy_from_settings,
    reseed_reason, seed_state, state_from_json, state_to_json, step,
)

T0 = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
SENSOR = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"

# Sample letters used by the timelines below (default limits: 100 ms / 10 %,
# recovery 80 ms / 8 %).
SAMPLES = {
    "C": {"responding": True, "rtt_ms": 5.0, "loss": 0.0},     # clean
    "S": {"responding": True, "rtt_ms": 150.0, "loss": 0.0},   # slow: latency breach
    "L": {"responding": True, "rtt_ms": 5.0, "loss": 1 / 3},   # lossy: one of three packets lost
    "M": {"responding": True, "rtt_ms": 90.0, "loss": 0.0},    # marginal: inside the hysteresis band
    "F": {"responding": False, "rtt_ms": None, "loss": 1.0},   # no reply
}

# Single-check behaviour of the releases before confirmation.
LEGACY = {"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1,
          "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10, "flap_detection_enabled": False}
# Every slow/clean alternation is a confirmed performance change; flap detection on (defaults).
FLAP_PERF = {"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1,
             "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10}


class Timeline:
    """Builds the check list of a case: one check per sample letter, `interval` seconds apart."""

    def __init__(self, start=T0, interval=30, source="central"):
        self.next = start
        self.interval = interval
        self.source = source
        self.items = []

    def add(self, pattern, *, maintenance=False, source=None, ordered=None, policy=None, **sample):
        """Append one check per letter of `pattern` (spaces ignored). `policy` (a raw
        document) is attached to the first check and applies from it onward;
        `sample` overrides rtt_ms/loss/responding for every check of the call."""
        source = source or self.source
        for index, letter in enumerate(pattern.replace(" ", "")):
            item = {"ts": format_ts(self.next), **SAMPLES[letter], **sample,
                    "interval_s": self.interval, "maintenance": maintenance,
                    "ordered": source == "central" if ordered is None else ordered, "source": source}
            if policy is not None and index == 0:
                item["policy_raw"] = policy
            self.items.append(item)
            self.next = self.next + timedelta(seconds=self.interval)
        return self

    def at(self, ts):
        """Place the next check at `ts` (a clock step back, a duplicate or a gap)."""
        self.next = ts
        return self

    def wait(self, seconds):
        """Place the next check `seconds` after the previous one."""
        self.next = parse(self.items[-1]["ts"]) + timedelta(seconds=seconds)
        return self


def parse(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def seed(status, owner="central", changed=T0 - timedelta(hours=1)):
    return {"devices_status": status, "status_changed_at": format_ts(changed), "owner": owner}


def state(**values):
    return state_to_json(State(**values))


CASES = []


def case(name, timeline, policy=None, *, seed_with=None, initial=None):
    if initial is None and seed_with is None:
        seed_with = seed("up")
    entry = {"name": name, "policy_raw": policy if policy is not None else {}, "initial": initial}
    if initial is None:
        entry["seed"] = seed_with
    entry["checks"] = timeline.items
    CASES.append(entry)


# --------------------------------------------------------------------------- default policy

case("default: one slow check is a blip and changes nothing", Timeline().add("C S C C C"))
case("default: one lossy check is a blip", Timeline().add("L C C"))
case("default: two failed checks then an answer stay up", Timeline().add("F F C C"))
case("default: 3 of the last 5 checks slow confirms degraded", Timeline().add("S C S C S"))
case("default: three lossy checks in a row confirm degraded (packet loss reason)", Timeline().add("L L L"))
case("default: latency and loss breached by one check give both clauses",
     Timeline().add("S S").add("S", rtt_ms=150.0, loss=1 / 3))
case("default: F F C enters degraded on a check within limits", Timeline().add("F F C F F C"))
case("default: F F C repeated stays degraded and never confirms down", Timeline().add("F F C" * 6))
case("default: degraded clears after 3 consecutive checks within the recovery limits",
     Timeline().add("S S S C C C C"))
case("default: marginal checks hold degraded until a full window has no bad check",
     Timeline().add("S S S M M M M M M"))
case("default: a marginal check resets the recovery streak; the clean window recovers first",
     Timeline().add("S S S C C M C C"))
case("default: marginal checks never enter degraded from up", Timeline().add("M M M M M M"))
case("default: recovery limit boundary (exactly 80 ms and 80 ms + 5e-7 are clear, 80.00001 is marginal)",
     Timeline().add("S S S").add("C", rtt_ms=80.00001).add("C", rtt_ms=80.0).add("C", rtt_ms=80.0000005)
     .add("C", rtt_ms=80.0))
case("default: the window is cleared on a hard change; reasons count the partial window",
     Timeline().add("F F F C S S S"))

# --------------------------------------------------------------------------- canonical predicates

case("boundary: loss 0.1 at a 10% limit is not a breach, 0.101 is",
     Timeline().add("C", loss=0.1).add("C", loss=0.1).add("C", loss=0.101).add("C"), LEGACY)
case("boundary: float32(0.05) at a 5% limit is not a breach, 0.0502 is",
     Timeline().add("C", loss=0.05000000074505806).add("C", loss=0.0502).add("C", loss=0.05000000074505806),
     {**LEGACY, "degraded_loss_pct": 5, "degraded_loss_clear_pct": 5})
case("boundary: 0.07 at a 7% limit is not a breach",
     Timeline().add("C", loss=0.07).add("C", loss=0.0701).add("C", loss=0.07),
     {**LEGACY, "degraded_loss_pct": 7, "degraded_loss_clear_pct": 7})
case("boundary: 1/3 loss at 33.3333% is within tolerance, at 33.333% it breaches",
     Timeline().add("L").add("L", policy={**LEGACY, "degraded_loss_pct": 33.333, "degraded_loss_clear_pct": 33.333})
     .add("C"),
     {**LEGACY, "degraded_loss_pct": 33.3333, "degraded_loss_clear_pct": 33.3333})
case("boundary: latency exactly at the limit and 1e-7 over are not breaches",
     Timeline().add("C", rtt_ms=100.0).add("C", rtt_ms=100.0000001).add("C", rtt_ms=100.00001)
     .add("C", rtt_ms=100.0), LEGACY)
case("boundary: fractional limits and derived recovery limits are formatted with fmt3",
     Timeline().add("C", rtt_ms=1300.25).add("C", rtt_ms=987.654).add("L", loss=0.0701)
     .add("C", rtt_ms=10.0, loss=0.056),
     {"degraded_rtt_ms": 1234.5678, "degraded_loss_pct": 7, "degraded_confirm_checks": 1,
      "degraded_window_checks": 1, "degraded_clear_checks": 1, "flap_detection_enabled": False})
case("derived recovery limits are correctly rounded (0.8045 -> 0.804, tie 1.0625 -> 1.062)",
     Timeline().add("S S S", rtt_ms=2.0).add("C", rtt_ms=0.8042).add("C", rtt_ms=0.5, loss=0.010625)
     .add("C C C", rtt_ms=0.5),
     {"degraded_rtt_ms": 1.005625, "degraded_loss_pct": 1.328125})

# --------------------------------------------------------------------------- legacy preset

case("legacy: one slow check is degraded and the next clean check is up", Timeline().add("C S C"), LEGACY)
case("legacy: the first slow answer after an outage is down->degraded", Timeline().add("F F F S C"), LEGACY)
case("legacy: a slow first answer from unknown is unknown->degraded", Timeline().add("S C"), LEGACY,
     seed_with=seed("unknown"))
case("legacy: a failed check never enters degraded", Timeline().add("F C F C"), LEGACY)
case("legacy: degraded -> down -> up", Timeline().add("S F F F C"), LEGACY)

# --------------------------------------------------------------------------- down and up confirmation

case("default: three consecutive failures confirm down; the first answer recovers", Timeline().add("C F F F F C"))
case("sensor: one failed sensor check is down (sensor_down_confirm_checks=1)",
     Timeline(source="sensor").add("C F C"), seed_with=seed("up", owner=SENSOR))
case("down confirmation follows the check source (central 3, sensor 2)",
     Timeline().add("F F C").add("F F", source="sensor").add("C"),
     {"sensor_down_confirm_checks": 2})
case("up_confirm=2 with slow replies: up first, degraded confirmed on its own",
     Timeline().add("F F F S S S S"), {"up_confirm_checks": 2})
case("up_confirm=3: a failure restarts the recovery count", Timeline().add("F F F C C F C C C"),
     {"up_confirm_checks": 3})
case("default: degraded -> down -> up (down outranks degraded, recovery goes to up)",
     Timeline().add("S S S F F F C"))
case("seeded unknown: fail, fail, answer is unknown->up and is not a recorded change",
     Timeline().add("F F C"), seed_with=seed("unknown"))
case("seeded unknown: three failures are unknown->down and are not a recorded change",
     Timeline().add("F F F"), seed_with=seed("unknown"))
case("streak saturation: fail_streak stops at 100 while down", Timeline().add("F" * 102),
     seed_with=seed("down"))

# --------------------------------------------------------------------------- window ring

case("window shrink masks old bits; growing back keeps the observed length",
     Timeline().add("S C C S").add("S", policy={"degraded_window_checks": 3}).add("S", policy={}))
case("window grow keeps the bits already observed",
     Timeline().add("S C").add("C S", policy={"degraded_confirm_checks": 2, "degraded_window_checks": 4}),
     {"degraded_confirm_checks": 2, "degraded_window_checks": 2})

# --------------------------------------------------------------------------- maintenance

case("maintenance entry and exit in the middle of a down confirmation",
     Timeline().add("C F F").add("F F", maintenance=True).add("F C"))
case("maintenance ends while the hard status is unknown: the overlay stays until confirmed",
     Timeline().add("F F F C"), seed_with=seed("maintenance"))
case("seeded maintenance ends with an answer: maintenance->up", Timeline().add("C C"),
     seed_with=seed("maintenance"))
case("maintenance ends while degraded: maintenance->degraded",
     Timeline().add("S S S").add("M M", maintenance=True).add("M C C C"))
case("maintenance ends while down and still recovering (up_confirm=2)",
     Timeline().add("F F F").add("F", maintenance=True).add("C C"), {"up_confirm_checks": 2})
case("maintenance entry clears flapping (flap_stop maintenance); changes are not recorded in the window",
     Timeline().add("C S C S C").add("S C S", maintenance=True).add("C S C"), FLAP_PERF)
case("hard changes during maintenance are silent; the window end reports the confirmed status",
     Timeline().add("F").add("C C F", maintenance=True).add("C"), seed_with=seed("down"))
case("maintenance entry from a seeded unknown", Timeline().add("C S", maintenance=True).add("C"),
     seed_with=seed("unknown"))

# --------------------------------------------------------------------------- flap detection

case("flapping starts at exactly flap_start_changes; that transition carries flapping=true",
     Timeline().add("C S C S C C"), FLAP_PERF)
case("prune boundary: a change exactly flap_window_minutes old is pruned",
     Timeline().add("S"), FLAP_PERF,
     initial=state(hard_status="up", status_since=T0 - timedelta(minutes=10),
                   perf_changes=[T0 - timedelta(minutes=30), T0 - timedelta(minutes=20), T0 - timedelta(minutes=10)],
                   last_sample_at=T0 - timedelta(seconds=30)))
case("prune boundary: a change one second inside the window still counts",
     Timeline().add("S"), FLAP_PERF,
     initial=state(hard_status="up", status_since=T0 - timedelta(minutes=10),
                   perf_changes=[T0 - timedelta(minutes=30) + timedelta(seconds=1), T0 - timedelta(minutes=20),
                                 T0 - timedelta(minutes=10)],
                   last_sample_at=T0 - timedelta(seconds=30)))
case("change history is capped at the newest 64 per class",
     Timeline().add("S C C"), {**FLAP_PERF, "flap_start_changes": 64},
     initial=state(hard_status="up", status_since=T0 - timedelta(seconds=15),
                   perf_changes=[T0 - timedelta(minutes=16) + timedelta(seconds=15 * k) for k in range(64)],
                   perf_flapping=True, flapping_since=T0 - timedelta(minutes=1),
                   last_sample_at=T0 - timedelta(seconds=15)))
case("stability stop clears the history; restarting needs flap_start_changes fresh changes",
     Timeline(interval=60).add("C S C S C").add("C" * 10).add("S C S C"), FLAP_PERF)
case("count stop keeps the history; a restart needs only enough changes in the window",
     Timeline(interval=10).add("C S C").add("C" * 24).add("S C C C C C S C"),
     {**FLAP_PERF, "flap_window_minutes": 5, "flap_stop_changes": 2, "flap_stable_minutes": 5})
case("disabling detection stops flapping (disabled) on a transition check; changes are still recorded",
     Timeline().add("C S C S C").add("S", policy={**FLAP_PERF, "flap_detection_enabled": False}).add("C S")
     .add("C", policy=FLAP_PERF).add("S"), FLAP_PERF)
case("re-enabling detection with only stale history does not open an episode",
     Timeline().add("C S C S C").add("C" * 22).add("C", policy=FLAP_PERF).add("S C S"),
     {**FLAP_PERF, "flap_detection_enabled": False})
case("lowering flap_start_changes mid-stream starts flapping on the next check",
     Timeline().add("C S C S C").add("C", policy=FLAP_PERF), {**FLAP_PERF, "flap_start_changes": 6})
case("latency flapping does not mark a down transition as flapping; both classes flap together",
     Timeline().add("C S C S C F C F C C").add("C" * 20), {**FLAP_PERF, "down_confirm_checks": 1})
case("both classes reaching the start count on one check start an episode of kind both",
     Timeline().add("C S C S C F C F C").add("C", policy={**FLAP_PERF, "down_confirm_checks": 1}),
     {**FLAP_PERF, "down_confirm_checks": 1, "flap_start_changes": 6})
case("reachability flapping from a remote sensor", Timeline(source="sensor").add("C F C F C C"),
     seed_with=seed("up", owner=SENSOR))
case("a transition on the same check as a count stop carries flapping=false",
     Timeline(source="sensor").add("C F C F C").wait(40 * 60).add("F C"), seed_with=seed("up", owner=SENSOR))
case("flapping stops on stability while the device stays down",
     Timeline(source="sensor").add("C F C F C F").add("F" * 21), seed_with=seed("up", owner=SENSOR))
case("a stop where the count and the stability conditions hold together clears the history",
     Timeline(interval=60).add("C S C S C").wait(29 * 60 + 30).add("C").add("S C S"), FLAP_PERF)
case("a change from unknown never carries the class flapping flag (hand-built state)",
     Timeline().add("F F F C"),
     initial=state(hard_status="unknown",
                   reach_changes=[T0 - timedelta(minutes=m) for m in (5, 4, 3, 2)],
                   reach_flapping=True, flapping_since=T0 - timedelta(minutes=3),
                   last_sample_at=T0 - timedelta(seconds=30)))

# --------------------------------------------------------------------------- ordering and gaps

case("sensor: duplicate and out-of-order samples are ignored",
     Timeline(source="sensor").add("F").at(T0).add("F").at(T0 - timedelta(seconds=10)).add("F")
     .at(T0 + timedelta(seconds=30)).add("F F"),
     {"sensor_down_confirm_checks": 3}, seed_with=seed("up", owner=SENSOR))
case("central: a clock step back is accepted and future change times are dropped",
     Timeline().add("C S C S").at(T0 + timedelta(seconds=45)).add("C S"), FLAP_PERF)
case("gap: a silence longer than max(3 x interval, 180 s) restarts confirmation",
     Timeline().add("F F").wait(181).add("F F F"))
case("gap boundary: exactly 180 s is not a gap", Timeline().add("F F").wait(180).add("F"))
case("gap: long intervals use 3 x interval", Timeline(interval=120).add("F F").wait(300).add("F"))
case("gap boundary at 3 x interval: exactly 183 s is not a gap, 183.000001 s is",
     Timeline(interval=61).add("F F").wait(183).add("F C F F").wait(183.000001).add("F"))

# --------------------------------------------------------------------------- seeding

case("seed up: nothing is announced until a status is confirmed", Timeline().add("C C S S S"))
case("seed down: further failures do not re-page; the first answer recovers",
     Timeline().add("F F F C"), seed_with=seed("down"))
case("seed degraded: three clear checks resolve it (degraded->up only)", Timeline().add("C C C"),
     seed_with=seed("degraded"))
case("seed degraded inside the hysteresis band: a full clean window resolves it",
     Timeline().add("M M M M M"), seed_with=seed("degraded"))
case("seed unknown: the first answer is the baseline", Timeline().add("C"), seed_with=seed("unknown"))
case("seed from an unrecognised status starts unknown", Timeline().add("S S"), seed_with=seed("pending"))
case("a stored hard status outside the contract is treated as unknown (hand-built state)",
     Timeline().add("C S"), initial=state(hard_status="pending", fail_streak=2,
                                          last_sample_at=T0 - timedelta(seconds=30)))
case("seed for a remote-sensor owner with a missing status_changed_at",
     Timeline(source="sensor").add("C F C"),
     seed_with={"devices_status": "down", "status_changed_at": None, "owner": SENSOR})

# --------------------------------------------------------------------------- policy loader

POLICY_CASES = [
    ("empty document gives the defaults and derived recovery limits", {}),
    ("null document gives the defaults", None),
    ("a non-object document gives the defaults", [1, 2, 3]),
    ("strings, bools and nulls take the field default",
     {"degraded_rtt_ms": "150", "degraded_loss_pct": True, "degraded_confirm_checks": "2",
      "down_confirm_checks": None, "flap_detection_enabled": "false", "flap_start_changes": False}),
    ("integer fields accept only integral numbers",
     {"degraded_confirm_checks": 2.5, "degraded_window_checks": 7.0, "down_confirm_checks": 2.0,
      "flap_window_minutes": 60.0, "flap_stable_minutes": 9.99}),
    ("out-of-range values clamp to their range",
     {"degraded_rtt_ms": 20000, "degraded_loss_pct": 0.5, "degraded_confirm_checks": 0,
      "degraded_window_checks": 50, "degraded_clear_checks": -3, "down_confirm_checks": 11,
      "sensor_down_confirm_checks": 0, "up_confirm_checks": 99, "flap_window_minutes": 2,
      "flap_start_changes": 100, "flap_stop_changes": -1, "flap_stable_minutes": 0}),
    ("huge integral values clamp", {"degraded_clear_checks": 1e9, "flap_window_minutes": 1e300}),
    ("non-positive latency limit means unset; loss below 1% clamps to 1%",
     {"degraded_rtt_ms": 0, "degraded_loss_pct": 0}),
    ("the window is raised to the confirm count", {"degraded_confirm_checks": 7, "degraded_window_checks": 4}),
    ("explicit recovery limits above the degraded limits clamp down to them",
     {"degraded_rtt_ms": 50, "degraded_rtt_clear_ms": 70, "degraded_loss_pct": 5, "degraded_loss_clear_pct": 9}),
    ("explicit recovery limits are kept as given", {"degraded_rtt_clear_ms": 90.12345, "degraded_loss_clear_pct": 0}),
    ("non-positive latency recovery limit is derived; negative loss recovery clamps to 0",
     {"degraded_rtt_clear_ms": -5, "degraded_loss_clear_pct": -2}),
    ("derived recovery limits are rounded to 3 decimals",
     {"degraded_rtt_ms": 1234.5678, "degraded_loss_pct": 7}),
    ("derived recovery limit rounding (33.3333 -> 26.667, 0.0004 -> 0)",
     {"degraded_rtt_ms": 0.0004, "degraded_loss_pct": 33.3333}),
    ("flap stop and stable clamp to start-1 and the window",
     {"flap_start_changes": 3, "flap_stop_changes": 5, "flap_window_minutes": 10, "flap_stable_minutes": 30}),
    ("legacy preset", LEGACY),
    ("flap detection disabled with the largest window and stable time",
     {"flap_detection_enabled": False, "flap_window_minutes": 1440, "flap_stable_minutes": 1440}),
    ("non-bool enabled flag is the default", {"flap_detection_enabled": 0}),
    ("derived recovery limits are correctly rounded, ties to even (0.8045 -> 0.804, 1.0625 -> 1.062)",
     {"degraded_rtt_ms": 1.005625, "degraded_loss_pct": 1.328125}),
    ("derived recovery limit exact tie below 1 ms (0.0625 -> 0.062)", {"degraded_rtt_ms": 0.078125}),
    ("numbers beyond the float64 range take the default",
     {"degraded_rtt_ms": 10 ** 400, "degraded_loss_pct": -(10 ** 400), "degraded_rtt_clear_ms": 10 ** 400,
      "degraded_confirm_checks": 10 ** 400, "flap_window_minutes": -(10 ** 400)}),
]

# --------------------------------------------------------------------------- reseed rule

RESEED_CASES = [
    ("no state row: seed from devices.status", None, "central", "down"),
    ("no state row while in maintenance: hard unknown with the overlay", None, "central", "maintenance"),
    ("another owner's row: owner_change", state(hard_status="up", owner=SENSOR), "central", "up"),
    ("sensor took over a central row: owner_change", state(hard_status="up"), SENSOR, "up"),
    ("another writer set unknown (sensor offline): reseed", state(hard_status="up", owner=SENSOR), SENSOR, "unknown"),
    ("devices.status degraded behind the machine: reseed", state(hard_status="up"), "central", "degraded"),
    ("maintenance status is an overlay and never compared",
     state(hard_status="down", in_maintenance=True, fail_streak=4), "central", "maintenance"),
    ("matching row: keep", state(hard_status="degraded", clear_streak=2), "central", "degraded"),
    ("unknown row and unknown status: keep", state(hard_status="unknown", fail_streak=1), "central", "unknown"),
]


# --------------------------------------------------------------------------- build

def record(st, events, policy, source):
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


def run_case(entry):
    policy = policy_from_settings(entry["policy_raw"])
    if entry["initial"] is not None:
        st = state_from_json(entry["initial"])
    else:
        sd = entry["seed"]
        changed = parse(sd["status_changed_at"]) if sd["status_changed_at"] else None
        st = seed_state(sd["devices_status"], changed, sd["owner"])
    expect = []
    for item in entry["checks"]:
        if "policy_raw" in item:
            policy = policy_from_settings(item["policy_raw"])
        check = check_from_json(item)
        st, events = step(st, check, policy)
        expect.append(record(st, events, policy, check.source))
    return expect


def build():
    names = [c["name"] for c in CASES] + [n for n, _ in POLICY_CASES] + [n for n, *_ in RESEED_CASES]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SystemExit(f"duplicate case names: {sorted(duplicates)}")
    reseeds = []
    for name, row, owner, status in RESEED_CASES:
        changed = T0 - timedelta(minutes=5)
        reason = reseed_reason(state_from_json(row) if row is not None else None, owner, status)
        reseeds.append({"name": name, "row": row, "owner": owner, "devices_status": status,
                        "status_changed_at": format_ts(changed), "expect_reason": reason,
                        "expect_state": state_to_json(seed_state(status, changed, owner)) if reason else None})
    return {
        "version": 1,
        "generated_by": "scripts/dev/device-status-contract/build_vectors.py",
        "policy_cases": [{"name": name, "raw": raw,
                          "expect_effective": effective_policy_dict(policy_from_settings(raw))}
                         for name, raw in POLICY_CASES],
        "reseed_cases": reseeds,
        "cases": [{**entry, "expect": run_case(entry)} for entry in CASES],
    }


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def render(doc):
    """Stable layout: one line per policy case, reseed case, check and expectation."""
    lines = ["{"]
    lines.append(f' "version": {dumps(doc["version"])},')
    lines.append(f' "generated_by": {dumps(doc["generated_by"])},')
    for key in ("policy_cases", "reseed_cases"):
        lines.append(f' "{key}": [')
        items = doc[key]
        lines += [f"  {dumps(item)}{',' if i < len(items) - 1 else ''}" for i, item in enumerate(items)]
        lines.append(" ],")
    lines.append(' "cases": [')
    for ci, entry in enumerate(doc["cases"]):
        lines.append("  {")
        head = [k for k in entry if k not in ("checks", "expect")]
        for key in head:
            lines.append(f"   {dumps(key)}: {dumps(entry[key])},")
        for key in ("checks", "expect"):
            lines.append(f'   "{key}": [')
            items = entry[key]
            lines += [f"    {dumps(item)}{',' if i < len(items) - 1 else ''}" for i, item in enumerate(items)]
            lines.append("   ]," if key == "checks" else "   ]")
        lines.append("  }," if ci < len(doc["cases"]) - 1 else "  }")
    lines.append(" ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when vectors.json is stale")
    parser.add_argument("--out", type=Path, default=OUT, help="output path (default: vectors.json next to this file)")
    args = parser.parse_args(argv)
    text = render(build())
    json.loads(text)  # the layout must stay valid JSON
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            diff = difflib.unified_diff(current.splitlines(), text.splitlines(), str(args.out), "rebuilt",
                                        lineterm="", n=1)
            sys.stdout.write("\n".join(list(diff)[:80]) + "\n")
            print(f"{args.out} is stale: run python scripts/dev/device-status-contract/build_vectors.py",
                  file=sys.stderr)
            return 1
        print(f"{args.out} is up to date ({len(CASES)} cases)")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(CASES)} cases, {len(POLICY_CASES)} policy cases, {len(RESEED_CASES)} reseed cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
