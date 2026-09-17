"""Network Configuration Management (NCM).

Validated, encrypted configuration history, scoped access and change events.
Slice 2 (professional): connection profiles (CLI credentials), per-device
enrollment, and REAL SSH config retrieval via netmiko, plus a manual paste
fallback. Per-device routes are under /devices/{id}/..., fleet + credential
management under /ncm/...
"""
import asyncio
import hashlib
import difflib
import json
import re
import time
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission, user_has_permission
from app.core.scoping import visible_tags, jsonb_tags_visible
from app.services import ncm_events
from app.services.ncm_content import normalize_config as _normalize_config, read_content, encrypt_content, checked_content, config_diff, redact_config
from app.services.ncm_collection import PLATFORM_COMMANDS, STARTUP_COMMANDS, fetch_config as _netmiko_fetch, CaptureError
from app.core import crypto
from app.models.user import User

router = APIRouter(prefix="/ncm", tags=["NCM"])
device_router = APIRouter(prefix="/devices", tags=["NCM"])

# Friendly labels offered in the UI; value is the netmiko device_type.
SUPPORTED_PLATFORMS = [
    {"value": "autodetect", "label": "Auto-detect"},
    {"value": "cisco_ios", "label": "Cisco IOS / IOS-XE"},
    {"value": "cisco_nxos", "label": "Cisco NX-OS"},
    {"value": "cisco_asa", "label": "Cisco ASA"},
    {"value": "arista_eos", "label": "Arista EOS"},
    {"value": "juniper_junos", "label": "Juniper Junos"},
    {"value": "paloalto_panos", "label": "Palo Alto PAN-OS"},
    {"value": "fortinet", "label": "Fortinet FortiOS"},
    {"value": "hp_comware", "label": "HPE Comware"},
    {"value": "huawei", "label": "Huawei VRP"},
]


async def _device_access(db, user, device_id):
    scope = await visible_tags(db, user)
    predicate = ' AND ' + jsonb_tags_visible('tags') if scope else ''
    row = (await db.execute(text('SELECT id FROM devices WHERE id=:id' + predicate),
                            {'id': device_id, 'vis_tags': scope})).first()
    if not row:
        raise HTTPException(404, 'Device not found')


async def _bulk_access(db, user, ids):
    # Validate the entire selection before the first mutation.
    for device_id in set(ids):
        await _device_access(db, user, device_id)


async def _global_credentials_access(db, user):
    if await visible_tags(db, user):
        raise HTTPException(403, 'Shared credential administration requires unrestricted device scope')


async def _profile_access(db, user, credential_id):
    if credential_id is None:
        return
    profiles = await list_credentials(db, user)
    if str(credential_id) not in {r['id'] for r in profiles['data']}:
        raise HTTPException(404, 'Connection profile not found in your scope')


async def _assurance_settings(db, user, device_id, data):
    names = {'notify_channels', 'freshness_hours', 'eligible_override', 'config_types', 'schedule_timezone'}
    updates = {k:getattr(data,k) for k in names & data.model_fields_set}
    if 'notify_channels' in updates:
        from app.services.ncm_delivery import SUPPORTED_CHANNELS
        for channel in updates['notify_channels']:
            row = (await db.execute(text('SELECT type FROM notification_channels WHERE id=:id AND enabled'), {'id':channel})).first()
            if not row or row.type not in SUPPORTED_CHANNELS:
                raise HTTPException(422, 'Select an enabled NCM-supported notification channel')
    if updates:
        sets = ','.join(f'{k}=:{k}' for k in sorted(updates))
        await db.execute(text(f'UPDATE device_ncm SET {sets} WHERE device_id=:device'), {'device':device_id, **updates})
    configured=(await db.execute(text('SELECT platform,config_types FROM device_ncm WHERE device_id=:id'),{'id':device_id})).first()
    if configured and 'startup' in configured.config_types and configured.platform not in ('autodetect',*STARTUP_COMMANDS):
        raise HTTPException(422,'Selected driver does not support startup configuration capture')
    await ncm_events.audit(db,user,'device.settings',device_id=device_id)


async def _save_config_version(db, device_id, config_type, content, captured_by, source_note,
                               *, actor=None, validated=False):
    content = checked_content(content)
    chash = hashlib.sha256(content.encode()).hexdigest()
    nhash = hashlib.sha256(_normalize_config(content).encode()).hexdigest()
    latest = (await db.execute(text("""SELECT id,content_hash,norm_hash,content,content_enc,
        captured_at,validated FROM device_configs WHERE device_id=:d AND config_type=:t
        ORDER BY captured_at DESC,id DESC LIMIT 1"""), {'d':device_id,'t':config_type})).first()
    prior = read_content(latest) if latest else None
    is_change = latest is None or _normalize_config(prior) != _normalize_config(content)
    if latest and latest.content_hash == chash and (latest.validated or not validated):
        return {'is_change':False,'version_id':str(latest.id),'captured_at':latest.captured_at.isoformat(),
                'deduplicated':True,'validated':bool(latest.validated)}
    row = (await db.execute(text("""INSERT INTO device_configs
        (device_id,config_type,content,content_enc,content_hash,norm_hash,size_bytes,line_count,
         captured_by,source_note,is_change,actor_id,validated,driver_version)
        VALUES (:d,:t,'',:encrypted,:h,:nh,:sz,:lc,:by,:note,:ic,:actor,:validated,'ncm-2')
        RETURNING id,captured_at"""),
        {'d':device_id,'t':config_type,'encrypted':encrypt_content(content),'h':chash,'nh':nhash,
         'sz':len(content.encode()),'lc':len(content.splitlines()),'by':captured_by,
         'note':source_note,'ic':is_change,'actor':getattr(actor,'id',None),'validated':validated})).first()
    if latest and is_change:
        enabled = (await db.execute(text('SELECT alert_on_change FROM device_ncm WHERE device_id=:d'),{'d':device_id})).scalar()
        if enabled:
            summary = config_diff(prior,content)
            await ncm_events.event(db,device_id,'config.changed',str(row.id),
                metadata={'config_type':config_type,'from_version':str(latest.id),'to_version':str(row.id),
                          'added':summary['added'],'removed':summary['removed'],
                          'sensitive_changed':summary['sensitive_changed']},
                message=f'{config_type.capitalize()} configuration changed')
    keep = (await db.execute(text('SELECT keep_versions FROM device_ncm WHERE device_id=:d'),{'d':device_id})).scalar() or 5
    # Retain N changed configurations AND the latest snapshot. Unchanged runs
    # cannot evict meaningful history; approved/pinned recovery points survive.
    await db.execute(text("""DELETE FROM device_configs WHERE device_id=:d AND config_type=:t
        AND NOT pinned AND id<>:latest
        AND NOT EXISTS(SELECT 1 FROM ncm_baselines b WHERE b.version_id=device_configs.id)
        AND id NOT IN (SELECT id FROM device_configs WHERE device_id=:d AND config_type=:t
                      AND validated ORDER BY captured_at DESC,id DESC LIMIT 1)
        AND id NOT IN
        (SELECT id FROM device_configs WHERE device_id=:d AND config_type=:t AND is_change
         ORDER BY captured_at DESC,id DESC LIMIT :keep)"""),
        {'d':device_id,'t':config_type,'latest':row.id,'keep':keep})
    await ncm_events.audit(db,actor,'config.capture',device_id=device_id,resource_id=row.id,
                           metadata={'config_type':config_type,'validated':validated,'is_change':is_change})
    return {'is_change':is_change,'version_id':str(row.id),'captured_at':row.captured_at.isoformat(),
            'validated':validated,'deduplicated':False}


# --------------------------------------------------------------------------- #
# Connection profiles (CLI credentials)
# --------------------------------------------------------------------------- #
class CredentialIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    protocol: str = Field(default="ssh", pattern="^ssh$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=120)
    password: Optional[str] = None
    enable_password: Optional[str] = None
    is_default: bool = False


@router.get("/platforms")
async def list_platforms(user: User = Depends(require_permission("ncm.view"))):
    return {"data": SUPPORTED_PLATFORMS}


@router.get("/credentials")
async def list_credentials(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view"))):
    scope = await visible_tags(db, user)
    allowed = " AND " + jsonb_tags_visible('d.tags') if scope else ''
    # Scoped users can reuse only profiles already assigned to visible devices.
    restriction = f"WHERE EXISTS (SELECT 1 FROM device_ncm n JOIN devices d ON d.id=n.device_id WHERE n.credential_id=c.id {allowed})" if scope else ''
    rows = (await db.execute(text(f"""
        SELECT c.id,c.name,c.description,c.protocol,c.port,c.username,c.is_default,
          (c.password_enc IS NOT NULL) AS has_password,
          (c.enable_password_enc IS NOT NULL) AS has_enable,
          (SELECT count(*) FROM device_ncm n JOIN devices d ON d.id=n.device_id
           WHERE n.credential_id=c.id {allowed}) AS used_by
        FROM ncm_credentials c {restriction} ORDER BY c.is_default DESC,c.name
    """), {'vis_tags':scope})).fetchall()
    return {"data": [{
        "id": str(r.id), "name": r.name, "description": r.description,
        "protocol": r.protocol, "port": r.port, "username": r.username,
        "is_default": r.is_default, "has_password": r.has_password,
        "has_enable": r.has_enable, "used_by": r.used_by,
    } for r in rows]}


@router.post("/credentials", status_code=201)
async def create_credential(data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    await _global_credentials_access(db,user)
    row = (await db.execute(
        text("""INSERT INTO ncm_credentials
                (name, description, protocol, port, username, password_enc, enable_password_enc, is_default)
                VALUES (:n, :d, :p, :port, :u, :pw, :en, :def)
                RETURNING id"""),
        {"n": data.name, "d": data.description, "p": data.protocol, "port": data.port,
         "u": data.username, "pw": crypto.encrypt(data.password),
         "en": crypto.encrypt(data.enable_password), "def": data.is_default},
    )).first()
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": row.id})
    await ncm_events.audit(db,user,"credential.create",resource_id=row.id)
    await db.commit()
    return {"id": str(row.id)}


@router.put("/credentials/{cred_id}")
async def update_credential(cred_id: UUID, data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    await _global_credentials_access(db,user)
    sets = ["name=:n", "description=:d", "protocol=:p", "port=:port", "username=:u",
            "is_default=:def", "updated_at=NOW()"]
    params = {"id": cred_id, "n": data.name, "d": data.description, "p": data.protocol,
              "port": data.port, "u": data.username, "def": data.is_default}
    # Only overwrite secrets when a new value is supplied.
    if data.password is not None:
        sets.append("password_enc=:pw"); params["pw"] = crypto.encrypt(data.password)
    if data.enable_password is not None:
        sets.append("enable_password_enc=:en"); params["en"] = crypto.encrypt(data.enable_password)
    r = (await db.execute(text(f"UPDATE ncm_credentials SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)).first()
    if not r:
        raise HTTPException(status_code=404, detail="Credential not found")
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": cred_id})
    await ncm_events.audit(db,user,"credential.update",resource_id=cred_id)
    await db.commit()
    return {"id": str(cred_id)}


@router.delete("/credentials/{cred_id}", status_code=204)
async def delete_credential(cred_id: UUID, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    await _global_credentials_access(db,user)
    r = (await db.execute(text("DELETE FROM ncm_credentials WHERE id=:id RETURNING id"), {"id": cred_id})).first()
    await ncm_events.audit(db,user,"credential.delete",resource_id=cred_id)
    await db.commit()
    if not r:
        raise HTTPException(status_code=404, detail="Credential not found")


# --------------------------------------------------------------------------- #
# Device enrollment
# --------------------------------------------------------------------------- #
class NcmEnroll(BaseModel):
    credential_id: Optional[UUID] = None
    platform: str = Field(default="autodetect", max_length=40)
    enabled: bool = True
    schedule_enabled: bool = False
    schedule_type: str = Field(default="interval", pattern="^(interval|daily|weekly)$")
    schedule_interval_hours: int = Field(default=24, ge=1, le=720)
    schedule_time: Optional[str] = None          # "HH:MM" for daily/weekly
    schedule_days: Optional[list[int]] = None     # weekly: 0=Sun..6=Sat
    keep_versions: int = Field(default=5, ge=1, le=100)
    alert_on_change: bool = True
    schedule_timezone: str = 'UTC'

    @field_validator('schedule_timezone')
    @classmethod
    def valid_timezone(cls,value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):
            raise ValueError('Unknown IANA timezone') from None
        return value

    @field_validator('platform')
    @classmethod
    def supported_platform(cls, value):
        if value is not None and value not in {'autodetect', *PLATFORM_COMMANDS}:
            raise ValueError('Unsupported device platform')
        return value
    notify_channels: list[UUID] = Field(default_factory=list, max_length=20)
    freshness_hours: int = Field(default=24, ge=1, le=720)
    eligible_override: Optional[bool] = None
    config_types: list[str] = Field(default_factory=lambda: ['running'], min_length=1, max_length=2)

    @model_validator(mode='after')
    def valid_schedule(self):
        if self.schedule_days is not None and (len(set(self.schedule_days)) != len(self.schedule_days) or
                any(day not in range(7) for day in self.schedule_days)):
            raise ValueError('Schedule days must be unique values from 0 to 6')
        if self.schedule_type in ('daily','weekly') and self.schedule_enabled:
            if not self.schedule_time or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',self.schedule_time):
                raise ValueError('A valid 24-hour schedule time is required')
            if self.schedule_type == 'weekly' and not self.schedule_days:
                raise ValueError('Select at least one scheduled day')
        if len(set(self.config_types))!=len(self.config_types) or any(t not in ('running','startup') for t in self.config_types):
            raise ValueError('Unsupported or duplicate configuration type')
        return self




@device_router.put("/{device_id}/ncm")
async def enroll_device(device_id: UUID, data: NcmEnroll, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("ncm.manage"))):
    await _device_access(db,user,device_id)
    await _profile_access(db,user,data.credential_id)
    dev = (await db.execute(text("SELECT id FROM devices WHERE id=:id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    days = data.schedule_days if data.schedule_type == "weekly" else None
    sched_time = None
    if data.schedule_type in ("daily", "weekly") and data.schedule_time:
        try:
            sched_time = datetime.strptime(data.schedule_time.strip(), "%H:%M").time()
        except ValueError:
            sched_time = None
    await db.execute(
        text("""INSERT INTO device_ncm
                  (device_id, credential_id, platform, enabled, schedule_enabled, schedule_type,
                   schedule_interval_hours, schedule_time, schedule_days, keep_versions, alert_on_change)
                VALUES (:d, :c, :p, :e, :s, :st, :h, :tm, :days, :kv, :ac)
                ON CONFLICT (device_id) DO UPDATE
                  SET credential_id=:c, platform=:p, enabled=:e, schedule_enabled=:s, schedule_type=:st,
                      schedule_interval_hours=:h, schedule_time=:tm, schedule_days=:days,
                      keep_versions=:kv, alert_on_change=:ac"""),
        {"d": device_id, "c": data.credential_id, "p": data.platform,
         "e": data.enabled, "s": data.schedule_enabled, "st": data.schedule_type,
         "h": data.schedule_interval_hours, "tm": sched_time, "days": days,
         "kv": data.keep_versions, "ac": data.alert_on_change},
    )
    for target_id in [device_id]:
        await _assurance_settings(db,user,target_id,data)
    await db.commit()
    return {"device_id": str(device_id), "enrolled": True}


@device_router.delete("/{device_id}/ncm", status_code=204)
async def unenroll_device(device_id: UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("ncm.manage"))):
    await _device_access(db,user,device_id)
    await db.execute(text("DELETE FROM device_ncm WHERE device_id=:d"), {"d": device_id})
    await ncm_events.audit(db,user,"device.unenroll",device_id=device_id)
    await db.commit()


class BulkAssign(BaseModel):
    device_ids: list[UUID] = Field(min_length=1, max_length=500)
    credential_id: UUID
    platform: Optional[str] = Field(default=None, max_length=40)

    @field_validator('platform')
    @classmethod
    def supported_platform(cls, value):
        if value is not None and value not in {'autodetect', *PLATFORM_COMMANDS}:
            raise ValueError('Unsupported device platform')
        return value



@router.post("/bulk-assign")
async def bulk_assign_profile(data: BulkAssign, db: AsyncSession = Depends(get_db),
                              user: User = Depends(require_permission("ncm.manage"))):
    await _bulk_access(db,user,data.device_ids)
    await _profile_access(db,user,data.credential_id)
    """Assign a connection profile to many devices at once and enable backup.
    Enrolls devices that are not yet configured (with defaults) and updates the
    credential (and optionally the platform) on those already enrolled, without
    disturbing their existing schedule / retention settings."""
    cred = (await db.execute(
        text("SELECT id FROM ncm_credentials WHERE id=:c"), {"c": data.credential_id})).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Connection profile not found")
    assigned = 0
    for did in data.device_ids:
        dev = (await db.execute(text("SELECT id FROM devices WHERE id=:id"), {"id": did})).first()
        if not dev:
            continue
        await db.execute(
            text("""INSERT INTO device_ncm (device_id, credential_id, platform, enabled)
                    VALUES (:d, :c, COALESCE(:p, 'autodetect'), true)
                    ON CONFLICT (device_id) DO UPDATE
                      SET credential_id = :c,
                          platform = COALESCE(:p, device_ncm.platform),
                          enabled = true"""),
            {"d": did, "c": data.credential_id, "p": data.platform},
        )
        await ncm_events.audit(db,user,"device.assign_profile",device_id=did)
        assigned += 1
    await db.commit()
    return {"assigned": assigned, "requested": len(data.device_ids)}


class BulkBackupSettings(BaseModel):
    device_ids: list[UUID] = Field(min_length=1, max_length=500)
    schedule_enabled: bool = False
    schedule_type: str = Field(default="interval", pattern="^(interval|daily|weekly)$")
    schedule_interval_hours: int = Field(default=24, ge=1, le=720)
    schedule_time: Optional[str] = None          # "HH:MM" for daily/weekly
    schedule_days: Optional[list[int]] = None     # weekly: 0=Sun..6=Sat
    keep_versions: int = Field(default=5, ge=1, le=100)
    alert_on_change: bool = True
    notify_channels: list[UUID] = Field(default_factory=list, max_length=20)
    freshness_hours: int = Field(default=24, ge=1, le=720)
    eligible_override: Optional[bool] = None
    config_types: list[str] = Field(default_factory=lambda: ['running'], min_length=1, max_length=2)

    @model_validator(mode='after')
    def valid_schedule(self):
        if self.schedule_days is not None and (len(set(self.schedule_days)) != len(self.schedule_days) or
                any(day not in range(7) for day in self.schedule_days)):
            raise ValueError('Schedule days must be unique values from 0 to 6')
        if self.schedule_type in ('daily','weekly') and self.schedule_enabled:
            if not self.schedule_time or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',self.schedule_time):
                raise ValueError('A valid 24-hour schedule time is required')
            if self.schedule_type == 'weekly' and not self.schedule_days:
                raise ValueError('Select at least one scheduled day')
        if len(set(self.config_types))!=len(self.config_types) or any(t not in ('running','startup') for t in self.config_types):
            raise ValueError('Unsupported or duplicate configuration type')
        return self



@router.post("/bulk-settings")
async def bulk_backup_settings(data: BulkBackupSettings, db: AsyncSession = Depends(get_db),
                               user: User = Depends(require_permission("ncm.manage"))):
    await _bulk_access(db,user,data.device_ids)
    """Apply schedule / retention / alert settings to many enrolled devices."""
    days = data.schedule_days if data.schedule_type == "weekly" else None
    sched_time = None
    if data.schedule_type in ("daily", "weekly") and data.schedule_time:
        try:
            sched_time = datetime.strptime(data.schedule_time.strip(), "%H:%M").time()
        except ValueError:
            sched_time = None

    updated = 0
    skipped = 0
    for did in data.device_ids:
        row = (await db.execute(
            text("SELECT device_id FROM device_ncm WHERE device_id=:id"),
            {"id": did},
        )).first()
        if not row:
            skipped += 1
            continue
        await db.execute(
            text("""UPDATE device_ncm
                    SET schedule_enabled=:s, schedule_type=:st,
                        schedule_interval_hours=:h, schedule_time=:tm, schedule_days=:days,
                        keep_versions=:kv, alert_on_change=:ac, enabled=true
                    WHERE device_id=:d"""),
            {"d": did, "s": data.schedule_enabled, "st": data.schedule_type,
             "h": data.schedule_interval_hours, "tm": sched_time, "days": days,
             "kv": data.keep_versions, "ac": data.alert_on_change},
        )
        updated += 1
    for target_id in data.device_ids:
        await _assurance_settings(db,user,target_id,data)
    await db.commit()
    return {"updated": updated, "skipped": skipped, "requested": len(data.device_ids)}


class _FetchError(Exception):
    pass


async def _do_fetch(db, device_id, *, actor=None, trigger='manual', config_type='running'):
    # Transaction-scoped advisory lock protects both manual and scheduler paths.
    locked=(await db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:device,0))'),
                             {'device':str(device_id)})).scalar()
    if not locked:
        raise _FetchError('A backup is already running for this device')
    row=(await db.execute(text("""SELECT host(d.ip_address) AS ip,n.platform,n.credential_id,
        n.enabled,c.username,c.password_enc,c.enable_password_enc,c.port FROM devices d
        LEFT JOIN device_ncm n ON n.device_id=d.id LEFT JOIN ncm_credentials c ON c.id=n.credential_id
        WHERE d.id=:id"""),{'id':device_id})).first()
    if not row:
        raise _FetchError('Device not found')
    if not row.credential_id or not row.enabled:
        raise _FetchError('Device is not enrolled with an enabled connection profile')
    run=(await db.execute(text("""INSERT INTO ncm_backup_runs(device_id,config_type,trigger,actor_id,status)
        VALUES (:d,:t,:trigger,:actor,'running') RETURNING id"""),
        {'d':device_id,'t':config_type,'trigger':trigger,'actor':getattr(actor,'id',None)})).first()
    try:
        password=crypto.decrypt(row.password_enc) if row.password_enc else ''
        enable=crypto.decrypt(row.enable_password_enc) if row.enable_password_enc else ''
        platform,content=await asyncio.to_thread(_netmiko_fetch,row.ip,row.platform or 'autodetect',
                                               row.username,password,enable,row.port or 22,config_type)
        # The savepoint ensures a storage failure cannot publish an alert or
        # prune the last good snapshot while the attempt is recorded as failed.
        async with db.begin_nested():
            result=await _save_config_version(db,device_id,config_type,content,'ssh',f'ssh:{platform}',
                                             actor=actor,validated=True)
    except Exception as exc:
        msg=str(exc) if isinstance(exc,CaptureError) else 'Collection or archive failed; verify connection, trust, permissions and encryption key'
        await db.execute(text("""UPDATE ncm_backup_runs SET status='failed',finished_at=now(),error_code=:error WHERE id=:id"""),{'id':run.id,'error':msg})
        await db.execute(text("""UPDATE device_ncm SET last_status='failed',last_error=:error,last_attempt_at=now() WHERE device_id=:d"""),{'d':device_id,'error':msg})
        await ncm_events.event(db,device_id,'backup.failed',str(run.id),message='Configuration backup failed')
        await ncm_events.audit(db,actor,'backup.failed',device_id=device_id,resource_id=run.id)
        await db.commit()
        raise _FetchError(msg) from None
    await db.execute(text("""UPDATE ncm_backup_runs SET status='success',finished_at=now(),validated=true,
        is_change=:changed,version_id=:version WHERE id=:id"""),
        {'id':run.id,'changed':result['is_change'],'version':UUID(result['version_id'])})
    await db.execute(text("""UPDATE device_ncm SET last_status='success',last_error=NULL,
        last_attempt_at=now(),last_success_at=now(),platform=CASE WHEN platform='autodetect' THEN :platform ELSE platform END
        WHERE device_id=:d"""),{'d':device_id,'platform':platform})
    await db.commit()
    return {**result,'platform':platform,'run_id':str(run.id)}


@device_router.post("/{device_id}/config-fetch")
async def fetch_config(device_id: UUID, config_type: str = Query(default="running", pattern="^(running|startup)$"), db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("ncm.manage"))):
    await _device_access(db,user,device_id)
    """Real SSH config backup via netmiko using the device's enrolled credential."""
    try:
        return await _do_fetch(db, device_id, actor=user, config_type=config_type)
    except _FetchError as e:
        msg = str(e)
        if msg == "Device not found":
            raise HTTPException(status_code=404, detail=msg)
        if "not enrolled" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=502, detail=f"Config fetch failed: {msg}")


@router.post("/run-scheduled")
async def run_scheduled(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.manage"))):
    return await _run_scheduled(db, user)


async def _run_scheduled(db, user=None):
    from app.services.ncm_jobs import enqueue
    scope=await visible_tags(db,user) if user else None
    predicate=jsonb_tags_visible('d.tags')+' AND ' if scope else ''
    due=(await db.execute(text(f"""SELECT device_id FROM device_ncm n JOIN devices d ON d.id=n.device_id
        WHERE {predicate} n.enabled AND schedule_enabled AND credential_id IS NOT NULL
        AND (last_scheduled_at IS NULL OR last_scheduled_at<now()-interval '1 minute')
        AND ((schedule_type='interval' AND (last_scheduled_at IS NULL OR
              last_scheduled_at<now()-make_interval(hours=>schedule_interval_hours)))
          OR (schedule_type IN ('daily','weekly')
              AND (now() AT TIME ZONE schedule_timezone)::time>=schedule_time
              AND (last_scheduled_at IS NULL OR (last_scheduled_at AT TIME ZONE schedule_timezone)::date
                    <(now() AT TIME ZONE schedule_timezone)::date)
              AND (schedule_type='daily' OR EXTRACT(DOW FROM now() AT TIME ZONE schedule_timezone)::int=ANY(schedule_days))))
        FOR UPDATE OF n SKIP LOCKED"""),{'vis_tags':scope})).fetchall()
    queued=0
    for row in due:
        result=await enqueue(db,row.device_id,actor=user,trigger='schedule')
        queued+=int(result['queued'])
        await db.execute(text('UPDATE device_ncm SET last_scheduled_at=now() WHERE device_id=:id'),{'id':row.device_id})
    await db.commit()
    return {'due':len(due),'queued':queued,'already_active':len(due)-queued}


# --------------------------------------------------------------------------- #
# Manual capture (paste) — slice 1
# --------------------------------------------------------------------------- #
class ConfigCapture(BaseModel):
    content: str = Field(..., min_length=1, max_length=16*1024*1024)
    config_type: str = Field(default="running", pattern="^(running|startup)$")
    source_note: Optional[str] = None
    captured_by: str = Field(default="manual", pattern="^(manual|api|ssh)$")

    @field_validator('content')
    @classmethod
    def valid_content(cls, value):
        return checked_content(value)


@device_router.post("/{device_id}/config-backup", status_code=201)
async def capture_config(device_id: UUID, data: ConfigCapture, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_permission("ncm.manage"))):
    await _device_access(db,user,device_id)
    dev = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    locked = (await db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:device,0))'), {'device':str(device_id)})).scalar()
    if not locked:
        raise HTTPException(409, 'A backup is already running for this device')
    result = await _save_config_version(db, device_id, data.config_type, data.content,
                                        "manual", data.source_note, actor=user, validated=False)
    await db.commit()
    if not result["is_change"]:
        result["message"] = "No change since last backup"
    return result


@device_router.get("/{device_id}/configs")
async def list_configs(device_id: UUID, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0), config_type: str = Query(default='running',pattern='^(running|startup)$'),
                       db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db,user,device_id)
    rows = (await db.execute(
        text("""SELECT id, config_type, content_hash, size_bytes, line_count,
                       captured_at, captured_by, source_note, is_change, validated, pinned, actor_id
                FROM device_configs WHERE device_id = :d AND config_type=:type
                ORDER BY captured_at DESC,id DESC LIMIT :lim OFFSET :offset"""),
        {"d": device_id, "lim": limit, "offset":offset,"type":config_type},
    )).fetchall()
    return {"data": [{
        "id": str(r.id), "config_type": r.config_type, "hash": r.content_hash[:12],
        "size_bytes": r.size_bytes, "line_count": r.line_count,
        "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
        "source_note": r.source_note, "is_change": r.is_change, "validated":r.validated, "pinned":r.pinned, "actor_id":str(r.actor_id) if r.actor_id else None,
    } for r in rows], "count": len(rows)}


@device_router.get("/{device_id}/configs/{version_id}")
async def get_config(device_id: UUID, version_id: UUID, raw: bool = False, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db,user,device_id)
    r = (await db.execute(
        text("""SELECT id, config_type, content, content_enc, content_hash, size_bytes, line_count,
                       captured_at, captured_by, source_note
                FROM device_configs WHERE id = :v AND device_id = :d"""),
        {"v": version_id, "d": device_id},
    )).first()
    if not r:
        raise HTTPException(status_code=404, detail="Config version not found")
    if raw and not await user_has_permission(db,user,'ncm.export'):
        raise HTTPException(403,'Raw configuration export permission required')
    content=read_content(r)
    await ncm_events.audit(db,user,'config.export' if raw else 'config.view',device_id=device_id,resource_id=version_id)
    await db.commit()
    return {"id": str(r.id), "config_type": r.config_type, "content": content if raw else redact_config(content), "redacted":not raw,
            "size_bytes": r.size_bytes, "line_count": r.line_count,
            "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
            "source_note": r.source_note}


@device_router.get("/{device_id}/configs-diff")
async def diff_configs(device_id: UUID, a: UUID, b: UUID, raw: bool = False, normalized: bool = True, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db,user,device_id)
    rows = (await db.execute(
        text("SELECT id, content, content_enc, content_hash, captured_at FROM device_configs WHERE device_id = :d AND id IN (:a, :b)"),
        {"d": device_id, "a": a, "b": b},
    )).fetchall()
    by_id = {str(r.id): r for r in rows}
    if str(a) not in by_id or str(b) not in by_id:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    ra, rb = by_id[str(a)], by_id[str(b)]
    if raw and not await user_has_permission(db,user,'ncm.export'):
        raise HTTPException(403,'Raw configuration export permission required')
    result=config_diff(read_content(ra),read_content(rb),raw=raw,normalized=normalized)
    await ncm_events.audit(db,user,'config.diff',device_id=device_id,metadata={'a':str(a),'b':str(b),'raw':raw})
    await db.commit()
    return result



@router.get("/overview")
async def ncm_overview(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view"))):
    scope=await visible_tags(db,user)
    predicate=' WHERE '+jsonb_tags_visible('d.tags') if scope else ''
    rows = (await db.execute(text(f"""
        SELECT d.id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor, d.location,
               n.credential_id, n.platform, n.enabled AS ncm_enabled, n.schedule_enabled,
               n.schedule_interval_hours, n.schedule_type, n.schedule_time, n.schedule_days, n.schedule_timezone,
               n.keep_versions, n.alert_on_change, n.notify_channels, n.freshness_hours, n.eligible_override, n.config_types,
               n.last_status, n.last_error, n.last_attempt_at, n.last_success_at,
               cr.name AS credential_name,
               c.versions, c.last_capture, c.last_by, c.validated_at, c.legacy_plaintext,
               (SELECT jsonb_object_agg(t.config_type,t.finished_at) FROM
                 (SELECT br.config_type,max(br.finished_at) AS finished_at
                  FROM ncm_backup_runs br JOIN device_configs dc ON dc.id=br.version_id
                  WHERE br.device_id=d.id AND br.status='success' AND br.validated AND dc.validated
                  GROUP BY br.config_type) t) AS verified_types
        FROM devices d
        LEFT JOIN device_ncm n ON n.device_id = d.id
        LEFT JOIN ncm_credentials cr ON cr.id = n.credential_id
        LEFT JOIN (
            SELECT device_id, count(*) AS versions, max(captured_at) AS last_capture,
                    max(captured_at) FILTER (WHERE validated) AS validated_at,
                    count(*) FILTER (WHERE content_enc IS NULL) AS legacy_plaintext,
                   (array_agg(captured_by ORDER BY captured_at DESC))[1] AS last_by
            FROM device_configs GROUP BY device_id
        ) c ON c.device_id = d.id
        {predicate}
        ORDER BY (n.device_id IS NULL), (c.last_capture IS NULL), c.last_capture DESC NULLS LAST, d.hostname
    """),{"vis_tags":scope})).fetchall()
    data, backed_up, enrolled = [], 0, 0
    from app.services.ncm_status import backup_status
    for r in rows:
        state=backup_status(r)
        if r.versions:
            backed_up += 1
        is_enrolled = r.platform is not None  # a device_ncm row exists for this device
        if is_enrolled:
            enrolled += 1
        data.append({
            "device_id": str(r.id), "hostname": r.hostname, **state, "ip": r.ip,
            "device_type": r.device_type, "vendor": r.vendor,
            "location": r.location,
            "enrolled": is_enrolled, "ncm_enabled":bool(r.ncm_enabled),
            "credential_id": str(r.credential_id) if r.credential_id else None,
            "credential_name": r.credential_name,
            "platform": r.platform,
            "schedule_enabled": r.schedule_enabled,
            "schedule_interval_hours": r.schedule_interval_hours,
            "schedule_type": r.schedule_type, "schedule_timezone":r.schedule_timezone or "UTC",
            "schedule_time": r.schedule_time.strftime("%H:%M") if r.schedule_time else None,
            "schedule_days": list(r.schedule_days) if r.schedule_days else None,
            "keep_versions": r.keep_versions,
            "alert_on_change": r.alert_on_change, "notify_channels":[str(x) for x in r.notify_channels or []],
            "freshness_hours":r.freshness_hours or 24,"eligible_override":r.eligible_override,"config_types":r.config_types or ["running"],
            "last_status": r.last_status,
            "last_error": r.last_error,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
            "versions": r.versions or 0,
            "last_capture": r.last_capture.isoformat() if r.last_capture else None,
            "last_by": r.last_by,
        })
    return {"data":data,"total_devices":len(rows),"backed_up":backed_up,"enrolled":enrolled,
            "eligible":sum(d['eligible'] for d in data),"fresh":sum(d['eligible'] and d['fresh'] for d in data),
            "legacy_plaintext":sum(r.legacy_plaintext or 0 for r in rows)}


@device_router.post('/{device_id}/ncm-jobs',status_code=202)
async def queue_backup(device_id:UUID,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.manage'))):
    await _device_access(db,user,device_id)
    from app.services.ncm_jobs import enqueue
    try:
        result=await enqueue(db,device_id,actor=user)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    await db.commit()
    return result


@device_router.get('/{device_id}/ncm-jobs')
async def backup_jobs(device_id:UUID,limit:int=Query(50,ge=1,le=200),db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.view'))):
    await _device_access(db,user,device_id)
    rows=(await db.execute(text('''SELECT id,status,config_types,trigger,actor_name,attempts,
        created_at,started_at,finished_at,error_code,cancel_requested,results
        FROM ncm_jobs WHERE device_id=:id ORDER BY created_at DESC LIMIT :limit'''),{'id':device_id,'limit':limit})).mappings().all()
    return {'data':[dict(r) for r in rows]}


@device_router.post('/{device_id}/ncm-jobs/{job_id}/cancel')
async def cancel_backup(device_id:UUID,job_id:UUID,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.manage'))):
    await _device_access(db,user,device_id)
    row=(await db.execute(text('''UPDATE ncm_jobs SET cancel_requested=true,
        status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
        finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END
        WHERE id=:job AND device_id=:device AND status IN ('queued','running') RETURNING status'''),{'job':job_id,'device':device_id})).first()
    if not row:
        raise HTTPException(409,'Job is absent or already finished')
    await ncm_events.audit(db,user,'job.cancel_requested',device_id=device_id,resource_id=job_id)
    await db.commit()
    return {'status':row.status,'message':'Cancellation takes effect before the next configuration capture'}


@device_router.get('/{device_id}/ncm-runs')
async def backup_runs(device_id:UUID,limit:int=Query(50,ge=1,le=200),db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.view'))):
    await _device_access(db,user,device_id)
    rows=(await db.execute(text('SELECT * FROM ncm_backup_runs WHERE device_id=:id ORDER BY started_at DESC LIMIT :limit'),{'id':device_id,'limit':limit})).mappings().all()
    return {'data':[dict(r) for r in rows]}


class BaselineIn(BaseModel):
    version_id:UUID
    name:str=Field(min_length=1,max_length=120)
    reason:str=Field(min_length=3,max_length=1000)


@device_router.post('/{device_id}/ncm-baselines',status_code=201)
async def approve_baseline(device_id:UUID,data:BaselineIn,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.baseline'))):
    await _device_access(db,user,device_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:device,0))'),{'device':str(device_id)})
    version=(await db.execute(text('SELECT config_type,validated FROM device_configs WHERE id=:version AND device_id=:device FOR UPDATE'),{'version':data.version_id,'device':device_id})).first()
    if not version:
        raise HTTPException(404,'Configuration version not found')
    if not version.validated:
        raise HTTPException(422,'A baseline requires a validated device capture')
    await db.execute(text('UPDATE ncm_baselines SET retired_at=now() WHERE device_id=:device AND config_type=:type AND retired_at IS NULL'),{'device':device_id,'type':version.config_type})
    await db.execute(text('UPDATE device_configs SET pinned=true WHERE id=:version'),{'version':data.version_id})
    baseline=(await db.execute(text('''INSERT INTO ncm_baselines(device_id,version_id,config_type,name,reason,actor_id,actor_name)
        VALUES(:device,:version,:type,:name,:reason,:actor,:username) RETURNING id'''),{'device':device_id,'version':data.version_id,'type':version.config_type,'name':data.name,'reason':data.reason,'actor':user.id,'username':user.username})).scalar()
    await ncm_events.audit(db,user,'baseline.approve',device_id=device_id,resource_id=baseline,metadata={'version':str(data.version_id)})
    await db.commit()
    return {'id':str(baseline)}


@device_router.get('/{device_id}/ncm-baselines')
async def baselines(device_id:UUID,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.view'))):
    await _device_access(db,user,device_id)
    rows=(await db.execute(text('SELECT * FROM ncm_baselines WHERE device_id=:id ORDER BY created_at DESC LIMIT 200'),{'id':device_id})).mappings().all()
    return {'data':[dict(r) for r in rows]}


class PinIn(BaseModel):
    pinned:bool


@device_router.put('/{device_id}/configs/{version_id}/pin')
async def pin_config(device_id:UUID,version_id:UUID,data:PinIn,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.baseline'))):
    await _device_access(db,user,device_id)
    if not data.pinned and (await db.execute(text('SELECT 1 FROM ncm_baselines WHERE version_id=:id LIMIT 1'),{'id':version_id})).first():
        raise HTTPException(409,'Baseline history protects this recovery version')
    row=(await db.execute(text('UPDATE device_configs SET pinned=:pinned WHERE id=:version AND device_id=:device RETURNING id'),{'pinned':data.pinned,'version':version_id,'device':device_id})).first()
    if not row:
        raise HTTPException(404,'Configuration version not found')
    await ncm_events.audit(db,user,'config.pin' if data.pinned else 'config.unpin',device_id=device_id,resource_id=version_id)
    await db.commit()
    return {'pinned':data.pinned}


@router.get('/compare')
async def compare_versions(device_a:UUID,version_a:UUID,device_b:UUID,version_b:UUID,raw:bool=False,normalized:bool=True,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.view'))):
    await _bulk_access(db,user,[device_a,device_b])
    if raw and not await user_has_permission(db,user,'ncm.export'):
        raise HTTPException(403,'Raw configuration export permission required')
    contents=[]
    for device,version in [(device_a,version_a),(device_b,version_b)]:
        row=(await db.execute(text('SELECT content,content_enc,content_hash FROM device_configs WHERE id=:v AND device_id=:d'),{'v':version,'d':device})).first()
        if not row:
            raise HTTPException(404,'Configuration version not found')
        contents.append(read_content(row))
    result=config_diff(*contents,raw=raw,normalized=normalized)
    for device,version in [(device_a,version_a),(device_b,version_b)]:
        await ncm_events.audit(db,user,'config.compare',device_id=device,resource_id=version,metadata={'raw':raw})
    await db.commit()
    return result


@router.get('/notification-channels')
async def notification_channels(db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.manage'))):
    from app.services.ncm_delivery import SUPPORTED_CHANNELS
    rows=(await db.execute(text('SELECT id,name,type FROM notification_channels WHERE enabled AND type=ANY(:types) ORDER BY name'),{'types':list(SUPPORTED_CHANNELS)})).mappings().all()
    return {'data':[dict(r) for r in rows]}


@device_router.get('/{device_id}/ncm-deliveries')
async def delivery_history(device_id:UUID,db:AsyncSession=Depends(get_db),user:User=Depends(require_permission('ncm.view'))):
    await _device_access(db,user,device_id)
    rows=(await db.execute(text('''SELECT id,event_key,channel_id,status,attempts,created_at,
        delivered_at,last_error FROM ncm_notification_outbox WHERE device_id=:device
        ORDER BY created_at DESC LIMIT 100'''),{'device':device_id})).mappings().all()
    return {'data':[dict(r) for r in rows]}
