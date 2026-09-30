from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import agents, servers
from app.schemas.agent import AgentCloneDecision, AgentEnrollRequest, AgentRegistrationConflictResolve


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
        if "INSERT INTO agents" in sql and "RETURNING *" in sql:
            self.clone_id = uuid4()
            return Result(mapping={"id": self.clone_id, "policy_id": None, **(params or {})})
        if "INSERT INTO agent_registration_resolutions" in sql:
            return Result()
        raise AssertionError(sql)

    async def commit(self):
        self.commits += 1

@pytest.mark.asyncio
async def test_cloned_installation_gets_its_own_pending_entry():
    # A VM cloned from a template reports the template's agent_uid with its
    # own pending secret: it must be listed for approval as a separate entry,
    # never parked on (or overwriting) the original agent.
    db = ConflictDB()
    request = AgentEnrollRequest(
        agent_uid="win-12345678",
        pending_secret="new-secret" * 4,
        hostname="WEB02",
        platform="windows",
        version="1.11.2",
        install_id="install-new",
        architecture="amd64",
    )

    response = await agents._enroll_pending_agent(request, "192.0.2.20", db)

    assert response.authorization_state == "pending"
    assert response.agent_id == str(db.clone_id)
    assert response.assigned_agent_uid is None  # handed over on approval
    assert db.commits == 1
    clone = next(params for sql, params in db.calls if "INSERT INTO agents" in sql)
    assert clone["pending"] == agents._sha256("new-secret" * 4)
    assert clone["uid"].startswith("windows-clone-") and clone["uid"] != "win-12345678"
    assert clone["hn"] == "WEB02" and clone["ip"] == "192.0.2.20"
    resolution = next(params for sql, params in db.calls if "INSERT INTO agent_registration_resolutions" in sql)
    assert resolution["reported"] == "win-12345678"
    assert resolution["pending"] == agents._sha256("new-secret" * 4)
    assert resolution["source"] == db.agent_id
    assert resolution["assigned"] == db.clone_id
    # The original agent row is never written.
    assert not any(sql.lstrip().startswith("UPDATE agents") for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_legacy_agent_without_continuity_secret_is_not_taken_over():
    # agent_uid is not a secret: a pending registration for a token-enrolled
    # agent (no stored pending secret) becomes a separate entry for review.
    db = ConflictDB(pending_secret_hash=None)
    request = AgentEnrollRequest(
        agent_uid="win-legacy01",
        pending_secret="new-secret" * 4,
        hostname="WEB01",
        platform="windows",
        version="1.11.3",
        install_id="install-new",
    )

    response = await agents._enroll_pending_agent(request, "192.0.2.21", db)

    assert response.authorization_state == "pending"
    assert response.agent_id == str(db.clone_id)
    assert not any(sql.lstrip().startswith("UPDATE agents") for sql, _ in db.calls)


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
                "authorized_at": object() if getattr(self, "authorized", False) else None,
                "revoked_at": None,
            })
        return Result()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_pending_clone_retry_routes_to_its_own_entry():
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
    # Retries route to the clone's own entry; no new entry is created and the
    # identity is only handed over once an administrator approves it.
    assert response.assigned_agent_uid is None
    assert not any("INSERT INTO agents" in sql for sql, _ in db.calls)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_approved_clone_receives_assigned_uid_and_credentials(monkeypatch):
    db = ApprovedCloneEnrollmentDB()
    db.authorized = True

    async def no_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(agents, "_sync_server_identity", no_sync)
    request = AgentEnrollRequest(
        agent_uid="win-12345678",
        pending_secret="clone-secret" * 4,
        hostname="WEB01",
        platform="windows",
        version="1.12.0",
    )

    response = await agents._enroll_pending_agent(request, "192.0.2.21", db)

    assert response.authorization_state == "authorized"
    assert response.agent_id == str(db.assigned_id)
    assert response.assigned_agent_uid == db.assigned_uid
    assert response.api_key
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


class CloneDecisionDB:
    def __init__(self, authorized=False, source=True):
        self.clone_id = uuid4()
        self.source_id = uuid4() if source else None
        self.resolution_id = uuid4()
        self.authorized = authorized
        self.calls = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "JOIN agent_registration_resolutions r" in sql:
            return Result(mapping={
                "id": self.clone_id, "hostname": "WEB02", "version": "1.14.3",
                "install_id": "install-clone", "last_ip": "192.0.2.29",
                "pending_secret_hash": agents._sha256("clone-secret" * 4),
                "authorized_at": object() if self.authorized else None,
                "resolution_id": self.resolution_id, "source_agent_id": self.source_id,
            })
        return Result()

    async def commit(self):
        self.commits += 1


def _operator():
    return type("User", (), {"id": uuid4(), "username": "operator", "role": "operator"})()


@pytest.mark.asyncio
async def test_clone_can_be_accepted_as_reinstall_of_original(monkeypatch):
    monkeypatch.setattr(servers, "write_audit_log", _noop_audit)
    db = CloneDecisionDB()
    out = await servers.decide_pending_clone(db.clone_id, AgentCloneDecision(action="replace_original"), db, _operator())

    assert out["agent_id"] == str(db.source_id)
    sql = "\n".join(statement for statement, _ in db.calls)
    assert "DELETE FROM agents WHERE id" in sql
    update = next(params for statement, params in db.calls if "authorization_source = 'admin_replacement'" in statement)
    assert update["source"] == db.source_id
    assert update["pending"] == agents._sha256("clone-secret" * 4)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_blocking_a_clone_leaves_the_original_untouched(monkeypatch):
    monkeypatch.setattr(servers, "write_audit_log", _noop_audit)
    db = CloneDecisionDB()
    out = await servers.decide_pending_clone(db.clone_id, AgentCloneDecision(action="block"), db, _operator())

    assert out["action"] == "block"
    sql = "\n".join(statement for statement, _ in db.calls)
    assert "action = 'block'" in sql and "assigned_agent_id = NULL" in sql
    assert "admin_replacement" not in sql and "UPDATE apm_ingest_keys" not in sql


@pytest.mark.asyncio
async def test_approved_clone_cannot_be_redirected():
    db = CloneDecisionDB(authorized=True)
    with pytest.raises(HTTPException) as exc:
        await servers.decide_pending_clone(db.clone_id, AgentCloneDecision(action="replace_original"), db, _operator())
    assert exc.value.status_code == 409
    assert db.commits == 0


async def _noop_audit(*args, **kwargs):
    return None


class ServerLinkDB:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if "SELECT s.id FROM servers s" in str(statement):
            return Result(row=None)
        return Result(row=(uuid4(),))


@pytest.mark.asyncio
async def test_new_agent_never_adopts_a_server_another_agent_reports_into():
    # Clones often keep the template's hostname; matching servers by hostname
    # alone would merge two machines' monitoring history.
    db = ServerLinkDB()
    agent_id = uuid4()
    request = AgentEnrollRequest(agent_uid="windows-clone-x", pending_secret="s" * 40,
                                 hostname="WEB01", platform="windows", version="1.14.3")
    await agents._ensure_pending_server(request, db, agent_id)

    lookup_sql, lookup_params = db.calls[0]
    assert "NOT EXISTS" in lookup_sql and "a.server_id = s.id" in lookup_sql
    assert lookup_params["aid"] == str(agent_id)
    assert any("INSERT INTO servers" in sql for sql, _ in db.calls)
