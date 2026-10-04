"""Disposable ClickHouse databases for the NetFlow SQL tests (opt-in).

Set ``NETFLOW_TEST_CLICKHOUSE=http://user:password@host:port`` to a scratch
ClickHouse whose ``zenplus`` database carries NetFlow schema v2 (migrate-137).
``flow_db`` copies the flow tables *and their materialized views* from it into
a fresh ``nftest_<hex>`` database, so rows inserted into ``flow_records`` reach
the rollups exactly as in production, and drops the copy afterwards. Without
the variable every test using it is skipped. Never point it at an appliance.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest

ENV = "NETFLOW_TEST_CLICKHOUSE"
FLOW_TABLES = (
    "flow_records", "flow_conversations_1h", "flow_qos_1h", "flow_interface_5m",
    "flow_exporter_minutes", "flow_traffic_5m",
)
FLOW_VIEWS = (
    "flow_conversations_1h_mv", "flow_qos_1h_mv", "flow_interface_5m_mv",
    "flow_exporter_minutes_mv", "flow_traffic_5m_mv",
)


def scratch_client():
    url = os.environ.get(ENV)
    if not url:
        pytest.skip(f"set {ENV} to run the NetFlow ClickHouse tests")
    import clickhouse_connect

    u = urlsplit(url)
    try:
        client = clickhouse_connect.get_client(
            host=u.hostname, port=u.port or 8123, username=u.username or "default", password=u.password or "",
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"scratch ClickHouse unreachable: {exc}")
    return client


@contextmanager
def flow_db(client):
    """A fresh database holding the v2 flow tables and views; dropped on exit."""
    cols = {r[0] for r in client.query(
        "SELECT name FROM system.columns WHERE database = 'zenplus' AND table = 'flow_records'"
    ).result_rows}
    if "fields_present" not in cols:
        pytest.skip("scratch zenplus database lacks NetFlow schema v2 (migrate-137)")
    db = f"nftest_{secrets.token_hex(4)}"
    client.command(f"CREATE DATABASE {db}")
    try:
        for name in FLOW_TABLES + FLOW_VIEWS:
            ddl = client.query(f"SHOW CREATE TABLE zenplus.{name}").result_rows[0][0]
            client.command(ddl.replace("zenplus.", f"{db}."))
        yield db
    finally:
        client.command(f"DROP DATABASE IF EXISTS {db} SYNC")
