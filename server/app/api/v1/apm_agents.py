"""Agent-facing APM control plane.

The host agent authenticates with its existing ``zpa_`` credential. This
router mints an agent-scoped ``zpi_`` ingest key and accepts the process/runtime
inventory produced by the local APM worker. Plaintext ingest keys are returned
only by the enrollment response and are never stored by the controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agents import _authenticate as authenticate_agent
from app.api.v1.agents import _strip_bearer
from app.api.v1.apm import _new_ingest_key, invalidate_ingest_key_cache
from app.core.database import get_ch_client, get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.audit_service import write_audit_log


router = APIRouter(prefix="/agents/apm", tags=["Agent APM"])
admin_router = APIRouter(prefix="/apm", tags=["APM agent discovery"])
logger = logging.getLogger("zenplus.apm.agents")

RuntimeT = Literal[
    "dotnet", "dotnet_framework", "java", "node", "python", "iis", "other"
]
InstrumentationStateT = Literal["none", "pending", "active", "failed", "unsupported"]


class AgentAPMEnrollRequest(BaseModel):
    environment: str = Field(default="prod", min_length=1, max_length=64)


class AgentAPMEnrollResponse(BaseModel):
    key: str
    key_id: uuid.UUID
    environment: str
    traces_path: str = "/v1/traces"


class DiscoveredProcess(BaseModel):
    process_key: str = Field(..., min_length=8, max_length=128)
    pid: int = Field(default=0, ge=0)
    ppid: int = Field(default=0, ge=0)
    exe_path: str = Field(default="", max_length=4096)
    cmdline: str = Field(default="", max_length=16384)
    runtime: RuntimeT = "other"
    runtime_version: str = Field(default="", max_length=128)
    service_name_guess: str = Field(default="", max_length=255)
    windows_service: Optional[str] = Field(default=None, max_length=255)
    iis_site: Optional[str] = Field(default=None, max_length=255)
    iis_app_pool: Optional[str] = Field(default=None, max_length=255)
    listening_ports: list[int] = Field(default_factory=list, max_length=256)
    instrumentation_state: InstrumentationStateT = "none"
    otel_detected: bool = False
    otel_endpoint: Optional[str] = Field(default=None, max_length=2048)
    artifact_path: str = Field(default="", max_length=4096)
    artifact_fingerprint: str = Field(default="", max_length=64, pattern=r"^(?:[0-9a-f]{64})?$")
    artifact_modified_at: Optional[datetime] = None


class DiscoveryReport(BaseModel):
    processes: list[DiscoveredProcess] = Field(default_factory=list, max_length=5000)


class DiscoveryAccepted(BaseModel):
    accepted: int
    reported_at: datetime


class InstrumentationRequest(BaseModel):
    enabled: bool
    restart: bool = True
    service_name: Optional[str] = Field(default=None, max_length=255)
    environment: str = Field(default="prod", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


async def _record_agent_deployment(db: AsyncSession, *, agent: dict, process: DiscoveredProcess) -> None:
    command = (await db.execute(
        text("""
            SELECT params FROM agent_commands
            WHERE agent_id = :agent_id AND command = 'apm_instrument'
              AND (params->>'process_key' = :process_key
                   OR params->>'target_name' = :target_name)
              AND status = 'completed'
            ORDER BY completed_at DESC NULLS LAST, created_at DESC LIMIT 1
        """),
        {"agent_id": agent["id"], "process_key": process.process_key,
         "target_name": process.windows_service or process.iis_app_pool or ""},
    )).mappings().first()
    params = dict(command["params"] or {}) if command else {}
    service_name = str(params.get("service_name") or process.service_name_guess or "").strip()
    environment = str(params.get("environment") or "prod").strip()
    if not service_name:
        return
    env = (await db.execute(
        text("SELECT id FROM apm_environments WHERE name = :name"), {"name": environment},
    )).first()
    if not env:
        return
    service_id = (await db.execute(
        text("""
            INSERT INTO apm_services (name, env_id, language, health)
            VALUES (:name, :env_id, :language, 'no_data')
            ON CONFLICT (name, env_id) DO UPDATE SET
                language = COALESCE(apm_services.language, EXCLUDED.language),
                updated_at = NOW()
            RETURNING id
        """),
        {"name": service_name, "env_id": env[0], "language": process.runtime},
    )).first()[0]
    metadata = {
        "detected_by": "zenplus_agent", "agent_id": str(agent["id"]),
        "server_id": str(agent["server_id"]), "process_key": process.process_key,
        "windows_service": process.windows_service,
        "artifact_path": process.artifact_path,
        "artifact_modified_at": process.artifact_modified_at.isoformat() if process.artifact_modified_at else None,
    }
    await db.execute(
        text("""
            INSERT INTO apm_deployments
                (service_id, version, env_id, deployed_at, metadata)
            VALUES (:service_id, :version, :env_id,
                    COALESCE(:modified_at, NOW()), CAST(:metadata AS jsonb))
        """),
        {"service_id": service_id,
         "version": "artifact-" + process.artifact_fingerprint[:12],
         "env_id": env[0], "modified_at": process.artifact_modified_at,
         "metadata": json.dumps(metadata)},
    )


async def _agent_from_headers(
    db: AsyncSession,
    x_agent_id: str,
    authorization: str,
) -> dict:
    return await authenticate_agent(
        x_agent_id, _strip_bearer(authorization), db
    )


@router.post("/enroll", response_model=AgentAPMEnrollResponse, status_code=201)
async def enroll_agent_apm(
    body: AgentAPMEnrollRequest,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _agent_from_headers(db, x_agent_id, authorization)
    if not agent.get("server_id"):
        raise HTTPException(409, "Agent is not attached to a server")

    env = (await db.execute(
        text("SELECT id, name FROM apm_environments WHERE name = :name"),
        {"name": body.environment},
    )).mappings().first()
    if not env:
        raise HTTPException(400, f"Unknown APM environment '{body.environment}'")

    # A call means that the host has no recoverable local APM credential.
    # Revoke any prior key before minting a replacement; the plaintext value
    # cannot and must not be recovered from Postgres.
    prior = (await db.execute(
        text("SELECT key_id FROM agent_apm_credentials WHERE agent_id = :aid"),
        {"aid": agent["id"]},
    )).first()
    if prior:
        await db.execute(
            text(
                "UPDATE apm_ingest_keys SET enabled = FALSE, revoked_at = NOW(), "
                "rotated_at = NOW() WHERE id = :kid AND revoked_at IS NULL"
            ),
            {"kid": prior[0]},
        )

    plaintext, key_hash, key_prefix = _new_ingest_key("sdk")
    row = (await db.execute(
        text(
            """
            INSERT INTO apm_ingest_keys
                (name, kind, key_hash, key_prefix, env_id, origin_allowlist)
            VALUES (:name, 'sdk', :hash, :prefix, :env, '[]'::jsonb)
            RETURNING id
            """
        ),
        {
            "name": f"ZenPlus agent {agent['id']}",
            "hash": key_hash,
            "prefix": key_prefix,
            "env": env["id"],
        },
    )).first()
    key_id = row[0]
    await db.execute(
        text(
            """
            INSERT INTO agent_apm_credentials (agent_id, key_id)
            VALUES (:aid, :kid)
            ON CONFLICT (agent_id) DO UPDATE SET
                key_id = EXCLUDED.key_id,
                rotated_at = NOW()
            """
        ),
        {"aid": agent["id"], "kid": key_id},
    )
    await db.commit()
    invalidate_ingest_key_cache()
    return AgentAPMEnrollResponse(
        key=plaintext,
        key_id=key_id,
        environment=str(env["name"]),
    )


@router.post("/discovery", response_model=DiscoveryAccepted)
async def report_discovery(
    body: DiscoveryReport,
    db: AsyncSession = Depends(get_db),
    x_agent_id: str = Header(default=""),
    authorization: str = Header(default=""),
):
    agent = await _agent_from_headers(db, x_agent_id, authorization)
    server_id = agent.get("server_id")
    if not server_id:
        raise HTTPException(409, "Agent is not attached to a server")

    # Agent 1.9+ reports a stable IIS app-pool identity. Remove legacy 1.8.x
    # w3wp rows that had PID/cmdline-derived keys so the UI does not show old
    # duplicate workers for the following 24 hours.
    if any(process.runtime == "iis" and process.iis_app_pool for process in body.processes):
        await db.execute(
            text("""
                DELETE FROM apm_agent_processes
                WHERE agent_id = :agent_id AND runtime = 'iis'
                  AND NULLIF(iis_app_pool, '') IS NULL
            """),
            {"agent_id": agent["id"]},
        )

    prior_artifacts = {
        str(row[0]): str(row[1] or "")
        for row in (await db.execute(
            text("SELECT process_key, artifact_fingerprint FROM apm_agent_processes WHERE agent_id = :agent_id"),
            {"agent_id": agent["id"]},
        )).all()
    }

    for process in body.processes:
        ports = sorted({p for p in process.listening_ports if 0 < p <= 65535})
        await db.execute(
            text(
                """
                INSERT INTO apm_agent_processes (
                    agent_id, server_id, process_key, pid, ppid, exe_path,
                    cmdline, runtime, runtime_version, service_name_guess,
                    windows_service, iis_site, iis_app_pool, listening_ports,
                    instrumentation_state, otel_detected, otel_endpoint
                    , artifact_path, artifact_fingerprint, artifact_modified_at
                ) VALUES (
                    :agent_id, :server_id, :process_key, :pid, :ppid, :exe_path,
                    :cmdline, :runtime, :runtime_version, :service_name_guess,
                    :windows_service, :iis_site, :iis_app_pool,
                    CAST(:listening_ports AS jsonb), :instrumentation_state,
                    :otel_detected, :otel_endpoint
                    , :artifact_path, :artifact_fingerprint, :artifact_modified_at
                )
                ON CONFLICT (agent_id, process_key) DO UPDATE SET
                    server_id = EXCLUDED.server_id,
                    pid = EXCLUDED.pid,
                    ppid = EXCLUDED.ppid,
                    exe_path = EXCLUDED.exe_path,
                    cmdline = EXCLUDED.cmdline,
                    runtime = EXCLUDED.runtime,
                    runtime_version = EXCLUDED.runtime_version,
                    service_name_guess = EXCLUDED.service_name_guess,
                    windows_service = EXCLUDED.windows_service,
                    iis_site = EXCLUDED.iis_site,
                    iis_app_pool = EXCLUDED.iis_app_pool,
                    listening_ports = EXCLUDED.listening_ports,
                    instrumentation_state = EXCLUDED.instrumentation_state,
                    otel_detected = EXCLUDED.otel_detected,
                    otel_endpoint = EXCLUDED.otel_endpoint,
                    artifact_path = EXCLUDED.artifact_path,
                    artifact_fingerprint = EXCLUDED.artifact_fingerprint,
                    artifact_modified_at = EXCLUDED.artifact_modified_at,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """
            ),
            {
                **process.model_dump(exclude={"listening_ports"}),
                "agent_id": agent["id"],
                "server_id": server_id,
                "listening_ports": json.dumps(ports),
            },
        )
        previous_fingerprint = prior_artifacts.get(process.process_key, "")
        if (process.windows_service and previous_fingerprint and process.artifact_fingerprint
                and previous_fingerprint != process.artifact_fingerprint):
            await _record_agent_deployment(db, agent=agent, process=process)
            await db.execute(
                text("""
                    UPDATE apm_agent_processes SET last_deployment_at = NOW()
                    WHERE agent_id = :agent_id AND process_key = :process_key
                """),
                {"agent_id": agent["id"], "process_key": process.process_key},
            )
    await db.commit()
    return DiscoveryAccepted(
        accepted=len(body.processes), reported_at=datetime.now(timezone.utc)
    )


@admin_router.get("/agent-processes")
async def list_agent_processes(
    server_id: Optional[uuid.UUID] = Query(default=None),
    active_hours: int = Query(default=24, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    del user
    rows = (await db.execute(
        text(
            """
            WITH ranked AS (
                SELECT p.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY p.agent_id,
                               COALESCE(NULLIF(p.iis_app_pool, ''),
                                        NULLIF(p.windows_service, ''), p.process_key)
                           ORDER BY p.last_seen_at DESC, p.updated_at DESC
                       ) AS row_rank
                FROM apm_agent_processes p
                WHERE p.last_seen_at >= NOW() - make_interval(hours => :active_hours)
            )
            SELECT p.*, a.hostname, a.status AS agent_status,
                   a.version AS agent_version, a.capabilities AS agent_capabilities,
                   COALESCE(NULLIF(s.display_name, ''), a.hostname) AS server_name,
                   COALESCE(host(s.primary_ip), host(a.last_ip)) AS server_ip,
                   latest.id AS last_command_id,
                   latest.status AS last_command_status,
                   latest.command AS last_command,
                   latest.params AS last_command_params,
                   latest.created_at AS last_command_created_at,
                   latest.completed_at AS last_command_completed_at,
                   result.error_message AS last_command_error,
                   result.output AS last_command_output
            FROM ranked p
            JOIN agents a ON a.id = p.agent_id
            LEFT JOIN servers s ON s.id = p.server_id
            LEFT JOIN LATERAL (
                SELECT c.id, c.status, c.command, c.params, c.created_at, c.completed_at
                FROM agent_commands c
                WHERE c.agent_id = p.agent_id
                  AND c.command IN ('apm_instrument','apm_uninstrument','apm_restart_target')
                  AND (c.params->>'process_key' = p.process_key
                     OR (NULLIF(p.iis_app_pool, '') IS NOT NULL
                         AND c.params->>'target_name' = p.iis_app_pool)
                     OR (NULLIF(p.windows_service, '') IS NOT NULL
                         AND c.params->>'target_name' = p.windows_service))
                ORDER BY c.created_at DESC LIMIT 1
            ) latest ON TRUE
            LEFT JOIN agent_command_results result ON result.command_id = latest.id
            WHERE (CAST(:server_id AS uuid) IS NULL
                   OR p.server_id = CAST(:server_id AS uuid))
              AND p.row_rank = 1
            ORDER BY p.service_name_guess, p.exe_path, p.pid
            """
        ),
        {"server_id": server_id, "active_hours": active_hours},
    )).mappings().all()
    output = [dict(row) for row in rows]

    # Verify the complete path from runtime to ClickHouse. This is deliberately
    # one bounded aggregate query for the whole fleet, never one query per row.
    def _trace_health():
        return get_ch_client().query("""
            SELECT service_name, env, max(timestamp) AS last_trace_at,
                   countIf(timestamp >= now() - INTERVAL 15 MINUTE) AS traces_15m
            FROM zenplus.apm_spans
            WHERE timestamp >= now() - INTERVAL 24 HOUR
            GROUP BY service_name, env
        """).result_rows

    trace_rows = []
    try:
        trace_rows = await asyncio.to_thread(_trace_health)
    except Exception:
        logger.warning("APM first-trace health query failed", exc_info=True)
    trace_health = {(str(r[0]), str(r[1])): (r[2], int(r[3])) for r in trace_rows}
    for item in output:
        command_params = item.get("last_command_params") or {}
        service_name = str(command_params.get("service_name") or item.get("service_name_guess") or "")
        environment = str(command_params.get("environment") or "prod")
        last_trace, traces_15m = trace_health.get((service_name, environment), (None, 0))
        item["configured_service_name"] = service_name
        item["configured_environment"] = environment
        item["last_trace_at"] = last_trace
        item["traces_15m"] = traces_15m
        if traces_15m:
            item["telemetry_status"] = "receiving"
        elif last_trace:
            item["telemetry_status"] = "stale"
        elif item.get("instrumentation_state") in {"pending", "active"}:
            item["telemetry_status"] = "waiting_for_first_trace"
        else:
            item["telemetry_status"] = "not_configured"
    return output


@admin_router.post("/agent-processes/{process_id}/instrumentation", status_code=202)
async def set_process_instrumentation(
    process_id: uuid.UUID,
    body: InstrumentationRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    process = (await db.execute(
        text("""
            SELECT p.*, a.status AS agent_status, a.capabilities
            FROM apm_agent_processes p JOIN agents a ON a.id = p.agent_id
            WHERE p.id = :process_id
        """), {"process_id": process_id},
    )).mappings().first()
    if not process:
        raise HTTPException(404, "Discovered application process not found")
    capabilities = process.get("capabilities") or []
    runtime = str(process["runtime"])
    if runtime == "iis" and process.get("iis_app_pool"):
        target_kind = "iis_app_pool"
        target_name = str(process["iis_app_pool"])
        required_capability = "apm_iis_instrumentation_v1"
        minimum_version = "1.9.0"
    elif runtime in {"dotnet", "dotnet_framework", "java", "node"} and process.get("windows_service"):
        target_kind = "windows_service"
        target_name = str(process["windows_service"])
        required_capability = "apm_windows_service_instrumentation_v1"
        minimum_version = "1.10.0"
    else:
        raise HTTPException(409, "Managed instrumentation requires an IIS pool or a dedicated .NET, Java, or Node.js Windows service")
    if required_capability not in capabilities:
        raise HTTPException(409, f"This host requires ZenPlus Agent {minimum_version} or newer for this managed runtime")

    command = "apm_instrument" if body.enabled else "apm_uninstrument"
    active = (await db.execute(
        text("""
            SELECT id FROM agent_commands
            WHERE agent_id = :agent_id
              AND command IN ('apm_instrument','apm_uninstrument','apm_restart_target')
              AND params->>'target_name' = :target_name
              AND status IN ('queued','sent','running')
              AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1
        """), {"agent_id": process["agent_id"], "target_name": target_name},
    )).first()
    if active:
        raise HTTPException(409, "An APM change is already pending for this target")

    service_name = (body.service_name or process.get("service_name_guess") or target_name).strip()
    params = {
        "process_key": process["process_key"], "runtime": runtime,
        "target_kind": target_kind, "target_name": target_name,
        "service_name": service_name, "environment": body.environment,
        "restart": body.restart,
    }
    command_id = (await db.execute(
        text("""
            INSERT INTO agent_commands
                (agent_id, command, params, expires_at, requested_by)
            VALUES (:agent_id, :command, CAST(:params AS jsonb),
                    NOW() + INTERVAL '5 minutes', :requested_by)
            RETURNING id, created_at
        """),
        {"agent_id": process["agent_id"], "command": command,
         "params": json.dumps(params), "requested_by": user.id},
    )).first()
    await db.execute(
        text("""
            UPDATE apm_agent_processes
            SET instrumentation_state = 'pending', updated_at = NOW()
            WHERE agent_id = :agent_id
              AND ((:target_kind = 'iis_app_pool' AND iis_app_pool = :target_name)
                OR (:target_kind = 'windows_service' AND windows_service = :target_name))
        """),
        {"agent_id": process["agent_id"], "target_kind": target_kind, "target_name": target_name},
    )
    await write_audit_log(
        db, actor=user,
        action="apm.instrumentation.enable" if body.enabled else "apm.instrumentation.disable",
        resource_type="apm_agent_process", resource_id=str(process_id),
        metadata={"command_id": str(command_id[0]),
                  "agent_id": str(process["agent_id"]),
                  "server_id": str(process["server_id"]),
                  "target_kind": target_kind, "target_name": target_name,
                  "restart": body.restart, "service_name": service_name,
                  "environment": body.environment},
    )
    await db.commit()
    return {
        "command_id": command_id[0], "status": "queued",
        "target_name": target_name, "enabled": body.enabled,
        "restart": body.restart,
        "detail": "The agent will apply the change on its next command poll (normally within 30 seconds).",
    }
