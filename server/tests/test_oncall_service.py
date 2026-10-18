"""On-call resolution (oncall_service): rotations, layers, overrides,
restriction windows and time zones, including DST changes.

Rotation is anchored to the schedule's wall clock: a daily 09:00 handoff in
Europe/London stays at 09:00 local across the March and October changes, and
a business-hours layer above a 24x7 base layer wins only inside its window.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services import oncall_service as oc

UTC = timezone.utc
A, B, C, D = "user-a", "user-b", "user-c", "user-d"


def layer(members, *, handoff, shift=24, position=0, restrict=None, lid="L"):
    return oc.Layer(id=lid, name=lid, position=position, shift_hours=shift, handoff_at=handoff,
                    members=list(members), restrict_to=restrict or [])


def sched(*layers, tz="UTC", overrides=()):
    return oc.Schedule(id="S", name="Primary", timezone=tz, layers=list(layers), overrides=list(overrides))


def test_rotation_by_shift_hours_from_handoff():
    s = sched(layer([A, B, C], handoff=datetime(2026, 10, 1, 9, tzinfo=UTC)))
    at = datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert oc.on_call_at(s, at).users == [A]
    assert oc.on_call_at(s, at + timedelta(hours=23, minutes=59)).users == [A]
    assert oc.on_call_at(s, at + timedelta(days=1)).users == [B]
    assert oc.on_call_at(s, at + timedelta(days=2)).users == [C]
    assert oc.on_call_at(s, at + timedelta(days=3)).users == [A]
    # Before the first handoff the rotation runs backwards continuously.
    assert oc.on_call_at(s, at - timedelta(hours=1)).users == [C]


def test_empty_and_disabled_schedules_have_nobody():
    assert oc.on_call_at(sched(layer([], handoff=datetime(2026, 1, 1, tzinfo=UTC))),
                         datetime(2026, 5, 1, tzinfo=UTC)).users == []
    s = sched(layer([A], handoff=datetime(2026, 1, 1, tzinfo=UTC)))
    s.enabled = False
    assert oc.on_call_at(s, datetime(2026, 5, 1, tzinfo=UTC)).source == "none"


def test_daily_handoff_stays_at_local_time_across_dst():
    london = ZoneInfo("Europe/London")
    s = sched(layer([A, B], handoff=datetime(2026, 3, 27, 9, tzinfo=london)), tz="Europe/London")
    # 2026-03-29 is the spring-forward day (GMT -> BST): 09:00 local is 08:00 UTC.
    assert oc.on_call_at(s, datetime(2026, 3, 29, 7, 59, tzinfo=UTC)).users == [B]   # 28th's shift
    assert oc.on_call_at(s, datetime(2026, 3, 29, 8, 0, tzinfo=UTC)).users == [A]
    assert oc.shift_boundary(s.layers[0], london, 2) == datetime(2026, 3, 29, 8, 0, tzinfo=UTC)
    # Autumn: 2026-10-25 back to GMT, 09:00 local is 09:00 UTC again.
    boundary = oc.shift_boundary(s.layers[0], london, 212)
    assert boundary.astimezone(london).hour == 9 and boundary == datetime(2026, 10, 25, 9, tzinfo=UTC)
    # The 25-hour day: the shift that started at 09:00 BST on the 24th lasts until 09:00 GMT.
    n = oc.shift_index(s.layers[0], london, datetime(2026, 10, 25, 8, 30, tzinfo=UTC))
    assert oc.shift_boundary(s.layers[0], london, n) == datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


def test_non_dst_zone_riyadh():
    riyadh = ZoneInfo("Asia/Riyadh")
    s = sched(layer([A, B], handoff=datetime(2026, 10, 18, 8, tzinfo=riyadh)), tz="Asia/Riyadh")
    assert oc.on_call_at(s, datetime(2026, 10, 18, 5, 0, tzinfo=UTC)).users == [A]     # 08:00 local
    assert oc.on_call_at(s, datetime(2026, 10, 19, 4, 59, tzinfo=UTC)).users == [A]
    assert oc.on_call_at(s, datetime(2026, 10, 19, 5, 0, tzinfo=UTC)).users == [B]


def test_highest_position_layer_on_duty_wins_inside_its_window():
    riyadh = ZoneInfo("Asia/Riyadh")
    base = layer([A], handoff=datetime(2026, 1, 1, tzinfo=riyadh), shift=168, position=0, lid="base")
    # Sunday..Thursday (6, 0, 1, 2, 3) 08:00-17:00 local.
    office = layer([B, C], handoff=datetime(2026, 1, 1, tzinfo=riyadh), shift=168, position=1, lid="office",
                   restrict=[{"days": [6, 0, 1, 2, 3], "start": "08:00", "end": "17:00"}])
    s = sched(base, office, tz="Asia/Riyadh")
    monday_noon = datetime(2026, 10, 19, 12, tzinfo=riyadh)
    who = oc.on_call_at(s, monday_noon)
    assert who.source == "layer" and who.layer_name == "office" and who.users[0] in (B, C)
    assert oc.on_call_at(s, datetime(2026, 10, 19, 18, tzinfo=riyadh)).users == [A]       # after hours
    assert oc.on_call_at(s, datetime(2026, 10, 23, 12, tzinfo=riyadh)).users == [A]       # Friday


def test_overnight_window_belongs_to_the_day_it_starts():
    w = [{"days": [4], "start": "18:00", "end": "08:00"}]                                    # Friday night
    z = ZoneInfo("UTC")
    assert oc.restriction_active(w, z, datetime(2026, 10, 23, 19, tzinfo=UTC))             # Fri 19:00
    assert oc.restriction_active(w, z, datetime(2026, 10, 24, 7, 59, tzinfo=UTC))          # Sat 07:59
    assert not oc.restriction_active(w, z, datetime(2026, 10, 24, 8, tzinfo=UTC))
    assert not oc.restriction_active(w, z, datetime(2026, 10, 24, 19, tzinfo=UTC))         # Sat night
    assert oc.restriction_active([{"days": [], "start": "00:00", "end": "00:00"}], z,
                                 datetime(2026, 10, 24, 3, tzinfo=UTC))                     # whole day


def test_overrides_win_over_every_layer_and_can_stack():
    s = sched(layer([A], handoff=datetime(2026, 1, 1, tzinfo=UTC), position=5),
              overrides=[oc.Override("o1", D, datetime(2026, 10, 18, 10, tzinfo=UTC),
                                     datetime(2026, 10, 18, 12, tzinfo=UTC)),
                         oc.Override("o2", C, datetime(2026, 10, 18, 11, tzinfo=UTC),
                                     datetime(2026, 10, 18, 13, tzinfo=UTC))])
    assert oc.on_call_at(s, datetime(2026, 10, 18, 9, 59, tzinfo=UTC)).users == [A]
    assert oc.on_call_at(s, datetime(2026, 10, 18, 10, tzinfo=UTC)).users == [D]
    both = oc.on_call_at(s, datetime(2026, 10, 18, 11, 30, tzinfo=UTC))
    assert both.source == "override" and both.users == [D, C]
    assert oc.on_call_at(s, datetime(2026, 10, 18, 13, tzinfo=UTC)).users == [A]          # end exclusive


def test_timeline_merges_shifts_marks_overrides_and_gaps():
    office = layer([A, B], handoff=datetime(2026, 10, 19, 0, tzinfo=UTC), shift=24,
                   restrict=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"}])
    s = sched(office, overrides=[oc.Override("o", C, datetime(2026, 10, 20, 12, tzinfo=UTC),
                                             datetime(2026, 10, 20, 14, tzinfo=UTC))])
    tl = oc.timeline(s, datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC))
    got = [(sh["start"].hour, sh["end"].hour, sh["user_id"], sh["override"]) for sh in tl["shifts"]]
    assert got == [(9, 17, A, False), (9, 12, B, False), (12, 14, C, True), (14, 17, B, False)]
    assert tl["gaps"][0] == {"start": datetime(2026, 10, 19, tzinfo=UTC),
                             "end": datetime(2026, 10, 19, 9, tzinfo=UTC)}
    assert oc.on_call_until(s, datetime(2026, 10, 19, 10, tzinfo=UTC)) == datetime(2026, 10, 19, 17, tzinfo=UTC)


def test_timeline_is_capped():
    s = sched(layer([A, B], handoff=datetime(2026, 1, 1, tzinfo=UTC), shift=1))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    tl = oc.timeline(s, start, start + timedelta(days=400))
    assert tl["shifts"][-1]["end"] == start + timedelta(days=oc.MAX_TIMELINE_DAYS)


def test_restriction_validation():
    assert oc.clean_restrictions([{"days": [4, 0, 4], "start": "8:00".zfill(5), "end": "17:30"}]) == [
        {"days": [0, 4], "start": "08:00", "end": "17:30"}]
    for bad in ([{"days": [7], "start": "08:00", "end": "09:00"}],
                [{"days": [1], "start": "8am", "end": "09:00"}], "x"):
        try:
            oc.clean_restrictions(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_contacts_and_recipient_keys_match_channel_dedupe():
    from app.services import host_alert_service as has
    assert oc.normalize_address("email", " Ops@X.test ") == has._recipients(" Ops@X.test ", "email")[0][0]
    assert oc.normalize_address("sms", "+966 (50) 123-4567") == has._recipients("+966 (50) 123-4567", "sms")[0][0]
    p = oc.Person("u1", "Aisha", True, "aisha@example.test", None,
                  [{"kind": "sms", "address": "+966501234567", "label": "Mobile"}])
    rec = p.recipients("schedule:Primary")
    assert [(r["kind"], r["address"]) for r in rec] == [("email", "aisha@example.test"), ("sms", "+966501234567")]
    # An own email method replaces the account email; inactive users get nothing.
    p.methods.append({"kind": "email", "address": "oncall@example.test"})
    assert [r["address"] for r in p.recipients("user") if r["kind"] == "email"] == ["oncall@example.test"]
    p.active = False
    assert p.recipients("user") == []
    dup = oc.dedupe_recipients([{"kind": "email", "address": "A@x.test"}, {"kind": "email", "address": "a@X.test"},
                                {"kind": "sms", "address": "+1 555 0100"}, {"kind": "sms", "address": "+15550100"}])
    assert len(dup) == 2
    assert oc.valid_address("email", "a@b.co") and not oc.valid_address("email", "nope")
    assert oc.valid_address("sms", "+966 50 123 4567") and not oc.valid_address("sms", "call me")
