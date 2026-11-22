"""Calendar scheduling, including the compatibility entry point for pre-1.24.46 installations."""
import configparser
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG = Path('/opt/zenplus/updater/config/agent.conf')
DROPIN = Path('/etc/systemd/system/zenplus-updater.timer.d/90-maintenance-window.conf')
CLOCK = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'


def automatic_install_allowed(config, now=None):
    """Scheduled runs check only unless enabled and inside the local-time window."""
    if not config.auto_update:
        return False
    start, end = config.maintenance_window_start, config.maintenance_window_end
    if not start and not end:
        return True
    if not all(re.fullmatch(CLOCK, value or '') for value in (start, end)):
        return False
    clock = (now or datetime.now().astimezone()).strftime('%H:%M')
    if start == end:
        return True
    return start <= clock < end if start < end else clock >= start or clock < end


def reconcile_calendar():
    """Only a validated local time from the appliance config may reach the calendar."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(CONFIG)
    start = config.get('update', 'maintenance_window_start', fallback='')
    end = config.get('update', 'maintenance_window_end', fallback='')
    enabled = config.getboolean('update', 'auto_update', fallback=True)
    if enabled and start != end and all(re.fullmatch(CLOCK, value) for value in (start, end)):
        body = f'[Timer]\nOnCalendar=*-*-* {start}:00\nAccuracySec=1s\nRandomizedDelaySec=0\n'
        if DROPIN.exists() and DROPIN.read_text() == body:
            return
        DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = DROPIN.with_suffix('.tmp')
        temporary.write_text(body)
        os.replace(temporary, DROPIN)
    elif DROPIN.exists():
        DROPIN.unlink()
    else:
        return
    subprocess.run(['/bin/systemctl', 'daemon-reload'], check=True)
    subprocess.run(['/bin/systemctl', 'try-restart', 'zenplus-updater.timer'], check=True)


def scheduled_run():
    # This root-owned entry point survives a legacy bridge replacing /opt/zenplus.
    sys.path.insert(0, '/opt/zenplus')
    from updater import agent
    from importlib.util import find_spec
    if find_spec('updater.plan') is not None:
        return agent.main()
    cfg = agent.load_config()
    agent.setup_logging(cfg)
    if not cfg.appliance.id or not cfg.appliance.api_key:
        raise RuntimeError('Appliance enrollment required')
    with agent.UpdateLock():
        release = agent.checkin(cfg)
        if not release:
            return 0
        if not automatic_install_allowed(agent.load_config().update):
            print(f"Automatic installation of v{release['version']} deferred by the update settings", flush=True)
            return 0
        current = agent.get_current_version()
        if agent.check_min_version(release, current) or not agent._is_newer(release['version'], current):
            raise RuntimeError('Offered release does not follow the installed version')
        if not agent.run_update(cfg, release) or agent.get_current_version() != release['version']:
            return 1
    os.execv(sys.executable, [sys.executable, '-I', '-B', __file__, 'run'])


if __name__ == '__main__':
    if sys.argv[1:] == ['calendar']:
        reconcile_calendar()
    elif sys.argv[1:] == ['run']:
        raise SystemExit(scheduled_run())
    else:
        raise SystemExit('Expected calendar or run')
