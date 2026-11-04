"""Large paginated FortiOS captures: configurable deadline, progress, completeness.

A FortiGate 201F (FortiOS 7.6.7) streamed more than 1 MB of `show
full-configuration` and was rejected by the fixed 240 s deadline. These tests
drive the real reader (run_command) and validator through a fake channel
that pages like FortiOS ("--More-- " then "\\r<spaces>\\r" once answered) on a
simulated clock, so multi-megabyte captures run in well under a second.
"""
import asyncio
import json
import re

import pytest

from app.services import ncm_collection as collection
from app.services.ncm_collection import CaptureError, CaptureProgress

PROMPT = 'FGT-201F #'


def fortios_config(target_bytes):
    """Balanced FortiOS CLI text of at least ``target_bytes``."""
    out = ['#config-version=FG201F-7.06-FW-build3401:opmode=0:vdom=0', 'config system global',
           '    set hostname "FGT-201F"', '    set timezone "Asia/Riyadh"', 'end']
    n = 0
    size = sum(len(x) + 1 for x in out)
    while size < target_bytes:
        block = ['config firewall address']
        for _ in range(200):
            n += 1
            block += [f'    edit "host-{n}"', f'        set uuid 4a1b{n:08x}-0000-51ee-0000-0000{n:08x}',
                      f'        set subnet 10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256} 255.255.255.255',
                      '        config tagging', '            edit "t"', '                set category "c"',
                      '            next', '        end', '    next']
        block.append('end')
        out += block
        size += sum(len(x) + 1 for x in block)
    return '\n'.join(out) + '\n'


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, n):
        self.value += n


class FortiOSChannel:
    """Pages ``config`` like FortiOS: ``page_lines`` per screen, waits for a
    space at each "--More--", answers with a CR/space erase sequence.
    ``page_delay`` simulated seconds pass before each new page arrives;
    ``truncate_after`` drops the session after that many pages (the device
    reprints its prompt, as a reset console does). ``normalize`` applies
    Netmiko 4.7 read_channel line-feed normalization (lone CR becomes LF)."""

    def __init__(self, config, clock, *, page_lines=60, chunk=16384, page_delay=0.01,
                 truncate_after=None, read_cost=0.0005, on_read=None, normalize=True):
        lines = config.replace('\n', '\r\n').split('\r\n')
        self.pages = ['\r\n'.join(lines[i:i + page_lines]) for i in range(0, len(lines), page_lines)]
        self.clock = clock
        self.chunk = chunk
        self.page_delay = page_delay
        self.truncate_after = truncate_after
        self.read_cost = read_cost
        self.on_read = on_read
        self.normalize = normalize
        self.buffer = ''
        self.page = None
        self.waiting = False
        self.ready_at = 0.0
        self.sent = []
        self.spaces = 0

    def clear_buffer(self):
        self.buffer = ''

    def _queue_page(self):
        if self.truncate_after is not None and self.page >= self.truncate_after:
            self.buffer += '\r\n' + PROMPT + ' '
            self.page = len(self.pages)
            return
        text = self.pages[self.page]
        self.page += 1
        if self.page < len(self.pages):
            self.buffer += text + '\r\n--More-- '
            self.waiting = True
        else:
            self.buffer += text + '\r\n' + PROMPT + ' '
        self.ready_at = self.clock.value + self.page_delay

    def write_channel(self, value):
        self.sent.append(value)
        if value == 'show full-configuration\n':
            self.buffer += 'show full-configuration\r\n'
            self.page = 0
            self._queue_page()
        elif value == ' ' and self.waiting:
            self.spaces += 1
            self.waiting = False
            self.buffer += '\r         \r'
            self._queue_page()

    def read_channel(self):
        self.clock.value += self.read_cost
        if self.on_read:
            self.on_read()
        if self.clock.value < self.ready_at or not self.buffer:
            return ''
        data, self.buffer = self.buffer[:self.chunk], self.buffer[self.chunk:]
        if self.normalize:
            # BaseConnection.read_channel: never end a block on CR, then
            # normalize_linefeeds() with RESPONSE_RETURN '\n'.
            while data.endswith('\r') and self.buffer:
                data, self.buffer = data + self.buffer[0], self.buffer[1:]
            data = re.sub('(\r\r\r\n|\r\r\n|\r\n|\n\r)', '\n', data).replace('\r', '\n')
        return data


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(collection, 'time', c)
    return c


@pytest.mark.parametrize('normalize', [True, False])
def test_multi_megabyte_paginated_fortios_capture_completes_within_deadline(clock, normalize):
    config = fortios_config(4 * 1024 * 1024)
    conn = FortiOSChannel(config, clock, page_lines=60, page_delay=0.05, normalize=normalize)
    assert len(conn.pages) > 1000                                  # many --More-- screens
    progress = CaptureProgress()
    deadline, source = collection.capture_deadline('fortinet')
    assert (deadline, source) == (900, 'driver')
    out = collection.run_command(conn, 'show full-configuration', PROMPT, timeout=deadline,
                                 idle_timeout=collection.DRIVERS['fortinet']['idle_timeout'], progress=progress)
    content = collection.validate_config(out, 'fortinet')           # complete-capture check untouched
    # Byte-exact: no blank lines where FortiOS erased each answered pager.
    assert content.rstrip('\n') == config.rstrip('\n')
    assert '--More--' not in content and PROMPT not in content
    assert conn.spaces == len(conn.pages) - 1                       # every pager answered exactly once
    # Simulated wall time: 50 ms device latency per page plus 20 ms polling,
    # i.e. linear in pages and far inside the FortiOS default.
    assert clock.value < 0.12 * len(conn.pages) < deadline
    final = progress.snapshot()
    assert final['phase'] == 'validating' and final['command'] == 'show full-configuration'
    assert final['bytes'] >= len(config) and final['pages'] == len(conn.pages) - 1
    assert final['lines'] >= config.count('\n') and final['deadline_seconds'] == 900
    assert 'content' not in json.dumps(final) and 'host-1' not in json.dumps(final)


def test_progress_is_reported_while_streaming(clock):
    config = fortios_config(1024 * 1024)
    seen = []
    progress = CaptureProgress()
    conn = FortiOSChannel(config, clock, page_lines=40, on_read=lambda: seen.append(progress.snapshot()))
    collection.run_command(conn, 'show full-configuration', PROMPT, timeout=900, progress=progress)
    capturing = [s for s in seen if s['phase'] == 'capturing']
    assert capturing and capturing[0]['bytes'] < capturing[-1]['bytes'] < len(config) + 64 * 1024
    assert capturing[-1]['pages'] > capturing[0]['pages']
    assert all(s['capture_elapsed_seconds'] is not None for s in capturing)
    elapsed = [s['capture_elapsed_seconds'] for s in capturing]
    assert elapsed == sorted(elapsed)


def test_slow_device_exceeds_configured_deadline_with_counters(clock):
    config = fortios_config(1024 * 1024)
    conn = FortiOSChannel(config, clock, page_lines=40, page_delay=0.5)   # 0.5 s per screen
    progress = CaptureProgress()
    with pytest.raises(CaptureError, match=r'deadline exceeded \(60 s, .*pages received\)') as exc:
        collection.run_command(conn, 'show full-configuration', PROMPT, timeout=60, progress=progress)
    assert exc.value.category == 'incomplete'
    assert 60 <= clock.value < 61
    assert 0 < progress.snapshot()['pages'] < len(conn.pages) - 1
    # The same device completes when the deadline is raised.
    clock.value = 0
    conn = FortiOSChannel(config, clock, page_lines=40, page_delay=0.5)
    collection.run_command(conn, 'show full-configuration', PROMPT, timeout=900)


def test_truncated_capture_is_still_rejected(clock):
    config = fortios_config(1024 * 1024)
    conn = FortiOSChannel(config, clock, page_lines=60, truncate_after=200)
    out = collection.run_command(conn, 'show full-configuration', PROMPT, timeout=900)
    with pytest.raises(CaptureError, match='Incomplete configuration blocks'):
        collection.validate_config(out, 'fortinet')


def test_stream_that_stops_without_prompt_is_rejected(clock):
    config = fortios_config(256 * 1024)
    conn = FortiOSChannel(config, clock, page_lines=60)
    conn.write_channel = lambda value: conn.sent.append(value) if value == ' ' else FortiOSChannel.write_channel(conn, value)
    with pytest.raises(CaptureError, match='device prompt did not return'):
        collection.run_command(conn, 'show full-configuration', PROMPT, timeout=900, idle_timeout=60)


def test_unpaged_output_mode_standard_capture(clock):
    """`config system console / set output standard` devices never page."""
    config = fortios_config(2 * 1024 * 1024)
    conn = FortiOSChannel(config, clock, page_lines=10 ** 9)
    out = collection.run_command(conn, 'show full-configuration', PROMPT, timeout=900)
    assert collection.validate_config(out, 'fortinet').rstrip('\n') == config.rstrip('\n') and conn.spaces == 0


def test_pager_split_across_reads_is_answered_once(clock):
    class Split:
        sent = []
        chunks = iter(['show full-configuration\r\nconfig system global\r\n    set hostname "x"\r\n--Mo',
                       're-- ', '', '\r         \rend\r\nFGT #'])

        def clear_buffer(self):
            pass

        def write_channel(self, value):
            self.sent.append(value)

        def read_channel(self):
            return next(self.chunks, '')
    conn = Split()
    out = collection.run_command(conn, 'show full-configuration', 'FGT #')
    assert conn.sent.count(' ') == 1
    assert collection.validate_config(out, 'fortinet').endswith('end\n')


@pytest.mark.parametrize('device,profile,platform,expected', [
    (None, None, 'fortinet', (900, 'driver')),
    (None, None, 'cisco_ios', (collection.DEFAULT_CAPTURE_TIMEOUT, 'system')),
    (None, None, 'autodetect', (collection.DEFAULT_CAPTURE_TIMEOUT, 'system')),
    (None, 1200, 'fortinet', (1200, 'profile')),
    (300, 1200, 'fortinet', (300, 'device')),
    (120, None, 'cisco_ios', (120, 'device')),
])
def test_capture_deadline_resolution(device, profile, platform, expected):
    assert collection.capture_deadline(platform, device, profile) == expected


def test_capture_deadline_bounds_in_api_models():
    from pydantic import ValidationError
    from app.api.v1.ncm import CredentialIn, NcmEnroll
    for model, extra in ((NcmEnroll, {}), (CredentialIn, {'name': 'p', 'username': 'u'})):
        assert model(**extra).capture_timeout_seconds is None             # inherit by default
        assert model(**extra, capture_timeout_seconds=900).capture_timeout_seconds == 900
        for bad in (59, 3601, 0):
            with pytest.raises(ValidationError):
                model(**extra, capture_timeout_seconds=bad)
    assert 'capture_timeout_seconds' not in NcmEnroll().model_fields_set  # omitted = keep saved value


def test_platform_catalog_reports_driver_deadline():
    rows = {r['value']: r for r in collection.platform_catalog()}
    assert rows['fortinet']['capture_timeout_default'] == 900
    assert rows['cisco_ios']['capture_timeout_default'] == collection.DEFAULT_CAPTURE_TIMEOUT


def test_fetch_config_passes_resolved_deadline_to_reader(monkeypatch):
    from netmiko.fortinet.fortinet_ssh import FortinetSSH
    seen = {}
    monkeypatch.setattr(FortinetSSH, '__init__', lambda self, **kw: None)
    monkeypatch.setattr(FortinetSSH, 'find_prompt', lambda self: PROMPT)
    monkeypatch.setattr(FortinetSSH, 'disconnect', lambda self: None)

    def reader(conn, command, prompt, *, timeout, idle_timeout, progress):
        seen.update(timeout=timeout, idle=idle_timeout)
        return 'config system global\n set hostname edge\nend\n'
    monkeypatch.setattr(collection, 'run_command', reader)
    collection.fetch_config('192.0.2.1', 'fortinet', 'u', 'p', '', 22)
    assert seen == {'timeout': 900, 'idle': 60}
    progress = CaptureProgress()
    collection.fetch_config('192.0.2.1', 'fortinet', 'u', 'p', '', 22, capture_timeout=1800, progress=progress)
    assert seen['timeout'] == 1800 and progress.snapshot()['deadline_seconds'] == 1800
    assert progress.snapshot()['platform'] == 'fortinet'


def test_job_progress_is_persisted_while_running():
    from app.api.v1 import ncm
    writes = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement, params):
            writes.append((str(statement), json.loads(params['p'])))

        async def commit(self):
            pass

    async def scenario():
        progress = CaptureProgress(900)
        stop = asyncio.Event()
        task = asyncio.create_task(ncm._write_progress(Session, 'job-1', progress, stop, 'running', interval=0.01))
        progress.update(phase='capturing', command='show full-configuration', bytes=1500000, lines=40000, pages=700)
        await asyncio.sleep(0.05)
        progress.update(phase='finished')
        stop.set()
        await task
    asyncio.run(scenario())
    assert len(writes) >= 2 and all('UPDATE ncm_jobs SET progress' in sql and "status='running'" in sql for sql, _ in writes)
    mid = next(p for _, p in writes if p['phase'] == 'capturing')
    assert mid['bytes'] == 1500000 and mid['pages'] == 700 and mid['config_type'] == 'running'
    assert mid['deadline_seconds'] == 900 and 'elapsed_seconds' in mid
    assert writes[-1][1]['phase'] == 'finished'                      # final write after stop
