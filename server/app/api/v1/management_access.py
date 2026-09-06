import asyncio
import ipaddress
import json
import os
from pathlib import Path
import subprocess
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.management_access import load_policy, address_allowed
from app.services.audit_service import write_audit_log

router = APIRouter(prefix='/system/security/access', tags=['Security Settings'])
HELPER = '/usr/local/sbin/zenplus-access-helper'
STAGING = Path('/opt/zenplus/data/access-staging/policy.json')

class AccessPolicy(BaseModel):
    web_restricted: bool = False
    ssh_restricted: bool = False
    allowed_cidrs: list[str] = Field(default_factory=list, max_length=50)

    @field_validator('allowed_cidrs')
    @classmethod
    def networks(cls, values):
        return list(dict.fromkeys(str(ipaddress.ip_network(value.strip(), strict=False)) for value in values))

    @model_validator(mode='after')
    def require_networks(self):
        if (self.web_restricted or self.ssh_restricted) and not self.allowed_cidrs:
            raise ValueError('Add an allowed IP or subnet before enabling restrictions')
        return self

@router.get('')
async def get_access(request: Request, user: User = Depends(require_admin_user)):
    return load_policy() | {'current_ip': request.client.host if request.client else None, 'helper_installed': Path(HELPER).is_file()}

@router.put('')
async def set_access(data: AccessPolicy, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    source = request.client.host if request.client else ''
    if (data.web_restricted or data.ssh_restricted) and not address_allowed(source, data.allowed_cidrs):
        raise HTTPException(400, 'Include your current source IP or subnet before enabling restrictions')
    try:
        result = await asyncio.to_thread(_apply_policy, data.model_dump())
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(500, 'Access policy could not be applied; check the appliance security log')
    if result.returncode:
        raise HTTPException(500, 'Access policy could not be applied; check the appliance security log')
    await write_audit_log(db, actor=user, action='security.access.update', resource_type='settings', metadata=data.model_dump() | {'source_ip':source})
    await db.commit()
    return data.model_dump() | {'current_ip':source,'helper_installed':True}


def _apply_policy(policy):
    import fcntl
    STAGING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(STAGING.parent / 'apply.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd = os.open(STAGING, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(policy, f)
            return subprocess.run(['sudo', '-n', HELPER, 'apply'], capture_output=True, text=True, timeout=120)
        finally:
            STAGING.unlink(missing_ok=True)
