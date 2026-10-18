"""Escalation-policy planning (escalation_policy_service.plan_policy), with an
injected clock, against the field-report matrix:

* no acknowledgement: each step fires on its deadline; repeats re-notify the
  current step, bounded by max_repeats;
* timely acknowledgement stops step escalation but not the unresolved-review
  timer; an expired acknowledgement re-opens escalation at the current step
  and re-anchors the remaining steps;
* recovery before a timer cancels the pending steps (only a close remains);
* fallback when a step reaches nobody or every delivery failed;
* route precedence: rule policy > priority default > legacy levels; and the
  policy validation the API relies on.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import escalation_policy_service as eps

T0 = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def p1(**kw) -> eps.Policy:
    """P1: primary at once, backup at 5, lead at 15, major incident at 30;
    updates every 15 minutes; repeats every 5 (twice); ack expires after 60."""
    base = dict(id="pol-1", name="P1", default_priorities=["P1"], ack_target_minutes=5,
                repeat_minutes=5, max_repeats=2, review_minutes=15, ack_expiry_minutes=60,
                fallback_targets=[{"type": "channel", "id": "fb"}],
                steps=[eps.Step(0, 0, "Primary", [{"type": "schedule", "id": "primary"}]),
                       eps.Step(1, 5, "Backup", [{"type": "schedule", "id": "backup"}]),
                       eps.Step(2, 15, "Lead", [{"type": "user", "id": "lead"}]),
                       eps.Step(3, 30, "Major", [{"type": "channel", "id": "noc"}])])
    base.update(kw)
    return eps.Policy(**base)


def run(policy, state, now, *, status="active", ack=None, suppressed=False):
    return eps.plan_policy(policy, state, status=status, anchor=T0, acknowledged_at=ack,
                           now=now, suppressed=suppressed)


def step(policy, state, now, **kw):
    """Plan and apply (what a sweep that won the claim stores)."""
    plan = run(policy, state, now, **kw)
    return plan, (eps.apply_plan(state, plan, policy, now) if plan else state)


def test_no_acknowledgement_walks_every_step_on_its_deadline():
    pol = p1(repeat_minutes=None, review_minutes=None)
    plan, st = step(pol, None, at(0))
    assert (plan.kind, plan.step) == ("escalate", 0)
    assert eps.delivery_event(plan) == "escalation:S0:r0"
    assert run(pol, st, at(4.9)) is None
    plan, st = step(pol, st, at(5))
    assert (plan.kind, plan.step) == ("escalate", 1)
    plan, st = step(pol, st, at(15))
    assert (plan.kind, plan.step) == ("escalate", 2)
    plan, st = step(pol, st, at(31))
    assert (plan.kind, plan.step) == ("escalate", 3) and eps.delivery_event(plan) == "escalation:S3:r0"
    assert run(pol, st, at(600)) is None                         # last step, no repeats
    assert st["history"] == {"0": at(0).isoformat(), "1": at(5).isoformat(),
                             "2": at(15).isoformat(), "3": at(31).isoformat()}


def test_late_discovery_pages_only_the_highest_due_step():
    plan = run(p1(), None, at(20))
    assert (plan.kind, plan.step, plan.skipped) == ("escalate", 2, [0, 1])


def test_repeats_re_notify_the_current_step_and_are_bounded():
    pol = p1(review_minutes=None, steps=[eps.Step(0, 0, "Primary", [{"type": "user", "id": "u"}]),
                                          eps.Step(1, 60, "Backup", [{"type": "user", "id": "b"}])])
    _, st = step(pol, None, at(0))
    assert run(pol, st, at(4)) is None
    plan, st = step(pol, st, at(5))
    assert (plan.kind, plan.repeat, eps.delivery_event(plan)) == ("repeat", 1, "escalation:S0:r1")
    plan, st = step(pol, st, at(10))
    assert (plan.kind, plan.repeat) == ("repeat", 2)
    assert run(pol, st, at(15)) is None and run(pol, st, at(59)) is None   # max_repeats = 2
    plan, st = step(pol, st, at(60))
    assert (plan.kind, plan.step) == ("escalate", 1) and st["repeats"] == 0  # per-step count
    assert run(p1(max_repeats=0, review_minutes=None), {**st, "step": 3}, at(200)) is None


def test_timely_acknowledgement_stops_steps_but_reviews_continue():
    pol = p1()
    _, st = step(pol, None, at(0))
    # Acknowledged at minute 3: no backup page at 5, no lead at 15 ...
    assert run(pol, st, at(5), status="acknowledged", ack=at(3)) is None
    # ... but the 15-minute unresolved update still goes out, and the next.
    plan, st = step(pol, st, at(15), status="acknowledged", ack=at(3))
    assert (plan.kind, plan.review, eps.delivery_event(plan)) == ("review", 1, "review:1")
    assert run(pol, st, at(29), status="acknowledged", ack=at(3)) is None
    plan, st = step(pol, st, at(30), status="acknowledged", ack=at(3))
    assert (plan.kind, plan.review) == ("review", 2)
    assert st["step"] == 0                                         # escalation never moved


def test_missed_reviews_are_not_replayed():
    plan = run(p1(), {**eps.new_state("pol-1"), "step": 3, "last_at": at(30).isoformat()},
               at(50), status="acknowledged", ack=at(31))
    assert (plan.kind, plan.review) == ("review", 3)


def test_review_due_with_a_step_page_is_covered_by_it():
    pol = p1(repeat_minutes=None)
    _, st = step(pol, None, at(0))
    _, st = step(pol, st, at(5))
    plan, st = step(pol, st, at(15))                              # lead paged at 15 ...
    assert (plan.kind, plan.step) == ("escalate", 2)
    plan = run(pol, st, at(15.5))                                 # ... which is the 15-min update
    assert plan.kind == "review" and plan.covered


def test_expired_acknowledgement_reopens_escalation_at_the_current_step():
    pol = p1(review_minutes=None)
    _, st = step(pol, None, at(0))
    _, st = step(pol, st, at(5))                                   # backup paged
    assert run(pol, st, at(64), status="acknowledged", ack=at(6)) is None
    plan, st = step(pol, st, at(66), status="acknowledged", ack=at(6))
    assert (plan.kind, plan.step, plan.expiry) == ("ack_expired", 1, 1)
    assert eps.delivery_event(plan) == "ack_expired:1"
    # Re-anchored: the lead (15) comes 10 minutes after re-opening, not at once.
    assert st["anchor_at"] == (at(66) - timedelta(minutes=5)).isoformat()
    assert run(pol, st, at(70)) is None
    plan, st = step(pol, st, at(76))
    assert (plan.kind, plan.step) == ("escalate", 2)
    # Next: the lead's repeat at 81, then the major-incident step at 61 + 30.
    assert eps.next_action(pol, st, status="active", anchor=T0, now=at(76)) == (at(81), "repeat:2")
    assert eps.next_action(p1(review_minutes=None, repeat_minutes=None), st, status="active",
                           anchor=T0, now=at(76)) == (at(91), "step:3")


def test_recovery_before_a_timer_cancels_the_pending_steps():
    pol = p1()
    _, st = step(pol, None, at(0))
    plan = run(pol, st, at(4), status="resolved")
    assert plan.kind == "close"
    closed = eps.apply_plan(st, plan, pol, at(4))
    assert closed["closed"] and run(pol, closed, at(10), status="resolved") is None
    # Nothing was ever paged by the policy: nothing to close.
    assert run(pol, None, at(4), status="resolved") is None


def test_suppressed_pending_and_unanchored_incidents_stay_quiet():
    pol = p1()
    assert run(pol, None, at(30), suppressed=True) is None
    assert run(pol, None, at(30), status="pending") is None
    assert eps.plan_policy(pol, None, status="active", anchor=None, now=at(30)) is None


def test_fallback_on_an_empty_step_and_on_all_deliveries_failed():
    assert eps.coverage_problem([], []) == "coverage_gap"
    assert eps.coverage_problem([{"kind": "email"}], []) is None
    assert eps.coverage_problem([], ["ch"]) is None
    assert eps.delivery_problem(0, True) == "route_failed"
    assert eps.delivery_problem(1, True) is None
    assert eps.delivery_problem(0, False) is None
    assert eps.fallback_event(2) == "fallback:S2"


def test_delivery_keys_are_new_per_repeat_and_per_expired_acknowledgement():
    pol = p1()
    assert eps.fallback_event(2, repeat=1) == "fallback:S2:r1"
    assert eps.fallback_event(2, repeat=1, expiries=2) == "fallback:S2:r1:x2"
    repeat = eps.Plan("repeat", step=1, repeat=1)
    assert eps.delivery_event(repeat) == "escalation:S1:r1"
    assert eps.delivery_event(repeat, expiries=1) == "escalation:S1:r1:x1"
    first = eps.Plan("escalate", step=0)
    assert eps.is_initial_page(pol, first) and eps.delivery_event(first, initial=True) == "trigger:S0"
    assert not eps.is_initial_page(pol, eps.Plan("escalate", step=1))
    assert not eps.is_initial_page(p1(steps=[eps.Step(0, 5, "Late", [])]), first)


def test_a_takeover_state_anchors_steps_and_reviews_at_the_takeover():
    pol = p1()
    st = eps.new_state(pol.id, at=at(120))                 # an incident open since T0
    plan = run(pol, st, at(120))
    assert (plan.kind, plan.step, plan.skipped) == ("escalate", 0, [])
    st = eps.apply_plan(st, plan, pol, at(120))
    assert run(pol, st, at(124)) is None
    assert run(pol, st, at(125)).step == 1
    # Acknowledged long ago: the review clock starts at the takeover.
    acked = eps.new_state(pol.id, at=at(120))
    assert run(pol, acked, at(120), status="acknowledged", ack=at(100)) is None
    assert run(pol, acked, at(135), status="acknowledged", ack=at(100)).review == 1
    when, what = eps.next_action(pol, acked, status="acknowledged", anchor=T0, acknowledged_at=at(100),
                                 now=at(120))
    assert (when, what) == (at(135), "review:1")


def test_policy_without_steps_only_reviews():
    pol = p1(steps=[], repeat_minutes=None)
    assert run(pol, None, at(10)) is None
    assert run(pol, None, at(15)).kind == "review"


def test_route_precedence():
    defaults = {"P1": {"id": "d1", "name": "P1 default"}}
    levels = [{"after_minutes": 15, "notify_channels": ["x"]}]
    r = eps.resolve_route(priority="P1", rule_policy={"id": "r1", "name": "Rule", "enabled": True},
                          defaults=defaults, legacy_levels=levels)
    assert (r.source, r.policy_id) == ("rule", "r1")
    # The rule's own tiers outrank a priority default (a default never takes
    # over a rule that already escalates), also when its policy is disabled.
    r = eps.resolve_route(priority="P1", rule_policy={"id": "r1", "name": "Rule", "enabled": False},
                          defaults=defaults, legacy_levels=levels)
    assert r.source == "legacy_levels" and "disabled" in r.notes[0] and "precedence" in r.notes[1]
    r = eps.resolve_route(priority="P1", rule_policy={"id": "r1", "name": "Rule", "enabled": False},
                          defaults=defaults, legacy_levels=[])
    assert (r.source, r.policy_id) == ("priority_default", "d1") and "disabled" in r.notes[0]
    assert eps.resolve_route(priority="P1", defaults=defaults, legacy_levels=levels).source == "legacy_levels"
    assert eps.resolve_route(priority="P1", defaults=defaults, legacy_levels=[]).policy_id == "d1"
    assert eps.resolve_route(priority="P3", defaults=defaults, legacy_levels=levels).source == "legacy_levels"
    assert eps.resolve_route(priority="P3", defaults=defaults, legacy_levels=[]).source == "none"
    assert "r.escalation_policy_id" in eps.route_policy_sql() and "default_priorities" in eps.route_policy_sql()


def _payload(**kw):
    tgt = [{"type": "user", "id": "11111111-1111-1111-1111-111111111111"}]
    base = {"name": "P2 High", "default_priorities": ["p2"],
            "steps": [{"delay_minutes": 0, "targets": tgt}, {"delay_minutes": 15, "targets": tgt}]}
    base.update(kw)
    return base


def test_validation_normalises_and_rejects():
    c = eps.clean_policy(_payload())
    assert c["default_priorities"] == ["P2"] and [s["position"] for s in c["steps"]] == [0, 1]
    assert c["max_repeats"] == 3 and c["fallback_targets"] == []
    tgt = [{"type": "user", "id": "11111111-1111-1111-1111-111111111111"}]
    bad = [
        _payload(name=""),
        _payload(default_priorities=["P5"]),
        _payload(steps=[{"delay_minutes": 15, "targets": tgt}, {"delay_minutes": 15, "targets": tgt}]),
        _payload(steps=[{"delay_minutes": 30, "targets": tgt}, {"delay_minutes": 15, "targets": tgt}]),
        _payload(steps=[{"delay_minutes": i, "targets": tgt} for i in range(11)]),
        _payload(steps=[{"delay_minutes": 0, "targets": []}]),
        _payload(steps=[{"delay_minutes": 0, "targets": [{"type": "team", "id": tgt[0]["id"]}]}]),
        _payload(steps=[{"delay_minutes": 0, "targets": [{"type": "user", "id": "nope"}]}]),
        _payload(review_minutes=1), _payload(max_repeats=99), _payload(ack_expiry_minutes=0),
    ]
    for b in bad:
        with pytest.raises(eps.PolicyError):
            eps.clean_policy(b)


def test_address_masking():
    assert eps.mask_address("email", "aisha@example.test") == "a***@example.test"
    assert eps.mask_address("sms", "+966501234567") == "•••567"


def test_a_takeover_restarts_the_acknowledgement_expiry_clock():
    """A policy with an expiry that takes over an incident acknowledged long
    ago does not re-open it at once: the clock starts at the takeover."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)
    taken = t0 + timedelta(hours=6)
    st = eps.new_state("p1", at=taken)
    assert eps._ack_clock(st, t0) == taken
    assert eps._ack_clock(st, taken + timedelta(minutes=5)) == taken + timedelta(minutes=5)
    assert eps._ack_clock(None, t0) == t0 and eps._ack_clock(st, None) is None
