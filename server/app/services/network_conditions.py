"""Pure, timestamped metric alert contract shared by fixtures and the workers.

Used by the SNMP (network) and agent (host) metric evaluators. Unknown data
never means zero or recovery. Holds start at the first observed breach (not the
previous healthy poll) and reset across missing-data gaps.

An open incident resolves only once its cleared condition has held for the
clear hold (``clear_duration``; NULL -> :data:`DEFAULT_CLEAR_S`, 0 = the first
healthy sample). Missing or stale samples never count as clear time, so a gap
restarts the clear hold rather than resolving, and a re-breach during the hold
keeps the same incident open. Percent metrics without an explicit
``reset_threshold`` clear :data:`AUTO_CLEAR_POINTS` points on the healthy side
of the trigger (a "> 90" rule clears below 88), so a reading hovering on the
threshold is one incident rather than one per crossing.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
import operator

INTERFACE_METRICS = {'if_in_bps', 'if_out_bps', 'if_util_pct', 'if_errors',
                     'if_discards', 'if_oper_status'}
SCALAR_METRICS = {'cpu', 'memory', 'temperature', 'session_count', 'uptime_reset'}
# Agent host metrics evaluated through the same contract (host_alert_service).
HOST_METRICS = {'host_cpu_pct', 'host_memory_pct', 'host_filesystem_pct', 'host_disk_util_pct'}
UNMAPPED_METRICS = {'fan_state', 'psu_state', 'vpn_tunnel_state', 'ha_state', 'bgp_neighbor_down'}
OPS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le,
       '==': operator.eq, '!=': operator.ne}
OPS.update(dict(zip(('gt', 'gte', 'lt', 'lte', 'eq', 'neq'), OPS.values())))
_HIGH_OPS = ('>', '>=', 'gt', 'gte')
_LOW_OPS = ('<', '<=', 'lt', 'lte')

# Seconds a cleared condition must hold before an open incident resolves when
# the rule leaves alert_rules.clear_duration NULL.
DEFAULT_CLEAR_S = 300
# Percent metrics whose incidents clear this many points on the healthy side
# of the trigger when the rule has no explicit reset_threshold.
AUTO_CLEAR_METRICS = frozenset({'cpu', 'memory', 'host_cpu_pct', 'host_memory_pct',
                                'host_filesystem_pct', 'host_disk_util_pct'})
AUTO_CLEAR_POINTS = 2.0


def supported(metric):
    return (metric in INTERFACE_METRICS | SCALAR_METRICS | HOST_METRICS
            or str(metric).startswith('tpl_'))


def clear_hold_seconds(clear_duration):
    """The clear hold for a rule's clear_duration column (NULL = default)."""
    if clear_duration is None:
        return DEFAULT_CLEAR_S
    return max(0, int(clear_duration))


def auto_reset_threshold(condition):
    """The automatic clear threshold for one condition, or None.

    Only percent metrics with a ">"/"<" style trigger and no explicit
    reset_threshold get one, and only when the band stays inside 0..100 (a
    "> 1%" rule would otherwise never clear).
    """
    if condition.get('reset_threshold') is not None:
        return None
    if condition.get('metric') not in AUTO_CLEAR_METRICS:
        return None
    op = condition.get('operator')
    try:
        thr = float(condition.get('threshold'))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(thr):
        return None
    if op in _HIGH_OPS and thr - AUTO_CLEAR_POINTS >= 0:
        return thr - AUTO_CLEAR_POINTS
    if op in _LOW_OPS and thr + AUTO_CLEAR_POINTS <= 100:
        return thr + AUTO_CLEAR_POINTS
    return None


def effective_reset_threshold(condition):
    """The threshold an open incident must clear: explicit, automatic or the trigger."""
    if condition.get('reset_threshold') is not None:
        return float(condition['reset_threshold'])
    auto = auto_reset_threshold(condition)
    return auto if auto is not None else condition['threshold']


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
            if op in _HIGH_OPS and reset <= c['threshold']:
                pass
            elif op in _LOW_OPS and reset >= c['threshold']:
                pass
            else:
                raise ValueError('Reset threshold must lie on the healthy side of a numeric trigger')
    metrics = [c['metric'] for c in conditions]
    if any(m in INTERFACE_METRICS for m in metrics) and any(m.startswith('tpl_') for m in metrics):
        raise ValueError('Interface and template-table conditions require separate rules (different entity identities)')


# ── Value domains (authoring checks) ─────────────────────────────────────────
#
# The values each metric can actually take. A condition that no reading can
# ever satisfy ("ping_status > 5", "cpu > 150") or that every reading
# satisfies ("packet_loss >= 0") is rejected when the rule is saved: the first
# never fires and the second fires on every sample. Metrics not listed here
# (temperature, template series, event toggles, NetFlow, trap, syslog) are not
# range-checked.

@dataclass(frozen=True)
class Domain:
    # Human description used in messages, e.g. "ping_status is 0 (down) or 1 (up)".
    text: str
    values: tuple = ()           # discrete domain (non-empty) ...
    lo: float | None = None      # ... or a continuous range [lo, hi]
    hi: float | None = None


_PERCENT = ('cpu', 'memory', 'packet_loss', 'if_util_pct', 'host_cpu_pct', 'host_memory_pct',
            'host_filesystem_pct', 'host_disk_util_pct', 'udt_port_capacity_pct', 'netpath_loss')
_FRACTION = ('apm_error_rate', 'apm_apdex', 'apm_rum_error_session_rate', 'apm_rum_resource_failure_rate')
_NON_NEGATIVE = ('rtt', 'jitter', 'if_in_bps', 'if_out_bps', 'if_errors', 'if_discards', 'session_count',
                 'apm_latency_p50', 'apm_latency_p95', 'apm_latency_p99', 'apm_throughput',
                 'apm_rum_lcp_p75', 'apm_rum_inp_p75', 'apm_rum_cls_p75', 'apm_rum_new_error_groups',
                 'apm_slo_burn', 'netpath_rtt', 'netpath_hop_count')

VALUE_DOMAINS: dict[str, Domain] = {
    'ping_status': Domain('ping_status is 0 (down) or 1 (up)', values=(0, 1)),
    'uptime_reset': Domain('uptime_reset is 1 after a reboot, otherwise 0', values=(0, 1)),
    'if_oper_status': Domain('if_oper_status is an IF-MIB code from 1 (up) to 7 (lowerLayerDown); 2 is down',
                             values=(1, 2, 3, 4, 5, 6, 7)),
    **{m: Domain(f'{m} is a percentage from 0 to 100', lo=0.0, hi=100.0) for m in _PERCENT},
    **{m: Domain(f'{m} is a fraction from 0 to 1', lo=0.0, hi=1.0) for m in _FRACTION},
    **{m: Domain(f'{m} is never negative', lo=0.0) for m in _NON_NEGATIVE},
}

_OP_SYMBOL = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=', 'eq': '==', 'neq': '!='}


def op_symbol(operator):
    op = str(operator or '').strip()
    return _OP_SYMBOL.get(op.lower(), op)


def _num(v):
    return int(v) if float(v).is_integer() else v


def domain_problem(metric, operator, threshold):
    """Why ``metric operator threshold`` is unusable, or None.

    Returns a message when the comparison can never be true or is always true
    for the values the metric can take; None when it is satisfiable, or when
    the metric/operator is unknown here (other validation owns those).
    """
    dom = VALUE_DOMAINS.get(metric)
    op = op_symbol(operator)
    fn = OPS.get(op)
    if dom is None or fn is None:
        return None
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(thr):
        return f'{metric}: the threshold must be a finite number'
    shown = f"'{op} {_num(thr)}'"
    if dom.values:
        hits = [v for v in dom.values if fn(v, thr)]
        if not hits:
            return f'{dom.text}; {shown} can never be true'
        if len(hits) == len(dom.values):
            return f'{dom.text}; {shown} is always true'
        return None
    lo = -math.inf if dom.lo is None else dom.lo
    hi = math.inf if dom.hi is None else dom.hi
    never = {'>': thr >= hi, '>=': thr > hi, '<': thr <= lo, '<=': thr < lo,
             '==': not lo <= thr <= hi, '!=': False}[op]
    always = {'>': thr < lo, '>=': thr <= lo, '<': thr > hi, '<=': thr >= hi,
              '==': lo == hi == thr, '!=': not lo <= thr <= hi}[op]
    if never:
        return f'{dom.text}; {shown} can never be true'
    if always:
        return f'{dom.text}; {shown} is always true'
    return None


def reset_problem(metric, reset_threshold):
    """Why an explicit reset level is outside the metric's range, or None."""
    dom = VALUE_DOMAINS.get(metric)
    if dom is None or reset_threshold is None or dom.values:
        return None
    try:
        reset = float(reset_threshold)
    except (TypeError, ValueError):
        return None
    lo = -math.inf if dom.lo is None else dom.lo
    hi = math.inf if dom.hi is None else dom.hi
    if not lo <= reset <= hi:
        return f'{dom.text}; a reset level of {_num(reset)} can never be reached'
    return None


# Device status triggers name the ping_status they fire on.
TRIGGER_PING_STATUS = {'down': 0, 'up': 1, 'degraded': 1}


def trigger_problem(trigger_on, metric, operator, threshold):
    """Why a ping_status condition contradicts the rule's ``trigger_on``, or None.

    A "down" rule fires on ping_status 0 and an "up"/"degraded" rule on 1; a
    ping_status condition that selects anything else contradicts the trigger
    (the evaluator follows the trigger, so the stored condition would lie).
    """
    want = TRIGGER_PING_STATUS.get(str(trigger_on or 'any'))
    if want is None or metric != 'ping_status':
        return None
    fn = OPS.get(op_symbol(operator))
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        return None
    if fn is None:
        return None
    hits = {v for v in (0, 1) if fn(v, thr)}
    if hits == {want}:
        return None
    return (f"trigger_on '{trigger_on}' fires on ping_status {want} "
            f"({'down' if want == 0 else 'up'}); the condition "
            f"'ping_status {op_symbol(operator)} {_num(thr)}' contradicts it, use 'ping_status == {want}'")


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
    # Seconds the cleared condition has held (active incidents only).
    cleared_seconds: float = 0


def evaluate(conditions, logic, history, *, now, hold=0, max_gap=180, active=False,
             clear_hold=0):
    """history maps metric to [(UTC epoch seconds, value), ...].

    Replayed timestamps are deduplicated; future and nonfinite readings do not
    establish evidence. max_gap is the explicitly chosen freshness budget.

    ``hold`` is the breach persistence (min_duration) before a new incident
    fires. With ``active`` (an incident is open) the cleared condition is
    judged against each condition's effective reset threshold and must hold
    for ``clear_hold`` seconds: until then the result is
    ``Evaluation(None, ..., reason='pending_clear')``, which keeps the
    incident open without announcing anything. Missing or stale data restarts
    that clock; a breach during it continues the incident (breach True).
    """
    validate_conditions(conditions)
    if active:
        conditions = [{**c, 'threshold': effective_reset_threshold(c)} for c in conditions]
        hold = 0  # the trigger hold does not delay a healthy recovery
    gaps = {c['metric']: max_gap.get(c['metric'], 180) if isinstance(max_gap, dict) else max_gap for c in conditions}
    if not conditions or any(g <= 0 for g in gaps.values()) or hold < 0 or clear_hold < 0:
        raise ValueError('Conditions, positive freshness and nonnegative holds required')
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
    cleared = None
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
        # Clear time only accumulates over continuous healthy evidence: a
        # breach or a gap (state None) restarts it.
        if state is not False:
            cleared = None
        elif cleared is None:
            cleared = t
    elapsed = now - started if started is not None else 0
    if state is True and elapsed < hold:
        return Evaluation(None, readings, elapsed, 'pending_hold')
    if active and state is False:
        clear_elapsed = now - cleared if cleared is not None else 0
        if clear_elapsed < clear_hold:
            return Evaluation(None, readings, 0, 'pending_clear', clear_elapsed)
        return Evaluation(False, readings, 0, 'evaluated', clear_elapsed)
    return Evaluation(state, readings, elapsed, 'no_data' if state is None else 'evaluated')
