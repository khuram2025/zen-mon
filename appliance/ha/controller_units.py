"""Render reviewed drop-ins; installation and dependency commissioning are separate."""
from pathlib import Path

MANAGED = (
    'zenplus-api.service', 'zenplus-poller.service', 'zenplus-netflow-collector.service',
    'zenplus-syslog.service', 'zenplus-ncm-worker.service', 'zenplus-ncm-backup.service',
    'zenplus-ncm-delivery.service', 'zenplus-ncm-backup.timer', 'zenplus-ncm-delivery.timer',
    'zenplus-support-queue.path', 'zenplus-support-dispatch.service',
    'zenplus-support-cleanup.service', 'zenplus-support-cleanup.timer',
)
START = ('zenplus-api.service', 'zenplus-poller.service', 'zenplus-netflow-collector.service',
         'zenplus-syslog.service', 'zenplus-ncm-worker.service', 'zenplus-ncm-backup.timer',
         'zenplus-ncm-delivery.timer', 'zenplus-support-queue.path', 'zenplus-support-cleanup.timer')
# Local management, clock, storage-pressure watchdog and infrastructure services
# remain node-local. OTA automation must be disabled for this commissioning mode.
LOCAL = ('zenplus-ha-controller.service', 'zenplus-ha-etcd.service', 'zenplus-ha-patroni.service',
         'zenplus-ha-keeper.service', 'zenplus-ha-clickhouse.service',
         'zenplus-ha-clock.service', 'zenplus-ha-watchdog.service', 'zenplus-storage-watchdog.service',
         'zenplus-management-access.service', 'zenplus-ha-firewall.service', 'zenplus-updater.service',
         'zenplus-updater.timer', 'zenplus-wait-deps.service')


SUPERVISOR='zenplus-ha-controller.service'


def dropin(unit):
    # Keep this inventory literal so rendering does not import a privileged
    # runtime or load any deployment credentials.
    if unit not in MANAGED:
        raise ValueError('Unknown controller consumer')
    value=f'''[Unit]
Requires=
Wants=
After=
BindsTo={SUPERVISOR}
After={SUPERVISOR}
'''
    if unit.endswith('.service'):
        value+='''[Service]
Environment=ZENPLUS_CONTROLLER_GRANT=/run/zenplus-controller/grant.json
Environment=PYTHONPATH=/opt/zenplus/server:/opt/zenplus
ExecStartPre=/opt/zenplus/venv/bin/python /usr/local/lib/zenplus-ha/admit.py
KillMode=control-group
TimeoutStopSec=5
SendSIGKILL=yes
'''
    return value


def write_new(output, units):
    if len(set(units))!=len(units) or any(u not in MANAGED for u in units):
        raise ValueError('Inventory must contain unique known consumers')
    root=Path(output);root.mkdir(exist_ok=False,mode=0o700)
    for unit in units:
        folder=root/(unit+'.d');folder.mkdir(mode=0o755)
        (folder/'50-controller-authority.conf').write_text(dropin(unit))
