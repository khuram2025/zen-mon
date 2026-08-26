from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1 import servers
from app.schemas.agent import AgentResultsBatch
from app.services import host_alert_service as host_alerts
from app.services import host_metric_service as metrics
from app.services import server_health_service


class QueryResult:
    def __init__(self, rows):
        self.result_rows = rows


class InsertClient:
    def __init__(self):
        self.calls = []

    def insert(self, table, data, column_names, settings=None):
        self.calls.append((table, data, column_names, settings))


def test_agent_cpu_memory_and_process_fields_map_to_clickhouse_columns():
    client = InsertClient()
    stamp = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    agent_id = str(uuid4())
    server_id = str(uuid4())

    assert metrics._insert_cpu(client, agent_id, server_id, [{
        "timestamp": stamp,
        "cpu_total_pct": 21.5,
        "cpu_idle_pct": 78.5,
        "per_core": [20.0, 23.0],
    }]) == 1
    assert metrics._insert_memory(client, agent_id, server_id, [{
        "timestamp": stamp,
        "total_bytes": 16_000,
        "used_bytes": 10_000,
        "available_bytes": 6_000,
        "used_pct": 62.5,
    }]) == 1
    assert metrics._insert_process(client, agent_id, server_id, [{
        "timestamp": stamp,
        "process_name": "w3wp.exe",
        "pid": 4242,
        "cpu_pct": 7.25,
        "memory_bytes": 512_000,
        "thread_count": 18,
        "handle_count": 220,
        "user_name": "IIS APPPOOL\\DefaultAppPool",
        "cmdline": "w3wp.exe --service",
        "started_at": "2026-08-24T11:00:00Z",
        "state": "running",
        "running": True,
        "watchlisted": True,
    }]) == 1

    cpu, memory, process = client.calls
    assert cpu[0] == "host_cpu_metrics"
    assert cpu[1][0][cpu[2].index("cpu_total_pct")] == 21.5
    assert memory[0] == "host_memory_metrics"
    assert memory[1][0][memory[2].index("used_pct")] == 62.5
    assert process[0] == "host_process_metrics"
    assert process[1][0][process[2].index("process_name")] == "w3wp.exe"
    assert process[1][0][process[2].index("memory_bytes")] == 512_000
    assert process[1][0][process[2].index("cmdline")] == "w3wp.exe --service"
    assert process[1][0][process[2].index("started_at")] == datetime(
        2026, 8, 24, 11, 0, tzinfo=timezone.utc,
    )
    assert process[1][0][process[2].index("state")] == "running"
    assert process[1][0][process[2].index("running")] == 1
    assert process[1][0][process[2].index("watchlisted")] == 1


def test_spool_delay_is_not_misclassified_as_agent_clock_skew():
    received_at = datetime(2026, 8, 25, 12, 0, 2, tzinfo=timezone.utc)
    sent_at = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
    collected_at = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
    rows = [{"timestamp": collected_at}]

    skew = metrics._correct_agent_clock_skew(
        rows, sent_at, "agent-1", received_at=received_at,
    )

    assert skew == -2
    assert rows[0]["timestamp"] == collected_at


def test_agent_clock_skew_correction_preserves_sample_age_and_spacing():
    received_at = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
    # The host clock is ten minutes fast. The samples are also an hour old
    # because they came from the spool; only the ten-minute skew is removed.
    sent_at = received_at + timedelta(minutes=10)
    rows = [
        {"timestamp": received_at - timedelta(minutes=50)},
        {"timestamp": received_at - timedelta(minutes=49)},
    ]

    skew = metrics._correct_agent_clock_skew(
        rows, sent_at, "agent-1", received_at=received_at,
    )

    assert skew == 600
    assert rows[0]["timestamp"] == received_at - timedelta(minutes=60)
    assert rows[1]["timestamp"] - rows[0]["timestamp"] == timedelta(minutes=1)


def test_host_insert_uses_thread_local_clickhouse_session(monkeypatch):
    client = InsertClient()
    monkeypatch.setattr(metrics, "get_ch_client", lambda: client)
    stamp = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    accepted = metrics._insert_metric_groups(
        str(uuid4()), str(uuid4()), "batch-1",
        {"cpu": [{"timestamp": stamp, "cpu_total_pct": 42.0}]},
    )

    assert accepted == 1
    assert client.calls[0][0] == "host_cpu_metrics"
    assert len(client.calls[0][3]["insert_deduplication_token"]) == 64


def test_clickhouse_session_concurrency_error_is_retryable(monkeypatch):
    class ConcurrentSessionClient:
        def insert(self, *_args, **_kwargs):
            raise RuntimeError(
                "Attempt to execute concurrent queries within the same session. "
                "Please use a separate client instance per thread/process."
            )

    monkeypatch.setattr(metrics, "get_ch_client", lambda: ConcurrentSessionClient())
    stamp = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(metrics.HostMetricStorageError) as exc:
        metrics._insert_metric_groups(
            str(uuid4()), str(uuid4()), "batch-1",
            {"cpu": [{"timestamp": stamp, "cpu_total_pct": 42.0}]},
        )

    assert exc.value.part == "cpu"


def test_replayed_metric_kind_uses_the_same_deduplication_token(monkeypatch):
    client = InsertClient()
    monkeypatch.setattr(metrics, "get_ch_client", lambda: client)
    agent_id, server_id = str(uuid4()), str(uuid4())
    rows = {
        "cpu": [{
            "timestamp": datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
            "cpu_total_pct": 42.0,
        }],
    }

    metrics._insert_metric_groups(agent_id, server_id, "batch-1", rows)
    metrics._insert_metric_groups(agent_id, server_id, "batch-1", rows)
    first_token = client.calls[0][3]["insert_deduplication_token"]
    retry_token = client.calls[1][3]["insert_deduplication_token"]

    assert first_token == retry_token
    assert client.calls[0][3]["insert_deduplicate"] == 1


def test_host_telemetry_readiness_is_not_inferred_from_heartbeat():
    ready, missing, partial = str(uuid4()), str(uuid4()), str(uuid4())

    result = servers._host_telemetry_readiness(
        {
            ready: {"cpu_pct": 20.0, "memory_pct": 40.0},
            partial: {"cpu_pct": 10.0},
            # ``missing`` deliberately has no current CPU/memory sample even
            # though it can still have a healthy agent heartbeat in Postgres.
        },
        {ready: 300, missing: 300, partial: 300},
    )

    assert result[ready]["state"] == "ready"
    assert result[ready]["available"] is True
    assert result[missing]["state"] == "missing"
    assert result[missing]["missing_signals"] == ["cpu", "memory"]
    assert result[partial]["state"] == "partial"


def test_agent_apm_status_requires_controller_scoped_credential():
    status = servers._apm_status_with_binding({
        "apm_status": {
            "enabled": True,
            "gateway": {"listening": True, "healthy": True},
        },
        "apm_credential_bound": False,
    })

    assert status["ready"] is False
    assert status["readiness"] == "missing_credential"
    assert "re-enroll" in status["last_error"]

    bound = servers._apm_status_with_binding({
        "apm_status": {
            "enabled": True,
            "gateway": {"listening": True, "healthy": True},
        },
        "apm_credential_bound": True,
    })
    assert bound["ready"] is True
    assert bound["readiness"] == "ready"


@pytest.mark.asyncio
async def test_agent_health_only_batch_cannot_mark_server_healthy(monkeypatch):
    agent_id, server_id = str(uuid4()), str(uuid4())
    stamp = datetime.now(timezone.utc)
    batch = AgentResultsBatch(
        agent_id=agent_id,
        server_id=server_id,
        batch_id="health-only",
        sequence_start=1,
        sequence_end=1,
        agent_version="1.12.0",
        collected_at=stamp,
        sent_at=stamp,
        metrics=[{
            "kind": "agent_health",
            "timestamp": stamp,
            "data": {"queue_depth": 1, "spool_bytes": 568},
        }],
    )
    health_calls = []

    monkeypatch.setattr(metrics, "_insert_metric_groups", lambda *_args: 1)

    async def compute(*_args):
        health_calls.append("compute")
        return "healthy", [], []

    async def store(*_args):
        health_calls.append("store")

    monkeypatch.setattr(server_health_service, "compute_server_health", compute)
    monkeypatch.setattr(server_health_service, "store_server_health", store)

    accepted, rejected, errors, _skew = await metrics.ingest_host_metric_batch(
        agent_id, server_id, batch, object(),
    )

    assert (accepted, rejected, errors) == (1, 0, [])
    assert health_calls == []


def test_latest_metric_queries_are_independent_and_use_current_snapshots(monkeypatch):
    server_id = str(uuid4())

    class Client:
        def __init__(self):
            self.sql = []

        def query(self, sql, parameters):
            self.sql.append(sql)
            if "host_cpu_metrics" in sql:
                raise RuntimeError("CPU table temporarily unavailable")
            if "host_memory_metrics" in sql:
                return QueryResult([(server_id, 61.25)])
            if "host_filesystem_metrics" in sql:
                return QueryResult([(server_id, 74.0)])
            if "host_network_metrics" in sql:
                return QueryResult([(server_id, 2048.0)])
            raise AssertionError(sql)

    client = Client()
    monkeypatch.setattr(metrics, "get_clickhouse_client", lambda: client)

    result = metrics.query_fleet_latest_metrics()

    # A CPU query failure must not erase the other host KPIs.
    assert result[server_id] == {
        "memory_pct": 61.25,
        "disk_max_pct": 74.0,
        "net_bps": 2048.0,
    }
    assert any("argMax(used_pct, timestamp)" in sql for sql in client.sql)
    assert any("argMax(cpu_total_pct, timestamp)" in sql for sql in client.sql)


def test_process_snapshot_can_be_rebuilt_from_clickhouse(monkeypatch):
    stamp = datetime(2026, 8, 24, 12, 0)  # ClickHouse commonly returns naive UTC.
    started = datetime(2026, 8, 24, 11, 0)

    class Client:
        def query(self, sql, parameters):
            assert "GROUP BY pid, process_name" in sql
            assert parameters["limit"] == 400
            return QueryResult([(
                4242, "w3wp.exe", "w3wp.exe --service",
                "IIS APPPOOL\\DefaultAppPool", 8.5, 768_000,
                started, "running", 1, 1, stamp,
            )])

    monkeypatch.setattr(metrics, "get_clickhouse_client", lambda: Client())

    rows = metrics.query_latest_process_snapshot(str(uuid4()))

    assert rows == [{
        "pid": 4242,
        "name": "w3wp.exe",
        "cmdline": "w3wp.exe --service",
        "user_name": "IIS APPPOOL\\DefaultAppPool",
        "cpu_pct": 8.5,
        "memory_bytes": 768_000,
        "started_at": started.replace(tzinfo=timezone.utc),
        "state": "running",
        "running": True,
        "watchlisted": True,
        "updated_at": stamp.replace(tzinfo=timezone.utc),
    }]


class EmptyProcessResult:
    def __init__(self, rows=None, first_row=None):
        self.rows = rows or []
        self.first_row = first_row

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.first_row


class EmptyProcessDB:
    async def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM agents a" in sql:
            return EmptyProcessResult(first_row=(30, 60))
        if "FROM server_process_inventory" in sql:
            return EmptyProcessResult()
        if "FROM server_process_watchlist_inventory" in sql:
            return EmptyProcessResult()
        if "SELECT memory_total_bytes FROM servers" in sql:
            return EmptyProcessResult(first_row=(16_384,))
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_process_api_falls_back_when_postgres_snapshot_is_empty(monkeypatch):
    server_id = uuid4()
    fallback = [{
        "pid": 7,
        "name": "worker.exe",
        "cmdline": None,
        "user_name": None,
        "cpu_pct": 3.0,
        "memory_bytes": 4096,
        "started_at": None,
        "updated_at": datetime.now(timezone.utc),
    }]
    monkeypatch.setattr(
        servers, "query_latest_process_snapshot",
        lambda sid, window_minutes=5: fallback,
    )
    monkeypatch.setattr(
        servers, "query_server_memory_total",
        lambda sid, window_minutes=10: 8192,
    )

    response = await servers.server_processes(
        server_id=server_id,
        db=EmptyProcessDB(),
        user=SimpleNamespace(),
    )

    assert response == {"items": fallback, "mem_total_bytes": 8192}


class WatchlistProcessDB(EmptyProcessDB):
    def __init__(self, updated_at):
        self.updated_at = updated_at

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM server_process_watchlist_inventory" in sql:
            return EmptyProcessResult(rows=[{
                "pid": 0,
                "name": "required-worker.exe",
                "cmdline": None,
                "user_name": None,
                "cpu_pct": 0.0,
                "memory_bytes": 0,
                "started_at": None,
                "state": "not_running",
                "running": False,
                "watchlisted": True,
                "updated_at": self.updated_at,
            }])
        return await super().execute(statement, params)


@pytest.mark.asyncio
async def test_process_api_returns_missing_watchlist_rows(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        servers, "query_latest_process_snapshot",
        lambda sid, window_minutes=5: [{
            "pid": 42, "name": "required-worker.exe", "cmdline": None,
            "user_name": None, "cpu_pct": 1.0, "memory_bytes": 2048,
            "started_at": None, "state": "running", "running": True,
            "watchlisted": True, "updated_at": now,
        }],
    )
    monkeypatch.setattr(
        servers, "query_server_memory_total",
        lambda sid, window_minutes=10: 0,
    )

    response = await servers.server_processes(
        server_id=uuid4(),
        db=WatchlistProcessDB(now),
        user=SimpleNamespace(),
    )

    assert response["mem_total_bytes"] == 16_384
    assert response["items"] == [{
        "pid": 0,
        "name": "required-worker.exe",
        "cmdline": None,
        "user_name": None,
        "cpu_pct": 0.0,
        "memory_bytes": 0,
        "started_at": None,
        "state": "not_running",
        "running": False,
        "watchlisted": True,
        "updated_at": now,
    }]


def test_process_snapshot_keeps_latest_watchlist_state(monkeypatch):
    older = datetime(2026, 8, 24, 11, 59, tzinfo=timezone.utc)
    newer = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    class Client:
        def query(self, sql, parameters):
            return QueryResult([
                (321, "worker.exe", "worker.exe", "svc", 1.0, 1024,
                 older, "running", 1, 1, older),
                (0, "worker.exe", "", "", 0.0, 0,
                 None, "not_running", 0, 1, newer),
            ])

    monkeypatch.setattr(metrics, "get_clickhouse_client", lambda: Client())

    assert metrics.query_latest_process_snapshot(str(uuid4())) == [{
        "pid": 0,
        "name": "worker.exe",
        "cmdline": None,
        "user_name": None,
        "cpu_pct": 0.0,
        "memory_bytes": 0,
        "started_at": None,
        "state": "not_running",
        "running": False,
        "watchlisted": True,
        "updated_at": newer,
    }]


class RecordingDB:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return EmptyProcessResult()


@pytest.mark.asyncio
async def test_inventory_updates_server_hardware_fields():
    db = RecordingDB()
    await metrics._upsert_inventory(str(uuid4()), {
        "hardware": {
            "cpu": {"model": "Example CPU", "logical_count": 16, "physical_count": 8},
            "memory": {"total_physical_bytes": 34_359_738_368},
            "physical_disks": [{
                "index": 0,
                "device_id": r"\\.\PHYSICALDRIVE0",
                "model": "Example SSD",
                "interface_type": "NVMe",
                "media_type": "SSD",
                "size_bytes": 1_000_204_886_016,
                "status": "ok",
            }],
        },
    }, db)

    sql, params = next(
        call for call in db.calls if "cpu_physical_cores" in call[0]
    )
    assert "memory_total_bytes" in sql
    assert params["cpu_model"] == "Example CPU"
    assert params["cpu_cores"] == 16
    assert params["physical_cores"] == 8
    assert params["memory_total"] == 34_359_738_368
    assert '"model": "Example SSD"' in params["physical_disks"]


@pytest.mark.asyncio
async def test_pid_zero_watchlist_process_is_persisted():
    db = RecordingDB()
    await metrics._upsert_process_inventory(str(uuid4()), [{
        "process_name": "required-worker.exe",
        "pid": 0,
        "state": "not_running",
        "running": False,
        "watchlisted": True,
    }], db)

    insert_sql, params = next(
        call for call in db.calls
        if "INSERT INTO server_process_watchlist_inventory" in call[0]
    )
    assert "ON CONFLICT (server_id, name)" in insert_sql
    assert params["name"] == "required-worker.exe"
    assert params["running"] is False
    assert params["watchlisted"] is True
    assert any(
        "DELETE FROM server_process_inventory" in call[0]
        and "lower(name)" in call[0]
        for call in db.calls
    )
    assert all(
        call[1].get("ttl") != 300
        for call in db.calls
        if "make_interval" in call[0]
    )


class ProcessTransitionDB(RecordingDB):
    def __init__(self):
        super().__init__()
        self.running_names = set()
        self.missing_names = set()

    async def execute(self, statement, params=None):
        result = await super().execute(statement, params)
        sql = str(statement)
        values = params or {}
        name = str(values.get("name") or "").casefold()
        if "INSERT INTO server_process_inventory" in sql:
            self.running_names.add(name)
        elif "INSERT INTO server_process_watchlist_inventory" in sql:
            self.missing_names.add(name)
        elif "DELETE FROM server_process_inventory" in sql and "lower(name)" in sql:
            self.running_names.discard(name)
        elif "DELETE FROM server_process_watchlist_inventory" in sql and "lower(name)" in sql:
            self.missing_names.discard(name)
        return result


@pytest.mark.asyncio
async def test_watchlisted_process_transition_replaces_running_row_with_missing_row():
    db = ProcessTransitionDB()
    server_id = str(uuid4())
    await metrics._upsert_process_inventory(server_id, [{
        "process_name": "worker.exe", "pid": 901,
        "running": True, "watchlisted": True, "state": "running",
    }], db)
    assert db.running_names == {"worker.exe"}
    assert db.missing_names == set()

    await metrics._upsert_process_inventory(server_id, [{
        "process_name": "worker.exe", "pid": 0,
        "running": False, "watchlisted": True, "state": "not_running",
    }], db)
    assert db.running_names == set()
    assert db.missing_names == {"worker.exe"}


def test_freshness_covers_slow_collection_and_upload_policy():
    assert metrics.telemetry_freshness_seconds(3600, 3600) == 7260
    assert metrics.telemetry_freshness_seconds(30, 60) == 300


@pytest.mark.asyncio
async def test_process_down_alert_uses_agent_policy_freshness():
    class AlertDB:
        def __init__(self):
            self.ttl = None

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "FROM agents a" in sql:
                return EmptyProcessResult(first_row=(3600, 3600))
            if "FROM server_process_inventory" in sql:
                self.ttl = params["ttl"]
                assert "running = TRUE" in sql
                return EmptyProcessResult(first_row=(1,))
            raise AssertionError(sql)

    db = AlertDB()
    rule = SimpleNamespace(
        metric="host_process_down", target="worker.exe",
        operator="gt", threshold=0,
    )
    result = await host_alerts._current_value(db, rule, str(uuid4()), {}, {})

    assert result == (False, 0.0, "worker.exe running")
    assert db.ttl == 7260


def test_hardware_and_process_enrichment_migrations_cover_both_datastores():
    scripts = __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
    postgres = (scripts / "migrate-089-agent-hardware-process-inventory.sql").read_text()
    clickhouse = (scripts / "migrate-090-agent-process-enrichment-clickhouse.sql").read_text()

    for column in (
        "cpu_model", "cpu_physical_cores", "physical_disks",
        "state", "running", "watchlisted",
    ):
        assert column in postgres
    assert "server_process_watchlist_inventory" in postgres
    for column in ("cmdline", "started_at", "state", "running", "watchlisted"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in clickhouse
