import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from app.services.syslog_receiver import Config, Counters, Receiver, Service


@asynccontextmanager
async def sessions():
    class DB:
        async def execute(self, *args): return SimpleNamespace(rowcount=2)
        async def commit(self): pass
    yield DB()


async def wait_until(predicate):
    async def check():
        while not predicate(): await asyncio.sleep(.005)
    await asyncio.wait_for(check(), 2)


@pytest.mark.parametrize('values', [dict(allowed=()), dict(host='bad-host'), dict(port=-1),
    dict(queue_size=0), dict(retention_days=0), dict(shutdown_timeout=999), dict(status_interval=float('nan'))])
def test_invalid_configuration_fails_closed(values):
    with pytest.raises(ValueError): Config(**values)


def test_env_configuration(monkeypatch):
    monkeypatch.setenv('SYSLOG_RETENTION_INTERVAL', '.1')
    monkeypatch.setenv('SYSLOG_ALLOWED_NETWORKS', '192.0.2.0/24, ::1/128')
    cfg=Config.from_env()
    assert cfg.retention_interval==.1 and cfg.allowed==('192.0.2.0/24','::1/128')
    monkeypatch.setenv('SYSLOG_PORT','0')
    with pytest.raises(ValueError): Config.from_env()


def test_overload_rejection_and_malformed_packets_have_distinct_counters():
    q=asyncio.Queue(1); counts=Counters(); r=Receiver(q,['127.0.0.0/8'],counts)
    r.datagram_received(b'<34>first',('127.0.0.1',1))
    r.datagram_received(b'<34>overflow',('127.0.0.1',1))
    r.datagram_received(b'malformed',('127.0.0.1',1))
    r.datagram_received(b'<34>denied',('192.0.2.1',1))
    r.error_received(OSError())
    assert (counts.received,counts.enqueued,counts.queue_full,counts.invalid,counts.rejected_source)==(4,1,1,1,1)
    assert counts.queue_high_water==1 and counts.transport_errors==1 and r.dropped==3


@pytest.mark.asyncio
async def test_idle_retention_and_status_continue_without_packets(tmp_path):
    cfg=Config(port=0,retention_interval=.03,status_interval=.01,status_path=str(tmp_path/'status.json'))
    service=Service(cfg,sessions); stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
    try:
        await asyncio.wait_for(service.ready.wait(),2)
        await wait_until(lambda:service.counters.retention_runs>=2)
        assert service.counters.received==0 and service.counters.retention_deleted>=4
        assert json.loads((tmp_path/'status.json').read_text())['running'] is True
    finally:
        stop.set(); await asyncio.wait_for(task,2)
    assert json.loads((tmp_path/'status.json').read_text())['running'] is False


@pytest.mark.asyncio
async def test_processing_failure_does_not_stop_next_event():
    seen=[]
    async def ingest(db,event):
        seen.append(event['message'])
        if event['message']=='fail': raise RuntimeError('fixture failure')
    service=Service(Config(port=0,status_path=''),sessions,ingest)
    stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
    try:
        await service.ready.wait()
        r=Receiver(service.queue,['127.0.0.0/8'],service.counters)
        for msg in ['fail','ok']: r.datagram_received(('<34>'+msg).encode(),('127.0.0.1',1))
        await asyncio.wait_for(service.queue.join(),2)
        assert seen==['fail','ok'] and service.counters.processing_failed==1 and service.counters.processed==1
    finally:
        stop.set(); await asyncio.wait_for(task,2)


@pytest.mark.asyncio
async def test_drain_deadline_accounts_for_pending_and_inflight_events():
    entered=asyncio.Event()
    async def blocked(db,event):
        entered.set(); await asyncio.Event().wait()
    service=Service(Config(port=0,status_path='',shutdown_timeout=.03),sessions,blocked)
    stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
    await service.ready.wait()
    r=Receiver(service.queue,['127.0.0.0/8'],service.counters)
    for i in range(3): r.datagram_received(b'<34>pending',('127.0.0.1',1))
    await asyncio.wait_for(entered.wait(),2)
    stop.set(); await asyncio.wait_for(task,1)
    assert service.counters.shutdown_abandoned==3
    assert service.counters.in_flight==0 and service.queue.empty()
    await asyncio.wait_for(service.queue.join(),.1)


@pytest.mark.asyncio
async def test_loopback_delivery_drains_then_rebinds_on_restart():
    seen=[]
    async def ingest(db,event): seen.append(event['message'])
    cfg=Config(port=0,status_path='')
    for i in range(2):
        service=Service(cfg,sessions,ingest); stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
        await asyncio.wait_for(service.ready.wait(),2)
        address=service.transport.get_extra_info('sockname')
        sender,_=await asyncio.get_running_loop().create_datagram_endpoint(asyncio.DatagramProtocol,remote_addr=address)
        try:
            sender.sendto(('<34>round'+str(i)).encode())
            await wait_until(lambda:service.counters.received==1)
            stop.set(); await asyncio.wait_for(task,2)
            assert service.counters.processed==1 and service.counters.shutdown_abandoned==0
        finally:
            sender.close(); stop.set(); await task
        cfg=Config(port=address[1],status_path='')
    assert seen==['round0','round1']


@pytest.mark.asyncio
async def test_retention_failure_retries_on_next_interval():
    service=Service(Config(port=0,status_path='',retention_interval=.01),sessions)
    original=service.retain; attempts=0
    async def fail_once():
        nonlocal attempts
        attempts+=1
        if attempts==1: raise RuntimeError('fixture')
        await original()
    service.retain=fail_once
    stop=asyncio.Event(); task=asyncio.create_task(service.serve(stop))
    try:
        await service.ready.wait(); await wait_until(lambda:service.counters.retention_runs>=1)
        assert service.counters.retention_failed==1
    finally:
        stop.set(); await asyncio.wait_for(task,2)
