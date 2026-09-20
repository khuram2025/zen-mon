from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.service_availability import (
    daily_summaries, read_confirmed_segments, read_fleet_segments, read_segments, summarize,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Evidence:
    def __init__(self, probes, events, rollups=None):
        self.probes, self.events, self.rollups = probes, events, rollups or {}
        self.calls = []

    def query(self, sql, parameters):
        self.calls.append(sql)
        fleet = 'ids' in parameters
        ids = parameters['ids'] if fleet else [parameters['id']]
        start = datetime.fromisoformat(parameters['f']).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(parameters['t']).replace(tzinfo=timezone.utc)
        rows = []
        for cid in ids:
            source = (self.events if 'service_status_log' in sql else
                      self.rollups if 'service_metrics_5m' in sql else self.probes)
            prior = 'timestamp < %(f)s' in sql
            selected = [row for row in source.get(cid, [])
                        if (row[0] < start if prior else start <= row[0] < end)]
            if prior:
                selected = selected[-1:]
            rows.extend([(cid, *row) if fleet else row for row in selected])
        return SimpleNamespace(result_rows=rows)


@pytest.mark.parametrize('age', [1, 45])
def test_fleet_matches_detail_for_confirmations_gaps_and_creation(age):
    start = NOW - timedelta(days=age)
    end = start + timedelta(minutes=10)
    probes = {
        'glitch': [(start + timedelta(seconds=i * 60), int(i != 2), 1) for i in range(10)],
        'incident': [(start + timedelta(seconds=i * 60), int(i not in (2, 3, 4)), 1) for i in range(10)],
        'gap': [(start, 1, 1), (end - timedelta(seconds=60), 1, 1)],
        'failed_only': [(start, 0, 1)],
    }
    rollups = {cid: [(start, .8 if cid == 'glitch' else 0, 5)] for cid in probes}
    events = {
        'glitch': [(start - timedelta(hours=1), 'up')],
        'incident': [(start - timedelta(hours=1), 'up'), (start + timedelta(seconds=180), 'down'),
                     (start + timedelta(seconds=300), 'up')],
    }
    checks = {cid: (None, 60) for cid in [*probes, 'empty']}
    checks['incident'] = (start + timedelta(seconds=30), 60)
    ch = Evidence(probes, events, rollups)
    fleet = list(read_fleet_segments(ch, checks, start, end))
    assert len(ch.calls) == 3  # One probe scan and two state scans, independent of fleet size.
    for cid, segments, count, observed_start in fleet:
        observations, detail_count = read_segments(ch, cid, observed_start, end, 60)
        detail = read_confirmed_segments(ch, cid, observed_start, end, observations)
        assert count == detail_count
        assert summarize(segments, observed_start, end) == summarize(detail, observed_start, end)
    by_id = {cid: summarize(segments, start, end) for cid, segments, _, _ in fleet}
    assert by_id['glitch']['uptime_pct'] == 100
    assert by_id['empty']['uptime_pct'] is None
    assert by_id['failed_only']['uptime_pct'] is None
    assert by_id['incident']['total_downtime_sec'] == 120
    if age == 1:
        assert by_id['gap']['unknown_sec'] == 420


def test_empty_fleet_does_not_query():
    ch = Evidence({}, {})
    assert list(read_fleet_segments(ch, {}, NOW - timedelta(days=1), NOW)) == []
    assert ch.calls == []


def test_daily_bars_use_local_midnight_and_preserve_unknown_days():
    start = datetime(2026, 9, 17, 20, 59, tzinfo=timezone.utc)
    end = start + timedelta(minutes=2)
    rows = daily_summaries([(start, start + timedelta(minutes=1), 0),
                            (start + timedelta(minutes=1), end, 1)],
                           start, end, ZoneInfo('Asia/Riyadh'))
    assert [(r['date'], r['uptime_pct']) for r in rows] == [('2026-09-17', 0), ('2026-09-18', 100)]
    assert sum(r['covered_sec'] for r in rows) == 120
    assert daily_summaries([], start, end, ZoneInfo('Asia/Riyadh')) == []


def test_daily_bins_keep_elapsed_time_across_dst():
    start = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
    rows = daily_summaries([(start, end, 1)], start, end, ZoneInfo('America/New_York'))
    assert len(rows) == 1
    assert rows[0]['covered_sec'] == 23 * 3600


@pytest.mark.asyncio
async def test_fleet_report_scopes_checks_and_keeps_no_data_unknown(monkeypatch):
    from app.services import service_check_service as service
    from app.services import service_availability
    from app.core import database
    queries = []

    class DB:
        async def execute(self, query):
            queries.append(query)
            return SimpleNamespace(all=lambda: [('visible', None, 60), ('empty', None, 60)])

    def read(ch, checks, start, end):
        assert set(checks) == {'visible', 'empty'}
        yield 'visible', [(start, end, 1)], 1, start
        yield 'empty', [], 0, start

    monkeypatch.setattr(database, 'get_clickhouse_client', lambda: None)
    monkeypatch.setattr(service_availability, 'read_fleet_segments', read)
    result = await service.get_confirmed_uptime_all(DB(), visible_tags=['team-a'])
    assert result['checks'] == {'visible': 100, 'empty': None}
    assert result['availability_basis'] == 'confirmed'
    assert 'WHERE' in str(queries[0])
    assert ['team-a'] in queries[0].compile().params.values()
