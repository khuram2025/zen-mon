from datetime import datetime, timezone
from app.services.f5_insights import enrich_f5_groups, ratio


def metric(key, value):
    return dict(key=key, value=value, has_data=True)


def memory():
    return dict(key='memory', metrics=[
        metric('f5_tmm_mem_total', 6501171200), metric('f5_tmm_mem_used', 1428847568),
        metric('f5_other_mem_total', 10325397504), metric('f5_other_mem_used', 9358487552),
        metric('f5_system_mem_total', 16826568704), metric('f5_system_mem_used', 10787326592),
        metric('f5_swap_total', 1048571904), metric('f5_swap_used', 990171136)])


def test_f5_percentages_use_matching_domains():
    group = enrich_f5_groups([memory()])[0]
    values = {m['key']: m['value'] for m in group['metrics']}
    assert 64 < values['f5_system_memory_pct'] < 65
    assert 21 < values['f5_tmm_memory_pct'] < 23
    assert 90 < values['f5_host_memory_pct'] < 92
    assert 94 < values['f5_swap_memory_pct'] < 95
    for pair in [(1, 0), (101, 100), (-1, 100), (float('nan'), 100), (None, 10)]:
        assert ratio(*pair) is None
    assert ratio(0, 100) == 0


def test_disk_uses_reported_block_size_and_has_no_fake_history():
    group = dict(key='disks', columns=[], rows=[dict(cells={
        'f5_disk_total_blocks': dict(value=100), 'f5_disk_free_blocks': dict(value=4),
        'f5_disk_block_size': dict(value=4096)})])
    enrich_f5_groups([memory(), group])
    cells = group['rows'][0]['cells']
    assert cells['f5_disk_used_pct']['value'] == 96
    assert cells['f5_disk_used_bytes']['value'] == 96 * 4096
    assert cells['f5_disk_used_pct']['series_key'] == ''
    assert group['status'] == 'crit'


def test_certificate_expiry_uses_server_clock_and_keeps_inventory_text():
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    group = dict(key='certificates', columns=[dict(key='f5_cert_expiry_epoch'), dict(key='f5_cert_expiry_text')], rows=[dict(cells={'f5_cert_expiry_epoch': dict(value=now.timestamp()-86400, status='none')})])
    enrich_f5_groups([memory(), group], now)
    assert group['rows'][0]['cells']['f5_cert_days_remaining']['value'] == -1
    assert group['status'] == 'crit'
    assert 'f5_cert_expiry_epoch' not in [c['key'] for c in group['columns']]


def test_members_get_distinct_labels_even_when_their_metrics_match():
    rows = [dict(instance=str(i), label='/Common/pool', cells={
        'f5_member_pool': dict(text='/Common/pool'), 'f5_member_node': dict(text=f'/Common/node{i}'),
        'f5_member_port': dict(value=443)}) for i in range(2)]
    group = dict(key='pool_members', rows=rows)
    enrich_f5_groups([memory(), group])
    assert rows[0]['label'] != rows[1]['label']
    assert rows[0]['label'].endswith('node0:443')


def test_other_vendors_unchanged_and_missing_f5_hardware_explained():
    generic = [dict(key='memory', metrics=[metric('other_vendor_memory', 42)])]
    assert enrich_f5_groups(generic) == generic
    hardware = dict(key='fans', rows=[])
    enrich_f5_groups([memory(), hardware])
    assert 'Virtual Edition' in hardware['no_data_reason']
