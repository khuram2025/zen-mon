"""APM instrumentation command-result reconciliation."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

APM_INSTRUMENTATION_COMMANDS = {"apm_instrument", "apm_uninstrument", "apm_restart_target"}


async def reconcile_apm_command_result(
    db: AsyncSession, *, agent_id, command: str, params: dict,
    success: bool, output: dict, error_message: str | None,
) -> None:
    if command not in APM_INSTRUMENTATION_COMMANDS:
        return
    process_key = str(params.get("process_key") or "").strip()
    target_kind = str(params.get("target_kind") or "").strip()
    target_name = str(params.get("target_name") or "").strip()
    state = "failed"
    if success:
        state = str(output.get("instrumentation_state") or "pending")
        if state not in {"none", "pending", "active", "failed", "unsupported"}:
            state = "pending"
    await db.execute(
        text("""
            UPDATE apm_agent_processes
            SET instrumentation_state = CAST(:state AS VARCHAR),
                otel_endpoint = CASE
                    WHEN CAST(:state AS VARCHAR) IN ('pending','active') THEN 'http://127.0.0.1:4318'
                    WHEN CAST(:state AS VARCHAR) = 'none' THEN NULL ELSE otel_endpoint END,
                updated_at = NOW()
            WHERE agent_id = :agent_id
              AND ((:process_key <> '' AND process_key = :process_key)
                OR (:target_kind = 'iis_app_pool' AND :target_name <> '' AND iis_app_pool = :target_name)
                OR (:target_kind = 'windows_service' AND :target_name <> '' AND windows_service = :target_name))
        """),
        {"agent_id": agent_id, "process_key": process_key, "target_kind": target_kind,
         "target_name": target_name, "state": state},
    )
