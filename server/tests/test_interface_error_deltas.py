"""Interface error/discard counters as per-poll deltas (NOTIFY 04).

ifInErrors/ifOutErrors are cumulative Counter32 values. An alert rule's
``if_errors`` keeps its meaning — errors counted between two consecutive polls,
inbound plus outbound — so configured thresholds do not change. These pin the
acceptance criteria: an unchanged cumulative counter never triggers, a real
burst does, a reset or wrap never manufactures a spike, and the message states
the interval and the direction.

ClickHouse is replaced by an in-memory row list; nothing here touches an
appliance database.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.services import alert_phrasing as ap
from app.services import network_history as nh
from app.services.network_conditions import evaluate

C32 = 2 ** 32


def _rows(samples, *, device="d", if_index=1, octets=None):
    """snmp_if_metrics rows from (stamp, in_errors, out_errors[, in_disc, out_disc])."""
    out = []
    for n, sample in enumerate(samples):
        stamp, ie, oe, *disc = sample
        idisc, odisc = (disc + [0, 0])[:2]
        in_oct, out_oct = octets[n] if octets else (1000 * (n + 1), 1000 * (n + 1))
        out.append((device, if_index, stamp, 1e6, 1e6, ie, oe, idisc, odisc, 1, in_oct, out_oct))
    return out


def _history(monkeypatch, rows, metrics=("if_errors",)):
    from app.core import database
    monkeypatch.setattr(database, "get_clickhouse_client",
                        lambda: NS(query=lambda *a, **k: NS(result_rows=rows)))
    _, interfaces = nh.fetch_history(list(metrics), 0, 10_000)
    return interfaces[("d", 1)]


# ── counter32_delta ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("prev,cur,elapsed,rate,expected", [
    (500, 500, 60, None, 0),                       # unchanged: zero, not unknown
    (500, 652, 60, None, 152),
    (C32 - 10, 5, 60, 1.0, 15),                    # a wrap at the counter's own pace
    (C32 - 10, 5, 60, None, None),                 # no recent rate: cannot tell from a reset
    (C32 - 10, 5, 60, 0.0, None),                  # a counter that sat still did not wrap
    (3_000_000_000, 7, 60, 1.0, None),             # reset: a 1.3e9 "wrap" is implausible
    (4_000_000_000, 7, 60, 10_000.0, None),        # 2.9e8 in 60 s at 10k/s: still a reset
    (C32 - 1_000_000, 1_000_000, 60, 50_000.0, 2_000_000),   # a busy counter really wrapping
    (C32 - 1_000_000, 1_000_000, 0, 50_000.0, None),         # no elapsed time
    (C32 + 5, 3, 60, 1e9, None),                   # not a 32-bit counter: going down is a reset
    (100, 50, 60, 1.0, None),                      # small backwards step: reset
    (None, 5, 60, 1.0, None),
])
def test_counter32_delta(prev, cur, elapsed, rate, expected):
    assert nh.counter32_delta(prev, cur, elapsed, recent_rate=rate) == expected


def test_a_wrap_beyond_what_a_port_can_count_is_a_reset():
    assert nh.counter32_delta(C32 - 1, 2_000_000, 0.1, recent_rate=1e12) is None


# ── fetch_history ────────────────────────────────────────────────────────────

def test_unchanged_cumulative_counters_never_trigger(monkeypatch):
    """A port that logged 5 million errors last year and none since reads 0
    per poll, every poll: the cumulative total is not a reading."""
    rows = _rows([(t, 5_000_000, 3_000) for t in range(0, 900, 60)])
    hist = _history(monkeypatch, rows)
    assert [v for _, v in hist["if_errors"]] == [0] * 14
    cond = [{"metric": "if_errors", "operator": ">", "threshold": 100}]
    result = evaluate(cond, "AND", hist, now=840, hold=300, max_gap=180)
    assert result.breach is False


def test_a_real_burst_triggers_with_interval_and_direction(monkeypatch):
    samples = [(t, 1_000, 10) for t in range(0, 360, 60)]
    # 150 inbound and 2 outbound errors per minute from t=360 on.
    samples += [(360 + 60 * k, 1_000 + 150 * (k + 1), 10 + 2 * (k + 1)) for k in range(7)]
    hist = _history(monkeypatch, _rows(samples))
    assert hist["if_errors"][-1] == (720.0, 152)
    assert hist["if_in_errors"][-1] == (720.0, 150) and hist["if_out_errors"][-1] == (720.0, 2)
    assert hist[nh.COUNTER_INTERVAL][-1] == (720.0, 60.0)

    cond = [{"metric": "if_errors", "operator": ">", "threshold": 100}]
    result = evaluate(cond, "AND", hist, now=720, hold=300, max_gap=180)
    assert result.breach is True and result.values["if_errors"] == 152

    ctx = nh.counter_context(hist, "if_errors", 720)
    assert ctx == {"interval_s": 60.0, "in": 150, "out": 2}
    assert ap.reading_text("if_errors", result.values["if_errors"], ">", 100, ctx) == \
        "152 errors in the last 60 s (150 in, 2 out)"


def test_a_counter_reset_is_unknown_never_a_spike(monkeypatch):
    """A reboot zeroes the counters: the poll that sees it is unknown (it
    neither triggers nor clears), and counting resumes from the new base."""
    samples = [(0, 3_000_000_000, 40), (60, 3_000_000_000, 40),
               (120, 12, 1),            # reset
               (180, 12, 1), (240, 20, 1)]
    hist = _history(monkeypatch, _rows(samples))
    assert hist["if_errors"] == [(60.0, 0), (120.0, None), (180.0, 0), (240.0, 8)]
    assert hist["if_in_errors"][1] == (120.0, None) and hist["if_out_errors"][1] == (120.0, None)
    assert max(v for _, v in hist["if_errors"] if v is not None) < 100


def test_a_64bit_octet_reset_marks_every_counter_unknown(monkeypatch):
    """Error counters that happen to rise across a reboot are still a reset
    when the interface's 64-bit octet counters went backwards."""
    octets = [(10 * C32, 10 * C32), (10 * C32 + 500, 10 * C32 + 500), (900, 900)]
    samples = [(0, 5, 5), (60, 6, 5), (120, 400, 7)]
    hist = _history(monkeypatch, _rows(samples, octets=octets))
    assert hist["if_errors"] == [(60.0, 1), (120.0, None)]


def test_a_counter32_wrap_is_counted_once_at_the_counters_pace(monkeypatch):
    samples = [(0, C32 - 300, 0), (60, C32 - 200, 0), (120, 50, 0), (180, 150, 0)]
    hist = _history(monkeypatch, _rows(samples))
    assert hist["if_in_errors"] == [(60.0, 100), (120.0, 250), (180.0, 100)]
    assert hist["if_errors"] == [(60.0, 100), (120.0, 250), (180.0, 100)]


def test_a_wrap_after_an_idle_counter_is_unknown(monkeypatch):
    """Sitting still near the top and then 'wrapping' is indistinguishable
    from a reset, so it is not counted."""
    samples = [(0, C32 - 100, 0), (60, C32 - 100, 0), (120, 5, 0)]
    hist = _history(monkeypatch, _rows(samples))
    assert hist["if_errors"] == [(60.0, 0), (120.0, None)]


def test_a_reset_poll_breaks_the_hold_rather_than_confirming_it(monkeypatch):
    """Unknown data resets a breach hold (network_conditions contract): a
    reset in the middle of a burst cannot complete a 5-minute hold early."""
    samples = [(60 * k, 1_000 + 200 * k, 0) for k in range(5)]
    samples += [(300, 3, 0)] + [(360 + 60 * k, 3 + 200 * (k + 1), 0) for k in range(3)]
    hist = _history(monkeypatch, _rows(samples))
    cond = [{"metric": "if_errors", "operator": ">", "threshold": 100}]
    assert evaluate(cond, "AND", hist, now=540, hold=300, max_gap=180).breach is None


def test_discards_get_the_same_per_direction_treatment(monkeypatch):
    samples = [(0, 0, 0, 10, 20), (60, 0, 0, 40, 25), (120, 0, 0, 2, 25)]
    hist = _history(monkeypatch, _rows(samples), metrics=("if_discards",))
    assert hist["if_discards"] == [(60.0, 35), (120.0, None)]
    assert hist["if_in_discards"] == [(60.0, 30), (120.0, None)]
    assert hist["if_out_discards"] == [(60.0, 5), (120.0, 0)]
    assert nh.counter_context(hist, "if_discards") == {"interval_s": 60.0, "in": None, "out": 0}


def test_the_reported_interval_follows_the_device_poll_interval(monkeypatch):
    samples = [(0, 0, 0), (300, 150, 2)]
    hist = _history(monkeypatch, _rows(samples))
    ctx = nh.counter_context(hist, "if_errors")
    assert ctx["interval_s"] == 300.0
    assert ap.reading_text("if_errors", 152, ">", 100, ctx) == \
        "152 errors in the last 5 min (150 in, 2 out)"


def test_counter_context_edges():
    assert nh.counter_context({}, "if_errors") is None
    assert nh.counter_context({"cpu": [(1.0, 5)]}, "cpu") is None
    hist = {"if_errors": [(60.0, 3), (120.0, 9)], "if_in_errors": [(60.0, 3), (120.0, 9)],
            "if_out_errors": [(60.0, 0), (120.0, 0)], nh.COUNTER_INTERVAL: [(60.0, 60.0), (120.0, 60.0)]}
    assert nh.counter_context(hist, "if_errors", 90) == {"interval_s": 60.0, "in": 3, "out": 0}
    assert nh.counter_context(hist, "if_errors", 10) is None


def test_entities_carry_the_direction_series_to_the_evaluator(monkeypatch):
    rows = _rows([(0, 0, 0), (60, 150, 2)])
    hist = _history(monkeypatch, rows)
    iface = {"if_index": 1, "if_name": "Gi0/1", "if_descr": "", "speed": 0,
             "admin_up": True, "ever_up": True}
    [(_, label, series, _)] = list(nh.entities(
        [{"metric": "if_errors", "operator": ">", "threshold": 100}], {}, {1: hist}, [iface],
        lambda i: True))
    assert label == "Gi0/1"
    assert nh.counter_context(series, "if_errors") == {"interval_s": 60.0, "in": 150, "out": 2}
