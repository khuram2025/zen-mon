import pytest
from app.services import flow_ingest_journal as flow


def test_recovery_adapts_numeric_ipv4_without_changing_identity_or_source(monkeypatch):
    from app.services.host_insert_batcher import HostInsertBatcher
    calls = []
    monkeypatch.setattr(HostInsertBatcher, '_write', lambda self, *args: calls.append(args))
    columns = ['exporter_ip', 'src_addr', 'bytes', 'field_ids']
    rows = [[0xC0000201, 0, 2**64-1, [27, 28]]]
    flow.write(None, 'flow_records', columns, rows, 'original-token')
    assert calls == [('flow_records', columns, [['192.0.2.1', '0.0.0.0', 2**64-1, [27, 28]]], 'original-token')]
    assert rows[0][0] == 0xC0000201


@pytest.mark.parametrize('value', [-1, 2**32, True])
def test_invalid_ipv4_is_rejected_before_writing(value):
    with pytest.raises(ValueError):
        flow.write(None, 'flow_if_counters', ['exporter_ip'], [[value]], 'token')


def test_unsafe_target_or_malformed_row_is_rejected():
    with pytest.raises(ValueError):
        flow.write(None, 'users', ['id'], [[1]], 'token')
    with pytest.raises(ValueError):
        flow.write(None, 'flow_records', ['exporter_ip'], [[]], 'token')
