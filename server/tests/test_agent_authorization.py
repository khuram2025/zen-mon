from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import agents, servers
from app.schemas.agent import AgentEnrollRequest, AgentRegistrationConflictResolve


class Result:
    def __init__(self, *, row=None, mapping=None, rows=None, rowcount=0):
        self.row = row
        self.mapping = mapping
        self.rows = rows or []
        self.rowcount = rowcount

    def first(self):
        return self.mapping if self.mapping is not None else self.row

    def mappings(self):
        return self

    def fetchall(self):
        return self.rows

    def all(self):
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


class ConflictDB:
    def __init__(self, pending_secret_hash="existing"):
        self.agent_id = uuid4()
        self.calls = []
        self.commits = 0
        self.pending_secret_hash = pending_secret_hash

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "SELECT * FROM agents WHERE agent_uid" in sql:
            return Result(mapping={
                "id": self.agent_id,
                "pending_secret_hash": (
                    agents._sha256("old-secret" * 4)
                    if self.pending_secret_hash == "existing" else None
                ),
            })
        if "FROM agent_registration_resolutions" in sql and "SELECT action" in sql:
            return Result(mapping=None)
        if "UPDATE agents SET" in sql and "pending_conflict_secret_hash" in sql:
            return Result()
        raise AssertionError(sql)

    async def commit(self):
        self.commits += 1

@pytest.mark.asyncio
async def test_pending_secret_conflict_is_persisted_before_409():
    db = ConflictDB()
    request = AgentEnrollRequest(
        agent_uid="win-12345678",
        pending_secret="new-secret" * 4,
        hostname="WEB01",
        platform="windows",
        version="1.11.2",
        install_id="install-new",
        architecture="amd64",
    )

    with pytest.raises(HTTPException) as exc:
        await agents._enroll_pending_agent(request, "192.0.2.20", db)

    assert exc.value.status_code == 409
    assert db.commits == 1
    update = next(
        (params for sql, params in db.calls if "pending_conflict_secret_hash" in sql),
        None,
    )
    assert update is not None
    assert update["pending"] == agents._sha256("new-secret" * 4)
    assert update["ip"] == "192.0.2.20"
    assert update["iid"] == "install-new"


@pytest.mark.asyncio
async def test_legacy_authorized_agent_without_continuity_secret_requires_review():
    db = ConflictDB(pending_secret_hash=None)
    request = AgentEnrollRequest(
        agent_uid="win-legacy01",
        pending_secret="new-secret" * 4,
        hostname="WEB01",
        platform="windows",
        version="1.11.3",
        install_id="install-new",
    )

    with pytest.raises(HTTPException) as exc:
        await agents._enroll_pending_agent(request, "192.0.2.21", db)

    assert exc.value.status_code == 409
    assert db.commits == 1
    assert any("registration_conflict_revision" in sql for sql, _ in db.calls)


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


class ResolveConflictDB:
    def __init__(self, revision=None):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.revision = revision or uuid4()
        self.server_id = uuid4()
        self.clone_agent_id = uuid4()

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "FROM agents WHERE id = :id FOR UPDATE" in sql:
            return Result(mapping={
                "pending_conflict_secret_hash": agents._sha256("candidate-secret"),
                "registration_conflict_revision": self.revision,
                "registration_conflict_ip": "192.0.2.20",
                "registration_conflict_at": None,
                "registration_conflict_attempts": 3,
                "registration_conflict_install_id": "install-new",
                "registration_conflict_hostname": "WEB01",
                "registration_conflict_version": "1.11.2",
                "agent_uid": "win-12345678",
                "hostname": "WEB01",
                "platform": "windows",
                "site_id": None,
                "policy_id": uuid4(),
                "update_ring": "stable",
                "server_id": uuid4(),
            })
        if "INSERT INTO servers" in sql and "RETURNING id" in sql:
            return Result(row=(self.server_id,))
        if "INSERT INTO agents" in sql and "RETURNING id" in sql:
            return Result(row=(self.clone_agent_id,))
        return Result()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_resolve_conflict_revokes_old_keys_and_promotes_candidate():
    db = ResolveConflictDB()
    agent_id = uuid4()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    response = await servers.resolve_registration_conflict(
        agent_id,
        AgentRegistrationConflictResolve(conflict_revision=db.revision),
        db,
        user,
    )

    assert response["ok"] is True
    assert db.commits == 1
    sql = "\n".join(statement for statement, _ in db.calls)
    assert "UPDATE apm_ingest_keys" in sql
    assert "DELETE FROM agent_apm_credentials" in sql
    assert "pending_secret_hash = pending_conflict_secret_hash" in sql
    assert "api_key_hash = NULL" in sql
    assert "authorization_source = 'admin_replacement'" in sql


@pytest.mark.asyncio
async def test_resolve_conflict_rejects_candidate_that_changed_after_review():
    db = ResolveConflictDB()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    with pytest.raises(HTTPException) as exc:
        await servers.resolve_registration_conflict(
            uuid4(),
            AgentRegistrationConflictResolve(conflict_revision=uuid4()),
            db,
            user,
        )

    assert exc.value.status_code == 409
    assert "changed" in exc.value.detail
    assert db.rollbacks == 1
    assert db.commits == 0
    assert not any("UPDATE apm_ingest_keys" in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_resolve_conflict_registers_clone_without_revoking_source():
    db = ResolveConflictDB()
    source_agent_id = uuid4()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    response = await servers.resolve_registration_conflict(
        source_agent_id,
        AgentRegistrationConflictResolve(
            conflict_revision=db.revision,
            action="register_clone",
            display_name="WEB01 Clone",
            authorize=True,
        ),
        db,
        user,
    )

    assert response["action"] == "register_clone"
    assert response["agent_id"] == str(db.clone_agent_id)
    assert response["server_id"] == str(db.server_id)
    assert response["authorization_state"] == "authorized"
    sql = "\n".join(statement for statement, _ in db.calls)
    assert "INSERT INTO agent_registration_resolutions" in sql
    assert "'register_clone'" in sql
    assert "INSERT INTO servers" in sql
    assert "INSERT INTO agents" in sql
    assert "UPDATE apm_ingest_keys" not in sql
    assert db.commits == 1


@pytest.mark.asyncio
async def test_resolve_conflict_blocks_only_reviewed_candidate():
    db = ResolveConflictDB()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    response = await servers.resolve_registration_conflict(
        uuid4(),
        AgentRegistrationConflictResolve(
            conflict_revision=db.revision,
            action="block",
        ),
        db,
        user,
    )

    assert response["action"] == "block"
    assert response["authorization_state"] == "blocked"
    sql = "\n".join(statement for statement, _ in db.calls)
    assert "'block'" in sql
    assert "UPDATE apm_ingest_keys" not in sql
    assert "INSERT INTO agents" not in sql
    assert db.commits == 1


class ApprovedCloneEnrollmentDB:
    def __init__(self):
        self.source_id = uuid4()
        self.assigned_id = uuid4()
        self.server_id = uuid4()
        self.policy_id = uuid4()
        self.assigned_uid = f"windows-clone-{uuid4()}"
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "SELECT * FROM agents WHERE agent_uid" in sql:
            return Result(mapping={
                "id": self.source_id,
                "pending_secret_hash": agents._sha256("source-secret" * 4),
            })
        if "FROM agent_registration_resolutions" in sql and "SELECT action" in sql:
            return Result(mapping={
                "action": "register_clone",
                "assigned_agent_id": self.assigned_id,
                "assigned_agent_uid": self.assigned_uid,
            })
        if "SELECT * FROM agents WHERE id" in sql:
            return Result(mapping={
                "id": self.assigned_id,
                "server_id": self.server_id,
                "policy_id": self.policy_id,
                "pending_secret_hash": agents._sha256("clone-secret" * 4),
                "authorized_at": None,
                "revoked_at": None,
            })
        return Result()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_approved_clone_receives_and_persists_assigned_uid():
    db = ApprovedCloneEnrollmentDB()
    request = AgentEnrollRequest(
        agent_uid="win-12345678",
        pending_secret="clone-secret" * 4,
        hostname="WEB01",
        platform="windows",
        version="1.12.0",
    )

    response = await agents._enroll_pending_agent(request, "192.0.2.21", db)

    assert response.authorization_state == "pending"
    assert response.agent_id == str(db.assigned_id)
    assert response.server_id == str(db.server_id)
    assert response.assigned_agent_uid == db.assigned_uid
    assert db.commits == 1


class DeleteAgentDB(MutationDB):
    def __init__(self):
        super().__init__()
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        result = await super().execute(statement, params)
        if "DELETE FROM agents WHERE id" in str(statement):
            result.rowcount = 1
        return result

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_delete_agent_revokes_apm_key_before_removing_provenance():
    db = DeleteAgentDB()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    response = await servers.delete_agent(uuid4(), db, user)

    assert response == {"ok": True}
    sql = "\n".join(db.calls)
    assert sql.index("UPDATE apm_ingest_keys") < sql.index("DELETE FROM agent_apm_credentials")
    assert sql.index("DELETE FROM agent_apm_credentials") < sql.index("DELETE FROM agents")
    assert db.commits == 1


class DeleteServerDB(DeleteAgentDB):
    def __init__(self):
        super().__init__()
        self.agent_id = uuid4()

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT id FROM agents WHERE server_id" in sql:
            self.calls.append(sql)
            return Result(rows=[(self.agent_id,)])
        result = await super().execute(statement, params)
        if "DELETE FROM servers WHERE id" in sql:
            result.rowcount = 1
        return result


@pytest.mark.asyncio
async def test_delete_server_revokes_all_agent_credentials_before_delete():
    db = DeleteServerDB()
    user = type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()

    response = await servers.delete_server(uuid4(), db, user)

    assert response == {"ok": True}
    sql = "\n".join(db.calls)
    assert sql.index("UPDATE apm_ingest_keys") < sql.index("DELETE FROM agents")
    assert sql.index("api_key_hash = NULL") < sql.index("DELETE FROM servers")
    assert db.commits == 1


def test_authorization_migration_backfills_existing_agents():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "migrate-056-agent-authorization.sql"
    ).read_text()
    assert "pending_secret_hash" in migration
    assert "authorized_at" in migration
    assert "WHERE api_key_hash IS NOT NULL" in migration


def test_registration_conflict_migration_never_stores_plaintext_secrets():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "migrate-087-agent-registration-conflicts.sql"
    ).read_text()
    assert "pending_conflict_secret_hash" in migration
    assert "registration_conflict_revision" in migration
    assert "registration_conflict_at" in migration
    assert "pending_conflict_secret " not in migration


def test_clone_resolution_migration_binds_only_hashed_candidate_secrets():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "scripts" / "migrate-091-agent-clone-resolutions.sql"
    ).read_text()
    assert "agent_registration_resolutions" in migration
    assert "pending_secret_hash" in migration
    assert "register_clone" in migration
    assert "pending_secret " not in migration
