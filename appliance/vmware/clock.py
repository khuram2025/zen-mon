#!/usr/bin/python3
"""Initialize UTC before identities, certificates, trial state and databases."""
import datetime
import os
import subprocess
import time
from pathlib import Path


def kvm_rtc_epoch():
    # sysfs reads the same virtual UTC RTC without hwclock's one-Hz IRQ wait,
    # which can stall under software emulation or interrupt-poor startup.
    rtc = Path('/sys/class/rtc/rtc0')
    day = (rtc/'date').read_text().strip()
    stamp = (rtc/'time').read_text().strip()
    if (rtc/'date').read_text().strip() != day:
        raise ValueError('RTC date changed while reading; retry')
    value = datetime.datetime.strptime(day + ' ' + stamp, '%Y-%m-%d %H:%M:%S')
    return int(value.replace(tzinfo=datetime.timezone.utc).timestamp())


def hyperv_host_epoch():
    # Linux's Hyper-V Time Synchronization integration exposes host UTC as a PHC.
    # Reading it avoids interpreting a host-local RTC as UTC on Windows hosts.
    for device in Path('/sys/class/ptp').glob('ptp*'):
        if (device/'clock_name').read_text().strip() != 'hyperv':
            continue
        fd = os.open('/dev/' + device.name, os.O_RDONLY | os.O_CLOEXEC)
        try:
            return int(time.clock_gettime(((~fd) << 3) | 3))
        finally:
            os.close(fd)
    raise RuntimeError('Hyper-V host UTC is unavailable; enable Time Synchronization')


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
        if platform == 'hyperv':
            subprocess.run(['/usr/sbin/modprobe', 'hv_utils'], env=env,
                           check=True, capture_output=True, timeout=5)
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
            elif platform == 'kvm':
                # The distributed KVM template explicitly sets an UTC RTC.
                epoch = kvm_rtc_epoch()
                current = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
                if not 2026 <= current.year < 2100:
                    raise ValueError('Hypervisor RTC is outside the supported date range')
                subprocess.run(['/usr/bin/date', '--utc', '--set', '@' + str(epoch)],
                               env=env, check=True, capture_output=True, timeout=5)
                source = 'QEMU/KVM UTC RTC'
            else:
                epoch = hyperv_host_epoch()
                current = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
                if not 2026 <= current.year < 2100:
                    raise ValueError('Hyper-V host clock is outside the supported date range')
                subprocess.run(['/usr/bin/date', '--utc', '--set', '@' + str(epoch)],
                               check=True, capture_output=True, timeout=5)
                source = 'Hyper-V host UTC'
            print('Appliance clock initialized from ' + source)
            return
        except (subprocess.SubprocessError, ValueError, OSError, RuntimeError) as error:
            if attempt == 9:
                raise RuntimeError('Cannot initialize UTC; check the hypervisor clock and profile (' + type(error).__name__ + ')') from None
            time.sleep(1)


if __name__ == '__main__':
    profile = Path('/etc/zentryc/hypervisor')
    initialize_clock(profile.read_text().strip() if profile.exists() else 'vmware')
