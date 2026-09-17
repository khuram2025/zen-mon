"""Validate a staged source tree using pre-provisioned disposable databases.

The fixture uses PostgreSQL on a private Unix socket (15432), ClickHouse on
127.0.0.1:18123, and the dedicated network_fixture user in both engines.
clickhouse-fixture.env is private runtime configuration and must never ship.
"""
from pathlib import Path
import argparse
import os
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-root', required=True, type=Path)
    parser.add_argument('--prepare-databases', action='store_true')
    parser.add_argument('--campaign', action='store_true', help='Run repeated loopback SNMP and syslog lifecycle scenarios')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.fixture_root.resolve()
    if root.parent != Path('/tmp') or not root.name.startswith('zenplus-network-integration-'):
        parser.error('Fixture must be a private /tmp/zenplus-network-integration-* directory')
    if (root/'pgsocket').is_symlink() or not (root/'pgsocket').is_dir():
        parser.error('Private PostgreSQL socket directory is missing or is a symlink')
    fixture = dict(line.split('=', 1) for line in (root/'clickhouse-fixture.env').read_text().splitlines() if '=' in line)
    if fixture.get('CLICKHOUSE_USER') != 'network_fixture' or fixture.get('CLICKHOUSE_DB') != 'zenplus':
        parser.error('ClickHouse fixture must use network_fixture and the isolated zenplus database')
    os.environ.update(DATABASE_URL=f'postgresql+asyncpg://network_fixture@localhost:15432/zenplus?host={root}/pgsocket',
        CLICKHOUSE_HOST='127.0.0.1', CLICKHOUSE_HTTP_PORT='18123', CLICKHOUSE_DB='zenplus',
        CLICKHOUSE_USER='network_fixture', CLICKHOUSE_PASSWORD=fixture['CLICKHOUSE_PASSWORD'],
        ZENPLUS_NETWORK_INTEGRATION='1', ZENPLUS_DIR=str(root), PYTHONPATH=f'{root}/deps:{root}/server',
        # Legacy smoke tests otherwise auto-connect to the installed dev API
        # on port 8000, independently of the isolated database settings.
        ZENPLUS_API='http://127.0.0.1:1')
    sys.path[:0] = [str(root/'deps'), str(root/'server'), str(root)]
    if args.prepare_databases:
        for _ in range(2):
            subprocess.run(['/usr/lib/postgresql/16/bin/psql', '-X', '-v', 'ON_ERROR_STOP=1',
                '-h', str(root/'pgsocket'), '-p', '15432', '-U', 'network_fixture', '-d', 'zenplus',
                '-f', str(root/'scripts/migrate-115-network-events.sql')], check=True)
        from app.core.database import get_clickhouse_client
        client = get_clickhouse_client()
        # These three fixed files contain simple statements without semicolons
        # in string literals. This is not a general-purpose migration parser.
        for filename in ['migrate-004-snmp-clickhouse.sql', 'migrate-116-network-trap-identity-clickhouse.sql',
                         'migrate-116-network-trap-identity-clickhouse.sql']:
            sql = re.sub(r'--[^\n]*', '', (root/'scripts'/filename).read_text())
            for statement in sql.split(';'):
                if statement.strip():
                    client.command(statement)
            print(f'Applied {filename}')
        client.close()
        return 0
    os.chdir(root/'server')
    import pytest
    options = args.pytest_args
    if options and options[0] == '--':
        options = options[1:]
    default = ['tests/integration/test_network_contracts.py', '-q']
    if args.campaign:
        default += ['-k', 'simulated_device or receiver_']
    return pytest.main(options or default)


if __name__ == '__main__':
    raise SystemExit(main())
