from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.audit_service import write_audit_log


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RecordingDB:
    def __init__(self):
        self.calls = []

    def begin_nested(self):
        return AsyncContext()

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))


class FailingDB(RecordingDB):
    async def execute(self, statement, params):
        raise RuntimeError("audit table missing")


def actor():
    return SimpleNamespace(id=uuid4(), username="admin", role="admin")


@pytest.mark.asyncio
async def test_write_audit_log_inserts_expected_payload():
    db = RecordingDB()
    user = actor()

    await write_audit_log(
        db,
        actor=user,
        action="user.create",
        resource_type="user",
        resource_id="target-id",
        metadata={"field": "value"},
    )

    assert len(db.calls) == 1
    _, params = db.calls[0]
    assert params["actor_id"] == user.id
    assert params["actor_username"] == "admin"
    assert params["actor_role"] == "admin"
    assert params["action"] == "user.create"
    assert params["resource_type"] == "user"
    assert params["resource_id"] == "target-id"
    assert '"field": "value"' in params["metadata"]


@pytest.mark.asyncio
async def test_write_audit_log_does_not_raise_when_insert_fails():
    await write_audit_log(
        FailingDB(),
        actor=actor(),
        action="user.create",
        resource_type="user",
    )
