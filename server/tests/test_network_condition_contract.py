from itertools import product
import pytest
from app.services.network_conditions import combine, evaluate


def conditions(*metrics):
    return [dict(metric=m, operator='>', threshold=90) for m in metrics]


@pytest.mark.parametrize('a,b', product([True, False, None], repeat=2))
def test_truth_tables(a, b):
    assert combine([a, b], 'AND') is (False if a is False or b is False else None if a is None or b is None else True)
    assert combine([a, b], 'OR') is (True if a is True or b is True else None if a is None or b is None else False)


def test_compound_cpu_and_memory_requires_both():
    history = {'cpu': [(0, 99)], 'memory': [(0, 20)]}
    assert evaluate(conditions('cpu', 'memory'), 'AND', history, now=0).breach is False
    assert evaluate(conditions('cpu', 'memory'), 'OR', history, now=0).breach is True


def test_hold_starts_at_first_bad_sample_not_previous_healthy_sample():
    h = {'cpu': [(0, 10), (90, 99), (180, 99), (270, 99), (360, 99)]}
    assert evaluate(conditions('cpu'), 'AND', h, now=360, hold=300).breach is None
    assert evaluate(conditions('cpu'), 'AND', h, now=390, hold=300).breach is True


@pytest.mark.parametrize('metric', ['cpu', 'tpl_tunnel_state_1', 'if_util_pct'])
def test_stale_and_gap_reset_hold_for_every_metric_family(metric):
    c = conditions(metric)
    assert evaluate(c, 'AND', {metric: [(0, 99)]}, now=180).reason == 'no_data'
    out = evaluate(c, 'AND', {metric: [(0, 99), (600, 99)]}, now=600, hold=300)
    assert out.breach is None and out.held_seconds == 0


def test_or_hold_can_continue_when_alternating_conditions_keep_expression_true():
    h = {'cpu': [(0, 99), (100, 20)], 'memory': [(0, 20), (90, 99)]}
    assert evaluate(conditions('cpu', 'memory'), 'OR', h, now=120, hold=120).breach is True


def test_missing_or_invalid_samples_never_clear_an_alert():
    for value in [None, float('nan'), float('inf')]:
        assert evaluate(conditions('cpu'), 'AND', {'cpu': [(0, value)]}, now=0).breach is None
    assert evaluate(conditions('cpu'), 'AND', {}, now=0).breach is None
    assert evaluate(conditions('cpu'), 'AND', {'cpu': [(0, 0)]}, now=0).breach is False


def test_future_and_duplicate_replay_do_not_create_hold_evidence():
    out = evaluate(conditions('cpu'), 'AND', {'cpu': [(100, 99), (100, 99), (1000, 99)]}, now=100, hold=1)
    assert out.breach is None and out.held_seconds == 0


def test_unmapped_canonical_metric_rejected_instead_of_silently_ignored():
    with pytest.raises(ValueError, match='No network evaluator'):
        evaluate(conditions('fan_state'), 'AND', {}, now=0)


def test_reset_hysteresis_prevents_flapping_without_delaying_recovery():
    c = [dict(metric='cpu', operator='>', threshold=90, reset_threshold=80)]
    assert evaluate(c, 'AND', {'cpu': [(0, 85)]}, now=0).breach is False
    assert evaluate(c, 'AND', {'cpu': [(0, 85)]}, now=0, active=True, hold=300).breach is True
    assert evaluate(c, 'AND', {'cpu': [(0, 79)]}, now=0, active=True, hold=300).breach is False
    with pytest.raises(ValueError, match='healthy side'):
        evaluate([{**c[0], 'reset_threshold': 95}], 'AND', {}, now=0)


def test_slow_template_cadence_does_not_extend_scalar_freshness():
    h = {'cpu': [(0, 99)], 'tpl_state': [(0, 99)]}
    budgets = {'cpu': 180, 'tpl_state': 900}
    assert evaluate(conditions('cpu', 'tpl_state'), 'AND', h, now=300, max_gap=budgets).breach is None
    assert evaluate(conditions('tpl_state'), 'AND', h, now=300, max_gap=budgets).breach is True
