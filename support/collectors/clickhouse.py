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
import shutil
import subprocess
from urllib import error, request

from . import CollectorContext, CollectorResult


CONTAINER = "zenplus-clickhouse"
DEFAULT_HOST = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
DEFAULT_DB = os.environ.get("CLICKHOUSE_DB", "zenplus")
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


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="clickhouse")

    for arcname, query in QUERIES.items():
        body, ok = _http_query(query)
        result.files[arcname] = body
        if not ok:
            result.warn(f"{arcname} HTTP query failed")

    # Container logs for startup / OOM / connection issues. The redactor will
    # mask any incidental secrets that show up in error messages.
    result.files["clickhouse/docker-logs.tail.log"] = _docker_logs_tail(result)
    return result


def _http_query(query: str) -> tuple[bytes, bool]:
    url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/?database={DEFAULT_DB}"
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


def _docker_logs_tail(result: CollectorResult) -> bytes:
    if not shutil.which("docker"):
        result.warn("docker not in PATH; skipping clickhouse container logs")
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
