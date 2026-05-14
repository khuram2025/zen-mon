"""Windows credentials API (CRUD) for WMI / WinRM discovery."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, encrypt
from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.discovery_v2 import WindowsCredential
from app.models.user import User
from app.services.audit_service import write_audit_log


router = APIRouter(prefix="/windows-credentials", tags=["Windows credentials"])


AuthMethod = Literal["basic", "ntlm", "kerberos", "credssp", "certificate"]
Transport = Literal["http", "https"]


class WinCredCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    username: str = Field(..., min_length=1, max_length=150)
    domain: Optional[str] = Field(default=None, max_length=150)
    password: str = Field(..., min_length=1)
    auth_method: AuthMethod = "ntlm"
    transport: Transport = "http"
    port: int = Field(default=5985, ge=1, le=65535)
    ssl_verify: bool = False
    description: Optional[str] = None


class WinCredUpdate(BaseModel):
    name: Optional[str] = None
    username: Optional[str] = None
    domain: Optional[str] = None
    password: Optional[str] = None
    auth_method: Optional[AuthMethod] = None
    transport: Optional[Transport] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    ssl_verify: Optional[bool] = None
    description: Optional[str] = None


class WinCredResponse(BaseModel):
    id: uuid.UUID
    name: str
    username: str
    domain: Optional[str] = None
    auth_method: AuthMethod
    transport: Transport
    port: int
    ssl_verify: bool
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=list[WinCredResponse])
async def list_creds(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(select(WindowsCredential).order_by(WindowsCredential.name.asc()))
    ).scalars().all()
    return rows


@router.post("", response_model=WinCredResponse, status_code=201)
async def create_cred(
    payload: WinCredCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    existing = (
        await db.execute(select(WindowsCredential).where(WindowsCredential.name == payload.name))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Credential name already in use")
    c = WindowsCredential(
        name=payload.name,
        username=payload.username,
        domain=payload.domain or None,
        password_enc=encrypt(payload.password),
        auth_method=payload.auth_method,
        transport=payload.transport,
        port=payload.port,
        ssl_verify=payload.ssl_verify,
        description=payload.description,
        created_by=user.id,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    await write_audit_log(
        db, actor=user, action="windows_credential.create",
        resource_type="windows_credential", resource_id=str(c.id),
        metadata={"name": c.name, "username": c.username, "auth": c.auth_method},
    )
    await db.commit()
    return c


@router.patch("/{cred_id}", response_model=WinCredResponse)
async def update_cred(
    cred_id: uuid.UUID,
    payload: WinCredUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    c = await db.get(WindowsCredential, cred_id)
    if not c:
        raise HTTPException(404, "Credential not found")
    fields = payload.model_dump(exclude_unset=True)
    if "password" in fields:
        pwd = fields.pop("password")
        if pwd:
            c.password_enc = encrypt(pwd)
    for k, v in fields.items():
        setattr(c, k, v)
    c.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(c)
    await write_audit_log(
        db, actor=user, action="windows_credential.update",
        resource_type="windows_credential", resource_id=str(c.id),
        metadata={"fields": list(fields.keys())},
    )
    await db.commit()
    return c


@router.delete("/{cred_id}", status_code=204)
async def delete_cred(
    cred_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    c = await db.get(WindowsCredential, cred_id)
    if not c:
        raise HTTPException(404, "Credential not found")
    name = c.name
    await db.delete(c)
    await db.commit()
    await write_audit_log(
        db, actor=user, action="windows_credential.delete",
        resource_type="windows_credential", resource_id=str(cred_id),
        metadata={"name": name},
    )
    await db.commit()


@router.post("/{cred_id}/test")
async def test_cred(
    cred_id: uuid.UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_operator_user),
):
    """Run a WinRM connection test against a user-provided IP."""
    from app.services.discovery_probes import winrm_probe
    c = await db.get(WindowsCredential, cred_id)
    if not c:
        raise HTTPException(404, "Credential not found")
    ip = (payload.get("ip") or "").strip()
    if not ip:
        raise HTTPException(400, "ip is required")
    try:
        password = decrypt(c.password_enc) if c.password_enc else ""
    except Exception:
        password = ""
    cred = {
        "id": str(c.id),
        "username": c.username,
        "domain": c.domain,
        "password": password,
        "auth_method": c.auth_method,
        "transport": c.transport,
        "port": c.port,
        "ssl_verify": c.ssl_verify,
    }
    r = await winrm_probe(ip, cred, timeout_s=6.0)
    return {
        "ok": bool(r.get("responsive")),
        "state": r.get("state"),
        "error": r.get("error"),
        "info": r.get("data") if r.get("responsive") else None,
    }
