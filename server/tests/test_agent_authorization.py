from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import agents, servers
from app.schemas.agent import AgentEnrollRequest


class Result:
    def __init__(self, *, row=None, mapping=None, rows=None):
        self.row = row
        self.mapping = mapping
        self.rows = rows or []

    def first(self):
        return self.mapping if self.mapping is not None else self.row

    def mappings(self):
        return self

    def fetchall(self):
        return self.rows


class PendingDB:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.agent_id = uuid4()
        self.policy_id = uuid4()

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "SELECT * FROM agents WHERE agent_uid" in sql:
            return Result(mapping=None)
        if "SELECT id FROM agent_policies" in sql:
            return Result(row=(self.policy_id,))
        if "INSERT INTO agents" in sql:
            return Result(row=(self.agent_id,))
        raise AssertionError(sql)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_tokenless_agent_registers_pending_without_api_key():
    db = PendingDB()
    request = AgentEnrollRequest(
        agent_uid="win-12345678",
        pending_secret="p" * 40,
        hostname="WEB01",
        platform="windows",
        version="1.5.2",
        architecture="amd64",
    )

    response = await agents._enroll_pending_agent(request, "192.0.2.10", db)

    assert response.authorization_state == "pending"
    assert response.api_key is None
    assert response.server_id is None
    assert response.agent_id == str(db.agent_id)
    assert db.commits == 1
    insert = next((params for sql, params in db.calls if "INSERT INTO agents" in sql), None)
    assert insert is not None
    assert insert["pending"] == agents._sha256("p" * 40)


class AuthDB:
    def __init__(self, row):
        self.row = row

    async def execute(self, statement, params=None):
        return Result(mapping=self.row)


@pytest.mark.asyncio
async def test_pending_agent_credentials_are_rejected_before_data_ingest():
    agent_id = uuid4()
    key = "zpa_key_test"
    row = {
        "id": agent_id,
        "status": "enrolling",
        "api_key_hash": agents._sha256(key),
        "authorized_at": None,
        "revoked_at": None,
    }

    with pytest.raises(HTTPException) as exc:
        await agents._authenticate(str(agent_id), key, AuthDB(row))

    assert exc.value.status_code == 403
    assert "awaiting authorization" in exc.value.detail


class MutationDB:
    def __init__(self, count=1):
        self.count = count
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        rows = [(uuid4(),) for _ in range(self.count)] if "RETURNING id" in sql else []
        return Result(rows=rows)


@pytest.mark.asyncio
async def test_revoke_invalidates_host_and_apm_credentials():
    db = MutationDB()
    affected = await servers._revoke_agents([uuid4()], uuid4(), db)

    assert affected == 1
    sql = "\n".join(db.calls)
    assert "UPDATE apm_ingest_keys" in sql
    assert "DELETE FROM agent_apm_credentials" in sql
    assert "api_key_hash = NULL" in sql
    assert "status = 'disabled'" in sql


def test_authorization_migration_backfills_existing_agents():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "migrate-056-agent-authorization.sql"
    ).read_text()
    assert "pending_secret_hash" in migration
    assert "authorized_at" in migration
    assert "WHERE api_key_hash IS NOT NULL" in migration
