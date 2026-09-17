"""Bounded, observable UDP syslog service (best effort; no durable spool)."""
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal

from app.services.network_events import parse_syslog, ingest_syslog

log = logging.getLogger('zenplus.syslog')


@dataclass(frozen=True)
class Config:
    host: str = '127.0.0.1'
    port: int = 1514
    allowed: tuple = ('127.0.0.0/8', '::1/128')
    queue_size: int = 4096
    retention_days: int = 30
    retention_interval: float = 3600
    processing_timeout: float = 30
    shutdown_timeout: float = 20
    status_interval: float = 10
    status_path: str = '/run/zenplus-syslog/status.json'

    def __post_init__(self):
        ipaddress.ip_address(self.host)
        if not self.allowed:
            raise ValueError('SYSLOG_ALLOWED_NETWORKS must not be empty')
        for network in self.allowed:
            ipaddress.ip_network(network)
        for name, low, high in [('port', 0, 65535), ('queue_size', 1, 65536),
                                ('retention_days', 1, 3650), ('retention_interval', .01, 86400),
                                ('processing_timeout', .01, 120), ('shutdown_timeout', .01, 120),
                                ('status_interval', .01, 3600)]:
            if not low <= getattr(self, name) <= high:
                raise ValueError(f'Invalid syslog setting: {name}')

    @classmethod
    def from_env(cls):
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.getenv('SYSLOG_' + name.upper())
            if raw is None:
                continue
            if name == 'allowed':
                continue
            if name in ('retention_interval', 'processing_timeout', 'shutdown_timeout', 'status_interval'):
                values[name] = float(raw)
            elif name in ('port', 'queue_size', 'retention_days'):
                values[name] = int(raw)
            else:
                values[name] = raw
        values['allowed'] = tuple(n.strip() for n in os.getenv('SYSLOG_ALLOWED_NETWORKS', '127.0.0.0/8,::1/128').split(',') if n.strip())
        if values.get('port', 1514) == 0:
            raise ValueError('SYSLOG_PORT must be 1..65535')
        return cls(**values)


@dataclass
class Counters:
    received: int = 0
    rejected_source: int = 0
    invalid: int = 0
    queue_full: int = 0
    enqueued: int = 0
    processed: int = 0
    processing_failed: int = 0
    shutdown_abandoned: int = 0
    transport_errors: int = 0
    retention_runs: int = 0
    retention_deleted: int = 0
    retention_failed: int = 0
    status_write_failed: int = 0
    queue_high_water: int = 0
    in_flight: int = 0


class Receiver(asyncio.DatagramProtocol):
    def __init__(self, queue, allowed, counters=None):
        self.queue = queue
        self.allowed = [ipaddress.ip_network(n.strip()) for n in allowed if n.strip()]
        self.counters = counters or Counters()

    @property
    def dropped(self):
        c = self.counters
        return c.rejected_source + c.invalid + c.queue_full

    def datagram_received(self, data, addr):
        c = self.counters
        c.received += 1
        try:
            source = ipaddress.ip_address(addr[0])
            if not any(source in network for network in self.allowed):
                c.rejected_source += 1
                return
            event = parse_syslog(data, str(source))
        except ValueError:
            c.invalid += 1
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            c.queue_full += 1
            return
        c.enqueued += 1
        c.queue_high_water = max(c.queue_high_water, self.queue.qsize())

    def error_received(self, exc):
        self.counters.transport_errors += 1


class Service:
    def __init__(self, config, sessions, ingest=ingest_syslog):
        self.config, self.sessions, self.ingest = config, sessions, ingest
        self.queue = asyncio.Queue(maxsize=config.queue_size)
        self.counters = Counters()
        self.ready = asyncio.Event()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.transport = None
        self.running = False

    def snapshot(self):
        return dict(format_version=1, pid=os.getpid(), started_at=self.started_at,
                    updated_at=datetime.now(timezone.utc).isoformat(), running=self.running,
                    queue_depth=self.queue.qsize(), queue_capacity=self.config.queue_size,
                    transport='udp', delivery='best_effort', counters=asdict(self.counters))

    def publish(self):
        data = self.snapshot()
        log.info('syslog status %s', json.dumps(data, sort_keys=True))
        if not self.config.status_path:
            return
        path = Path(self.config.status_path)
        temp = path.with_name(path.name + '.tmp')
        try:
            with temp.open('w', encoding='utf-8') as handle:
                json.dump(data, handle, sort_keys=True)
            os.chmod(temp, 0o640)
            temp.replace(path)
        except OSError:
            self.counters.status_write_failed += 1
            log.error('syslog status file could not be written')

    async def process(self, event):
        async with self.sessions() as db:
            await self.ingest(db, event)

    async def consume(self):
        while True:
            event = await self.queue.get()
            self.counters.in_flight = 1
            try:
                await asyncio.wait_for(self.process(event), self.config.processing_timeout)
                self.counters.processed += 1
            except asyncio.CancelledError:
                # A transaction may already have committed. Do not claim this is a definite loss.
                self.counters.shutdown_abandoned += 1
                raise
            except Exception:
                self.counters.processing_failed += 1
                log.error('syslog processing failed: id=%s; outcome may be partial', event['id'])
            finally:
                self.counters.in_flight = 0
                self.queue.task_done()

    async def retain(self):
        from sqlalchemy import text
        async with self.sessions() as db:
            result = await db.execute(text('DELETE FROM network_events WHERE received_at < now() - make_interval(days => :days)'),
                                      {'days': self.config.retention_days})
            await db.commit()
            self.counters.retention_deleted += max(0, result.rowcount or 0)
            self.counters.retention_runs += 1

    async def maintenance(self, stop):
        # Runs immediately and periodically even when no datagrams arrive.
        while not stop.is_set():
            try:
                await asyncio.wait_for(self.retain(), self.config.processing_timeout)
            except Exception:
                self.counters.retention_failed += 1
                log.error('syslog retention failed')
            try:
                await asyncio.wait_for(stop.wait(), self.config.retention_interval)
            except asyncio.TimeoutError:
                pass

    async def report(self, stop):
        while not stop.is_set():
            self.publish()
            try:
                await asyncio.wait_for(stop.wait(), self.config.status_interval)
            except asyncio.TimeoutError:
                pass

    async def serve(self, stop):
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: Receiver(self.queue, self.config.allowed, self.counters),
            local_addr=(self.config.host, self.config.port))
        self.running = True
        maintenance_stop = asyncio.Event()
        worker = asyncio.create_task(self.consume())
        background = [asyncio.create_task(self.maintenance(maintenance_stop)),
                      asyncio.create_task(self.report(maintenance_stop))]
        self.ready.set()
        try:
            await stop.wait()
        finally:
            self.transport.close()
            maintenance_stop.set()
            try:
                await asyncio.wait_for(self.queue.join(), self.config.shutdown_timeout)
            except asyncio.TimeoutError:
                log.warning('syslog drain deadline reached')
            finally:
                for task in [worker, *background]:
                    task.cancel()
                await asyncio.gather(worker, *background, return_exceptions=True)
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
                    self.counters.shutdown_abandoned += 1
                self.running = False
                self.publish()


async def run():
    from app.core.database import AsyncSessionLocal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            signals.append(sig)
        except NotImplementedError:
            pass
    try:
        await Service(Config.from_env(), AsyncSessionLocal).serve(stop)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
