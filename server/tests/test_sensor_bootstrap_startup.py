"""Execute the generated bootstrap control flow using harmless command doubles."""
import os
import shutil
import subprocess

import pytest
import yaml

from app.api.v1.sensors import _bootstrap_cloud_init


@pytest.mark.parametrize('failure', ['', 'update-ca-certificates', 'sshd', 'systemctl'])
def test_bootstrap_stops_before_sensor_start_on_setup_failure(tmp_path, failure):
    shell = shutil.which('sh')
    if not shell or os.name == 'nt':
        pytest.skip('POSIX shell fixture requires Linux')
    config = yaml.safe_load(_bootstrap_cloud_init(
        'https://controller.test', 'fixture-only-token', 'fixture',
        ca_pem='-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----',
    ))
    log = tmp_path / 'commands.log'
    for name in ['install', 'sshd', 'systemctl', 'update-ca-certificates']:
        command = tmp_path / name
        command.write_text('#!/bin/sh\n'
                           f'printf "%s\\n" "{name} $*" >> "$COMMAND_LOG"\n'
                           f'[ "$FAIL_COMMAND" != "{name}" ]\n')
        command.chmod(0o755)
    script = config['runcmd'][0].replace('/usr/sbin/sshd', 'sshd')
    result = subprocess.run([shell, '-c', script], env={
        **os.environ, 'PATH': str(tmp_path), 'COMMAND_LOG': str(log),
        'FAIL_COMMAND': failure,
    }, text=True, capture_output=True)
    commands = log.read_text().splitlines()
    if failure:
        assert result.returncode != 0
        assert not any('enable --now zenplus-sensor.service' in line for line in commands)
    else:
        assert result.returncode == 0, result.stderr
        assert commands == ['update-ca-certificates ', 'install -d -m 0755 /run/sshd',
                            'sshd -t', 'systemctl reload-or-restart ssh',
                            'systemctl enable --now zenplus-sensor.service']
