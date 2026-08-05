"""Agent-facing APM control plane.

The host agent authenticates with its existing ``zpa_`` credential. This
router mints an agent-scoped ``zpi_`` ingest key and accepts the process/runtime
inventory produced by the local APM worker. Plaintext ingest keys are returned
only by the enrollment response and are never stored by the controller.
"""

from __future__ import annotations

import json
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
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User


router = APIRouter(prefix="/agents/apm", tags=["Agent APM"])
admin_router = APIRouter(prefix="/apm", tags=["APM agent discovery"])

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


class DiscoveryReport(BaseModel):
    processes: list[DiscoveredProcess] = Field(default_factory=list, max_length=5000)


class DiscoveryAccepted(BaseModel):
    accepted: int
    reported_at: datetime


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
                ) VALUES (
                    :agent_id, :server_id, :process_key, :pid, :ppid, :exe_path,
                    :cmdline, :runtime, :runtime_version, :service_name_guess,
                    :windows_service, :iis_site, :iis_app_pool,
                    CAST(:listening_ports AS jsonb), :instrumentation_state,
                    :otel_detected, :otel_endpoint
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
            SELECT p.*, a.hostname, a.status AS agent_status
            FROM apm_agent_processes p
            JOIN agents a ON a.id = p.agent_id
            WHERE (:server_id IS NULL OR p.server_id = :server_id)
              AND p.last_seen_at >= NOW() - make_interval(hours => :active_hours)
            ORDER BY p.service_name_guess, p.exe_path, p.pid
            """
        ),
        {"server_id": server_id, "active_hours": active_hours},
    )).mappings().all()
    return [dict(row) for row in rows]
