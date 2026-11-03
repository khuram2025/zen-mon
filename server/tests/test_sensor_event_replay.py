"""Lost-ACK, concurrent retry and crash-boundary tests on private PostgreSQL.

Only authentication/rate limiting are replaced; the real endpoint, ledger,
command update, event insert and transaction boundaries execute against SQL.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.api.v1 import sensor_api as api
from app.schemas.sensor import EventsBatch
from pg_fixture import pg_cluster, pg_db  # noqa: F401


@pytest.fixture
def event_context(pg_db, monkeypatch):
    sensor_id = uuid4()
    pg_db.execute("INSERT INTO sensors (id, name) VALUES ($1, $2)", sensor_id, str(sensor_id))

    async def authenticate(*args, **kwargs):
        return {"id": sensor_id}

    async def limit(*args, **kwargs):
        pass

    monkeypatch.setattr(api, "_authenticate", authenticate)
    monkeypatch.setattr(api, "_limit_sensor_request", limit)
    sessions = async_sessionmaker(create_async_engine(pg_db.url, poolclass=NullPool))

    async def send(body):
        request = Request({"type": "http", "method": "POST", "path": "/sensor/events", "headers": []})
        async with sessions() as db:
            return await api.post_events(body, request, db=db, x_sensor_id=str(sensor_id), authorization="Bearer fixture")

    return sensor_id, send


def event_batch(key="events-replay-0001", **overrides):
    return EventsBatch(idempotency_key=key, items=[{
        "type": "sensor_event", "timestamp": "2026-10-29T00:00:00Z",
        "data": {"message": "synthetic fixture"}, **overrides,
    }])


@pytest.mark.asyncio
async def test_lost_ack_replays_one_event_and_rejects_changed_payload(pg_db, event_context):
    sensor_id, send = event_context
    first = await send(event_batch())
    second = await send(event_batch())
    assert first == {"accepted": 1, "duplicate": False, "idempotent": True}
    assert second == {"accepted": 1, "duplicate": True, "idempotent": True}
    with pytest.raises(HTTPException) as error:
        await send(event_batch(data={"message": "changed"}))
    assert error.value.status_code == 409
    assert pg_db.scalar("SELECT count(*) FROM sensor_events WHERE sensor_id=$1", sensor_id) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_empty_batch(pg_db, event_context):
    sensor_id, send = event_context
    results = await asyncio.gather(send(event_batch()), send(event_batch()))
    assert sorted(r["duplicate"] for r in results) == [False, True]
    empty = EventsBatch(idempotency_key="empty-event-0001", items=[])
    assert (await send(empty))["duplicate"] is False
    assert (await send(empty))["duplicate"] is True
    assert pg_db.scalar("SELECT count(*) FROM sensor_events WHERE sensor_id=$1", sensor_id) == 1


@pytest.mark.asyncio
async def test_failure_before_commit_rolls_back_command_event_and_ledger(pg_db, event_context, monkeypatch):
    sensor_id, send = event_context
    command = uuid4()
    pg_db.execute("INSERT INTO sensor_commands (id, sensor_id, verb) VALUES ($1, $2, 'reload_config')", command, sensor_id)
    body = event_batch(type="command_completed", data={"command_id": str(command), "message": "done"})
    complete = api._complete_ingest

    async def crash(*args, **kwargs):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(api, "_complete_ingest", crash)
    with pytest.raises(RuntimeError, match="injected"):
        await send(body)
    assert pg_db.scalar("SELECT count(*) FROM sensor_events WHERE sensor_id=$1", sensor_id) == 0
    assert pg_db.scalar("SELECT count(*) FROM sensor_ingest_ledger WHERE sensor_id=$1", sensor_id) == 0
    assert pg_db.scalar("SELECT status FROM sensor_commands WHERE id=$1", command) == "pending"
    monkeypatch.setattr(api, "_complete_ingest", complete)
    await send(body)
    await send(body)
    assert pg_db.scalar("SELECT count(*) FROM sensor_events WHERE sensor_id=$1", sensor_id) == 1
    assert pg_db.scalar("SELECT status FROM sensor_commands WHERE id=$1", command) == "succeeded"


@pytest.mark.asyncio
async def test_legacy_clients_remain_explicitly_non_idempotent(pg_db, event_context):
    sensor_id, send = event_context
    result = await send(event_batch(key=None))
    assert result["idempotent"] is False
    assert result["accepted"] == 1
    assert pg_db.scalar("SELECT count(*) FROM sensor_ingest_ledger WHERE sensor_id=$1", sensor_id) == 0


def test_events_validate_keys_and_enforce_two_hundred_items():
    with pytest.raises(ValidationError):
        event_batch(key="short")
    with pytest.raises(ValidationError):
        EventsBatch(idempotency_key="events-limit-0001", items=event_batch().items * 201)
