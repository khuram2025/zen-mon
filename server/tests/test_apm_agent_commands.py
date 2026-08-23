from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.v1.apm_agents import DiscoveredProcess, InstrumentationRequest
from app.services.apm_agent_service import reconcile_apm_command_result


class RecordingDB:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))


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
