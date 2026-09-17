from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services import metric_service


DEVICE = UUID('a2100c03-4a8f-4e4c-94fb-8ef5ba8ee026')
NOW = datetime.now(timezone.utc)


def client(monkeypatch, responses):
    calls = []
    def query(sql, parameters):
        calls.append(sql)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(result_rows=response)
    monkeypatch.setattr(metric_service, 'get_clickhouse_client', lambda: SimpleNamespace(query=query))
    return calls


def row(fraction, samples):
    return (NOW - timedelta(hours=1), 1.2, 0, 0, 1, 2, fraction, samples)


def test_hourly_rollup_preserves_partial_failures_and_weight(monkeypatch):
    calls = client(monkeypatch, [[row(0.9996, 40000)]])
    result = metric_service.get_device_metrics(DEVICE, NOW - timedelta(days=30), NOW)
    assert result.granularity == '1h'
    point = result.points[0]
    assert point.is_up is True  # Legacy majority flag remains compatible.
    assert point.uptime_pct == pytest.approx(99.96)
    assert point.sample_count == 40000
    assert point.sample_count * (1 - point.uptime_pct / 100) == pytest.approx(16)
    assert 'sum(sample_count) AS samples' in calls[0]
    assert 'GROUP BY timestamp' in calls[0]


@pytest.mark.parametrize('fraction,expected', [(1, 100), (0, 0), (None, None)])
def test_raw_metric_preserves_availability_and_unknown(monkeypatch, fraction, expected):
    client(monkeypatch, [[row(fraction, 1)]])
    result = metric_service.get_device_metrics(DEVICE, NOW - timedelta(hours=1), NOW)
    assert result.points[0].uptime_pct == expected
    assert result.points[0].sample_count == 1
    if fraction is None:
        assert result.points[0].is_up is None


def test_empty_rollup_fallback_retains_counts_and_fraction(monkeypatch):
    calls = client(monkeypatch, [[], [row(0.75, 4)]])
    result = metric_service.get_device_metrics(DEVICE, NOW - timedelta(days=30), NOW)
    assert result.points[0].uptime_pct == 75
    assert result.points[0].sample_count == 4
    assert 'count() AS sample_count' in calls[1]


def test_missing_rollup_fallback_preserves_failures(monkeypatch):
    client(monkeypatch, [Exception('UNKNOWN_TABLE'), [row(0.25, 8)]])
    result = metric_service.get_device_metrics(DEVICE, NOW - timedelta(days=30), NOW)
    assert result.points[0].uptime_pct == 25
    assert result.points[0].sample_count == 8


def test_zero_sample_bucket_is_unknown_not_down(monkeypatch):
    client(monkeypatch, [[row(None, 0)]])
    result = metric_service.get_device_metrics(DEVICE, NOW - timedelta(days=30), NOW)
    assert result.points[0].uptime_pct is None
    assert result.points[0].is_up is None
    assert result.points[0].sample_count == 0
