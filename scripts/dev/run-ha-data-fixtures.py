#!/usr/bin/env python3
"""Run opt-in data regressions against private servers inside an empty netns.

Dependencies are preinstalled. Pass a checksum-verified ClickHouse binary.
This runner refuses an ordinary host network; no installed database is used.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clickhouse", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, default=Path("/usr/lib/postgresql/16/bin"))
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run as the non-root builder inside the isolated network namespace")
    links = subprocess.check_output(["ip", "-o", "link", "show"], text=True).splitlines()
    routes = subprocess.check_output(["ip", "route", "show"], text=True).strip()
    if len(links) != 1 or ": lo:" not in links[0] or routes:
        raise SystemExit("Refusing fixtures: network namespace must have only loopback and no routes")
    with tempfile.TemporaryDirectory(prefix="ha-data-") as temporary:
        root = Path(temporary)
        config = root / "clickhouse.xml"
        config.write_text(f"""<clickhouse>
          <logger><level>warning</level><console>1</console></logger>
          <path>{root}/ch/</path><tmp_path>{root}/tmp/</tmp_path>
          <listen_host>127.0.0.1</listen_host><http_port>18123</http_port><tcp_port>19000</tcp_port>
          <max_server_memory_usage>2147483648</max_server_memory_usage>
          <mark_cache_size>67108864</mark_cache_size><uncompressed_cache_size>67108864</uncompressed_cache_size>
          <background_schedule_pool_size>16</background_schedule_pool_size>
          <background_buffer_flush_schedule_pool_size>4</background_buffer_flush_schedule_pool_size>
          <background_message_broker_schedule_pool_size>4</background_message_broker_schedule_pool_size>
          <background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size>
          <profiles><default><max_threads>4</max_threads></default></profiles>
          <users><default><password></password><networks><ip>127.0.0.1</ip></networks>
          <profile>default</profile><quota>default</quota><access_management>1</access_management></default></users>
          <quotas><default><interval><duration>3600</duration><queries>0</queries><errors>0</errors>
          <result_rows>0</result_rows><read_rows>0</read_rows><execution_time>0</execution_time></interval></default></quotas>
        </clickhouse>""")
        log = (root / "fixture.log").open("w+")
        ch = subprocess.Popen([str(args.clickhouse), "server", "--config-file=" + str(config)], stdout=log, stderr=log)
        pg_started = False
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:18123/?query=SELECT%201", timeout=1) as response:
                        if response.read().strip() == b"1":
                            break
                except OSError:
                    pass
                if ch.poll() is not None:
                    raise RuntimeError("scratch ClickHouse exited")
                time.sleep(0.1)
            else:
                raise RuntimeError("scratch ClickHouse did not become ready")
            run(str(args.pg_bin / "initdb"), "-D", str(root / "pg"), "-U", "fixture", "-A", "trust", stdout=log, stderr=log)
            run(str(args.pg_bin / "pg_ctl"), "-D", str(root / "pg"), "-l", str(root / "postgres.log"),
                "-o", f"-p 15432 -h 127.0.0.1 -k {root}", "-w", "start", stdout=log, stderr=log)
            pg_started = True
            run(str(args.pg_bin / "createuser"), "-h", "127.0.0.1", "-p", "15432", "-U", "fixture", "--no-login", "zenplus", stdout=log, stderr=log)
            run(str(args.pg_bin / "createdb"), "-h", "127.0.0.1", "-p", "15432", "-U", "fixture", "udt_fixture", stdout=log, stderr=log)
            env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
            env.update(NETFLOW_TEST_CH_URL="http://default@127.0.0.1:18123",
                       UDT_TEST_DSN="postgresql://fixture@127.0.0.1:15432/udt_fixture",
                       DATABASE_URL="postgresql+asyncpg://fixture@127.0.0.1:1/fixture",
                       REDIS_URL="redis://127.0.0.1:1/15", CLICKHOUSE_HOST="127.0.0.1",
                       CLICKHOUSE_HTTP_PORT="1", ZENPLUS_API="http://127.0.0.1:1")
            print("Private ClickHouse and PostgreSQL ready; loopback-only namespace verified.", flush=True)
            run(sys.executable, "-m", "pytest", "server/tests/test_netflow_rollup.py",
                "server/tests/test_udt_managed_integration.py", "-q", "-rs", "--disable-warnings", env=env)
        except BaseException:
            log.flush()
            log.seek(0)
            print("\n".join(log.read().splitlines()[-30:]), file=sys.stderr)
            raise
        finally:
            if pg_started:
                subprocess.run([str(args.pg_bin / "pg_ctl"), "-D", str(root / "pg"), "-m", "immediate", "-w", "stop"], stdout=log, stderr=log)
            ch.terminate()
            try:
                ch.wait(timeout=15)
            except subprocess.TimeoutExpired:
                ch.kill()
                ch.wait()
            log.close()


if __name__ == "__main__":
    main()
