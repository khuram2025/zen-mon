from __future__ import annotations

import asyncio
import copy
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
        self._transaction_snapshot = None

    def _begin(self):
        if self._transaction_snapshot is None:
            self._transaction_snapshot = copy.deepcopy(self.ledger)

    async def execute(self, statement, params=None):
        self._begin()
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        key = (str(params.get("aid")), str(params.get("bid")))

        if "INSERT INTO agent_host_result_batches" in sql:
            existing = self.ledger.get(key)
            if existing is not None:
                stale_before = datetime.now(timezone.utc) - timedelta(
                    seconds=params["stale_seconds"],
                )
                if (
                    existing["completed_at"] is None
                    and existing["created_at"] < stale_before
                    and str(existing["server_id"]) == str(params["sid"])
                    and existing["payload_sha256"] == params["payload"]
                ):
                    existing["created_at"] = datetime.now(timezone.utc)
                    return Result(row=(1,))
                return Result(row=None)
            self.ledger[key] = {
                "server_id": params["sid"],
                "payload_sha256": params["payload"],
                "accepted": 0,
                "rejected": 0,
                "errors": [],
                "created_at": datetime.now(timezone.utc),
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
        self._transaction_snapshot = None

    async def rollback(self):
        self.rollbacks += 1
        if self._transaction_snapshot is not None:
            self.ledger = self._transaction_snapshot
            self._transaction_snapshot = None


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


def seed_incomplete_batch(
    db: BatchLedgerDB,
    agent_id,
    server_id,
    batch: AgentResultsBatch,
    *,
    age_seconds: float,
):
    db.ledger[(str(agent_id), batch.batch_id)] = {
        "server_id": server_id,
        "payload_sha256": agents._host_results_batch_digest(batch),
        "accepted": 0,
        "rejected": 0,
        "errors": [],
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        "completed_at": None,
    }


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
async def test_host_results_fresh_incomplete_claim_returns_retry_after(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()
    batch = make_batch(agent_id, server_id)
    seed_incomplete_batch(db, agent_id, server_id, batch, age_seconds=1)
    ingest_calls = 0

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        nonlocal ingest_calls
        ingest_calls += 1
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    with pytest.raises(HTTPException) as exc:
        await agents.post_results(batch, db, str(agent_id), "Bearer key")

    assert exc.value.status_code == 409
    assert "still being processed" in exc.value.detail
    assert exc.value.headers == {
        "Retry-After": str(agents.HOST_RESULTS_IN_PROGRESS_RETRY_AFTER_S),
    }
    assert ingest_calls == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_host_results_atomically_reclaims_matching_stale_claim(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()
    batch = make_batch(agent_id, server_id)
    seed_incomplete_batch(
        db,
        agent_id,
        server_id,
        batch,
        age_seconds=agents.HOST_RESULTS_IN_PROGRESS_TTL_S + 1,
    )
    stale_created_at = db.ledger[(str(agent_id), batch.batch_id)]["created_at"]
    ingest_calls = 0

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        nonlocal ingest_calls
        ingest_calls += 1
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    response = await agents.post_results(batch, db, str(agent_id), "Bearer key")

    row = db.ledger[(str(agent_id), batch.batch_id)]
    assert response.accepted == 1
    assert response.duplicates == 0
    assert ingest_calls == 1
    assert row["created_at"] > stale_created_at
    assert row["completed_at"] is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_host_results_does_not_reclaim_stale_claim_for_different_payload(
    monkeypatch,
):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()
    original = make_batch(agent_id, server_id)
    seed_incomplete_batch(
        db,
        agent_id,
        server_id,
        original,
        age_seconds=agents.HOST_RESULTS_IN_PROGRESS_TTL_S + 1,
    )
    original_digest = agents._host_results_batch_digest(original)
    ingest_calls = 0

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        nonlocal ingest_calls
        ingest_calls += 1
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)

    with pytest.raises(HTTPException) as exc:
        await agents.post_results(
            make_batch(agent_id, server_id, cpu_pct=99.0),
            db,
            str(agent_id),
            "Bearer key",
        )

    assert exc.value.status_code == 409
    assert "different payload" in exc.value.detail
    assert ingest_calls == 0
    assert db.ledger[(str(agent_id), original.batch_id)]["payload_sha256"] == original_digest


@pytest.mark.asyncio
async def test_host_results_claim_contention_has_short_retryable_deadline(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()

    class ContendedClaimDB(BatchLedgerDB):
        async def execute(self, statement, params=None):
            if "INSERT INTO agent_host_result_batches" in str(statement):
                await asyncio.sleep(1)
            return await super().execute(statement, params)

    db = ContendedClaimDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "HOST_RESULTS_CLAIM_TIMEOUT_S", 0.01)

    with pytest.raises(HTTPException) as exc:
        await agents.post_results(
            make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
        )

    assert exc.value.status_code == 503
    assert "claim is busy" in exc.value.detail
    assert exc.value.headers == {"Retry-After": "2"}
    assert db.rollbacks == 1
    assert db.ledger == {}


@pytest.mark.asyncio
async def test_host_results_ingest_timeout_rolls_back_claim(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    db = BatchLedgerDB()

    async def authenticate(*_args):
        return {"id": agent_id, "server_id": server_id}

    async def ingest(*_args):
        await asyncio.sleep(1)
        return 1, 0, [], 0

    monkeypatch.setattr(agents, "_authenticate", authenticate)
    monkeypatch.setattr(agents, "ingest_host_metric_batch", ingest)
    monkeypatch.setattr(agents, "HOST_RESULTS_INGEST_TIMEOUT_S", 0.01)

    with pytest.raises(HTTPException) as exc:
        await agents.post_results(
            make_batch(agent_id, server_id), db, str(agent_id), "Bearer key",
        )

    assert exc.value.status_code == 503
    assert "processing timed out" in exc.value.detail
    assert exc.value.headers == {"Retry-After": "2"}
    assert db.rollbacks == 1
    assert db.ledger == {}
    assert not any(
        "UPDATE agent_host_result_batches" in sql for sql, _params in db.calls
    )


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
