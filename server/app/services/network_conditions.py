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
                     'if_discards', 'if_oper_status', 'if_error_rate_pct',
                     'if_discard_rate_pct', 'if_flaps', 'if_speed_changed'}
# Interface metrics that are counted over a window rather than read as a level
# (docs/alert-notification-policy.md "Interface quality"): if_flaps counts
# oper-status changes in the rule's window, if_speed_changed is 1 while a
# negotiated speed change is recent. Each is the only condition of its rule.
WINDOW_METRICS = frozenset({'if_flaps'})
EVENT_METRICS = frozenset({'if_speed_changed'})
SCALAR_METRICS = {'cpu', 'memory', 'temperature', 'session_count', 'uptime_reset'}
# Agent host metrics evaluated through the same contract (host_alert_service).
HOST_METRICS = {'host_cpu_pct', 'host_memory_pct', 'host_filesystem_pct', 'host_disk_util_pct',
                # Server depth metrics (alerting plan G-9, migrate-208).
                'host_swap_pct', 'host_disk_latency_ms', 'host_nic_errors', 'host_clock_skew_s',
                'host_fs_days_left'}
# Canonical hardware/protocol state keys no collector writes. Device templates
# collect these states per component; a rule names the template state metric
# instead (docs/alert-rule-overrides.md "State rules"). Refused at authoring,
# and an old rule on one is skipped by the evaluator with this reason.
UNMAPPED_METRICS = {
    'fan_state': "Fan state is collected by device templates, per fan: use the template state metric "
                 "Fans: State (tpl_cisco_fan_state), Chassis Fans: Status (tpl_f5_fan_status), "
                 "Fans: Status (tpl_aruba_fan_status) or Hardware Sensors: Alarm (tpl_fgt_sensor_alarm), "
                 "or accept the template's recommended state rule",
    'psu_state': "Power supply state is collected by device templates, per power supply: use the template "
                 "state metric Power Supplies: State (tpl_cisco_psu_state), Power Supplies: Status "
                 "(tpl_f5_psu_status or tpl_aruba_psu_status) or Hardware Sensors: Alarm (tpl_fgt_sensor_alarm), "
                 "or accept the template's recommended state rule",
    'vpn_tunnel_state': "VPN tunnel state is collected by device templates, per tunnel: use the template state "
                        "metric IPsec Tunnels: Status (tpl_fgt_tun_status), or accept the template's "
                        "recommended state rule",
    'ha_state': "HA state is collected by device templates: use the template state metric HA Members: Sync "
                "Status (tpl_fgt_ha_sync), HA & Config Sync: Failover State or Sync Status (tpl_f5_failover, "
                "tpl_f5_sync), High Availability: Local State (tpl_pan_ha_state) or Failover: Primary/Secondary "
                "Unit (tpl_asa_fo_primary, tpl_asa_fo_secondary), or accept the template's recommended state rule",
    'bgp_neighbor_down': "BGP peer state is collected by device templates, per peer: use the template state "
                         "metric BGP Peers: State (tpl_jnx_bgp_state), or accept the template's recommended "
                         "state rule",
}
# Keys that existing rules may keep but new rules may not use: no collector
# supplies them (session_count; the poller writes the FortiGate-only
# "sessions" series) or the device status path never measures them (jitter).
RETIRED_METRICS = {
    'session_count': "Session count is not collected under this name: use the template metric for the "
                     "device's session table instead: Active Sessions (tpl_fgt_sessions, tpl_pan_sess_active, "
                     "tpl_jnx_sessions), Session Utilization (tpl_pan_sess_util) or Current Connections "
                     "(tpl_asa_conns)",
    'jitter': "Device status checks do not measure jitter, so a jitter rule would never fire: use a "
              "NetPath rule or the template metric SD-WAN Health: Jitter (tpl_fgt_sdwan_jitter)",
}
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
                                'host_filesystem_pct', 'host_disk_util_pct', 'host_swap_pct'})
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
    if condition.get('metric') in WINDOW_METRICS:
        # A flapping interface clears once its window holds no change at all.
        return {'>': 0.0, 'gt': 0.0, '>=': 1.0, 'gte': 1.0}.get(condition.get('operator'))
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


def refuse_new_metrics(conditions, existing=()):
    """Raise ValueError for retired or unmapped keys a rule newly uses.

    ``existing`` are the metrics the rule already had: an old rule keeps
    loading and stays editable with them; only introducing one is refused.
    """
    kept = set(existing or ())
    for c in conditions:
        m = c.get('metric')
        if m in UNMAPPED_METRICS:
            raise ValueError(UNMAPPED_METRICS[m])
        if m in RETIRED_METRICS and m not in kept:
            raise ValueError(RETIRED_METRICS[m])


def validate_conditions(conditions):
    for c in conditions:
        if c.get('metric') in UNMAPPED_METRICS:
            raise ValueError(UNMAPPED_METRICS[c['metric']])
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
    special = [m for m in metrics if m in WINDOW_METRICS | EVENT_METRICS]
    if special and len(conditions) > 1:
        raise ValueError('Interface flapping and speed change conditions need a rule of their own')
    for c in conditions:
        if c['metric'] in WINDOW_METRICS and c['operator'] not in _HIGH_OPS:
            raise ValueError('An interface flapping rule fires on at least a number of state changes: use > or >=')
        if c['metric'] in EVENT_METRICS and not (OPS[c['operator']](1, float(c['threshold']))
                                                 and not OPS[c['operator']](0, float(c['threshold']))):
            raise ValueError('A speed change rule fires when the speed changes: use == 1')
    if any(m in INTERFACE_METRICS for m in metrics) and any(m.startswith('tpl_') or m == 'temperature'
                                                            for m in metrics):
        raise ValueError('Interface and per-component (template or temperature sensor) conditions require separate rules (different entity identities)')


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
