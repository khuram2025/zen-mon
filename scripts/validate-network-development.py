"""Run isolated A/B/C contracts. No appliance login, discovery scan or external notification."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

TESTS = [
    'test_discovery_contracts.py', 'test_discovery_import_contract.py',
    'test_network_condition_contract.py', 'test_network_rule_validation.py',
    'test_network_worker_contract.py', 'test_network_development_validation.py',
    'test_network_trap_contract.py', 'test_mib_compiler.py', 'test_syslog_contract.py',
    'test_syslog_receiver_lifecycle.py', 'test_updater_dependency_permissions.py',
    'test_alert_hold_time.py', 'test_alert_rule_preview.py', 'test_alert_rule_templates.py',
    'test_alert_reset_follows_trigger.py', 'test_alert_tag_scope.py',
    'test_sensor_snmp.py', 'test_sensor_fabric_contract.py', 'test_service_availability.py',
    'test_migrations_lint.py',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go', default=shutil.which('go'))
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--output', type=Path, default=Path('output/network-development-validation'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'ZENPLUS_DIR': str(root)}
    results = []
    def run(name, command, cwd):
        if not command[0]:
            results.append(dict(name=name, status='not_run', reason='Required runtime not found'))
            return
        started = time.monotonic()
        print(f'Running {name}...', flush=True)
        with (out / f'{name}.log').open('w', encoding='utf-8') as log:
            try:
                result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=600, check=False)
                code = result.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.write(f'Runner failed: {type(exc).__name__}: {exc}\n'); code = -1
        results.append(dict(name=name, status='passed' if code == 0 else 'failed', exit_code=code,
                            seconds=round(time.monotonic()-started, 2), log=f'{name}.log'))
        print(f'{name}: {results[-1]["status"]}', flush=True)
    run('python-contracts', [sys.executable, '-m', 'pytest', *['tests/'+t for t in TESTS],
                            '-q', '--disable-warnings', '--tb=short', f'--junitxml={out / "python-contracts.xml"}'], root/'server')
    run('go-contracts', [args.go, 'test', '-count=1', './internal/checker/snmp', './internal/store',
                        './internal/pinger', './internal/sensorspool'], root/'poller')
    run('editor-contracts', [args.node, 'scripts/test-network-contracts.mjs'], root/'dashboard')
    run('dashboard-bundle', [args.node, 'node_modules/vite/bin/vite.js', 'build'], root/'dashboard')
    summary = dict(generated_at=datetime.now(timezone.utc).isoformat(), results=results,
                   scope='Synthetic contracts and loopback UDP; database interactions are test doubles. No hardware certification.')
    (out/'results.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return 0 if all(r['status'] == 'passed' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
