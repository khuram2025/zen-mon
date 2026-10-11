#!/usr/bin/python3
"""Read VMware host UTC before databases, certificate creation and trial setup."""
import datetime
import os
import subprocess
import time


def initialize_clock():
    env = dict(os.environ, LC_ALL='C', TZ='UTC')
    for attempt in range(10):
        try:
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
            print('Appliance clock initialized from VMware host UTC')
            return
        except (subprocess.SubprocessError, ValueError):
            if attempt == 9:
                raise RuntimeError('Cannot initialize VMware host UTC; check the hypervisor clock') from None
            time.sleep(1)


if __name__ == '__main__':
    initialize_clock()
