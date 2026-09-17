import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services.network_events import parse_syslog, ingest_syslog
from app.services.syslog_receiver import Receiver


def test_rfc5424_preserves_structured_data_and_socket_identity():
    event = parse_syslog(b'<165>1 2026-09-10T12:00:00Z spoofed-host app 123 ID47 [meta x="a\\]b"] test', '2001:db8::1')
    assert event['source_ip'] == '2001:db8::1'
    assert (event['facility'], event['severity']) == (20, 5)
    assert event['reported_hostname'] == 'spoofed-host'
    assert event['message'].endswith('test')


def test_rfc3164_and_invalid_priority():
    assert parse_syslog(b'<34>Sep 10 12:00:00 router link down', '192.0.2.1')['message'] == 'link down'
    for data in [b'', b'<192>invalid', b'no priority']:
        with pytest.raises(ValueError): parse_syslog(data, '192.0.2.1')


@pytest.mark.asyncio
async def test_real_loopback_datagram_reaches_local_sink():
    queue = asyncio.Queue(maxsize=1)
    loop = asyncio.get_running_loop()
    transport, receiver = await loop.create_datagram_endpoint(lambda: Receiver(queue, ['127.0.0.0/8']), local_addr=('127.0.0.1', 0))
    sender, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=transport.get_extra_info('sockname'))
    try:
        sender.sendto(b'<34>Sep 10 12:00:00 test link down')
        event = await asyncio.wait_for(queue.get(), 2)
        assert event['message'] == 'link down'
        receiver.datagram_received(b'<34>rejected', ('192.0.2.1', 123))
        assert receiver.dropped == 1
    finally:
        sender.close(); transport.close()


class DB:
    def __init__(self): self.ids = set(); self.alerts = []; self.notified = []
    async def execute(self, sql, params=None):
        sql = str(sql); first = None; rows = []
        if 'FROM devices WHERE' in sql:
            first = dict(id='d', hostname='fixture', group_id=None, tags=[], status='up', device_type='switch', location='')
        elif 'INSERT INTO network_events' in sql:
            if params['id'] not in self.ids: first = (params['id'],); self.ids.add(params['id'])
        elif 'FROM alert_rules' in sql:
            rows = [dict(id='r', name='Critical syslog', device_id=None, group_id=None, scope_tag=None,
                         device_type=None, location=None, target='link', operator='<=', threshold=4,
                         cooldown=60, severity='warning', notify_channels=['sink'], schedule_start=None,
                         schedule_end=None, schedule_days=None)]
        elif 'INSERT INTO alerts' in sql:
            self.alerts.append(params); first = ('a',)
        elif "metadata->>'syslog_source'" in sql:
            first = ('a',) if self.alerts else None
        elif 'UPDATE alerts' in sql: self.notified.append(params)
        return NS(first=lambda: first, mappings=lambda: NS(first=lambda: first, all=lambda: rows))
    async def commit(self): pass


@pytest.mark.asyncio
async def test_ingest_matches_rule_delivers_to_sink_and_replay_is_idempotent(monkeypatch):
    from app.api.v1 import alert_engine
    from app.services import alert_schedule
    monkeypatch.setattr(alert_engine, '_find_suppressing_dependency', AsyncMock(return_value=None))
    monkeypatch.setattr(alert_schedule, 'get_configured_timezone', AsyncMock(return_value='UTC'))
    monkeypatch.setattr(alert_schedule, 'notifications_allowed', lambda *a: True)
    db = DB(); sink = AsyncMock(return_value=1)
    event = parse_syslog(b'<34>Sep 10 12:00:00 bogus link down', '192.0.2.1')
    assert (await ingest_syslog(db, event, sink))['alerts_created'] == 1
    assert (await ingest_syslog(db, event, sink))['replayed'] is True
    assert len(db.alerts) == 1 and sink.await_count == 1
    assert sink.call_args.args[2]['hostname'] == 'fixture'
    assert db.notified
