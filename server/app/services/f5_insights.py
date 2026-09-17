"""F5 presentation calculations from device-reported samples, never guessed totals."""
import math
from datetime import datetime, timezone


def ratio(used, total):
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (used, total)):
        return None
    return used / total * 100 if total > 0 and 0 <= used <= total else None


def enrich_f5_groups(groups, now=None):
    now = now or datetime.now(timezone.utc)
    by_key = {g['key']: g for g in groups}
    memory = by_key.get('memory')
    if not memory or not any(m['key'] == 'f5_tmm_mem_total' for m in memory.get('metrics', [])):
        return groups
    metrics = {m['key']: m for m in memory['metrics']}
    percentages = []
    for prefix, key, name in [
        ('f5_system_mem', 'f5_system_memory_pct', 'System RAM utilization'),
        ('f5_tmm_mem', 'f5_tmm_memory_pct', 'TMM utilization'),
        ('f5_other_mem', 'f5_host_memory_pct', 'Non-TMM utilization'),
        ('f5_swap', 'f5_swap_memory_pct', 'Swap utilization'),
    ]:
        used, total = metrics.get(prefix + '_used', {}), metrics.get(prefix + '_total', {})
        value = ratio(used.get('value'), total.get('value'))
        percentages.append(dict(key=key, name=name, type='gauge', unit='%', value=value,
                                text='', status='none', series_key=key, has_data=value is not None))
    memory['metrics'] = percentages + memory['metrics']
    for group in groups:
        if group['key'] in ('fans', 'psus', 'temperature') and not group.get('rows'):
            group['no_data_reason'] = 'No physical sensor readings returned. BIG-IP Virtual Edition may not expose this hardware.'
        if group['key'] == 'pool_members':
            for row in group.get('rows', []):
                cells = row['cells']
                pool = cells.get('f5_member_pool', {}).get('text') or row['label']
                node = cells.get('f5_member_node', {}).get('text') or row['instance']
                port = cells.get('f5_member_port', {}).get('value')
                row['label'] = f'{pool} · {node}' + (f':{int(port)}' if port is not None else '')
        if group['key'] == 'disks':
            group['columns'] = [dict(key=k, name=n, unit=u, type='gauge') for k, n, u in [
                ('f5_disk_used_pct', 'Used', '%'), ('f5_disk_used_bytes', 'Used space', 'bytes'),
                ('f5_disk_total_bytes', 'Total space', 'bytes')]]
            for row in group.get('rows', []):
                cells = row['cells']
                total = cells.get('f5_disk_total_blocks', {}).get('value')
                free = cells.get('f5_disk_free_blocks', {}).get('value')
                block = cells.get('f5_disk_block_size', {}).get('value')
                pct = ratio(total - free, total) if isinstance(total, (int, float)) and isinstance(free, (int, float)) else None
                if pct is not None:
                    cells['f5_disk_used_pct'] = _cell(pct, 'crit' if pct >= 95 else 'warn' if pct >= 90 else 'ok')
                    if isinstance(block, (int, float)) and block > 0:
                        cells['f5_disk_used_bytes'] = _cell((total - free) * block)
                        cells['f5_disk_total_bytes'] = _cell(total * block)
        if group['key'] == 'certificates':
            group['columns'] = [c for c in group['columns'] if c['key'] != 'f5_cert_expiry_epoch']
            group['columns'].insert(0, dict(key='f5_cert_days_remaining', name='Days remaining', unit='days', type='gauge'))
            for row in group.get('rows', []):
                epoch = row['cells'].get('f5_cert_expiry_epoch', {}).get('value')
                if isinstance(epoch, (int, float)) and math.isfinite(epoch) and epoch > 0:
                    days = math.floor((epoch - now.timestamp()) / 86400)
                    row['cells']['f5_cert_days_remaining'] = _cell(days, 'crit' if days < 0 else 'warn' if days <= 30 else 'ok')
        if group['key'] in ('disks', 'certificates'):
            rank = {'none': 0, 'info': 1, 'ok': 2, 'warn': 3, 'crit': 4}
            group['status'] = max((c.get('status', 'none') for r in group.get('rows', []) for c in r['cells'].values()), key=lambda s: rank.get(s, 0), default='none')
    return groups


def _cell(value, status='none'):
    # Derived table snapshots have no stored history series. Do not link a fake chart.
    return dict(value=value, text='', status=status, series_key='')
