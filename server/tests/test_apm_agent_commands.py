from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.api.v1 import apm_agents
from app.api.v1.apm_agents import DiscoveredProcess, InstrumentationRequest
from app.services.apm_agent_service import reconcile_apm_command_result


class RecordingDB:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))


class MappingRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_successful_instrumentation_result_sets_active_state():
    db = RecordingDB()
    agent_id = uuid4()
    await reconcile_apm_command_result(
        db, agent_id=agent_id, command="apm_instrument",
        params={"process_key": "process-1", "target_kind": "iis_app_pool", "target_name": "DefaultAppPool"},
        success=True, output={"instrumentation_state": "active"}, error_message=None,
    )
    assert len(db.calls) == 1
    assert db.calls[0][1] == {
        "agent_id": agent_id, "process_key": "process-1", "target_kind": "iis_app_pool",
        "target_name": "DefaultAppPool", "state": "active",
    }


@pytest.mark.asyncio
async def test_failed_instrumentation_result_sets_failed_state():
    db = RecordingDB()
    await reconcile_apm_command_result(
        db, agent_id=uuid4(), command="apm_uninstrument",
        params={"target_kind": "iis_app_pool", "target_name": "DefaultAppPool"}, success=False,
        output={}, error_message="IIS configuration failed",
    )
    assert db.calls[0][1]["state"] == "failed"


@pytest.mark.asyncio
async def test_unrelated_agent_command_is_ignored():
    db = RecordingDB()
    await reconcile_apm_command_result(
        db, agent_id=uuid4(), command="collect_now", params={},
        success=True, output={}, error_message=None,
    )
    assert db.calls == []


def test_instrumentation_environment_rejects_resource_attribute_injection():
    with pytest.raises(ValueError):
        InstrumentationRequest(enabled=True, environment="prod,service.name=spoofed")


def test_deployment_fingerprint_requires_lowercase_sha256():
    with pytest.raises(ValueError):
        DiscoveredProcess(process_key="process-1", artifact_fingerprint="not-a-sha256")
    row = DiscoveredProcess(process_key="process-1", artifact_fingerprint="a" * 64)
    assert row.artifact_fingerprint == "a" * 64


@pytest.mark.asyncio
async def test_windows_service_result_can_reconcile_by_stable_target():
    db = RecordingDB()
    await reconcile_apm_command_result(
        db, agent_id=uuid4(), command="apm_instrument",
        params={"target_kind": "windows_service", "target_name": "OrdersApi"},
        success=True, output={"instrumentation_state": "pending"}, error_message=None,
    )
    assert db.calls[0][1]["target_kind"] == "windows_service"
    assert "windows_service" in db.calls[0][0]


@pytest.mark.asyncio
async def test_agent_process_list_exposes_server_identity_for_cloned_hostnames(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()

    class ProcessDB:
        def __init__(self):
            self.sql = ""

        async def execute(self, statement, params=None):
            self.sql = str(statement)
            return MappingRows([{
                "id": uuid4(),
                "agent_id": agent_id,
                "server_id": server_id,
                "hostname": "WIN-CLONED",
                "server_name": "WIN-CLONED (clone 192.0.2.21)",
                "server_ip": "192.0.2.21",
                "apm_credential_bound": False,
                "service_name_guess": "TestWebApp",
                "instrumentation_state": "none",
                "last_command_params": {},
            }])

    async def no_trace_rows(_callable):
        return []

    db = ProcessDB()
    monkeypatch.setattr(apm_agents.asyncio, "to_thread", no_trace_rows)
    output = await apm_agents.list_agent_processes(
        server_id=None,
        active_hours=24,
        db=db,
        user=object(),
    )

    assert "LEFT JOIN servers s ON s.id = p.server_id" in db.sql
    assert "AS apm_credential_bound" in db.sql
    assert "COALESCE(host(s.primary_ip), host(a.last_ip)) AS server_ip" in db.sql
    assert output[0]["agent_id"] == agent_id
    assert output[0]["server_name"] == "WIN-CLONED (clone 192.0.2.21)"
    assert output[0]["server_ip"] == "192.0.2.21"
    assert output[0]["telemetry_status"] == "credential_missing"


@pytest.mark.asyncio
async def test_agent_process_trace_health_requires_exact_agent_and_server_binding(monkeypatch):
    agent_id, server_id = uuid4(), uuid4()
    other_agent_id, other_server_id = uuid4(), uuid4()
    traced_at = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)

    class ProcessDB:
        async def execute(self, statement, params=None):
            return MappingRows([{
                "id": uuid4(),
                "agent_id": agent_id,
                "server_id": server_id,
                "hostname": "WIN-CLONED",
                "server_name": "WIN-CLONED (clone 192.0.2.21)",
                "server_ip": "192.0.2.21",
                "apm_credential_bound": True,
                "service_name_guess": "DefaultAppPool",
                "instrumentation_state": "active",
                "last_command_params": {"environment": "prod"},
            }])

    trace_rows = [[
        str(other_agent_id), str(other_server_id), "DefaultAppPool", "prod",
        traced_at, 12,
    ]]
    queries = []

    class TraceResult:
        @property
        def result_rows(self):
            return trace_rows

    class FakeClickHouse:
        def query(self, sql):
            queries.append(sql)
            return TraceResult()

    monkeypatch.setattr(apm_agents, "get_ch_client", lambda: FakeClickHouse())
    output = await apm_agents.list_agent_processes(
        server_id=None, active_hours=24, db=ProcessDB(), user=object(),
    )
    assert output[0]["telemetry_status"] == "waiting_for_first_trace"
    assert output[0]["traces_15m"] == 0
    assert "JSONExtractString(resource, 'zenplus.agent_id')" in queries[0]
    assert "JSONExtractString(resource, 'zenplus.server_id')" in queries[0]
    assert "GROUP BY agent_id, server_id, service_name, env" in queries[0]

    trace_rows = [[
        str(agent_id), str(server_id), "DefaultAppPool", "prod",
        traced_at, 12,
    ]]
    output = await apm_agents.list_agent_processes(
        server_id=None, active_hours=24, db=ProcessDB(), user=object(),
    )
    assert output[0]["telemetry_status"] == "receiving"
    assert output[0]["last_trace_at"] == traced_at
    assert output[0]["traces_15m"] == 12
