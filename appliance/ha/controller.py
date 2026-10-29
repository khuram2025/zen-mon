#!/usr/bin/env python3
"""Active controller supervisor. Must run as root from a private configuration.

Never configure against an installed standalone appliance. Commission the
database cell, application dependencies, managed-unit inventory and an external
power fence first. No health-check-only takeover or optional fencing mode.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import time
import urllib.request
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from app.services import controller_authority as authority

LOG = logging.getLogger('zenplus.controller')
RUNTIME = Path('/run/zenplus-controller')
GRANT = RUNTIME/'grant.json'
WITHDRAWN = Path('/etc/zenplus-ha/controller-withdrawn.json')
from controller_units import MANAGED, START, LOCAL
import controller_endpoint
import analytics_health


class LocalDependencyUnavailable(authority.AuthorityUnavailable):
    """The pinned local writer cannot provide its application dependencies."""


def private_root_file(path, *, executable=False):
    p = Path(path)
    st = p.stat()
    if not p.is_absolute() or p.is_symlink() or not p.is_file() or st.st_uid != 0 or st.st_mode & 0o077:
        raise ValueError('Configuration and fencing programs must be root-only regular files')
    if executable and not st.st_mode & 0o100:
        raise ValueError('Fence program must be executable by root')
    return p


def load_config(path):
    value = json.loads(private_root_file(path).read_text())
    required = {'cell_id','node_id','database_url','system_identifier','patroni_url','ca','cert','key','fence_program','dependencies'}
    if set(value)-{'service_address','dependency_failure_seconds'} != required:
        raise ValueError('Controller configuration keys do not match the supported schema')
    for key in ('cell_id','node_id'):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', value[key]):
            raise ValueError('Invalid controller identity')
    from urllib.parse import urlsplit
    url = urlsplit(value['patroni_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.query or url.fragment or url.path not in ('','/'):
        raise ValueError('Patroni requires an authenticated HTTPS origin')
    from sqlalchemy.engine import make_url
    database = make_url(value['database_url'])
    if database.drivername != 'postgresql+asyncpg' or database.host != url.hostname:
        raise ValueError('Database and Patroni must identify the same local node')
    if not re.fullmatch(r'[0-9]{10,30}',value['system_identifier']):
        raise ValueError('Pin the commissioned database system identifier')
    # Fixed executable, no user-supplied shell fragments or positional secrets.
    private_root_file(value['fence_program'], executable=True)
    private_root_file(value['key'])
    if not isinstance(value['dependencies'],list) or not value['dependencies']:
        raise ValueError('Commissioned local dependency units are required')
    if any(not re.fullmatch(r'[a-z][a-z0-9-]*\.service',v) or v in MANAGED for v in value['dependencies']):
        raise ValueError('Invalid local dependency unit')
    delay=value.get('dependency_failure_seconds',120)
    if type(delay) is not int or not 30<=delay<=900:
        raise ValueError('Dependency failure grace must be 30 to 900 seconds')
    return value


async def command(*args, input=None, timeout=8, check=True):
    env = {k:v for k,v in os.environ.items() if k not in ('NOTIFY_SOCKET','WATCHDOG_PID','WATCHDOG_USEC')}
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if input else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input),timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if check and proc.returncode:
        raise RuntimeError('Command failed: '+args[0])  # no credential-bearing stderr
    return proc.returncode, out.decode()


def notify(message):
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:
        raise RuntimeError('Controller must run under its watchdog-enabled systemd unit')
    if address.startswith('@'):address='\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
        sock.connect(address);sock.sendall(message.encode())


async def watchdog():
    while True:
        notify('WATCHDOG=1')
        await asyncio.sleep(2)


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.engine = create_async_engine(config['database_url'], poolclass=NullPool,
                                         connect_args={'command_timeout':4,'timeout':4,
                                            'ssl':ssl.create_default_context(cafile=config['ca'])})
        self.sessions = async_sessionmaker(self.engine)
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.token = str(uuid4())
        self.generation = None
        self.starting = None
        self.running = False
        self.units = []
        self.stop_event = asyncio.Event()
        self.last_reason = None
        self.endpoint = None
        self.endpoint_announced = False
        self.health_failure_since = None
        self.ever_ready = False
        self.dependency_failure_since = None
        self.tls = ssl.create_default_context(cafile=config['ca'])
        self.tls.load_cert_chain(config['cert'], config['key'])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.tls))

    @property
    def owner(self):
        return dict(cell=self.config['cell_id'],node=self.config['node_id'],token=self.token,generation=self.generation)

    async def inventory(self):
        from urllib.parse import urlsplit
        _, addresses = await command('ip','-json','address','show')
        local = {a['local'] for interface in json.loads(addresses) for a in interface.get('addr_info',[])}
        if urlsplit(self.config['patroni_url']).hostname not in local:
            raise RuntimeError('Controller authority must use a local node address')
        if self.config.get('service_address'):
            self.endpoint=controller_endpoint.select(self.config['service_address'],urlsplit(self.config['patroni_url']).hostname,json.loads(addresses))
        patroni=json.loads(Path('/etc/zenplus-ha/patroni.json').read_text())
        if patroni.get('postgresql',{}).get('pre_promote')!='/usr/bin/python3 /usr/local/lib/zenplus-ha/pre_promote.py':
            raise RuntimeError('The commissioned pre-promotion fencing hook is required')
        promotion=json.loads(Path('/etc/zenplus-ha/promotion.json').read_text())
        if promotion.get('dependencies')!=self.config['dependencies']:
            raise RuntimeError('Promotion and controller dependency inventories must match')
        _, out = await command('systemctl','list-unit-files','--no-legend','--no-pager','zenplus*')
        installed = {line.split()[0] for line in out.splitlines() if line.strip()}
        unknown = installed - set(MANAGED) - set(LOCAL)
        if unknown:
            raise RuntimeError('Unclassified ZenPlus units: '+','.join(sorted(unknown)))
        if not {'zenplus-api.service','zenplus-poller.service'} <= installed:
            raise RuntimeError('Application API and poller units are required')
        self.units = [u for u in MANAGED if u in installed]
        for unit in self.units:
            _, out = await command('systemctl','show',unit,'-p','BindsTo','-p','KillMode','-p','ExecStartPre')
            if 'zenplus-ha-controller.service' not in out:
                raise RuntimeError('Unmanaged application unit '+unit)
            if unit.endswith('.service') and ('KillMode=control-group' not in out or '/usr/local/lib/zenplus-ha/admit.py' not in out):
                raise RuntimeError('Application lifecycle guard missing '+unit)
        code,_=await command('systemctl','is-enabled','zenplus-updater.timer',check=False)
        if code==0:
            raise RuntimeError('Cluster-aware OTA is not commissioned; disable the automatic updater timer')

    async def stop(self):
        GRANT.unlink(missing_ok=True)
        await self.withdraw_endpoint()
        if self.starting:
            self.starting.cancel()
            await asyncio.gather(self.starting,return_exceptions=True)
            self.starting=None
        # Timers and paths first, then every triggered service. A unit in failed
        # state can still have children; inspect cgroups via systemd's stop.
        for group in ([u for u in self.units if not u.endswith('.service')],
                      [u for u in self.units if u.endswith('.service')]):
            if group:
                await command('systemctl','stop',*group,timeout=20)
        for unit in self.units:
            _, out=await command('systemctl','show',unit,'-p','ActiveState','-p','ControlGroup','-p','MainPID')
            values=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
            if values.get('ActiveState') not in ('inactive','failed') or values.get('MainPID','0')!='0':
                raise RuntimeError('Could not stop '+unit)
            cg=values.get('ControlGroup')
            if cg:
                for file in (Path('/sys/fs/cgroup')/cg.lstrip('/')).rglob('cgroup.procs'):
                    if file.read_text().strip():
                        raise RuntimeError('Controller child remains alive')
        self.running=False
        self.health_failure_since=None
        self.ever_ready=False

    async def withdraw_endpoint(self):
        if self.endpoint:
            address,interface=self.endpoint
            _,out=await command('ip','-json','address','show','dev',interface)
            if any(a.get('local')==address.split('/')[0] for item in json.loads(out) for a in item.get('addr_info',[])):
                await command('ip','address','del',address,'dev',interface)
        self.endpoint_announced=False

    async def publish_endpoint(self):
        def ready():
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:8000/api/v1/system/health',timeout=2) as response:
                return json.load(response).get('status')=='ok'
        try:
            healthy=await asyncio.to_thread(ready)
            _,out=await command('systemctl','is-active','zenplus-api.service','zenplus-poller.service')
            healthy=healthy and out.splitlines()==['active','active']
        except Exception:
            healthy=False
        if not healthy:
            await self.withdraw_endpoint()
            now=time.monotonic()
            if self.health_failure_since is None:self.health_failure_since=now
            if now-self.health_failure_since>(10 if self.ever_ready else 30):
                raise LocalDependencyUnavailable('Core controller consumers unavailable')
            return
        self.health_failure_since=None
        self.ever_ready=True
        self.dependency_failure_since=None
        if self.endpoint and not self.endpoint_announced:
            address,interface=self.endpoint
            await command('ip','address','replace',address,'dev',interface)
            controller_endpoint.announce(interface,address)
            self.endpoint_announced=True

    async def local_primary(self):
        def check():
            with self.opener.open(self.config['patroni_url'].rstrip('/')+'/primary',timeout=2) as r:
                body=json.load(r)
            if (body.get('role')!='primary' or body.get('state')!='running'
                    or body.get('patroni',{}).get('name')!=self.config['node_id']
                    or str(body.get('database_system_identifier'))!=self.config['system_identifier']):
                raise authority.AuthorityUnavailable('Local database is not the commissioned primary')
        await asyncio.to_thread(check)
    async def eligible(self):
        if WITHDRAWN.exists():
            raise authority.AuthorityUnavailable('Controller is quarantined for operator repair')
        await self.local_primary()
        _, states = await command('systemctl','is-active',*self.config['dependencies'],timeout=3,check=False)
        if states.splitlines() != ['active'] * len(self.config['dependencies']):
            raise LocalDependencyUnavailable('A commissioned local dependency is unavailable')
        if 'zenplus-ha-clickhouse.service' in self.config['dependencies']:
            try:
                await asyncio.to_thread(analytics_health.check,caught_up=self.generation is None)
            except Exception:
                # Quorum loss can affect every member. Withhold admission without
                # quarantining all database voters as locally defective.
                raise authority.AuthorityUnavailable('Analytics admission unavailable') from None
        async with self.sessions() as db:
            row=(await db.execute(text('SELECT pg_is_in_recovery(),system_identifier::text FROM pg_control_system()'))).one()
            if row[0] or row[1]!=self.config['system_identifier']:
                raise authority.AuthorityUnavailable('Database endpoint is not the pinned local writer')

    async def dependency_failure(self):
        """Withdraw a confirmed local writer only after sustained app failure.

        Quarantine survives a power fence or reboot. Patroni performs its normal
        consensus election; the replacement still has to positively fence us.
        A lost database connection or loss of quorum never calls this method.
        """
        now=time.monotonic()
        if self.dependency_failure_since is None:self.dependency_failure_since=now
        if now-self.dependency_failure_since < self.config.get('dependency_failure_seconds',120):
            return False
        await self.local_primary()  # Do not demote from a stale health observation.
        await self.stop()  # Withdraw client ingress and verify consumers stopped.
        value={'cell_id':self.config['cell_id'],'node_id':self.config['node_id'],
               'boot_id':self.boot,'reason':'persistent_local_dependency_failure'}
        temporary=WITHDRAWN.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,WITHDRAWN)
        fd=os.open(WITHDRAWN.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        LOG.error('Persistent dependency failure: controller quarantined; requesting database withdrawal')
        # Stopping Patroni also stops this Requires-bound supervisor. Queue the
        # request so systemd does not wait on a child of the unit it is stopping.
        await command('systemctl','--no-block','stop','zenplus-ha-patroni.service')
        return True

    async def fence(self,node,boot):
        request=str(uuid4())
        _, out=await command(self.config['fence_program'],input=json.dumps({'cell_id':self.config['cell_id'],
            'node_id':node,'boot_id':boot,'request_id':request,'action':'power_off'}).encode(),timeout=45)
        receipt=json.loads(out)
        if receipt.get('request_id')!=request or receipt.get('cell_id')!=self.config['cell_id']:
            raise authority.AuthorityUnavailable('Fence receipt does not match the request')
        return receipt

    def publish(self,started):
        # Count DB/eligibility round-trip time against the conservative local
        # allowance. Never derive a fresh full deadline from a delayed response.
        until=started+6
        if time.clock_gettime(time.CLOCK_BOOTTIME)>=until:
            raise authority.AuthorityUnavailable('Authority operation exceeded admission allowance')
        value={**self.owner,'boot_id':self.boot,'valid_until':until}
        tmp=RUNTIME/'grant.tmp'
        tmp.write_text(json.dumps(value));tmp.chmod(0o644);os.replace(tmp,GRANT)

    async def run(self):
        await self.inventory()
        RUNTIME.mkdir(mode=0o755,exist_ok=True)
        await self.stop()
        notify('READY=1')
        while not self.stop_event.is_set():
            try:
                if self.generation is None:
                    async with self.sessions() as db:
                        self.generation=await authority.acquire(db,cell=self.config['cell_id'],node=self.config['node_id'],
                            boot=self.boot,token=self.token,eligible=self.eligible,fence=self.fence)
                        await db.commit()
                    # Fresh renewal after potentially long fencing, before work.
                start=time.clock_gettime(time.CLOCK_BOOTTIME)
                await self.eligible()
                async with self.sessions() as db:
                    await authority.renew(db,**self.owner)
                    await db.commit()
                self.publish(start)
                self.last_reason=None
                if self.running:
                    await self.publish_endpoint()
                if not self.running:
                    if self.starting is None:
                        self.starting=asyncio.create_task(command('systemctl','start',*[u for u in START if u in self.units],timeout=60))
                    elif self.starting.done():
                        self.starting.result();self.starting=None;self.running=True
                        LOG.info('Controller generation %s active',self.generation)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # No exception strings: DB URLs and fence outputs can contain
                # private data. Operators correlate type with service logs.
                reason=type(error).__name__
                if self.last_reason!=reason:
                    LOG.info('Controller standby: %s',reason)
                    self.last_reason=reason
                if self.generation is not None or self.running or self.starting:
                    await self.stop()
                self.generation=None
                if isinstance(error,LocalDependencyUnavailable):
                    await self.dependency_failure()
            try:
                await asyncio.wait_for(self.stop_event.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
        await self.stop()
        if self.generation:
            async with self.sessions() as db:
                await authority.release(db,**self.owner);await db.commit()
        await self.engine.dispose()


async def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True)
    args=p.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError('Controller supervisor requires root')
    supervisor=Supervisor(load_config(args.config))
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,supervisor.stop_event.set)
    heartbeat=asyncio.create_task(watchdog())
    try:
        await supervisor.run()
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat,return_exceptions=True)
        await supervisor.stop()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
