"""Read timestamped SNMP evidence and bind conditions to monitored entities."""
from collections import defaultdict
from datetime import datetime, timezone
from app.services.network_conditions import INTERFACE_METRICS


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
            # Counter decreases are resets, never negative errors or a huge wrap.
            counts = (ie, oe, idisc, odisc)
            if key in prior and stamp > prior[key][0]:
                before = prior[key][1]
                for metric, indices in [('if_errors', (0, 1)), ('if_discards', (2, 3))]:
                    delta = sum(counts[i] - before[i] for i in indices) if all(counts[i] >= before[i] for i in indices) else None
                    series[metric].append((float(stamp), delta))
            prior[key] = (stamp, counts)
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
            if not iface['admin_up']:
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
