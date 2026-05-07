"""SNMP Credentials CRUD — reusable credential sets for devices, groups, discovery.

Routes:
    GET    /api/v1/snmp-credentials              list all credentials
    GET    /api/v1/snmp-credentials/{id}         get one credential (with usage stats)
    POST   /api/v1/snmp-credentials              create a credential
    PUT    /api/v1/snmp-credentials/{id}         update a credential
    DELETE /api/v1/snmp-credentials/{id}         delete (unlinks devices/groups first)
    POST   /api/v1/snmp-credentials/{id}/assign  bulk assign to devices/groups
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/snmp-credentials", tags=["SNMP Credentials"])


# ── Schemas ──────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    snmp_version: str = Field(default="2c", pattern="^(1|2c|3)$")
    # v1/v2c
    community: Optional[str] = None
    # v3
    v3_username: Optional[str] = None
    v3_context: Optional[str] = None
    v3_security_level: Optional[str] = Field(default="authPriv")
    v3_auth_protocol: Optional[str] = None
    v3_auth_passphrase: Optional[str] = None
    v3_priv_protocol: Optional[str] = None
    v3_priv_passphrase: Optional[str] = None
    # connection
    port: int = Field(default=161, ge=1, le=65535)
    timeout_ms: int = Field(default=2000, ge=200, le=30000)
    retries: int = Field(default=2, ge=0, le=10)
    is_default: bool = False


class CredentialUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    snmp_version: Optional[str] = Field(default=None, pattern="^(1|2c|3)$")
    community: Optional[str] = None
    v3_username: Optional[str] = None
    v3_context: Optional[str] = None
    v3_security_level: Optional[str] = None
    v3_auth_protocol: Optional[str] = None
    v3_auth_passphrase: Optional[str] = None
    v3_priv_protocol: Optional[str] = None
    v3_priv_passphrase: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    timeout_ms: Optional[int] = Field(default=None, ge=200, le=30000)
    retries: Optional[int] = Field(default=None, ge=0, le=10)
    is_default: Optional[bool] = None


class CredentialResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    snmp_version: str
    community: Optional[str]
    v3_username: Optional[str]
    v3_context: Optional[str]
    v3_security_level: Optional[str]
    v3_auth_protocol: Optional[str]
    has_auth_passphrase: bool = False
    v3_priv_protocol: Optional[str]
    has_priv_passphrase: bool = False
    port: int
    timeout_ms: int
    retries: int
    is_default: bool
    device_count: int = 0
    group_count: int = 0
    created_at: datetime
    updated_at: datetime


class AssignRequest(BaseModel):
    device_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────

def _row_to_response(r: dict) -> CredentialResponse:
    return CredentialResponse(
        id=str(r["id"]),
        name=r["name"],
        description=r.get("description"),
        snmp_version=r["snmp_version"],
        community=r.get("community"),
        v3_username=r.get("v3_username"),
        v3_context=r.get("v3_context"),
        v3_security_level=r.get("v3_security_level"),
        v3_auth_protocol=r.get("v3_auth_protocol"),
        has_auth_passphrase=bool(r.get("v3_auth_passphrase")),
        v3_priv_protocol=r.get("v3_priv_protocol"),
        has_priv_passphrase=bool(r.get("v3_priv_passphrase")),
        port=r.get("port", 161),
        timeout_ms=r.get("timeout_ms", 2000),
        retries=r.get("retries", 2),
        is_default=r.get("is_default", False),
        device_count=r.get("device_count", 0),
        group_count=r.get("group_count", 0),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


_LIST_SQL = """
    SELECT c.*,
           COALESCE(d.cnt, 0) AS device_count,
           COALESCE(g.cnt, 0) AS group_count
    FROM snmp_credentials c
    LEFT JOIN (SELECT snmp_credential_id, COUNT(*) cnt FROM devices WHERE snmp_credential_id IS NOT NULL GROUP BY snmp_credential_id) d
        ON d.snmp_credential_id = c.id
    LEFT JOIN (SELECT snmp_credential_id, COUNT(*) cnt FROM device_groups WHERE snmp_credential_id IS NOT NULL GROUP BY snmp_credential_id) g
        ON g.snmp_credential_id = c.id
"""


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("", response_model=list[CredentialResponse])
async def list_credentials(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    rows = (await db.execute(text(f"{_LIST_SQL} ORDER BY c.is_default DESC, c.name"))).mappings().all()
    return [_row_to_response(dict(r)) for r in rows]


@router.get("/{cred_id}", response_model=CredentialResponse)
async def get_credential(
    cred_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    row = (await db.execute(text(f"{_LIST_SQL} WHERE c.id = :id"), {"id": cred_id})).mappings().first()
    if not row:
        raise HTTPException(404, "Credential not found")
    return _row_to_response(dict(row))


@router.post("", response_model=CredentialResponse, status_code=201)
async def create_credential(
    data: CredentialCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    # If setting as default, clear any existing default
    if data.is_default:
        await db.execute(text("UPDATE snmp_credentials SET is_default = FALSE WHERE is_default = TRUE"))

    row = (await db.execute(
        text("""
            INSERT INTO snmp_credentials (
                name, description, snmp_version, community,
                v3_username, v3_context, v3_security_level,
                v3_auth_protocol, v3_auth_passphrase,
                v3_priv_protocol, v3_priv_passphrase,
                port, timeout_ms, retries, is_default, created_by
            ) VALUES (
                :name, :desc, :ver, :community,
                :v3user, :v3ctx, :v3sec,
                :v3auth, :v3authpw,
                :v3priv, :v3privpw,
                :port, :timeout, :retries, :is_default, :uid
            )
            RETURNING *
        """),
        {
            "name": data.name, "desc": data.description, "ver": data.snmp_version,
            "community": data.community,
            "v3user": data.v3_username, "v3ctx": data.v3_context,
            "v3sec": data.v3_security_level if data.snmp_version == "3" else None,
            "v3auth": data.v3_auth_protocol, "v3authpw": data.v3_auth_passphrase,
            "v3priv": data.v3_priv_protocol, "v3privpw": data.v3_priv_passphrase,
            "port": data.port, "timeout": data.timeout_ms, "retries": data.retries,
            "is_default": data.is_default, "uid": user.id,
        },
    )).mappings().first()
    await write_audit_log(
        db,
        actor=user,
        action="snmp_credential.create",
        resource_type="snmp_credential",
        resource_id=str(row["id"]) if row else None,
        metadata={"name": data.name, "snmp_version": data.snmp_version, "is_default": data.is_default},
    )
    await db.commit()
    return _row_to_response(dict(row))


@router.put("/{cred_id}", response_model=CredentialResponse)
async def update_credential(
    cred_id: uuid.UUID,
    data: CredentialUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    existing = (await db.execute(text("SELECT id FROM snmp_credentials WHERE id = :id"), {"id": cred_id})).first()
    if not existing:
        raise HTTPException(404, "Credential not found")

    sets = ["updated_at = NOW()"]
    params: dict = {"id": cred_id}

    field_map = {
        "name": "name", "description": "description", "snmp_version": "snmp_version",
        "community": "community", "v3_username": "v3_username", "v3_context": "v3_context",
        "v3_security_level": "v3_security_level",
        "v3_auth_protocol": "v3_auth_protocol", "v3_auth_passphrase": "v3_auth_passphrase",
        "v3_priv_protocol": "v3_priv_protocol", "v3_priv_passphrase": "v3_priv_passphrase",
        "port": "port", "timeout_ms": "timeout_ms", "retries": "retries",
        "is_default": "is_default",
    }
    update_data = data.model_dump(exclude_unset=True)
    for py_field, db_col in field_map.items():
        if py_field in update_data:
            sets.append(f"{db_col} = :{py_field}")
            params[py_field] = update_data[py_field]

    if data.is_default:
        await db.execute(text("UPDATE snmp_credentials SET is_default = FALSE WHERE is_default = TRUE AND id != :id"), {"id": cred_id})

    await db.execute(text(f"UPDATE snmp_credentials SET {', '.join(sets)} WHERE id = :id"), params)
    audit_fields = sorted(k for k in update_data.keys() if "passphrase" not in k and k != "community")
    await write_audit_log(
        db,
        actor=user,
        action="snmp_credential.update",
        resource_type="snmp_credential",
        resource_id=str(cred_id),
        metadata={"fields": audit_fields},
    )
    await db.commit()
    return await get_credential(cred_id, db, user)


@router.delete("/{cred_id}", status_code=204)
async def delete_credential(
    cred_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    existing = (await db.execute(text("SELECT id FROM snmp_credentials WHERE id = :id"), {"id": cred_id})).first()
    if not existing:
        raise HTTPException(404, "Credential not found")
    # Unlink
    await db.execute(text("UPDATE devices SET snmp_credential_id = NULL WHERE snmp_credential_id = :id"), {"id": cred_id})
    await db.execute(text("UPDATE device_groups SET snmp_credential_id = NULL WHERE snmp_credential_id = :id"), {"id": cred_id})
    await db.execute(text("DELETE FROM snmp_credentials WHERE id = :id"), {"id": cred_id})
    await write_audit_log(
        db,
        actor=user,
        action="snmp_credential.delete",
        resource_type="snmp_credential",
        resource_id=str(cred_id),
    )
    await db.commit()


@router.post("/{cred_id}/assign")
async def assign_credential(
    cred_id: uuid.UUID,
    data: AssignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    existing = (await db.execute(text("SELECT id FROM snmp_credentials WHERE id = :id"), {"id": cred_id})).first()
    if not existing:
        raise HTTPException(404, "Credential not found")

    d_count = 0
    g_count = 0

    if data.device_ids:
        uuids = [uuid.UUID(d) for d in data.device_ids]
        res = await db.execute(
            text("UPDATE devices SET snmp_credential_id = :cid WHERE id = ANY(:ids)"),
            {"cid": cred_id, "ids": uuids},
        )
        d_count = res.rowcount

    if data.group_ids:
        uuids = [uuid.UUID(g) for g in data.group_ids]
        res = await db.execute(
            text("UPDATE device_groups SET snmp_credential_id = :cid WHERE id = ANY(:ids)"),
            {"cid": cred_id, "ids": uuids},
        )
        g_count = res.rowcount

    await write_audit_log(
        db,
        actor=user,
        action="snmp_credential.assign",
        resource_type="snmp_credential",
        resource_id=str(cred_id),
        metadata={"assigned_devices": d_count, "assigned_groups": g_count},
    )
    await db.commit()
    return {"assigned_devices": d_count, "assigned_groups": g_count}


class CredentialSecrets(BaseModel):
    community: Optional[str] = None
    v3_auth_passphrase: Optional[str] = None
    v3_priv_passphrase: Optional[str] = None


@router.get("/{cred_id}/secrets", response_model=CredentialSecrets)
async def get_credential_secrets(
    cred_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """Reveal the stored community/passphrases for an authenticated user.

    Used by the edit dialog so admins can review what they previously saved.
    Passphrases on snmp_credentials are stored as plaintext (see
    device_service._apply_credential), so no decryption step is needed here.
    """
    row = (await db.execute(
        text("SELECT community, v3_auth_passphrase, v3_priv_passphrase "
             "FROM snmp_credentials WHERE id = :id"),
        {"id": cred_id},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "Credential not found")
    await write_audit_log(
        db,
        actor=user,
        action="snmp_credential.secrets_view",
        resource_type="snmp_credential",
        resource_id=str(cred_id),
    )
    await db.commit()
    return CredentialSecrets(
        community=row.get("community"),
        v3_auth_passphrase=row.get("v3_auth_passphrase"),
        v3_priv_passphrase=row.get("v3_priv_passphrase"),
    )


@router.get("/{cred_id}/usage")
async def credential_usage(
    cred_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin_user),
):
    """List devices and groups using this credential."""
    devices = (await db.execute(
        text("SELECT id, hostname, host(ip_address)::text AS ip_address FROM devices WHERE snmp_credential_id = :id ORDER BY hostname"),
        {"id": cred_id},
    )).mappings().all()

    groups = (await db.execute(
        text("SELECT id, name FROM device_groups WHERE snmp_credential_id = :id ORDER BY name"),
        {"id": cred_id},
    )).mappings().all()

    return {
        "devices": [dict(d) for d in devices],
        "groups": [dict(g) for g in groups],
    }
