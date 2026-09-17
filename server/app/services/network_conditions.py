"""Pure, timestamped network alert contract shared by fixtures and the worker.

Unknown data never means zero or recovery. Holds start at the first observed
breach (not the previous healthy poll) and reset across missing-data gaps.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
import operator

INTERFACE_METRICS = {'if_in_bps', 'if_out_bps', 'if_util_pct', 'if_errors',
                     'if_discards', 'if_oper_status'}
SCALAR_METRICS = {'cpu', 'memory', 'temperature', 'session_count', 'uptime_reset'}
UNMAPPED_METRICS = {'fan_state', 'psu_state', 'vpn_tunnel_state', 'ha_state', 'bgp_neighbor_down'}
OPS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le,
       '==': operator.eq, '!=': operator.ne}
OPS.update(dict(zip(('gt', 'gte', 'lt', 'lte', 'eq', 'neq'), OPS.values())))


def supported(metric):
    return metric in INTERFACE_METRICS | SCALAR_METRICS or str(metric).startswith('tpl_')


def validate_conditions(conditions):
    for c in conditions:
        if not supported(c.get('metric')):
            raise ValueError(f"No network evaluator for {c.get('metric')}; select a collected template metric")
        if c.get('operator') not in OPS or not math.isfinite(float(c.get('threshold', float('nan')))):
            raise ValueError('Network conditions require a supported operator and finite threshold')
        reset = c.get('reset_threshold')
        if reset is not None:
            if not math.isfinite(float(reset)):
                raise ValueError('Reset threshold must be finite')
            op = c['operator']
            if op in ('>', '>=', 'gt', 'gte') and reset <= c['threshold']:
                pass
            elif op in ('<', '<=', 'lt', 'lte') and reset >= c['threshold']:
                pass
            else:
                raise ValueError('Reset threshold must lie on the healthy side of a numeric trigger')
    metrics = [c['metric'] for c in conditions]
    if any(m in INTERFACE_METRICS for m in metrics) and any(m.startswith('tpl_') for m in metrics):
        raise ValueError('Interface and template-table conditions require separate rules (different entity identities)')


def combine(values, logic):
    if logic == 'AND':
        return False if False in values else (None if None in values else True)
    if logic == 'OR':
        return True if True in values else (None if None in values else False)
    raise ValueError('Condition logic must be AND or OR')


@dataclass(frozen=True)
class Evaluation:
    breach: bool | None
    values: dict[str, float | None]
    held_seconds: float = 0
    reason: str = ''


def evaluate(conditions, logic, history, *, now, hold=0, max_gap=180, active=False):
    """history maps metric to [(UTC epoch seconds, value), ...].

    Replayed timestamps are deduplicated; future and nonfinite readings do not
    establish evidence. max_gap is the explicitly chosen freshness budget.
    """
    validate_conditions(conditions)
    if active:
        conditions = [{**c, 'threshold': c.get('reset_threshold') if c.get('reset_threshold') is not None else c['threshold']}
                      for c in conditions]
        hold = 0  # the trigger hold does not delay a healthy recovery
    gaps = {c['metric']: max_gap.get(c['metric'], 180) if isinstance(max_gap, dict) else max_gap for c in conditions}
    if not conditions or any(g <= 0 for g in gaps.values()) or hold < 0:
        raise ValueError('Conditions, positive freshness and nonnegative hold required')
    points = {}
    for c in conditions:
        points[c['metric']] = sorted({float(t): float(v) if v is not None and math.isfinite(float(v)) else None
                                      for t, v in history.get(c['metric'], []) if t <= now}.items())
    times = {now}
    for key, series in points.items():
        for t, _ in series:
            times.add(t)
            if t + gaps[key] <= now:
                times.add(t + gaps[key])
    indexes = {m: [t for t, _ in series] for m, series in points.items()}
    started = None
    state = None
    readings = {}
    for t in sorted(times):
        states = []
        for c in conditions:
            key = c['metric']
            ix = bisect_right(indexes[key], t) - 1
            stamp, value = points[key][ix] if ix >= 0 else (0, None)
            if ix < 0 or t - stamp >= gaps[key]:
                value = None
            readings[key] = value
            states.append(None if value is None else OPS[c['operator']](value, c['threshold']))
        state = combine(states, logic)
        if state is not True:
            started = None
        elif started is None:
            started = t
    elapsed = now - started if started is not None else 0
    if state is True and elapsed < hold:
        return Evaluation(None, readings, elapsed, 'pending_hold')
    return Evaluation(state, readings, elapsed, 'no_data' if state is None else 'evaluated')
