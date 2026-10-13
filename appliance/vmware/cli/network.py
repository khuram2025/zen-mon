"""Management network candidate, persistent commit and independent rollback."""
import fcntl
import ipaddress
import json
import os
import re
import secrets
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
import yaml
from auth import atomic_private

TARGET = Path('/etc/netplan/50-zentryc.yaml')
STATE = Path('/var/lib/zentryc/network-transaction.json')


def validate(candidate):
    nic = candidate.get('interface', '')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', nic):
        raise ValueError('Select a valid management interface')
    item = {'match': {'name': nic}, 'dhcp4': candidate.get('dhcp') is True, 'optional': True}
    if not item['dhcp4']:
        addr = ipaddress.IPv4Interface(candidate['address'])
        gateway = ipaddress.IPv4Address(candidate['gateway'])
        if gateway not in addr.network or gateway in (addr.network.network_address, addr.network.broadcast_address, addr.ip):
            raise ValueError('Gateway must be a usable different address in the management subnet')
        item.update(addresses=[str(addr)], routes=[{'to': 'default', 'via': str(gateway)}])
    dns = candidate.get('dns', [])
    if dns:
        if not 1 <= len(dns) <= 4:
            raise ValueError('Use one through four DNS servers')
        item['nameservers'] = {'addresses': [str(ipaddress.ip_address(x)) for x in dns]}
    return {'network': {'version': 2, 'renderer': 'networkd', 'ethernets': {'management': item}}}


@contextmanager
def lock():
    fd = os.open('/var/lib/zentryc/network-transaction.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def run(args):
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


def restore(state, boot=False):
    if state['previous'] is None:
        TARGET.unlink(missing_ok=True)
    else:
        TARGET.write_text(state['previous'])
        TARGET.chmod(0o600)
    run(['/usr/sbin/netplan', 'generate' if boot else 'apply'])
    STATE.unlink(missing_ok=True)


def commit(candidate, seconds=120):
    config = validate(candidate)
    with lock():
        if STATE.exists():
            raise ValueError('A network commit is pending; confirm or rollback first')
        # Appliance recipe has one management file. Refuse conflicting old
        # menu/network customizations rather than silently override them.
        if Path('/etc/netplan/90-zentryc-management.yaml').exists():
            raise ValueError('Legacy management override exists; migrate it before using CLI networking')
        tx = {'id': secrets.token_hex(16), 'deadline': time.time() + seconds,
              'previous': TARGET.read_text() if TARGET.exists() else None}
        atomic_private(STATE, tx)
        try:
            # systemd owns the guard outside zentryc-console's cgroup, so killing
            # a console, logging out or restarting it cannot cancel rollback.
            run(['/usr/bin/systemd-run', '--quiet', '--collect', '--unit=zentryc-network-rollback',
                 '/opt/zenplus/venv/bin/python', '/usr/local/libexec/zentryc-cli/network.py', '--guard', tx['id']])
            TARGET.write_text(yaml.safe_dump(config))
            TARGET.chmod(0o600)
            run(['/usr/sbin/netplan', 'apply'])
        except Exception:
            restore(tx)
            raise
    return 'Network applied temporarily. Run commit confirm within 120 seconds; otherwise previous settings return.'


def confirm():
    with lock():
        if not STATE.exists():
            raise ValueError('No network commit is pending')
        state = json.loads(STATE.read_text())
        if time.time() >= state['deadline']:
            restore(state)
            raise ValueError('Confirmation timed out; previous network restored')
        STATE.unlink()
    return 'Network configuration confirmed and saved.'


def rollback():
    with lock():
        if not STATE.exists():
            return 'No network commit is pending.'
        restore(json.loads(STATE.read_text()))
    return 'Previous network configuration restored.'


def guard(identity):
    # A second transaction is blocked until this systemd unit exits; the caller
    # can retry after the original deadline without cancelling another guard.
    while True:
        with lock():
            if not STATE.exists():
                return
            state = json.loads(STATE.read_text())
            if state['id'] != identity:
                return
            if time.time() >= state['deadline']:
                restore(state)
                return
        time.sleep(1)


if __name__ == '__main__':
    import sys
    if os.geteuid() != 0:
        raise SystemExit('Root required')
    if sys.argv[1:] == ['--boot-recovery']:
        with lock():
            if STATE.exists():
                restore(json.loads(STATE.read_text()), boot=True)
    elif len(sys.argv) == 3 and sys.argv[1] == '--guard' and re.fullmatch('[0-9a-f]{32}', sys.argv[2]):
        guard(sys.argv[2])
    else:
        raise SystemExit('Invalid operation')
