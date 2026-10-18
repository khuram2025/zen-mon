"""Read timestamped SNMP evidence and bind conditions to monitored entities.

Interface error and discard counters (ifInErrors/ifOutErrors,
ifInDiscards/ifOutDiscards) are cumulative Counter32 values. Alert rules see
``if_errors`` / ``if_discards`` as the number counted between two consecutive
polls, inbound plus outbound — the meaning existing thresholds were written
for. Each direction is differenced on its own (``if_in_errors``,
``if_out_errors``, ``if_in_discards``, ``if_out_discards``), with the poll
interval in ``if_counter_interval_s``, so a notification can say "152 errors
in the last 60 s (150 in, 2 out)" (see :func:`counter_context`).

A counter that went down is either a Counter32 wrap or a reset (reboot,
counter clear, re-indexed interface). It is read as a wrap only when that is
plausible for the elapsed time and the counter's recent rate; otherwise the
delta is unknown (None), never a negative count or a 4-billion-error spike.
"""
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from app.services.network_conditions import INTERFACE_METRICS

COUNTER32 = 2 ** 32
# The most errors (or discards) one port can count per second: a 10 GbE port
# receiving nothing but minimum-size frames, every one of them bad.
MAX_COUNTER_RATE_PER_S = 15_000_000
# A wrap is believed only when its delta is within this factor of the rate the
# same counter last ran at over the elapsed time. A counter that sat still
# near the top of its range and then "wrapped" by millions was reset.
WRAP_RATE_SLACK = 10.0

# Rule metric -> its (inbound, outbound) per-direction series.
COUNTER_DIRECTIONS = {
    'if_errors': ('if_in_errors', 'if_out_errors'),
    'if_discards': ('if_in_discards', 'if_out_discards'),
}
COUNTER_INTERVAL = 'if_counter_interval_s'


def counter32_delta(prev, cur, elapsed_s, *, recent_rate: Optional[float] = None,
                    max_rate: float = MAX_COUNTER_RATE_PER_S) -> Optional[int]:
    """What a Counter32 counted between two polls, or None when unknown.

    ``cur >= prev`` is the plain difference (an unchanged counter is 0). A
    decrease is a wrap — ``2**32 - prev + cur`` — only when both readings fit
    32 bits, the wrapped delta is below half the counter range, within what a
    port can count in ``elapsed_s``, and within WRAP_RATE_SLACK times the
    counter's ``recent_rate`` (per second, from its last known delta). Anything
    else is a reset and returns None.
    """
    if prev is None or cur is None:
        return None
    try:
        prev, cur = int(prev), int(cur)
    except (TypeError, ValueError):
        return None
    if prev < 0 or cur < 0:
        return None
    if cur >= prev:
        return cur - prev
    if not elapsed_s or elapsed_s <= 0:
        return None
    if prev >= COUNTER32 or cur >= COUNTER32:
        return None              # not a 32-bit counter: going down is a reset
    wrapped = COUNTER32 - prev + cur
    if wrapped >= COUNTER32 // 2 or wrapped > max_rate * elapsed_s:
        return None
    if not recent_rate or wrapped > WRAP_RATE_SLACK * recent_rate * elapsed_s:
        return None
    return wrapped


def _octets_reset(before, after) -> bool:
    """A 64-bit octet counter (one already past 2**32) going backwards means
    the interface's counters were reset, whatever the error counters say."""
    for prev, cur in zip(before, after):
        if prev is not None and cur is not None and int(prev) >= COUNTER32 and int(cur) < int(prev):
            return True
    return False


def counter_context(history: dict, metric: str, at: Optional[float] = None) -> Optional[dict]:
    """``{'interval_s', 'in', 'out'}`` for the ``metric`` sample at ``at``
    (the newest one at or before it; the newest overall when ``at`` is None).

    ``history`` is an entity's series map (as :func:`entities` yields it).
    ``in`` / ``out`` are the per-direction deltas (None = unknown). None when
    ``metric`` is not a per-poll counter or has no sample. The result goes to
    ``alert_phrasing.reading_text(..., context=...)``.
    """
    directions = COUNTER_DIRECTIONS.get(metric)
    if not directions or not history:
        return None
    stamps = [t for t, _ in history.get(metric, []) if at is None or t <= at]
    if not stamps:
        return None
    stamp = max(stamps)
    inbound, outbound = (dict(history.get(name, [])) for name in directions)
    interval = dict(history.get(COUNTER_INTERVAL, [])).get(stamp)
    return {'interval_s': interval, 'in': inbound.get(stamp), 'out': outbound.get(stamp)}


def fetch_history(metrics, since, until):
    from app.core.database import get_clickhouse_client
    client = get_clickhouse_client()
    scalar = defaultdict(lambda: defaultdict(list))
    interfaces = defaultdict(lambda: defaultdict(list))
    scalar_keys = sorted(set('uptime' if m == 'uptime_reset' else m
                             for m in metrics if m not in INTERFACE_METRICS))
    params = {'since': datetime.fromtimestamp(since, timezone.utc),
              'until': datetime.fromtimestamp(until, timezone.utc), 'keys': scalar_keys}
    if scalar_keys:
        rows = client.query('''SELECT toString(device_id), metric_key,
                toUnixTimestamp64Milli(timestamp) / 1000, value
            FROM zenplus.snmp_metrics
            WHERE timestamp >= %(since)s AND timestamp <= %(until)s
              AND arrayExists(k -> metric_key = k OR startsWith(metric_key, concat(k, '_')), %(keys)s)
            ORDER BY device_id, metric_key, timestamp
            LIMIT 1000000''', parameters=params).result_rows
        if len(rows) >= 1000000:
            raise RuntimeError('Network alert history limit reached; reduce rule scope/window')
        for did, key, stamp, value in rows:
            scalar[did][key].append((float(stamp), value))
        if 'uptime_reset' in metrics:
            for series in scalar.values():
                previous = None
                for stamp, value in series.get('uptime', []):
                    if previous is not None:
                        series['uptime_reset'].append((stamp, float(value < previous - 60)))
                    previous = value
    if set(metrics) & INTERFACE_METRICS:
        rows = client.query('''SELECT toString(device_id), if_index,
                toUnixTimestamp64Milli(timestamp) / 1000,
                in_bps, out_bps, in_errors, out_errors, in_discards, out_discards, oper_status, in_octets, out_octets
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(since)s AND timestamp <= %(until)s
            ORDER BY device_id, if_index, timestamp LIMIT 1000000''', parameters=params).result_rows
        if len(rows) >= 1000000:
            raise RuntimeError('Network interface history limit reached')
        prior = {}
        prior_octets = {}
        recent_rate = {}  # (interface, counter position) -> errors/s of its last known delta
        for did, idx, stamp, ibps, obps, ie, oe, idisc, odisc, oper, in_octets, out_octets in rows:
            key = (did, int(idx))
            series = interfaces[key]
            # The storage contract historically writes zero for a new/reset
            # baseline. Zero throughput is evidence only when counters agree;
            # otherwise preserve unknown instead of clearing a low/high alert.
            before_octets = prior_octets.get(key)
            if before_octets is None:
                ibps = obps = None
            else:
                if ibps == 0 and in_octets != before_octets[0]:
                    ibps = None
                if obps == 0 and out_octets != before_octets[1]:
                    obps = None
            prior_octets[key] = (in_octets, out_octets)
            for metric, value in [('if_in_bps', ibps), ('if_out_bps', obps), ('if_oper_status', oper or None)]:
                series[metric].append((float(stamp), value))
            # Per-direction deltas over the poll interval: a plausible Counter32
            # wrap is counted, a reset is unknown — never negative errors or a
            # 4-billion-error spike. The rule metric stays in + out.
            counts = (ie, oe, idisc, odisc)
            octets = (in_octets, out_octets)
            if key in prior and stamp > prior[key][0]:
                before_stamp, before, before_oct = prior[key]
                elapsed = float(stamp) - float(before_stamp)
                reset = _octets_reset(before_oct, octets)
                deltas = []
                for i in range(4):
                    d = None if reset else counter32_delta(
                        before[i], counts[i], elapsed, recent_rate=recent_rate.get((key, i)))
                    if d is not None and elapsed > 0:
                        recent_rate[(key, i)] = d / elapsed
                    elif d is None:
                        recent_rate.pop((key, i), None)
                    deltas.append(d)
                at = float(stamp)
                series[COUNTER_INTERVAL].append((at, elapsed))
                for metric, (i_in, i_out) in (('if_errors', (0, 1)), ('if_discards', (2, 3))):
                    d_in, d_out = deltas[i_in], deltas[i_out]
                    name_in, name_out = COUNTER_DIRECTIONS[metric]
                    series[name_in].append((at, d_in))
                    series[name_out].append((at, d_out))
                    series[metric].append((at, None if d_in is None or d_out is None else d_in + d_out))
            prior[key] = (stamp, counts, octets)
    return scalar, interfaces


def entities(conditions, device_series, interface_series, interfaces, target_matches):
    """Yield (identity, label, history, metadata). Scalar values broadcast to entities."""
    metrics = [c['metric'] for c in conditions]
    if any(m in INTERFACE_METRICS for m in metrics):
        for iface in interfaces:
            if not target_matches(iface):
                continue
            series = dict(device_series)
            series.update(interface_series.get(iface['if_index'], {}))
            speed = iface['speed']
            if speed:
                incoming = dict(series.get('if_in_bps', []))
                outgoing = dict(series.get('if_out_bps', []))
                series['if_util_pct'] = [(t, max(v, outgoing[t]) * 100 / speed
                                         if v is not None and outgoing.get(t) is not None else None)
                                         for t, v in incoming.items()]
            # Admin-down and never-up ports (unused access ports) are oper-down
            # by design; no oper-status evidence means no interface-down alert.
            if not iface['admin_up'] or not iface.get('ever_up', True):
                series.pop('if_oper_status', None)
            yield iface['if_index'], iface['if_name'] or iface['if_descr'], series, {'if_name': iface['if_name']}
        return
    roots = [m for m in metrics if m.startswith('tpl_')]
    suffixes = {key[len(root):] for root in roots for key in device_series
                if key == root or key.startswith(root + '_')}
    if not roots:
        yield None, None, device_series, {}
    for suffix in sorted(suffixes):
        series = dict(device_series)
        for root in roots:
            series[root] = device_series.get(root + suffix, [])
        identity = 'series:' + roots[0] + suffix
        yield identity, roots[0] + suffix, series, {'entity_key': identity, 'series_suffix': suffix}
