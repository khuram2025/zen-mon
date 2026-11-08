"""Large configuration comparisons stay bounded and off the event loop.

Field report (production, 2026-11-08): fetching the normalized diff of a large
FortiGate configuration (~1,000 changed lines of a multi-MB file) ran
difflib synchronously inside the request handler for minutes, and the API
worker stopped answering /api/v1/system/health for about two minutes. These
tests pin that large comparisons finish quickly, that one too large to show
returns a clear result instead of running on, and that a comparison never
blocks a concurrent request.
"""
from __future__ import annotations

import asyncio
import difflib
import random
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import ncm
from app.services import ncm_content
from app.services.ncm_content import DIFF_TOO_LARGE_MESSAGE, config_diff, config_diff_async


def fortigate(policies: int = 8000, renumber: bool = False, changed: frozenset = frozenset()) -> str:
    """A synthetic FortiGate configuration: ~15 lines per policy, 3 MB at 8,000."""
    rnd = random.Random(7)
    out = ['#config-version=FGT1800F-7.2.8-FW-build1639:opmode=0:vdom=0', 'config firewall address']
    for i in range(policies):
        out += [f'    edit "host-{i}"', f'        set subnet 10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256} 255.255.255.255',
                '    next']
    out += ['end', 'config firewall policy']
    for i in range(policies):
        out += [f'    edit {(90000 if renumber else 0) + i + 1}',
                f'        set name "pol-{i}"' + (' # reviewed' if i in changed else ''),
                '        set srcintf "port1"', '        set dstintf "port2"', '        set action accept',
                f'        set srcaddr "host-{i}"', '        set dstaddr "all"', '        set schedule "always"',
                '        set service "ALL"', '        set logtraffic all',
                f'        set psksecret ENC {rnd.getrandbits(128):x}', '    next']
    out.append('end')
    return '\n'.join(out)


SCATTERED = frozenset(random.Random(1).sample(range(8000), 1000))


def _reconstructs(a: list[str], b: list[str]) -> None:
    blocks = ncm_content._matching_blocks(a, b, ncm_content._Deadline(60))
    rebuilt = []
    for tag, i1, i2, j1, j2 in ncm_content._KnownBlocks(a, b, blocks).get_opcodes():
        if tag == 'equal':
            assert a[i1:i2] == b[j1:j2]
            rebuilt += a[i1:i2]
        else:
            rebuilt += b[j1:j2]
    assert rebuilt == b


def test_thousand_scattered_changes_in_multi_megabyte_config_are_fast_and_exact():
    before, after = fortigate(), fortigate(changed=SCATTERED)
    assert len(before) > 3_000_000
    started = time.monotonic()
    result = config_diff(before, after)
    elapsed = time.monotonic() - started
    assert elapsed < 8, f'diff took {elapsed:.1f}s'
    assert not result['too_large'] and not result['identical']
    assert (result['added'], result['removed']) == (1000, 1000)
    assert result['diff'].startswith('--- before\n+++ after\n@@ ')
    assert result['diff'].count('\n+        set name "pol-') == 1000
    # Secrets stay redacted in the display diff.
    assert 'psksecret' not in result['diff'] and '<sensitive command redacted>' in result['diff']


def test_policy_renumber_counts_quickly_and_reports_too_large_to_show():
    # Every policy id changes: 8,000 interleaved one-line changes. difflib took
    # 36 s per pass on this input; the display diff would be 72,000 lines.
    started = time.monotonic()
    result = config_diff(fortigate(), fortigate(renumber=True))
    elapsed = time.monotonic() - started
    assert elapsed < 8, f'diff took {elapsed:.1f}s'
    assert (result['added'], result['removed']) == (8000, 8000)
    assert result['too_large'] and result['diff'] == ''
    assert result['message'] == DIFF_TOO_LARGE_MESSAGE
    assert 'Download both versions' in result['message']


def test_time_budget_stops_counting_and_reports_too_large():
    result = config_diff(fortigate(), fortigate(renumber=True), budget=0)
    assert result['too_large'] and result['diff'] == ''
    assert result['added'] is None and result['removed'] is None
    assert not result['identical']


def test_display_line_cap_keeps_counts():
    result = config_diff(fortigate(200), fortigate(200, changed=frozenset(range(0, 200, 2))), max_display_lines=50)
    assert result['too_large'] and result['diff'] == ''
    assert (result['added'], result['removed']) == (100, 100)


def test_identical_and_counting_only_comparisons():
    same = config_diff(fortigate(50), fortigate(50))
    assert same['identical'] and not same['too_large'] and (same['added'], same['removed'], same['diff']) == (0, 0, '')
    summary = config_diff(fortigate(), fortigate(changed=SCATTERED), display=False)
    assert (summary['added'], summary['removed'], summary['diff']) == (1000, 1000, '')


def test_small_inputs_produce_exactly_the_difflib_output():
    before = fortigate(60)
    after = fortigate(60, changed=frozenset({3, 17, 40})).replace('host-9"', 'host-9-new"')
    a, b = ncm_content.normalize_config(before), ncm_content.normalize_config(after)
    for raw, (da, db) in [(True, (a, b)), (False, (ncm_content.redact_config(a), ncm_content.redact_config(b)))]:
        expected = '\n'.join(difflib.unified_diff(da.splitlines(), db.splitlines(), fromfile='before',
                                                  tofile='after', lineterm=''))
        assert config_diff(before, after, raw=raw)['diff'] == expected


def test_large_matcher_output_is_always_a_valid_diff():
    a = fortigate().splitlines()
    shapes = {
        'scatter': fortigate(changed=SCATTERED).splitlines(),
        'renumber': fortigate(renumber=True).splitlines(),
        'moved half': a[len(a) // 2:] + a[:len(a) // 2],
        'block rewrite': a[:30000] + [line.replace('host-', 'hst-') for line in a[30000:50000]] + a[50000:],
        'inserted and deleted': a[:1000] + ['    set comments "new"'] * 3000 + a[1000:90000],
        'repeated lines only': ['    next'] * 3000,
    }
    for name, b in shapes.items():
        started = time.monotonic()
        _reconstructs(a, b)
        assert time.monotonic() - started < 5, name
    _reconstructs(['    next'] * 3000, ['    next', '    set x'] * 1500)


def test_diff_runs_off_the_event_loop(monkeypatch):
    def slow_diff(before, after, **options):  # stand-in for a CPU-bound comparison
        time.sleep(1.0)
        return {'diff': '', 'options': options}

    monkeypatch.setattr(ncm_content, 'config_diff', slow_diff)

    async def scenario():
        gaps, last = [], time.monotonic()
        task = asyncio.create_task(config_diff_async('a', 'b', raw=True))
        while not task.done():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
        return await task, max(gaps)

    result, worst_gap = asyncio.run(scenario())
    assert result['options'] == {'raw': True}
    assert worst_gap < 0.25, f'event loop stalled for {worst_gap:.2f}s'


def test_real_large_diff_keeps_event_loop_responsive():
    before, after = fortigate(), fortigate(renumber=True)

    async def scenario():
        gaps, last = [], time.monotonic()
        task = asyncio.create_task(config_diff_async(before, after))
        while not task.done():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
        return await task, max(gaps)

    result, worst_gap = asyncio.run(scenario())
    assert result['added'] == 8000
    # Single regular-expression passes over the whole file hold the GIL briefly;
    # the old synchronous diff stalled the loop for over a minute on this input.
    assert worst_gap < 1.0, f'event loop stalled for {worst_gap:.2f}s'


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, rows):
        self.rows = {str(r.id): r for r in rows}

    async def execute(self, statement, params=None):
        params = params or {}
        if 'v' in params:
            return _Rows([self.rows[str(params['v'])]])
        return _Rows(list(self.rows.values()))

    async def commit(self):
        pass


def _version(content):
    return NS(id=uuid.uuid4(), content=content, content_enc=None, content_hash=None,
              captured_at=datetime.now(timezone.utc), config_type='running', platform='fortinet')


@pytest.fixture
def slow_routes(monkeypatch):
    calls = []

    def slow_diff(before, after, **options):
        calls.append(options)
        time.sleep(1.0)
        return {'diff': '@@ -1 +1 @@', 'added': 1, 'removed': 1, 'identical': False, 'too_large': False}

    monkeypatch.setattr(ncm_content, 'config_diff', slow_diff)
    monkeypatch.setattr(ncm, '_device_access', AsyncMock())
    monkeypatch.setattr(ncm, '_bulk_access', AsyncMock())
    monkeypatch.setattr(ncm.ncm_events, 'audit', AsyncMock())
    return calls


async def _health_while(request):
    """Start ``request``, then time a concurrent cheap request while it runs."""
    started = time.monotonic()
    task = asyncio.create_task(request)
    await asyncio.sleep(0.1)  # the concurrent request: a blocked loop delays it by the whole diff
    health = time.monotonic() - started
    assert not task.done()
    return await task, health


def test_configs_diff_endpoint_does_not_block_concurrent_requests(slow_routes):
    a, b = _version('hostname a'), _version('hostname b')
    device = uuid.uuid4()
    request = ncm.diff_configs(device, a.id, b.id, False, True, _FakeDb([a, b]), NS(id=uuid.uuid4()))
    result, health = asyncio.run(_health_while(request))
    assert result['added'] == 1 and health < 0.5
    assert slow_routes == [{'raw': False, 'normalized': True}]


def test_cross_device_compare_does_not_block_concurrent_requests(slow_routes):
    a, b = _version('hostname a'), _version('hostname b')
    request = ncm.compare_versions(uuid.uuid4(), a.id, uuid.uuid4(), b.id, False, True,
                                   _FakeDb([a, b]), NS(id=uuid.uuid4()))
    result, health = asyncio.run(_health_while(request))
    assert result['added'] == 1 and result['warnings'] == [] and health < 0.5
