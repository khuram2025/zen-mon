from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.service_availability import read_segments, summarize, recovery_start, continuous_up_start


NOW = datetime.now(timezone.utc).replace(microsecond=0)


class CH:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, parameters):
        return SimpleNamespace(result_rows=self.rows)


def test_missing_probes_are_unknown_not_healthy_or_down():
    start = NOW - timedelta(hours=1)
    segments, count = read_segments(CH([]), 'check', start, NOW, 60)
    stats = summarize(segments, start, NOW)
    assert count == 0
    assert stats['uptime_pct'] is None
    assert stats['unknown_sec'] == 3600
    assert stats['incident_count'] == 0


def test_gap_is_not_extrapolated_and_recovery_is_time_weighted():
    start = NOW - timedelta(hours=1)
    rows = [(start, 0, 1), (NOW - timedelta(seconds=60), 1, 1)]
    segments, count = read_segments(CH(rows), 'check', start, NOW, 60)
    stats = summarize(segments, start, NOW)
    assert count == 2
    assert stats['covered_sec'] == 180
    assert stats['total_downtime_sec'] == 120
    assert stats['unknown_sec'] == 3420
    assert stats['uptime_pct'] == pytest.approx(100 / 3)
    assert stats['incident_count'] == 1


def test_partial_window_uses_preceding_probe_without_counting_it():
    start = NOW - timedelta(seconds=30)
    segments, count = read_segments(CH([(start - timedelta(seconds=15), 0, 1),
                                       (start + timedelta(seconds=10), 1, 1)]), 'check', start, NOW, 60)
    stats = summarize(segments, start, NOW)
    assert count == 1
    assert stats['covered_sec'] == 30
    assert stats['total_downtime_sec'] == 10
    assert stats['uptime_pct'] == pytest.approx(200 / 3)


def test_initial_up_does_not_invent_downtime_before_monitoring():
    start = NOW - timedelta(days=7)
    segments, _ = read_segments(CH([(NOW - timedelta(seconds=60), 1, 1)]), 'check', start, NOW, 60)
    stats = summarize(segments, start, NOW)
    assert stats['uptime_pct'] == 100
    assert stats['total_downtime_sec'] == 0
    assert stats['covered_sec'] == 60


def test_hourly_partition_matches_whole_window():
    start = NOW - timedelta(hours=2)
    segments = [(start, start + timedelta(minutes=12), 0),
                (start + timedelta(minutes=12), NOW - timedelta(minutes=20), 1)]
    whole = summarize(segments, start, NOW)
    left = summarize(segments, start, start + timedelta(hours=1))
    right = summarize(segments, start + timedelta(hours=1), NOW)
    assert whole['covered_sec'] == left['covered_sec'] + right['covered_sec']
    assert whole['total_downtime_sec'] == left['total_downtime_sec'] + right['total_downtime_sec']


def test_streak_starts_at_recovery_and_stale_up_is_unknown():
    recovery = NOW - timedelta(minutes=3)
    assert recovery_start([(recovery, 'up')], NOW, NOW, 60, 'up') == recovery
    assert recovery_start([(recovery, 'up')], NOW, NOW - timedelta(minutes=10), 60, 'up') is None
    assert recovery_start([(recovery, 'up')], NOW, NOW, 60, 'unknown') is None
    assert recovery_start([], NOW, NOW, 60, 'up') is None


def test_long_window_rollup_uses_sample_weighted_fraction_and_bounded_coverage():
    start = NOW - timedelta(days=45)
    segments, count = read_segments(CH([(start, .25, 2)]), 'check', start, start + timedelta(hours=1), 60)
    stats = summarize(segments, start, start + timedelta(hours=1))
    assert count == 2
    assert stats['covered_sec'] == 120
    assert stats['uptime_pct'] == 25
    assert stats['total_downtime_sec'] == 90


def test_healthy_streak_restarts_after_missing_results():
    start = NOW - timedelta(hours=1)
    recovery = NOW - timedelta(minutes=3)
    assert continuous_up_start([(start, start + timedelta(minutes=2), 1), (recovery, NOW, 1)], NOW) == recovery
    assert continuous_up_start([(start, NOW - timedelta(minutes=5), 1)], NOW) is None
    assert continuous_up_start([(start, NOW, 0)], NOW) is None
