"""ClickHouse diagnostics collector.

ClickHouse runs in the ``zenplus-clickhouse`` Docker container. We hit the
HTTP interface on 127.0.0.1:8123 with the credentials from environment
variables (same source the API uses), plus tail the container's stdout via
``docker logs`` for any startup errors.

Never queries metric tables for content — only ``system.*`` introspection
queries and table-size summaries.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from urllib import error, request
from urllib.parse import urlencode

from . import CollectorContext, CollectorResult


CONTAINER = "zenplus-clickhouse"
DEFAULT_HOST = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
try:
    DEFAULT_PORT = int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
except ValueError:
    DEFAULT_PORT = 8123
_db_from_env = os.environ.get("CLICKHOUSE_DB", "zenplus")
DEFAULT_DB = _db_from_env if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _db_from_env) else "zenplus"
DEFAULT_USER = os.environ.get("CLICKHOUSE_USER", "default")
DEFAULT_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")


QUERIES: dict[str, str] = {
    "clickhouse/version.txt": "SELECT version()",
    "clickhouse/ping.txt": "SELECT 1",
    "clickhouse/tables.json": (
        f"SELECT name, engine, total_rows, total_bytes "
        f"FROM system.tables WHERE database = '{DEFAULT_DB}' "
        f"ORDER BY total_bytes DESC LIMIT 200 FORMAT JSON"
    ),
    "clickhouse/parts-sizes.json": (
        f"SELECT table, sum(bytes_on_disk) AS bytes, count() AS parts "
        f"FROM system.parts WHERE database = '{DEFAULT_DB}' AND active "
        f"GROUP BY table ORDER BY bytes DESC FORMAT JSON"
    ),
    "clickhouse/disks.json": (
        "SELECT name, path, free_space, total_space "
        "FROM system.disks FORMAT JSON"
    ),
    "clickhouse/mutations.json": (
        f"SELECT database, table, mutation_id, command, create_time, "
        f"is_done, latest_failed_part, latest_fail_reason "
        f"FROM system.mutations WHERE database = '{DEFAULT_DB}' AND NOT is_done "
        f"ORDER BY create_time DESC LIMIT 50 FORMAT JSON"
    ),
    "clickhouse/replication-queue.json": (
        "SELECT database, table, type, num_tries, last_exception, postpone_reason "
        "FROM system.replication_queue LIMIT 50 FORMAT JSON"
    ),
}

HOST_METRIC_TABLES = (
    "host_cpu_metrics",
    "host_memory_metrics",
    "host_filesystem_metrics",
    "host_disk_io_metrics",
    "host_network_metrics",
    "host_process_metrics",
    "host_service_state",
    "host_event_log_summary",
    "agent_health_metrics",
)

DATA_FRESHNESS_TABLES = (
    "ping_metrics",
    "service_metrics",
    "snmp_metrics",
    "snmp_if_metrics",
    "snmp_traps",
    "flow_records",
    *HOST_METRIC_TABLES,
    "host_network_traffic_samples",
    "host_network_flows",
    "apm_spans",
    "apm_exceptions",
    "apm_service_graph",
    "apm_synthetic_results",
    "apm_ingest_stats",
    "apm_rum_events",
    "apm_profiles",
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="clickhouse")

    for arcname, query in QUERIES.items():
        body, ok = _http_query(query)
        result.files[arcname] = body
        if not ok:
            result.warn(f"{arcname} HTTP query failed")

    targeted = {
        "clickhouse/data-freshness.json": _data_freshness_query(),
        "clickhouse/apm-freshness-by-service.json": _apm_freshness_query(),
        "clickhouse/apm-ingest-quality.json": _apm_ingest_quality_query(),
    }
    for arcname, query in targeted.items():
        body, ok = _http_query(query)
        result.files[arcname] = body
        if not ok:
            result.warn(f"{arcname} telemetry diagnostic failed")

    # Query host tables independently. An older appliance may legitimately be
    # missing one recently introduced table; one absence must not erase CPU,
    # memory, filesystem, and all other freshness evidence from the bundle.
    for table in HOST_METRIC_TABLES:
        arcname = f"clickhouse/host-metric-freshness/{table}.json"
        body, ok = _http_query(_host_freshness_query(table))
        result.files[arcname] = body
        if not ok:
            result.warn(f"{arcname} telemetry diagnostic failed")

    # Container logs for startup / OOM / connection issues. The redactor will
    # mask any incidental secrets that show up in error messages.
    result.files["clickhouse/docker-logs.tail.log"] = _docker_logs_tail(result)
    return result


def _http_query(query: str) -> tuple[bytes, bool]:
    url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/?{urlencode({'database': DEFAULT_DB})}"
    req = request.Request(url, data=query.encode("utf-8"), method="POST")
    if DEFAULT_USER:
        creds = f"{DEFAULT_USER}:{DEFAULT_PASSWORD}".encode("utf-8")
        req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode("ascii"))
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.read(4 * 1024 * 1024), True
    except error.HTTPError as e:
        body = e.read(2000)
        return (b"[clickhouse HTTP error]\n" + body), False
    except Exception as exc:  # noqa: BLE001
        return (f"[clickhouse unreachable: {exc!r}]\n".encode("utf-8")), False


def _data_freshness_query() -> str:
    tables = ", ".join(f"'{table}'" for table in DATA_FRESHNESS_TABLES)
    # system.parts provides a cheap, non-content inventory even when a raw
    # table contains hundreds of millions of rows. ``max_time`` is sufficient
    # to distinguish an empty/stale pipeline from a currently writing one.
    return (
        "SELECT table, sum(rows) AS rows, max(max_time) AS latest, "
        "dateDiff('second', max(max_time), now()) AS lag_seconds "
        "FROM system.parts "
        f"WHERE database = '{DEFAULT_DB}' AND active AND table IN ({tables}) "
        "GROUP BY table ORDER BY table FORMAT JSON"
    )


def _host_freshness_query(table: str) -> str:
    if table not in HOST_METRIC_TABLES:
        raise ValueError(f"unsupported host metric table: {table}")
    return (
        f"SELECT '{table}' AS metric_table, toString(server_id) AS server_id, "
        "toString(agent_id) AS agent_id, max(timestamp) AS latest, "
        "countIf(timestamp >= now() - INTERVAL 1 HOUR) AS rows_1h "
        f"FROM {DEFAULT_DB}.{table} "
        "WHERE timestamp >= now() - INTERVAL 7 DAY "
        "GROUP BY server_id, agent_id ORDER BY server_id FORMAT JSON"
    )


def _apm_freshness_query() -> str:
    return (
        "SELECT service_name, env, "
        "JSONExtractString(resource, 'zenplus.agent.id') AS agent_id, "
        "JSONExtractString(resource, 'zenplus.server.id') AS server_id, "
        "max(timestamp) AS latest, "
        "countIf(timestamp >= now() - INTERVAL 1 HOUR) AS spans_1h, "
        "uniqExactIf(tuple(trace_id, span_id), timestamp >= now() - INTERVAL 1 HOUR) "
        "AS unique_spans_1h "
        f"FROM {DEFAULT_DB}.apm_spans "
        "WHERE timestamp >= now() - INTERVAL 7 DAY "
        "GROUP BY service_name, env, agent_id, server_id "
        "ORDER BY service_name, env, agent_id FORMAT JSON"
    )


def _apm_ingest_quality_query() -> str:
    return (
        "SELECT sum(accepted) AS accepted, sum(rejected) AS rejected, "
        "sum(dropped) AS dropped, sum(skewed) AS skewed, sum(flushes) AS flushes, "
        "max(timestamp) AS latest "
        f"FROM {DEFAULT_DB}.apm_ingest_stats "
        "WHERE timestamp >= now() - INTERVAL 24 HOUR FORMAT JSON"
    )


def _docker_logs_tail(result: CollectorResult) -> bytes:
    sockets = ("/run/docker.sock", "/var/run/docker.sock")
    if not any(os.access(path, os.R_OK | os.W_OK) for path in sockets):
        return (
            b"[container stdout intentionally unavailable to the unprivileged "
            b"support worker; Docker journal and ClickHouse HTTP diagnostics "
            b"were collected instead]\n"
        )
    if not shutil.which("docker"):
        return b"[docker not available]\n"
    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", "300", CONTAINER],
            capture_output=True, timeout=15,
        )
        body = proc.stdout + b"\n" + proc.stderr
        if proc.returncode != 0:
            result.warn(f"docker logs {CONTAINER} exited {proc.returncode}")
        return body
    except subprocess.TimeoutExpired:
        return b"[docker logs timeout]\n"
    except Exception as exc:  # noqa: BLE001
        return (f"[docker logs failed: {exc!r}]\n").encode("utf-8")
