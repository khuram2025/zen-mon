#!/usr/bin/python3
"""Initialize UTC before identities, certificates, trial state and databases."""
import datetime
import os
import subprocess
import time
from pathlib import Path


def initialize_clock(platform='vmware'):
    env = dict(os.environ, LC_ALL='C', TZ='UTC')
    if platform not in ('vmware', 'kvm', 'hyperv'):
        raise RuntimeError('Unsupported appliance hypervisor profile')
    if platform != 'vmware':
        detected = subprocess.run(['/usr/bin/systemd-detect-virt', '--vm'],
                                  env=env, text=True, capture_output=True, check=True,
                                  timeout=5).stdout.strip()
        expected = {'kvm': ('kvm', 'qemu'), 'hyperv': ('microsoft',)}
        if detected not in expected[platform]:
            raise RuntimeError('Appliance profile does not match the running hypervisor')
    for attempt in range(10):
        try:
            if platform == 'vmware':
                result = subprocess.run(
                    ['/usr/bin/vmware-toolbox-cmd', 'stat', 'hosttime'],
                    env=env, text=True, capture_output=True, check=True, timeout=5,
                )
                host = datetime.datetime.strptime(result.stdout.strip(), '%d %b %Y %H:%M:%S')
                host = host.replace(tzinfo=datetime.timezone.utc)
                if not 2026 <= host.year < 2100:
                    raise ValueError('VMware host clock is outside the supported date range')
                subprocess.run(['/usr/bin/date', '--utc', '--set', '@' + str(int(host.timestamp()))],
                               check=True, capture_output=True, timeout=5)
                source = 'VMware host UTC'
            else:
                # KVM templates explicitly use an UTC RTC. Hyper-V Generation 2
                # supplies UTC RTC and its Linux time-sync integration remains enabled.
                subprocess.run(['/usr/sbin/hwclock', '--hctosys', '--utc', '--noadjfile'],
                               env=env, check=True, capture_output=True, timeout=5)
                epoch = subprocess.run(['/usr/bin/date', '--utc', '+%s'], env=env,
                                       text=True, check=True, capture_output=True,
                                       timeout=5).stdout.strip()
                current = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
                if not 2026 <= current.year < 2100:
                    raise ValueError('Hypervisor RTC is outside the supported date range')
                source = ('QEMU/KVM' if platform == 'kvm' else 'Hyper-V') + ' UTC RTC'
            print('Appliance clock initialized from ' + source)
            return
        except (subprocess.SubprocessError, ValueError):
            if attempt == 9:
                raise RuntimeError('Cannot initialize UTC; check the hypervisor clock and profile') from None
            time.sleep(1)


if __name__ == '__main__':
    profile = Path('/etc/zentryc/hypervisor')
    initialize_clock(profile.read_text().strip() if profile.exists() else 'vmware')
