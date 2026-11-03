"""The operator update command must use signed OTA and propagate its result."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX operator CLI')

@pytest.mark.parametrize('result', [0, 41])
def test_cli_update_only_invokes_signed_updater_and_propagates_failure(tmp_path, result):
    commands = tmp_path/'commands'
    commands.mkdir()
    log = tmp_path/'calls'
    def stub(name, body):
        path = commands/name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
    stub('id', 'echo 0\n')
    stub('python', f'echo "python $*" >> "{log}"\nexit {result}\n')
    for name in ('git','go','npm','pip'):
        stub(name, f'echo "unsigned {name}" >> "{log}"\nexit 77\n')
    environment = dict(os.environ, PATH=str(commands)+os.pathsep+os.environ['PATH'])
    # Redirect the fixed appliance interpreter and cwd to disposable fixtures.
    script = tmp_path / 'management'
    script.write_text((ROOT/'bin/zenplus-management').read_text().replace(
        '/opt/zenplus/venv/bin/python', str(commands/'python')).replace('cd /opt/zenplus', 'cd ' + str(tmp_path)))
    completed = subprocess.run(['bash', str(script), 'update'],
                               env=environment,capture_output=True,text=True,timeout=10)
    assert completed.returncode == result
    assert log.read_text().splitlines() == ['python -B -m updater --install-latest']
