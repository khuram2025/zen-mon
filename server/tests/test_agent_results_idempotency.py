from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import agents
from app.schemas.agent import AgentResultsBatch


class Result:
    def __init__(self, *, row=None, mapping=None):
        self.row = row
        self.mapping = mapping

    def first(self):
        return self.mapping if self.mapping is not None else self.row

    def mappings(self):
        return self


class BatchLedgerDB:
    def __init__(self):
        self.ledger: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        key = (str(params.get("aid")), str(params.get("bid")))

        if "INSERT INTO agent_host_result_batches" in sql:
            if key in self.ledger:
                return Result(row=None)
            self.ledger[key] = {
                "server_id": params["sid"],
                "payload_sha256": params["payload"],
                "accepted": 0,
                "rejected": 0,
                "errors": [],
                "completed_at": None,
            }
            return Result(row=(1,))
        if "FROM agent_host_result_batches" in sql:
            return Result(mapping=self.ledger.get(key))
        if "UPDATE agent_host_result_batches" in sql:
            row = self.ledger[key]
            row.update({
                "accepted": params["accepted"],
                "rejected": params["rejected"],
                "errors": json.loads(params["errors"]),
                "completed_at": datetime.now(timezone.utc),
            })
            return Result()
        if "UPDATE agents SET last_metric_at" in sql:
            return Result()
        if "UPDATE servers SET last_seen" in sql:
            return Result()
        if "DELETE FROM agent_host_result_batches" in sql:
            return Result()
        raise AssertionError(sql)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def make_batch(agent_id, server_id, *, cpu_pct=12.5, sent_at=None):
    collected_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    return AgentResultsBatch(
        agent_id=str(agent_id),
        server_id=str(server_id),
        batch_id="batch-123",
        sequence_start=41,
        sequence_end=41,
        agent_version="1.12.0",
        collected_at=collected_at,
        sent_at=sent_at or collected_at + timedelta(seconds=5),
        metrics=[{
            "kind": "cpu",
            "timestamp": collected_at,
            "data": {"cpu_total_pct": cpu_pct},
        }],
    )


def make_agent_health_batch(agent_id, server_id):
    batch = make_batch(agent_id, server_id)
    batch.metrics = [batch.metrics[0].model_copy(update={
        "kind": "agent_health",
        "data": {"queue_depth": 1, "spool_bytes": 568},
    })]
    return batch


@pytest.mark.asyncio
async def test_host_results_retry_uses_durable_batch_outcome(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()
    ingest_calls = 0

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        nonlocal ingest_calls
        ingest_calls += 1
        return 1, 0, [], 3

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    batch = make_batch(agent_id, server_id)
    first = await agents.post_results(batch, db, str(agent_id), "Bearer key")
    retry = await agents.post_results(
        make_batch(
            agent_id,
            server_id,
            sent_at=batch.sent_at + timedelta(minutes=1),
        ),
        db,
        str(agent_id),
        "Bearer key",
    )

    assert first.accepted == 1
    assert first.rejected == 0
    assert first.duplicates == 0
    assert retry.accepted == 0
    assert retry.rejected == 0
    assert retry.duplicates == 1
    assert ingest_calls == 1
    assert db.commits == 2
    assert sum("UPDATE agent_host_result_batches" in sql for sql, _ in db.calls) == 1


@pytest.mark.asyncio
async def test_host_results_rejects_batch_id_payload_collision(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()
    ingest_calls = 0

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        nonlocal ingest_calls
        ingest_calls += 1
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    await agents.post_results(
        make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
    )
    with pytest.raises(HTTPException) as exc:
        await agents.post_results(
            make_batch(agent_id, server_id, cpu_pct=99.0),
            db,
            str(agent_id),
            "Bearer key",
        )

    assert exc.value.status_code == 409
    assert "different payload" in exc.value.detail
    assert ingest_calls == 1
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_host_results_prunes_completed_ledger_rows_on_sampled_upload(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)
    monkeypatch.setattr(agents, "_host_results_ledger_cleanup_due", lambda *_: True)

    await agents.post_results(
        make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
    )

    cleanup = next(
        (params for sql, params in db.calls if "DELETE FROM agent_host_result_batches" in sql),
        None,
    )
    assert cleanup == {
        "aid": agent_id,
        "days": agents.HOST_RESULTS_LEDGER_RETENTION_DAYS,
    }


@pytest.mark.asyncio
async def test_host_results_storage_failure_returns_retryable_503(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        raise agents.HostMetricStorageError("cpu")

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    with pytest.raises(HTTPException) as exc:
        await agents.post_results(
            make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
        )

    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "2"}
    assert db.rollbacks == 1
    assert not any(
        "UPDATE agent_host_result_batches" in sql for sql, _params in db.calls
    )


@pytest.mark.asyncio
async def test_agent_health_only_batch_does_not_advance_host_metric_freshness(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    await agents.post_results(
        make_agent_health_batch(agent_id, server_id),
        db,
        str(agent_id),
        "Bearer key",
    )

    metric_update = next(
        params for sql, params in db.calls if "UPDATE agents SET last_metric_at" in sql
    )
    assert metric_update["has_host_telemetry"] is False
    assert not any("UPDATE servers SET last_seen" in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_host_telemetry_batch_advances_server_data_freshness(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    await agents.post_results(
        make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
    )

    server_updates = [
        params for sql, params in db.calls if "UPDATE servers SET last_seen" in sql
    ]
    assert server_updates == [{"id": server_id}]


def test_host_results_uuid_identity_comparison_is_format_insensitive():
    identity = uuid4()
    assert agents._same_uuid(identity, str(identity).upper())
    assert not agents._same_uuid(identity, uuid4())
    assert not agents._same_uuid(identity, "not-a-uuid")


def test_host_results_batch_migration_defines_durable_agent_ledger():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "migrate-088-agent-host-result-batches.sql"
    ).read_text()

    assert "CREATE TABLE IF NOT EXISTS agent_host_result_batches" in migration
    assert "PRIMARY KEY (agent_id, batch_id)" in migration
    assert "payload_sha256" in migration
    assert "completed_at" in migration
    assert "idx_agent_host_result_batches_agent_completed" in migration
    assert "not one atomic transaction" in migration
