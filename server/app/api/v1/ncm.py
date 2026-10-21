"""Network Configuration Management (NCM).

Validated, encrypted configuration history, scoped access and change events.
Connection profiles (CLI credentials: password or SSH key), per-device
enrollment, durable SSH collection jobs, correlated failure incidents, baselines, restore evidence and a manual paste fallback.
Per-device routes are under /devices/{id}/..., fleet + credential management
under /ncm/...
"""
import asyncio
import hashlib
import json
import logging
import re
from uuid import UUID
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission, user_has_permission
from app.core.scoping import visible_tags, jsonb_tags_visible
from app.services import ncm_events, ssh_host_keys
from app.services.ncm_content import normalize_config as _normalize_config, read_content, encrypt_content, checked_content, config_diff, redact_config, safe_pattern
from app.services.ncm_collection import (PLATFORM_COMMANDS, STARTUP_COMMANDS, DRIVERS, UNSUPPORTED, platform_catalog,
                                         fetch_config as _netmiko_fetch, CaptureError, key_fingerprint,
                                         load_private_key)
from app.services.ncm_errors import classify, exception_chain, GUIDANCE, STAGES, PrerequisiteError
from app.services import ncm_schedule
from app.core import crypto
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ncm", tags=["NCM"])
device_router = APIRouter(prefix="/devices", tags=["NCM"])

# Kept for API compatibility; the catalog adds capability/scope metadata.
SUPPORTED_PLATFORMS = [{"value": p["value"], "label": p["label"]} for p in platform_catalog()]
RISK_ORDER = ['failed', 'blocked', 'stale', 'unverified', 'pending', 'unconfigured', 'fresh', 'disabled', 'excluded']


def _valid_platform(value):
    if value is not None and value not in {'autodetect', *PLATFORM_COMMANDS}:
        raise ValueError('Unsupported device platform')
    return value


def _valid_timezone(value):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Unknown IANA timezone') from None
    return value


def _valid_time(value):
    if value is not None and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value.strip()):
        raise ValueError('A valid 24-hour schedule time (HH:MM) is required')
    return value.strip() if value else value


def _valid_days(days):
    if days is not None and (len(set(days)) != len(days) or any(d not in range(7) for d in days)):
        raise ValueError('Schedule days must be unique values from 0 to 6')
    return days


def _valid_types(types):
    if types is not None and (not types or len(set(types)) != len(types) or any(t not in ('running', 'startup') for t in types)):
        raise ValueError('Select at least one supported, non-duplicate configuration type')
    return types


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


async def _validate_channels(db, channels):
    from app.services.ncm_delivery import SUPPORTED_CHANNELS
    for channel in channels:
        row = (await db.execute(text('SELECT type FROM notification_channels WHERE id=:id AND enabled'), {'id': channel})).first()
        if not row or row.type not in SUPPORTED_CHANNELS:
            raise HTTPException(422, 'Select an enabled NCM-supported notification channel')


async def _assurance_settings(db, user, device_id, data):
    names = {'notify_channels', 'freshness_hours', 'eligible_override', 'config_types', 'schedule_timezone', 'event_backup'}
    updates = {k: getattr(data, k) for k in names & data.model_fields_set}
    if 'notify_channels' in updates:
        await _validate_channels(db, updates['notify_channels'])
    if updates:
        sets = ','.join(f'{k}=:{k}' for k in sorted(updates))
        await db.execute(text(f'UPDATE device_ncm SET {sets} WHERE device_id=:device'), {'device': device_id, **updates})
    configured = (await db.execute(text('SELECT platform,config_types FROM device_ncm WHERE device_id=:id'), {'id': device_id})).first()
    if configured and 'startup' in configured.config_types and configured.platform not in ('autodetect', *STARTUP_COMMANDS):
        raise HTTPException(422, 'Selected driver does not support startup configuration capture')
    await ncm_events.audit(db, user, 'device.settings', device_id=device_id, metadata={'fields': sorted(data.model_fields_set)})


async def _save_config_version(db, device_id, config_type, content, captured_by, source_note,
                               *, actor=None, validated=False, platform=None):
    content = checked_content(content)
    chash = hashlib.sha256(content.encode()).hexdigest()
    nhash = hashlib.sha256(_normalize_config(content).encode()).hexdigest()
    latest = (await db.execute(text("""SELECT id,content_hash,norm_hash,content,content_enc,
        captured_at,validated FROM device_configs WHERE device_id=:d AND config_type=:t
        ORDER BY captured_at DESC,id DESC LIMIT 1"""), {'d': device_id, 't': config_type})).first()
    prior = read_content(latest) if latest else None
    is_change = latest is None or _normalize_config(prior) != _normalize_config(content)
    if latest and latest.content_hash == chash and (latest.validated or not validated):
        return {'is_change': False, 'version_id': str(latest.id), 'captured_at': latest.captured_at.isoformat(),
                'deduplicated': True, 'validated': bool(latest.validated), 'content': content}
    row = (await db.execute(text("""INSERT INTO device_configs
        (device_id,config_type,content,content_enc,content_hash,norm_hash,size_bytes,line_count,
         captured_by,source_note,is_change,actor_id,validated,driver_version,platform,artifact_format)
        VALUES (:d,:t,'',:encrypted,:h,:nh,:sz,:lc,:by,:note,:ic,:actor,:validated,'ncm-3',:platform,:fmt)
        RETURNING id,captured_at"""),
        {'d': device_id, 't': config_type, 'encrypted': encrypt_content(content), 'h': chash, 'nh': nhash,
         'sz': len(content.encode()), 'lc': len(content.splitlines()), 'by': captured_by,
         'note': source_note, 'ic': is_change, 'actor': getattr(actor, 'id', None), 'validated': validated,
         'platform': platform, 'fmt': 'cli_text' if captured_by == 'ssh' else 'manual_text'})).first()
    if latest and is_change:
        enabled = (await db.execute(text('SELECT alert_on_change FROM device_ncm WHERE device_id=:d'), {'d': device_id})).scalar()
        if enabled:
            summary = config_diff(prior, content)
            attribution = (await db.execute(text('''SELECT reported_user FROM ncm_change_events
                WHERE device_id=:d AND reported_user IS NOT NULL AND processed_at IS NULL
                ORDER BY received_at DESC LIMIT 1'''), {'d': device_id})).scalar()
            await ncm_events.event(db, device_id, 'config.changed', str(row.id),
                metadata={'config_type': config_type, 'from_version': str(latest.id), 'to_version': str(row.id),
                          'added': summary['added'], 'removed': summary['removed'],
                          'sensitive_changed': summary['sensitive_changed'],
                          **({'reported_user': attribution} if attribution else {})},
                message=f'{config_type.capitalize()} configuration changed')
    keep = (await db.execute(text('SELECT keep_versions FROM device_ncm WHERE device_id=:d'), {'d': device_id})).scalar() or 5
    # Retain N changed configurations AND the latest snapshot. Unchanged runs
    # cannot evict meaningful history; approved/pinned/restore-tested recovery
    # points survive.
    await db.execute(text("""DELETE FROM device_configs WHERE device_id=:d AND config_type=:t
        AND NOT pinned AND id<>:latest
        AND NOT EXISTS(SELECT 1 FROM ncm_baselines b WHERE b.version_id=device_configs.id)
        AND NOT EXISTS(SELECT 1 FROM ncm_restore_tests r WHERE r.version_id=device_configs.id)
        AND id NOT IN (SELECT id FROM device_configs WHERE device_id=:d AND config_type=:t
                      AND validated ORDER BY captured_at DESC,id DESC LIMIT 1)
        AND id NOT IN
        (SELECT id FROM device_configs WHERE device_id=:d AND config_type=:t AND is_change
         ORDER BY captured_at DESC,id DESC LIMIT :keep)"""),
        {'d': device_id, 't': config_type, 'latest': row.id, 'keep': keep})
    await ncm_events.audit(db, actor, 'config.capture', device_id=device_id, resource_id=row.id,
                           metadata={'config_type': config_type, 'validated': validated, 'is_change': is_change})
    return {'is_change': is_change, 'version_id': str(row.id), 'captured_at': row.captured_at.isoformat(),
            'validated': validated, 'deduplicated': False, 'content': content}


def _public(result):
    return {k: v for k, v in result.items() if k != 'content'}


# --------------------------------------------------------------------------- #
# Connection profiles (CLI credentials)
# --------------------------------------------------------------------------- #
class CredentialIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    protocol: str = Field(default="ssh", pattern="^ssh$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=120)
    auth_method: str = Field(default='password', pattern='^(password|key)$')
    password: Optional[str] = None
    enable_password: Optional[str] = None
    clear_enable_password: bool = False
    ssh_legacy_compat: bool = False
    private_key: Optional[str] = Field(default=None, max_length=16384)
    key_passphrase: Optional[str] = Field(default=None, max_length=1024)
    is_default: bool = False
    rotation_days: Optional[int] = Field(default=None, ge=1, le=3650)


def _parse_key(data):
    try:
        key = load_private_key(data.private_key, data.key_passphrase)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return key.get_name(), key_fingerprint(key.get_name(), key.get_base64())


@router.get("/platforms")
async def list_platforms(user: User = Depends(require_permission("ncm.view"))):
    return {"data": platform_catalog()}


@router.get('/support-matrix')
async def support_matrix(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    """Collector capability per driver, with evidence gathered on THIS appliance."""
    runs = {r.platform: r for r in (await db.execute(text('''SELECT platform,count(*) FILTER (WHERE status='success' AND validated) AS ok,
        count(*) FILTER (WHERE status='failed') AS failed, max(finished_at) FILTER (WHERE status='success') AS last_ok
        FROM ncm_backup_runs WHERE platform IS NOT NULL GROUP BY platform'''))).fetchall()}
    restores = {r.platform: r for r in (await db.execute(text('''SELECT c.platform,count(*) FILTER (WHERE t.result='passed') AS passed,
        count(*) AS total, max(t.tested_at) AS last FROM ncm_restore_tests t JOIN device_configs c ON c.id=t.version_id
        WHERE c.platform IS NOT NULL GROUP BY c.platform'''))).fetchall()}
    data = []
    for p in platform_catalog():
        if p['value'] == 'autodetect':
            continue
        r, t = runs.get(p['value']), restores.get(p['value'])
        evidence = 'restore_tested' if t and t.passed else 'captured' if r and r.ok else 'not_captured'
        data.append({**p, 'status': 'implemented', 'evidence': evidence,
                     'validated_captures': r.ok if r else 0, 'failed_attempts': r.failed if r else 0,
                     'last_capture': r.last_ok.isoformat() if r and r.last_ok else None,
                     'restore_tests_passed': t.passed if t else 0, 'last_restore_test': t.last.isoformat() if t and t.last else None})
    for vendor, reason in UNSUPPORTED.items():
        data.append({'value': None, 'label': vendor, 'vendor': vendor, 'status': 'unsupported', 'evidence': 'none',
                     'config_types': [], 'scope': reason, 'omissions': '', 'capability_resolved': True})
    return {'data': data, 'evidence_levels': {
        'not_captured': 'Collector implemented, but no validated capture has succeeded on this appliance yet; verify it in a pilot.',
        'captured': 'At least one validated capture succeeded on this appliance. Recovery is not yet demonstrated.',
        'restore_tested': 'An operator recorded a passed restore test of a captured artifact.',
        'none': 'No collector is implemented.'}}


@router.get("/credentials")
async def list_credentials(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view"))):
    scope = await visible_tags(db, user)
    allowed = " AND " + jsonb_tags_visible('d.tags') if scope else ''
    # Scoped users can reuse only profiles already assigned to visible devices.
    restriction = f"WHERE EXISTS (SELECT 1 FROM device_ncm n JOIN devices d ON d.id=n.device_id WHERE n.credential_id=c.id {allowed})" if scope else ''
    rows = (await db.execute(text(f"""
        SELECT c.id,c.name,c.description,c.protocol,c.port,c.username,c.is_default,c.auth_method,c.ssh_legacy_compat,
          (c.password_enc IS NOT NULL) AS has_password,
          (c.enable_password_enc IS NOT NULL) AS has_enable,
          (c.private_key_enc IS NOT NULL) AS has_private_key,
          (c.key_passphrase_enc IS NOT NULL) AS has_passphrase,
          c.key_type,c.key_fingerprint,c.secret_rotated_at,c.rotation_days,c.updated_at,
          (SELECT count(*) FROM device_ncm n JOIN devices d ON d.id=n.device_id
           WHERE n.credential_id=c.id {allowed}) AS used_by
        FROM ncm_credentials c {restriction} ORDER BY c.is_default DESC,c.name
    """), {'vis_tags': scope})).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        rotated = r.secret_rotated_at
        due = bool(r.rotation_days and rotated and (now - rotated).days >= r.rotation_days)
        out.append({
            "id": str(r.id), "name": r.name, "description": r.description,
            "protocol": r.protocol, "port": r.port, "username": r.username,
            "is_default": r.is_default, "auth_method": r.auth_method or 'password',
            "ssh_legacy_compat": r.ssh_legacy_compat,
            "has_password": r.has_password, "has_enable": r.has_enable,
            "has_private_key": r.has_private_key, "has_passphrase": r.has_passphrase,
            "key_type": r.key_type, "key_fingerprint": r.key_fingerprint,
            "secret_rotated_at": rotated.isoformat() if rotated else None,
            "rotation_days": r.rotation_days, "rotation_due": due,
            "rotation_unknown": bool(r.rotation_days and not rotated),
            "used_by": r.used_by,
        })
    return {"data": out}


@router.get('/credentials/{cred_id}/usage')
async def credential_usage(cred_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.credentials'))):
    await _global_credentials_access(db, user)
    rows = (await db.execute(text('''SELECT d.id,d.hostname,host(d.ip_address) AS ip,n.enabled,n.schedule_enabled
        FROM device_ncm n JOIN devices d ON d.id=n.device_id WHERE n.credential_id=:id ORDER BY d.hostname'''), {'id': cred_id})).fetchall()
    return {'data': [{'device_id': str(r.id), 'hostname': r.hostname, 'ip': r.ip, 'enabled': r.enabled,
                      'schedule_enabled': r.schedule_enabled} for r in rows]}


@router.post("/credentials", status_code=201)
async def create_credential(data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    await _global_credentials_access(db, user)
    key_type = key_fp = None
    if data.auth_method == 'key':
        if not data.private_key:
            raise HTTPException(422, 'SSH key authentication requires a private key')
        key_type, key_fp = _parse_key(data)
    secret_set = bool(data.password) or bool(data.private_key)
    row = (await db.execute(
        text("""INSERT INTO ncm_credentials
                (name, description, protocol, port, username, password_enc, enable_password_enc, is_default,
                 auth_method, private_key_enc, key_passphrase_enc, key_type, key_fingerprint, rotation_days,
                 secret_rotated_at, ssh_legacy_compat)
                VALUES (:n, :d, :p, :port, :u, :pw, :en, :def, :am, :pk, :pp, :kt, :kf, :rot,
                        CASE WHEN :secret THEN now() ELSE NULL END, :legacy)
                RETURNING id"""),
        {"n": data.name, "d": data.description, "p": data.protocol, "port": data.port,
         "u": data.username, "pw": crypto.encrypt(data.password),
         "en": crypto.encrypt(data.enable_password), "def": data.is_default,
         "am": data.auth_method, "pk": crypto.encrypt(data.private_key) if data.auth_method == 'key' else None,
         "pp": crypto.encrypt(data.key_passphrase) if data.auth_method == 'key' else None,
         "kt": key_type, "kf": key_fp, "rot": data.rotation_days, "secret": secret_set, "legacy": data.ssh_legacy_compat},
    )).first()
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": row.id})
    await ncm_events.audit(db, user, "credential.create", resource_id=row.id, metadata={'auth_method': data.auth_method})
    await db.commit()
    return {"id": str(row.id)}


@router.put("/credentials/{cred_id}")
async def update_credential(cred_id: UUID, data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    await _global_credentials_access(db, user)
    current = (await db.execute(text('SELECT private_key_enc FROM ncm_credentials WHERE id=:id'), {'id': cred_id})).first()
    if not current:
        raise HTTPException(status_code=404, detail="Credential not found")
    sets = ["name=:n", "description=:d", "protocol=:p", "port=:port", "username=:u",
            "is_default=:def", "auth_method=:am", "rotation_days=:rot", "ssh_legacy_compat=:legacy", "updated_at=NOW()"]
    params = {"id": cred_id, "n": data.name, "d": data.description, "p": data.protocol,
              "port": data.port, "u": data.username, "def": data.is_default, "am": data.auth_method,
              "rot": data.rotation_days, "legacy": data.ssh_legacy_compat}
    changed = []
    # Blank means keep: secrets are only overwritten when a new value is supplied.
    if data.password:
        sets.append("password_enc=:pw"); params["pw"] = crypto.encrypt(data.password); changed.append('password')
    if data.clear_enable_password:
        sets.append("enable_password_enc=NULL"); changed.append('enable_password_cleared')
    elif data.enable_password:
        sets.append("enable_password_enc=:en"); params["en"] = crypto.encrypt(data.enable_password); changed.append('enable_password')
    if data.private_key:
        key_type, key_fp = _parse_key(data)
        sets += ["private_key_enc=:pk", "key_passphrase_enc=:pp", "key_type=:kt", "key_fingerprint=:kf"]
        params.update(pk=crypto.encrypt(data.private_key), pp=crypto.encrypt(data.key_passphrase), kt=key_type, kf=key_fp)
        changed.append('private_key')
    elif data.auth_method == 'key' and current.private_key_enc is None:
        raise HTTPException(422, 'SSH key authentication requires a private key')
    if 'password' in changed or 'private_key' in changed:
        sets.append('secret_rotated_at=now()')
    await db.execute(text(f"UPDATE ncm_credentials SET {', '.join(sets)} WHERE id=:id"), params)
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": cred_id})
    await ncm_events.audit(db, user, "credential.update", resource_id=cred_id, metadata={'secrets': changed})
    await db.commit()
    return {"id": str(cred_id)}


@router.delete("/credentials/{cred_id}")
async def delete_credential(cred_id: UUID, replacement_id: Optional[UUID] = Query(default=None),
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission("ncm.credentials"))):
    """Delete a profile. A profile used by devices is never removed silently:
    the caller must name a replacement, which is assigned first."""
    await _global_credentials_access(db, user)
    if not (await db.execute(text('SELECT 1 FROM ncm_credentials WHERE id=:id FOR UPDATE'), {'id': cred_id})).first():
        raise HTTPException(status_code=404, detail="Credential not found")
    used = (await credential_usage(cred_id, db, user))['data']
    reassigned = 0
    if used:
        if replacement_id is None:
            raise HTTPException(409, {'code': 'profile_in_use',
                                      'message': f'This profile is used by {len(used)} device(s). Choose a replacement profile to reassign them before deleting.',
                                      'devices': used})
        if replacement_id == cred_id:
            raise HTTPException(422, 'The replacement must be a different profile')
        if not (await db.execute(text('SELECT 1 FROM ncm_credentials WHERE id=:id'), {'id': replacement_id})).first():
            raise HTTPException(404, 'Replacement profile not found')
        reassigned = (await db.execute(text('UPDATE device_ncm SET credential_id=:r WHERE credential_id=:id'),
                                       {'r': replacement_id, 'id': cred_id})).rowcount
        for d in used:
            await ncm_events.audit(db, user, 'device.assign_profile', device_id=UUID(d['device_id']),
                                   metadata={'from': str(cred_id), 'to': str(replacement_id), 'reason': 'profile deletion'})
    await db.execute(text("DELETE FROM ncm_credentials WHERE id=:id"), {"id": cred_id})
    await ncm_events.audit(db, user, "credential.delete", resource_id=cred_id,
                           metadata={'reassigned': reassigned, 'replacement': str(replacement_id) if replacement_id else None})
    await db.commit()
    return {'deleted': True, 'reassigned': reassigned}


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
    notify_channels: list[UUID] = Field(default_factory=list, max_length=20)
    freshness_hours: int = Field(default=24, ge=1, le=720)
    eligible_override: Optional[bool] = None
    config_types: list[str] = Field(default_factory=lambda: ['running'], min_length=1, max_length=2)
    event_backup: bool = True

    @field_validator('schedule_timezone')
    @classmethod
    def valid_timezone(cls, value):
        return _valid_timezone(value)

    @field_validator('platform')
    @classmethod
    def supported_platform(cls, value):
        return _valid_platform(value)

    @model_validator(mode='after')
    def valid_schedule(self):
        _valid_days(self.schedule_days)
        if self.schedule_type in ('daily', 'weekly') and self.schedule_enabled:
            if not self.schedule_time or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.schedule_time):
                raise ValueError('A valid 24-hour schedule time is required')
            if self.schedule_type == 'weekly' and not self.schedule_days:
                raise ValueError('Select at least one scheduled day')
        if len(set(self.config_types)) != len(self.config_types) or any(t not in ('running', 'startup') for t in self.config_types):
            raise ValueError('Unsupported or duplicate configuration type')
        return self


@device_router.put("/{device_id}/ncm")
async def enroll_device(device_id: UUID, data: NcmEnroll, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("ncm.manage"))):
    await _device_access(db, user, device_id)
    await _profile_access(db, user, data.credential_id)
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
    previous = (await db.execute(text('''SELECT schedule_enabled,schedule_type,schedule_interval_hours,schedule_time,
        schedule_days,schedule_timezone,last_scheduled_at FROM device_ncm WHERE device_id=:d'''), {'d': device_id})).mappings().first()
    anchor = ncm_schedule.reanchor(dict(previous) if previous else {}, {
        'schedule_enabled': data.schedule_enabled, 'schedule_type': data.schedule_type,
        'schedule_interval_hours': data.schedule_interval_hours, 'schedule_time': sched_time,
        'schedule_days': days, 'schedule_timezone': data.schedule_timezone if 'schedule_timezone' in data.model_fields_set else (previous or {}).get('schedule_timezone', 'UTC'),
        'last_scheduled_at': (previous or {}).get('last_scheduled_at')}, datetime.now(timezone.utc))
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
    if anchor:
        await db.execute(text('UPDATE device_ncm SET last_scheduled_at=:a WHERE device_id=:d'), {'a': anchor, 'd': device_id})
    await _assurance_settings(db, user, device_id, data)
    await db.commit()
    return {"device_id": str(device_id), "enrolled": True}


@device_router.delete("/{device_id}/ncm", status_code=204)
async def unenroll_device(device_id: UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("ncm.manage"))):
    """Remove enrollment. Stored versions, baselines and audit history are kept."""
    await _device_access(db, user, device_id)
    await db.execute(text("UPDATE ncm_jobs SET status='cancelled',finished_at=now(),cancel_requested=true WHERE device_id=:d AND status='queued'"), {"d": device_id})
    await db.execute(text("DELETE FROM device_ncm WHERE device_id=:d"), {"d": device_id})
    await ncm_events.audit(db, user, "device.unenroll", device_id=device_id)
    await db.commit()


class BulkAssign(BaseModel):
    device_ids: list[UUID] = Field(min_length=1, max_length=500)
    credential_id: UUID
    platform: Optional[str] = Field(default=None, max_length=40)
    # None keeps each existing enrollment's collection state; new enrollments
    # start enabled. True/False sets it explicitly for every target.
    enabled: Optional[bool] = None

    @field_validator('platform')
    @classmethod
    def supported_platform(cls, value):
        return _valid_platform(value)


@router.post("/bulk-assign")
async def bulk_assign_profile(data: BulkAssign, db: AsyncSession = Depends(get_db),
                              user: User = Depends(require_permission("ncm.manage"))):
    """Assign a connection profile to many devices. Enrolls devices that are not
    yet configured and updates the credential (and optionally platform) of
    those already enrolled without touching schedule, retention or collection
    state unless `enabled` is given explicitly."""
    await _bulk_access(db, user, data.device_ids)
    await _profile_access(db, user, data.credential_id)
    cred = (await db.execute(text("SELECT id FROM ncm_credentials WHERE id=:c"), {"c": data.credential_id})).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Connection profile not found")
    results = []
    for did in dict.fromkeys(data.device_ids):
        existing = (await db.execute(text('SELECT enabled FROM device_ncm WHERE device_id=:id'), {'id': did})).first()
        enabled = data.enabled if data.enabled is not None else (existing.enabled if existing else True)
        await db.execute(
            text("""INSERT INTO device_ncm (device_id, credential_id, platform, enabled)
                    VALUES (:d, :c, COALESCE(:p, 'autodetect'), :e)
                    ON CONFLICT (device_id) DO UPDATE
                      SET credential_id = :c,
                          platform = COALESCE(:p, device_ncm.platform),
                          enabled = :e"""),
            {"d": did, "c": data.credential_id, "p": data.platform, "e": enabled},
        )
        await ncm_events.audit(db, user, "device.assign_profile", device_id=did,
                               metadata={'enrolled': not existing, 'enabled': enabled})
        results.append({'device_id': str(did), 'status': 'updated' if existing else 'enrolled', 'enabled': enabled})
    await db.commit()
    return {"assigned": len(results), "requested": len(data.device_ids), "results": results}


class BulkBackupSettings(BaseModel):
    """Every field is optional: only supplied (non-null) fields change. Omitted
    fields keep each device's own value, including collection state and
    timezone."""
    device_ids: list[UUID] = Field(min_length=1, max_length=500)
    dry_run: bool = False
    enabled: Optional[bool] = None
    schedule_enabled: Optional[bool] = None
    schedule_type: Optional[str] = Field(default=None, pattern="^(interval|daily|weekly)$")
    schedule_interval_hours: Optional[int] = Field(default=None, ge=1, le=720)
    schedule_time: Optional[str] = None
    schedule_days: Optional[list[int]] = None
    schedule_timezone: Optional[str] = None
    keep_versions: Optional[int] = Field(default=None, ge=1, le=100)
    alert_on_change: Optional[bool] = None
    notify_channels: Optional[list[UUID]] = Field(default=None, max_length=20)
    freshness_hours: Optional[int] = Field(default=None, ge=1, le=720)
    eligibility: Optional[str] = Field(default=None, pattern='^(auto|include|exclude)$')
    config_types: Optional[list[str]] = Field(default=None, max_length=2)
    event_backup: Optional[bool] = None

    @field_validator('schedule_timezone')
    @classmethod
    def valid_timezone(cls, value):
        return value if value is None else _valid_timezone(value)

    @field_validator('schedule_time')
    @classmethod
    def valid_time(cls, value):
        return _valid_time(value)

    @field_validator('schedule_days')
    @classmethod
    def valid_days(cls, value):
        return _valid_days(value)

    @field_validator('config_types')
    @classmethod
    def valid_types(cls, value):
        return _valid_types(value)


_BULK_FIELDS = ('enabled', 'schedule_enabled', 'schedule_type', 'schedule_interval_hours', 'schedule_time',
                'schedule_days', 'schedule_timezone', 'keep_versions', 'alert_on_change', 'notify_channels',
                'freshness_hours', 'eligible_override', 'config_types', 'event_backup')


def _settings_view(r):
    return {'enabled': r.enabled, 'schedule_enabled': r.schedule_enabled, 'schedule_type': r.schedule_type,
            'schedule_interval_hours': r.schedule_interval_hours,
            'schedule_time': r.schedule_time.strftime('%H:%M') if r.schedule_time else None,
            'schedule_days': sorted(r.schedule_days) if r.schedule_days else None,
            'schedule_timezone': r.schedule_timezone or 'UTC', 'keep_versions': r.keep_versions,
            'alert_on_change': r.alert_on_change, 'notify_channels': sorted(str(x) for x in r.notify_channels or []),
            'freshness_hours': r.freshness_hours, 'eligible_override': r.eligible_override,
            'config_types': list(r.config_types or ['running']), 'event_backup': r.event_backup,
            'last_scheduled_at': r.last_scheduled_at}


def _merge_settings(before, data):
    after = dict(before)
    for field in _BULK_FIELDS:
        if field == 'eligible_override':
            if data.eligibility is not None:
                after[field] = None if data.eligibility == 'auto' else data.eligibility == 'include'
            continue
        value = getattr(data, field)
        if value is None:
            continue
        if field == 'notify_channels':
            value = sorted(str(x) for x in value)
        elif field == 'schedule_days':
            value = sorted(value)
        after[field] = value
    if after['schedule_type'] != 'weekly':
        after['schedule_days'] = before['schedule_days'] if data.schedule_days is None else after['schedule_days']
    errors = []
    if after['schedule_enabled'] and after['schedule_type'] in ('daily', 'weekly'):
        if not after['schedule_time']:
            errors.append('Daily and weekly schedules need a time.')
        if after['schedule_type'] == 'weekly' and not after['schedule_days']:
            errors.append('Weekly schedules need at least one day.')
    return after, errors


@router.post("/bulk-settings")
async def bulk_backup_settings(data: BulkBackupSettings, db: AsyncSession = Depends(get_db),
                               user: User = Depends(require_permission("ncm.manage"))):
    """Preview (dry_run) or apply schedule/retention/assurance changes to many
    devices. Returns per-device changed fields and the next scheduled run
    computed exactly as the scheduler will."""
    await _bulk_access(db, user, data.device_ids)
    if data.notify_channels:
        await _validate_channels(db, data.notify_channels)
    now = datetime.now(timezone.utc)
    results, invalid = [], 0
    _settings_view_last = {}
    for did in dict.fromkeys(data.device_ids):
        r = (await db.execute(text('''SELECT n.*,d.hostname,d.vendor FROM device_ncm n JOIN devices d ON d.id=n.device_id
            WHERE n.device_id=:id FOR UPDATE OF n'''), {'id': did})).first()
        if not r:
            host = (await db.execute(text('SELECT hostname FROM devices WHERE id=:id'), {'id': did})).scalar()
            results.append({'device_id': str(did), 'hostname': host, 'status': 'skipped', 'reason': 'Not enrolled for backup'})
            continue
        before = _settings_view(r)
        _settings_view_last[str(did)] = before['last_scheduled_at']
        after, errors = _merge_settings(before, data)
        if 'startup' in after['config_types'] and r.platform not in ('autodetect', *STARTUP_COMMANDS):
            errors.append(f"The {DRIVERS.get(r.platform, {}).get('label', r.platform)} driver cannot capture startup configuration.")
        changes = {k: {'before': before[k], 'after': after[k]} for k in _BULK_FIELDS if before[k] != after[k]}
        item = {'device_id': str(did), 'hostname': r.hostname, 'changes': changes,
                'timezone': after['schedule_timezone'], 'collection_enabled': after['enabled'],
                'schedule_summary': ncm_schedule.describe(after)}
        # A changed daily/weekly schedule starts at its next slot rather than
        # firing at once for a slot that passed before the change.
        anchor = ncm_schedule.reanchor(before, after, now)
        if anchor:
            after['last_scheduled_at'] = anchor
        due = ncm_schedule.next_due(after, now)
        item['next_run_at'] = max(due, now).isoformat() if due else None
        if errors:
            invalid += 1
            item.update(status='invalid', errors=errors)
        else:
            item['status'] = 'will_update' if changes else 'unchanged'
        item['_after'] = after
        results.append(item)
    summary = {'requested': len(data.device_ids),
               'will_update': sum(r['status'] == 'will_update' for r in results),
               'unchanged': sum(r['status'] == 'unchanged' for r in results),
               'skipped': sum(r['status'] == 'skipped' for r in results), 'invalid': invalid}
    if data.dry_run or invalid:
        for r in results:
            r.pop('_after', None)
        if invalid and not data.dry_run:
            await db.rollback()
            raise HTTPException(422, {'code': 'invalid_targets', 'message': f'{invalid} device(s) would be left with an invalid configuration; nothing was changed.', 'results': results, **summary})
        await db.rollback()
        return {'dry_run': True, 'results': results, **summary}
    for r in results:
        after = r.pop('_after', None)
        if r['status'] != 'will_update':
            continue
        values = dict(after)
        values['schedule_time'] = datetime.strptime(values['schedule_time'], '%H:%M').time() if values['schedule_time'] else None
        values['schedule_days'] = values['schedule_days'] if values['schedule_type'] == 'weekly' else None
        values['notify_channels'] = [UUID(x) for x in values['notify_channels']]
        fields = [k for k in r['changes']]
        if values['last_scheduled_at'] != _settings_view_last.get(r['device_id']):
            fields.append('last_scheduled_at')
        sets = ','.join(f'{k}=:{k}' for k in fields)
        await db.execute(text(f'UPDATE device_ncm SET {sets} WHERE device_id=:device'),
                         {'device': UUID(r['device_id']), **{k: values[k] for k in fields}})
        await ncm_events.audit(db, user, 'device.settings', device_id=UUID(r['device_id']),
                               metadata={'bulk': True, 'fields': fields})
        r['status'] = 'updated'
    await db.commit()
    return {'dry_run': False, 'results': results, 'updated': summary['will_update'], **summary}


class BulkJobs(BaseModel):
    device_ids: list[UUID] = Field(min_length=1, max_length=500)


@router.post('/bulk-jobs')
async def bulk_jobs(data: BulkJobs, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage'))):
    """Queue backups for many devices with a per-device outcome: queued,
    already_active, blocked (with reasons), not_enrolled or not_found."""
    from app.services.ncm_jobs import enqueue
    from app.services.ncm_readiness import runtime_health
    scope = await visible_tags(db, user)
    runtime = await asyncio.to_thread(runtime_health)
    results = []
    for did in dict.fromkeys(data.device_ids):
        device = await _device_summary(db, did, scope)
        if not device:
            results.append({'device_id': str(did), 'status': 'not_found', 'reason': 'Device not found in your scope'})
            continue
        base = {'device_id': str(did), 'hostname': device['hostname']}
        if not device['enrolled']:
            results.append({**base, 'status': 'not_enrolled', 'reason': 'Not enrolled for backup'})
            continue
        readiness = await _readiness(db, device, runtime)
        if not readiness['can_capture']:
            results.append({**base, 'status': 'blocked', 'reason': '; '.join(b['label'] for b in readiness['blockers']),
                            'blockers': readiness['blockers']})
            continue
        try:
            async with db.begin_nested():
                outcome = await enqueue(db, did, actor=user)
        except ValueError as exc:
            results.append({**base, 'status': 'failed', 'reason': str(exc)})
            continue
        results.append({**base, 'status': 'queued' if outcome['queued'] else 'already_active', 'job_id': outcome['job_id']})
    await db.commit()
    counts = {s: sum(r['status'] == s for r in results) for s in ('queued', 'already_active', 'blocked', 'not_enrolled', 'not_found', 'failed')}
    return {'results': results, 'requested': len(data.device_ids), **counts}


class _FetchError(Exception):
    def __init__(self, message, category='unknown', retryable=False, run_id=None):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.run_id = run_id


async def _do_fetch(db, device_id, *, actor=None, trigger='manual', config_type='running', job_id=None):
    # Transaction-scoped advisory lock protects both manual and scheduler paths.
    locked = (await db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:device,0))'),
                               {'device': str(device_id)})).scalar()
    if not locked:
        raise _FetchError('A backup is already running for this device', 'busy', True)
    row = (await db.execute(text("""SELECT host(d.ip_address) AS ip,n.platform,n.credential_id,
        n.enabled,c.username,c.password_enc,c.enable_password_enc,c.port,c.auth_method,
        c.private_key_enc,c.key_passphrase_enc,c.ssh_legacy_compat FROM devices d
        LEFT JOIN device_ncm n ON n.device_id=d.id LEFT JOIN ncm_credentials c ON c.id=n.credential_id
        WHERE d.id=:id"""), {'id': device_id})).first()
    if not row:
        raise _FetchError('Device not found', 'not_found', False)
    if not row.credential_id or not row.enabled:
        raise _FetchError('Device is not enrolled with an enabled connection profile', 'not_enrolled', False)
    run = (await db.execute(text("""INSERT INTO ncm_backup_runs(device_id,config_type,trigger,actor_id,status,job_id,stage)
        VALUES (:d,:t,:trigger,:actor,'running',:job,'prerequisites') RETURNING id"""),
        {'d': device_id, 't': config_type, 'trigger': trigger, 'actor': getattr(actor, 'id', None), 'job': job_id})).first()
    stage = 'prerequisites'
    try:
        port = row.port or 22
        stage = 'credentials'
        password = crypto.decrypt(row.password_enc) if row.password_enc else ''
        enable = crypto.decrypt(row.enable_password_enc) if row.enable_password_enc else ''
        pkey = None
        if (row.auth_method or 'password') == 'key':
            if row.private_key_enc is None:
                raise ValueError('missing key')
            pkey = load_private_key(crypto.decrypt(row.private_key_enc),
                                    crypto.decrypt(row.key_passphrase_enc) if row.key_passphrase_enc else None)
        stage = 'collection'
        await db.execute(text("UPDATE ncm_backup_runs SET stage='collection' WHERE id=:id"), {'id': run.id})
        observed_key: dict = {}
        platform, content = await asyncio.to_thread(_netmiko_fetch, row.ip, row.platform or 'autodetect',
                                                    row.username, password, enable, port, config_type,
                                                    pkey=pkey, legacy_compat=row.ssh_legacy_compat,
                                                    observed_key=observed_key)
        # The savepoint ensures a storage failure cannot publish an alert or
        # prune the last good snapshot while the attempt is recorded as failed.
        stage = 'archive'
        async with db.begin_nested():
            result = await _save_config_version(db, device_id, config_type, content, 'ssh', f'ssh:{platform}',
                                                actor=actor, validated=True, platform=platform)
    except Exception as exc:
        failure = classify(exc, stage)
        if failure.category in ('negotiation', 'authentication', 'unreachable'):
            stage = 'connect'
        await db.execute(text("""UPDATE ncm_backup_runs SET status='failed',finished_at=now(),error_code=:error,
            stage=:stage,failure_category=:cat,retryable=:retry,platform=:platform WHERE id=:id"""),
            {'id': run.id, 'error': failure.message, 'stage': stage, 'cat': failure.category,
             'retry': failure.retryable, 'platform': row.platform if row.platform != 'autodetect' else None})
        await db.execute(text("""UPDATE device_ncm SET last_status='failed',last_error=:error,last_failure_category=:cat,
            last_attempt_at=now() WHERE device_id=:d"""), {'d': device_id, 'error': failure.message, 'cat': failure.category})
        await ncm_events.audit(db, actor, 'backup.failed', device_id=device_id, resource_id=run.id,
                               metadata={'category': failure.category, 'stage': stage, 'config_type': config_type,
                                         'exception_types': [type(error).__name__ for error in exception_chain(exc)]})
        await db.commit()
        raise _FetchError(failure.message, failure.category, failure.retryable, run.id) from None
    content = result.pop('content')
    host_key = await _observe_host_key(db, device_id, row.ip, port, observed_key, actor=actor, run_id=run.id)
    await db.execute(text("""UPDATE ncm_backup_runs SET status='success',finished_at=now(),validated=true,stage='archive',
        is_change=:changed,version_id=:version,platform=:platform WHERE id=:id"""),
        {'id': run.id, 'changed': result['is_change'], 'version': UUID(result['version_id']), 'platform': platform})
    await db.execute(text("""UPDATE device_ncm SET last_status='success',last_error=NULL,last_failure_category=NULL,
        last_attempt_at=now(),last_success_at=now(),platform=CASE WHEN platform='autodetect' THEN :platform ELSE platform END
        WHERE device_id=:d"""), {'d': device_id, 'platform': platform})
    if job_id:
        await db.execute(text('''UPDATE ncm_change_events SET version_id=:v WHERE job_id=:job AND version_id IS NULL'''),
                         {'v': UUID(result['version_id']), 'job': job_id})
    if config_type == 'running':
        from app.services import ncm_compliance
        try:
            async with db.begin_nested():
                await ncm_compliance.evaluate_device(db, device_id, version_id=UUID(result['version_id']),
                                                     content=content, platform=platform)
        except Exception:
            pass  # Compliance evaluation never fails a successful backup.
    await db.commit()
    return {**result, 'platform': platform, 'run_id': str(run.id), 'host_key': host_key}


async def _observe_host_key(db, device_id, host, port, observed_key, *, actor, run_id):
    """Warn-only SSH host trust: record the first key, audit a changed one.
    Never fails a backup that already succeeded."""
    try:
        async with db.begin_nested():
            outcome = await ssh_host_keys.observe_backup_key(db, device_id, host, port, observed_key)
            if outcome and outcome['status'] == 'changed':
                await ncm_events.audit(db, actor, 'backup.host_key_changed', device_id=device_id,
                                       resource_id=run_id,
                                       metadata={'host': host, 'port': port, 'key_type': outcome['key_type'],
                                                 'fingerprint': outcome['fingerprint'],
                                                 'previous_fingerprint': outcome['previous_fingerprint']})
        return outcome
    except Exception:
        logger.warning('NCCM host key observation failed', exc_info=True)
        return None


@device_router.post("/{device_id}/config-fetch")
async def fetch_config(device_id: UUID, config_type: str = Query(default="running", pattern="^(running|startup)$"), db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("ncm.manage"))):
    """Synchronous single-attempt SSH backup. Subject to the same readiness
    gate as queued jobs; prefer POST /ncm-jobs."""
    await _device_access(db, user, device_id)
    await _require_ready(db, user, device_id)
    try:
        result = await _do_fetch(db, device_id, actor=user, config_type=config_type)
    except _FetchError as e:
        msg = str(e)
        if e.category == 'not_found':
            raise HTTPException(status_code=404, detail=msg)
        if e.category == 'not_enrolled':
            raise HTTPException(status_code=400, detail=msg)
        if e.category not in ('busy',):
            await ncm_events.open_incident(db, device_id, category=e.category, message=msg)
            await db.commit()
        raise HTTPException(status_code=502, detail=f"Config fetch failed: {msg}")
    await ncm_events.resolve_incident(db, device_id, run_id=UUID(result['run_id']))
    await db.commit()
    return result


@router.post("/run-scheduled")
async def run_scheduled(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.manage"))):
    return await _run_scheduled(db, user)


async def _run_scheduled(db, user=None):
    """Queue every due scheduled backup. Devices whose local prerequisites are
    blocked are not queued (no retries, no connections); they are marked
    blocked and correlated into one incident."""
    from app.services.ncm_jobs import enqueue
    from app.services.ncm_readiness import runtime_health
    scope = await visible_tags(db, user) if user else None
    predicate = jsonb_tags_visible('d.tags') + ' AND ' if scope else ''
    candidates = (await db.execute(text(f"""SELECT n.device_id,n.schedule_enabled,n.schedule_type,n.schedule_interval_hours,
        n.schedule_time,n.schedule_days,n.schedule_timezone,n.last_scheduled_at
        FROM device_ncm n JOIN devices d ON d.id=n.device_id
        WHERE {predicate} n.enabled AND n.schedule_enabled AND n.credential_id IS NOT NULL
        FOR UPDATE OF n SKIP LOCKED"""), {'vis_tags': scope})).fetchall()
    now = datetime.now(timezone.utc)
    due = [r for r in candidates if ncm_schedule.is_due(r, now)]
    runtime = await asyncio.to_thread(runtime_health) if due else None
    queued = blocked = 0
    for row in due:
        if ncm_schedule.clock_anomaly(row.last_scheduled_at, now):
            await ncm_events.audit(db, None, 'schedule.clock_anomaly', device_id=row.device_id,
                                   metadata={'last_scheduled_at': row.last_scheduled_at.isoformat()})
        device = await _device_summary(db, row.device_id, None)
        readiness = await _readiness(db, device, runtime) if device else None
        if readiness and not readiness['can_capture']:
            blocked += 1
            message = 'Scheduled backup skipped: ' + '; '.join(b['label'] for b in readiness['blockers'])
            await db.execute(text("""UPDATE device_ncm SET last_status='blocked',last_error=:m,last_failure_category='blocked',
                last_attempt_at=now() WHERE device_id=:id"""), {'id': row.device_id, 'm': message})
            await ncm_events.open_incident(db, row.device_id, category='blocked', message=message)
        else:
            result = await enqueue(db, row.device_id, actor=user, trigger='schedule')
            queued += int(result['queued'])
        await db.execute(text('UPDATE device_ncm SET last_scheduled_at=now() WHERE device_id=:id'), {'id': row.device_id})
    await db.commit()
    return {'due': len(due), 'queued': queued, 'blocked': blocked, 'already_active': len(due) - queued - blocked}


# --------------------------------------------------------------------------- #
# Manual capture (paste)
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
    await _device_access(db, user, device_id)
    dev = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    locked = (await db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:device,0))'), {'device': str(device_id)})).scalar()
    if not locked:
        raise HTTPException(409, 'A backup is already running for this device')
    result = _public(await _save_config_version(db, device_id, data.config_type, data.content,
                                                "manual", data.source_note, actor=user, validated=False))
    await db.commit()
    if not result["is_change"]:
        result["message"] = "No change since last backup"
    return result


@device_router.get("/{device_id}/configs")
async def list_configs(device_id: UUID, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0), config_type: str = Query(default='running', pattern='^(running|startup)$'),
                       protected_only: bool = False,
                       db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db, user, device_id)
    protected = ''' AND (c.pinned OR EXISTS(SELECT 1 FROM ncm_baselines b WHERE b.version_id=c.id)
        OR EXISTS(SELECT 1 FROM ncm_restore_tests t WHERE t.version_id=c.id))''' if protected_only else ''
    params = {"d": device_id, "lim": limit, "offset": offset, "type": config_type}
    total = (await db.execute(text(f'SELECT count(*) FROM device_configs c WHERE device_id=:d AND config_type=:type{protected}'), params)).scalar()
    rows = (await db.execute(
        text(f"""SELECT c.id, c.config_type, c.content_hash, c.size_bytes, c.line_count,
                       c.captured_at, c.captured_by, c.source_note, c.is_change, c.validated, c.pinned, c.actor_id,
                       c.platform, c.artifact_format,
                       (SELECT json_agg(json_build_object('name',b.name,'active',b.retired_at IS NULL) ORDER BY b.created_at DESC)
                          FROM ncm_baselines b WHERE b.version_id=c.id) AS baselines,
                       (SELECT json_build_object('result',t.result,'tested_at',t.tested_at) FROM ncm_restore_tests t
                          WHERE t.version_id=c.id ORDER BY t.tested_at DESC LIMIT 1) AS restore_test,
                       (SELECT count(*) FROM ncm_change_events e WHERE e.version_id=c.id) AS change_events
                FROM device_configs c WHERE c.device_id = :d AND c.config_type=:type{protected}
                ORDER BY c.captured_at DESC,c.id DESC LIMIT :lim OFFSET :offset"""), params,
    )).fetchall()
    return {"data": [{
        "id": str(r.id), "config_type": r.config_type, "hash": r.content_hash[:12],
        "size_bytes": r.size_bytes, "line_count": r.line_count,
        "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
        "source_note": r.source_note, "is_change": r.is_change, "validated": r.validated, "pinned": r.pinned,
        "actor_id": str(r.actor_id) if r.actor_id else None, "platform": r.platform,
        "artifact_format": r.artifact_format or ('cli_text' if r.captured_by == 'ssh' else 'manual_text'),
        "baselines": r.baselines or [], "baseline_protected": bool(r.baselines),
        "active_baseline": next((b['name'] for b in r.baselines or [] if b['active']), None),
        "restore_test": r.restore_test, "change_events": r.change_events,
    } for r in rows], "count": len(rows), "total": total, "limit": limit, "offset": offset}


@device_router.get("/{device_id}/configs/{version_id}")
async def get_config(device_id: UUID, version_id: UUID, raw: bool = False, db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db, user, device_id)
    r = (await db.execute(
        text("""SELECT id, config_type, content, content_enc, content_hash, size_bytes, line_count,
                       captured_at, captured_by, source_note, platform
                FROM device_configs WHERE id = :v AND device_id = :d"""),
        {"v": version_id, "d": device_id},
    )).first()
    if not r:
        raise HTTPException(status_code=404, detail="Config version not found")
    if raw and not await user_has_permission(db, user, 'ncm.export'):
        raise HTTPException(403, 'Raw configuration export permission required')
    content = read_content(r)
    await ncm_events.audit(db, user, 'config.export' if raw else 'config.view', device_id=device_id, resource_id=version_id)
    await db.commit()
    return {"id": str(r.id), "config_type": r.config_type, "content": content if raw else redact_config(content), "redacted": not raw,
            "size_bytes": r.size_bytes, "line_count": r.line_count,
            "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
            "source_note": r.source_note, "platform": r.platform}


@device_router.get("/{device_id}/configs-diff")
async def diff_configs(device_id: UUID, a: UUID, b: UUID, raw: bool = False, normalized: bool = True, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_permission("ncm.view"))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(
        text("SELECT id, content, content_enc, content_hash, captured_at FROM device_configs WHERE device_id = :d AND id IN (:a, :b)"),
        {"d": device_id, "a": a, "b": b},
    )).fetchall()
    by_id = {str(r.id): r for r in rows}
    if str(a) not in by_id or str(b) not in by_id:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    ra, rb = by_id[str(a)], by_id[str(b)]
    if raw and not await user_has_permission(db, user, 'ncm.export'):
        raise HTTPException(403, 'Raw configuration export permission required')
    result = config_diff(read_content(ra), read_content(rb), raw=raw, normalized=normalized)
    await ncm_events.audit(db, user, 'config.diff', device_id=device_id, metadata={'a': str(a), 'b': str(b), 'raw': raw})
    await db.commit()
    return result


# --------------------------------------------------------------------------- #
# Overview / device summary
# --------------------------------------------------------------------------- #
_OVERVIEW_SQL = """
    SELECT d.id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor, d.location,
           n.credential_id, n.platform, n.enabled AS ncm_enabled, n.schedule_enabled,
           n.schedule_interval_hours, n.schedule_type, n.schedule_time, n.schedule_days, n.schedule_timezone,
           n.keep_versions, n.alert_on_change, n.notify_channels, n.freshness_hours, n.eligible_override, n.config_types,
           n.last_status, n.last_error, n.last_attempt_at, n.last_success_at, n.last_scheduled_at,
           n.last_failure_category, n.event_backup,
           cr.name AS credential_name, cr.port AS credential_port, cr.auth_method,
           (coalesce(cr.username,'')<>'' AND (coalesce(octet_length(cr.password_enc),0)>0
             OR (cr.auth_method='key' AND cr.private_key_enc IS NOT NULL))) AS profile_configured,
           c.versions, c.last_capture, c.last_by, c.validated_at, c.legacy_plaintext,
           (SELECT jsonb_object_agg(t.config_type,t.finished_at) FROM
             (SELECT br.config_type,max(br.finished_at) AS finished_at
              FROM ncm_backup_runs br JOIN device_configs dc ON dc.id=br.version_id
              WHERE br.device_id=d.id AND br.status='success' AND br.validated AND dc.validated
              GROUP BY br.config_type) t) AS verified_types,
           (SELECT row_to_json(i) FROM (SELECT id,opened_at,occurrences,failure_category,last_message
              FROM ncm_incidents WHERE device_id=d.id AND status='open' LIMIT 1) i) AS open_incident,
           (SELECT row_to_json(t) FROM (SELECT result,tested_at FROM ncm_restore_tests
              WHERE device_id=d.id ORDER BY tested_at DESC LIMIT 1) t) AS last_restore_test,
           (SELECT jsonb_build_object('fail',count(*) FILTER (WHERE status='fail'),
                                      'pass',count(*) FILTER (WHERE status='pass'),
                                      'unknown',count(*) FILTER (WHERE status='unknown'),
                                      'excepted',count(*) FILTER (WHERE status='excepted'))
              FROM ncm_compliance_results cr2 WHERE cr2.device_id=d.id) AS compliance,
           EXISTS(SELECT 1 FROM ncm_jobs j WHERE j.device_id=d.id AND j.status IN ('queued','running')) AS job_active
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
"""


def _device_row(r, now):
    from app.services.ncm_status import backup_status
    from app.services.ncm_readiness import support_details
    state = backup_status(r, now)
    is_enrolled = r.platform is not None  # a device_ncm row exists for this device
    settings = {'schedule_enabled': r.schedule_enabled, 'schedule_type': r.schedule_type,
                'schedule_interval_hours': r.schedule_interval_hours, 'schedule_time': r.schedule_time,
                'schedule_days': r.schedule_days, 'schedule_timezone': r.schedule_timezone,
                'last_scheduled_at': r.last_scheduled_at}
    due = ncm_schedule.next_due(settings, now) if is_enrolled and r.ncm_enabled and r.credential_id else None
    compliance = r.compliance or {}
    return {
        "device_id": str(r.id), "hostname": r.hostname, **state, "ip": r.ip,
        "device_type": r.device_type, "vendor": r.vendor,
        "location": r.location,
        "enrolled": is_enrolled, "ncm_enabled": bool(r.ncm_enabled),
        "credential_id": str(r.credential_id) if r.credential_id else None,
        "credential_name": r.credential_name, "auth_method": r.auth_method,
        "credential_port": r.credential_port or 22, "profile_configured": bool(r.profile_configured),
        "support": support_details(r.vendor, r.platform, r.config_types),
        "platform": r.platform,
        "platform_label": DRIVERS.get(r.platform, {}).get('label') if r.platform else None,
        "schedule_enabled": r.schedule_enabled,
        "schedule_interval_hours": r.schedule_interval_hours,
        "schedule_type": r.schedule_type, "schedule_timezone": r.schedule_timezone or "UTC",
        "schedule_time": r.schedule_time.strftime("%H:%M") if r.schedule_time else None,
        "schedule_days": list(r.schedule_days) if r.schedule_days else None,
        "schedule_summary": ncm_schedule.describe(settings) if is_enrolled else None,
        "next_due": due.isoformat() if due else None,
        "overdue": bool(due and (now - due).total_seconds() > 900 and not r.job_active),
        "clock_anomaly": ncm_schedule.clock_anomaly(r.last_scheduled_at, now),
        "keep_versions": r.keep_versions,
        "alert_on_change": r.alert_on_change, "notify_channels": [str(x) for x in r.notify_channels or []],
        "freshness_hours": r.freshness_hours or 24, "eligible_override": r.eligible_override, "config_types": r.config_types or ["running"],
        "event_backup": r.event_backup if r.event_backup is not None else True,
        "last_status": r.last_status,
        "last_error": r.last_error, "last_failure_category": r.last_failure_category,
        "recommended_action": GUIDANCE.get(r.last_failure_category) if r.last_failure_category else None,
        "last_attempt_at": r.last_attempt_at.isoformat() if r.last_attempt_at else None,
        "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
        "versions": r.versions or 0,
        "last_capture": r.last_capture.isoformat() if r.last_capture else None,
        "last_by": r.last_by,
        "open_incident": r.open_incident, "last_restore_test": r.last_restore_test,
        "compliance": {k: compliance.get(k, 0) for k in ('pass', 'fail', 'unknown', 'excepted')} if compliance else None,
        "job_active": bool(r.job_active),
    }


async def _overview_rows(db, scope, device_id=None):
    clauses, params = [], {'vis_tags': scope}
    if scope:
        clauses.append(jsonb_tags_visible('d.tags'))
    if device_id is not None:
        clauses.append('d.id=:device_id'); params['device_id'] = device_id
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    return (await db.execute(text(_OVERVIEW_SQL + where + ' ORDER BY d.hostname'), params)).fetchall()


async def _device_summary(db, device_id, scope):
    rows = await _overview_rows(db, scope, device_id)
    return _device_row(rows[0], datetime.now(timezone.utc)) if rows else None


def _csv_set(value):
    return {v for v in (value or '').split(',') if v} or None


@router.get("/overview")
async def ncm_overview(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission("ncm.view")),
                       q: Optional[str] = None, status: Optional[str] = None, vendor: Optional[str] = None,
                       device_type: Optional[str] = None, location: Optional[str] = None,
                       attention: bool = False, ids: Optional[str] = None,
                       flag: Annotated[Optional[str], Query(pattern='^(review|unsupported|overdue|incident|compliance|never_captured|outside_scope)$')] = None,
                       sort: Annotated[str, Query(pattern='^(default|risk|hostname|last_capture|next_due)$')] = 'default',
                       page: Annotated[int, Query(ge=1)] = 1,
                       page_size: Annotated[Optional[int], Query(ge=1, le=1000)] = None):
    """Fleet protection overview. KPIs, state counts and facets always describe
    the caller's whole visible inventory; `data` is filtered and (when
    page_size is given) paged on the server."""
    scope = await visible_tags(db, user)
    rows = await _overview_rows(db, scope)
    now = datetime.now(timezone.utc)
    devices = [_device_row(r, now) for r in rows]
    by_state, facets = {}, {'status': {}, 'vendor': {}, 'type': {}, 'location': {}}
    for d in devices:
        by_state[d['backup_state']] = by_state.get(d['backup_state'], 0) + 1
        for key, value in (('status', d['backup_state']), ('vendor', d['vendor'] or 'unknown'),
                           ('type', d['device_type'] or 'unknown'), ('location', d['location'] or 'unassigned')):
            facets[key][value] = facets[key].get(value, 0) + 1
    eligible = [d for d in devices if d['eligible']]
    statuses, vendors, types, locations = _csv_set(status), _csv_set(vendor), _csv_set(device_type), _csv_set(location)
    wanted = _csv_set(ids)
    needle = (q or '').strip().lower()

    def keep(d):
        if wanted is not None and d['device_id'] not in wanted:
            return False
        if needle and not any(needle in str(v or '').lower() for v in (d['hostname'], d['ip'], d['vendor'], d['device_type'], d['location'], d['credential_name'], d['platform'])):
            return False
        if statuses and d['backup_state'] not in statuses:
            return False
        if vendors and (d['vendor'] or 'unknown') not in vendors:
            return False
        if types and (d['device_type'] or 'unknown') not in types:
            return False
        if locations and (d['location'] or 'unassigned') not in locations:
            return False
        if attention and not (d['eligible'] and d['backup_state'] not in ('fresh', 'excluded', 'disabled')):
            return False
        if flag and not {
            'review': bool(d['eligibility_review']),
            'unsupported': d['eligible'] and d['support']['state'] == 'unsupported',
            'overdue': d['overdue'],
            'incident': bool(d['open_incident']),
            'compliance': bool(d['compliance'] and d['compliance']['fail']),
            'never_captured': d['eligible'] and d['enrolled'] and not d['versions'],
            'outside_scope': d['enrolled'] and not d['eligible'],
        }[flag]:
            return False
        return True
    filtered = [d for d in devices if keep(d)]
    if sort == 'risk':
        filtered.sort(key=lambda d: (RISK_ORDER.index(d['backup_state']) if d['backup_state'] in RISK_ORDER else 99, not d['eligible'], d['hostname'] or ''))
    elif sort == 'hostname':
        filtered.sort(key=lambda d: (d['hostname'] or '').lower())
    elif sort == 'last_capture':
        filtered.sort(key=lambda d: d['last_capture'] or '', reverse=True)
    elif sort == 'next_due':
        filtered.sort(key=lambda d: d['next_due'] or '9999')
    else:
        # Enrolled first, then most recently captured, then hostname.
        filtered.sort(key=lambda d: (d['hostname'] or '').lower())
        filtered.sort(key=lambda d: d['last_capture'] or '', reverse=True)
        filtered.sort(key=lambda d: (not d['enrolled'], d['last_capture'] is None))
    total_filtered = len(filtered)
    if page_size:
        pages = max(1, -(-total_filtered // page_size))
        page = min(page, pages)
        filtered = filtered[(page - 1) * page_size: page * page_size]
    attention_counts = {
        'failed': sum(d['eligible'] and d['backup_state'] == 'failed' for d in devices),
        'blocked': sum(d['eligible'] and d['backup_state'] == 'blocked' for d in devices),
        'stale': sum(d['eligible'] and d['backup_state'] == 'stale' for d in devices),
        'never_captured': sum(d['eligible'] and d['enrolled'] and not d['versions'] for d in devices),
        'eligible_not_enrolled': sum(d['eligible'] and not d['enrolled'] for d in devices),
        'unsupported_eligible': sum(d['eligible'] and d['support']['state'] == 'unsupported' for d in devices),
        'eligibility_review': sum(bool(d['eligibility_review']) for d in devices),
        'overdue': sum(d['overdue'] for d in devices),
        'open_incidents': sum(bool(d['open_incident']) for d in devices),
        'compliance_failing': sum(bool(d['compliance'] and d['compliance']['fail']) for d in devices),
    }
    return {"data": filtered, "total_devices": len(devices), "filtered_total": total_filtered,
            "page": page, "page_size": page_size,
            "backed_up": sum(bool(d['versions']) for d in devices),
            "enrolled": sum(d['enrolled'] for d in devices),
            "eligible_enrolled": sum(d['enrolled'] for d in eligible),
            "enrolled_outside_scope": sum(d['enrolled'] and not d['eligible'] for d in devices),
            "eligible": len(eligible), "fresh": sum(d['fresh'] for d in eligible),
            "restore_tested": sum(bool(d['last_restore_test'] and d['last_restore_test']['result'] == 'passed') for d in eligible),
            "states": by_state,
            "eligible_states": {k: sum(d['backup_state'] == k for d in eligible) for k in RISK_ORDER if any(d['backup_state'] == k for d in eligible)}, "facets": {k: sorted(v.items(), key=lambda i: -i[1]) for k, v in facets.items()},
            "coverage_by": {key: _coverage_by(eligible, key) for key in ('vendor', 'location', 'device_type')},
            "attention": attention_counts,
            "legacy_plaintext": sum(r.legacy_plaintext or 0 for r in rows),
            "server_time": now.isoformat()}


def _coverage_by(eligible, key):
    groups = {}
    for d in eligible:
        name = d.get(key) or ('unassigned' if key == 'location' else 'unknown')
        g = groups.setdefault(name, {'name': name, 'eligible': 0, 'protected': 0, 'failed': 0, 'unsupported': 0})
        g['eligible'] += 1
        g['protected'] += int(d['fresh'])
        g['failed'] += int(d['backup_state'] in ('failed', 'blocked'))
        g['unsupported'] += int(d['support']['state'] == 'unsupported')
    return sorted(groups.values(), key=lambda g: (-g['eligible'], g['name']))


@router.get('/outcomes')
async def backup_outcomes(days: int = Query(14, ge=1, le=90), db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission('ncm.view'))):
    """Daily attempt outcomes (UTC days). Days without attempts are returned
    as zero only inside the requested window; nothing is extrapolated."""
    scope = await visible_tags(db, user)
    clause = (' AND ' + jsonb_tags_visible('d.tags')) if scope else ''
    rows = (await db.execute(text(f'''SELECT (COALESCE(r.finished_at,r.started_at) AT TIME ZONE 'UTC')::date AS day,
        count(*) FILTER (WHERE r.status='success' AND r.validated) AS succeeded,
        count(*) FILTER (WHERE r.status='failed') AS failed,
        count(*) FILTER (WHERE r.status='success' AND r.is_change) AS changed
        FROM ncm_backup_runs r JOIN devices d ON d.id=r.device_id
        WHERE COALESCE(r.finished_at,r.started_at) >= (now() AT TIME ZONE 'UTC')::date - CAST(:days AS integer) + 1{clause}
        GROUP BY 1 ORDER BY 1'''), {'days': days, 'vis_tags': scope})).fetchall()
    by_day = {r.day: r for r in rows}
    today = datetime.now(timezone.utc).date()
    from datetime import timedelta
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        r = by_day.get(day)
        series.append({'day': day.isoformat(), 'succeeded': r.succeeded if r else 0, 'failed': r.failed if r else 0,
                       'changed': r.changed if r else 0})
    return {'data': series, 'days': days, 'timezone': 'UTC'}


@device_router.get('/{device_id}/ncm-summary')
async def device_summary(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    """One device's NCCM state without loading the whole fleet."""
    scope = await visible_tags(db, user)
    device = await _device_summary(db, device_id, scope)
    if not device:
        raise HTTPException(404, 'Device not found')
    names = {}
    if device['notify_channels']:
        names = {str(r.id): {'name': r.name, 'type': r.type, 'enabled': r.enabled} for r in (await db.execute(
            text('SELECT id,name,type,enabled FROM notification_channels WHERE id = ANY(:ids)'),
            {'ids': [UUID(x) for x in device['notify_channels']]})).fetchall()}
    device['notify_channel_details'] = [{'id': c, **names.get(c, {'name': 'Deleted channel', 'type': None, 'enabled': False})}
                                        for c in device['notify_channels']]
    device['server_time'] = datetime.now(timezone.utc).isoformat()
    return device


@router.get('/health')
async def ncm_health(user: User = Depends(require_permission('ncm.view'))):
    from app.services.ncm_readiness import runtime_health
    return await asyncio.to_thread(runtime_health)


async def _readiness(db, device, runtime=None):
    from app.services.ncm_readiness import runtime_health, device_readiness
    runtime = runtime or await asyncio.to_thread(runtime_health)
    return device_readiness(device, runtime)


async def _require_ready(db, user, device_id):
    scope = await visible_tags(db, user)
    device = await _device_summary(db, device_id, scope)
    if not device:
        raise HTTPException(404, 'Device not found')
    readiness = await _readiness(db, device)
    if not readiness['can_capture']:
        raise HTTPException(409, {'code': 'readiness_blocked',
                                  'message': 'Backup not queued: ' + '; '.join(b['label'] for b in readiness['blockers']) + '.',
                                  'blockers': readiness['blockers']})
    return device


@device_router.get('/{device_id}/ncm-readiness')
async def ncm_readiness(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    device = await _device_summary(db, device_id, await visible_tags(db, user))
    if device is None:
        raise HTTPException(status_code=404, detail='Device not found')
    return await _readiness(db, device)


# --------------------------------------------------------------------------- #
# Jobs, attempts and incidents
# --------------------------------------------------------------------------- #
def _run_view(r):
    duration = (r['finished_at'] - r['started_at']).total_seconds() if r.get('finished_at') and r.get('started_at') else None
    return {'id': str(r['id']), 'job_id': str(r['job_id']) if r.get('job_id') else None, 'config_type': r['config_type'],
            'trigger': r['trigger'], 'status': r['status'], 'started_at': r['started_at'].isoformat() if r['started_at'] else None,
            'finished_at': r['finished_at'].isoformat() if r.get('finished_at') else None, 'duration_seconds': duration,
            'stage': r.get('stage'), 'stage_label': STAGES.get(r.get('stage') or ''), 'failure_category': r.get('failure_category'),
            'retryable': r.get('retryable'), 'message': r.get('error_code'), 'platform': r.get('platform'),
            'recommended_action': GUIDANCE.get(r.get('failure_category')) if r.get('failure_category') else None,
            'validated': r.get('validated'), 'is_change': r.get('is_change'),
            'version_id': str(r['version_id']) if r.get('version_id') else None}


@device_router.post('/{device_id}/ncm-jobs', status_code=202)
async def queue_backup(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage'))):
    await _device_access(db, user, device_id)
    await _require_ready(db, user, device_id)
    from app.services.ncm_jobs import enqueue
    try:
        result = await enqueue(db, device_id, actor=user)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await db.commit()
    return result


def _job_view(r, runs):
    return {'id': str(r['id']), 'status': r['status'], 'config_types': r['config_types'], 'trigger': r['trigger'],
            'actor_name': r['actor_name'], 'attempts': r['attempts'], 'max_attempts': r.get('max_attempts') or 3,
            'created_at': r['created_at'].isoformat(), 'started_at': r['started_at'].isoformat() if r['started_at'] else None,
            'finished_at': r['finished_at'].isoformat() if r['finished_at'] else None,
            'error_code': r['error_code'], 'failure_category': r.get('failure_category'),
            'recommended_action': GUIDANCE.get(r.get('failure_category')) if r.get('failure_category') else None,
            'cancel_requested': r['cancel_requested'], 'results': r['results'],
            'device_id': str(r['device_id']), 'hostname': r.get('hostname'),
            'runs': [_run_view(x) for x in runs]}


async def _jobs_with_runs(db, rows):
    ids = [r['id'] for r in rows]
    runs = {}
    if ids:
        for x in (await db.execute(text('SELECT * FROM ncm_backup_runs WHERE job_id = ANY(:ids) ORDER BY started_at'), {'ids': ids})).mappings().all():
            runs.setdefault(x['job_id'], []).append(x)
    return [_job_view(r, runs.get(r['id'], [])) for r in rows]


@device_router.get('/{device_id}/ncm-jobs')
async def backup_jobs(device_id: UUID, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
                      db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    total = (await db.execute(text('SELECT count(*) FROM ncm_jobs WHERE device_id=:id'), {'id': device_id})).scalar()
    rows = (await db.execute(text('''SELECT j.*,d.hostname FROM ncm_jobs j JOIN devices d ON d.id=j.device_id
        WHERE j.device_id=:id ORDER BY j.created_at DESC LIMIT :limit OFFSET :offset'''),
        {'id': device_id, 'limit': limit, 'offset': offset})).mappings().all()
    return {'data': await _jobs_with_runs(db, rows), 'total': total, 'limit': limit, 'offset': offset}


@router.get('/jobs')
async def fleet_jobs(status: Optional[str] = None, trigger: Optional[str] = None, q: Optional[str] = None,
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    """Fleet job/attempt history with queue, worker and schedule state."""
    from app.services.ncm_readiness import runtime_health
    scope = await visible_tags(db, user)
    clauses, params = ['TRUE'], {'vis_tags': scope, 'limit': limit, 'offset': offset}
    if scope:
        clauses.append(jsonb_tags_visible('d.tags'))
    if status:
        clauses.append('j.status = ANY(:statuses)'); params['statuses'] = status.split(',')
    if trigger:
        clauses.append('j.trigger = ANY(:triggers)'); params['triggers'] = trigger.split(',')
    if q:
        clauses.append("(d.hostname ILIKE :q OR host(d.ip_address) LIKE :q)"); params['q'] = f'%{q}%'
    where = ' AND '.join(clauses)
    total = (await db.execute(text(f'SELECT count(*) FROM ncm_jobs j JOIN devices d ON d.id=j.device_id WHERE {where}'), params)).scalar()
    rows = (await db.execute(text(f'''SELECT j.*,d.hostname FROM ncm_jobs j JOIN devices d ON d.id=j.device_id
        WHERE {where} ORDER BY j.created_at DESC LIMIT :limit OFFSET :offset'''), params)).mappings().all()
    scope_clause = (' AND ' + jsonb_tags_visible('d.tags')) if scope else ''
    queue = (await db.execute(text(f'''SELECT count(*) FILTER (WHERE j.status='queued') AS queued,
        count(*) FILTER (WHERE j.status='running') AS running,
        count(*) FILTER (WHERE j.status='failed' AND j.finished_at>now()-interval '24 hours') AS failed_24h,
        count(*) FILTER (WHERE j.status='success' AND j.finished_at>now()-interval '24 hours') AS success_24h,
        min(j.created_at) FILTER (WHERE j.status='queued') AS oldest_queued
        FROM ncm_jobs j JOIN devices d ON d.id=j.device_id WHERE TRUE{scope_clause}'''), {'vis_tags': scope})).mappings().first()
    runtime = await asyncio.to_thread(runtime_health)
    return {'data': await _jobs_with_runs(db, rows), 'total': total, 'limit': limit, 'offset': offset,
            'queue': {**{k: v for k, v in queue.items() if k != 'oldest_queued'},
                      'oldest_queued': queue['oldest_queued'].isoformat() if queue['oldest_queued'] else None},
            'runtime': runtime}


@device_router.post('/{device_id}/ncm-jobs/{job_id}/cancel')
async def cancel_backup(device_id: UUID, job_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage'))):
    await _device_access(db, user, device_id)
    row = (await db.execute(text('''UPDATE ncm_jobs SET cancel_requested=true,
        status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
        finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END
        WHERE id=:job AND device_id=:device AND status IN ('queued','running') RETURNING status'''), {'job': job_id, 'device': device_id})).first()
    if not row:
        raise HTTPException(409, 'Job is absent or already finished')
    await ncm_events.audit(db, user, 'job.cancel_requested', device_id=device_id, resource_id=job_id)
    await db.commit()
    return {'status': row.status, 'message': 'Cancellation takes effect before the next configuration capture'}


@device_router.get('/{device_id}/ncm-runs')
async def backup_runs(device_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      status: Optional[str] = Query(default=None, pattern='^(running|success|failed|interrupted)$'),
                      db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    extra = ' AND status=:status' if status else ''
    params = {'id': device_id, 'limit': limit, 'offset': offset, 'status': status}
    total = (await db.execute(text('SELECT count(*) FROM ncm_backup_runs WHERE device_id=:id' + extra), params)).scalar()
    rows = (await db.execute(text('SELECT * FROM ncm_backup_runs WHERE device_id=:id' + extra + ' ORDER BY started_at DESC LIMIT :limit OFFSET :offset'), params)).mappings().all()
    return {'data': [_run_view(r) for r in rows], 'total': total, 'limit': limit, 'offset': offset}


@device_router.get('/{device_id}/ncm-incidents')
async def device_incidents(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(text('SELECT * FROM ncm_incidents WHERE device_id=:id ORDER BY opened_at DESC LIMIT 50'), {'id': device_id})).mappings().all()
    return {'data': [{**{k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v) for k, v in r.items()},
                      'recommended_action': GUIDANCE.get(r['failure_category'])} for r in rows]}


@device_router.get('/{device_id}/ncm-change-events')
async def device_change_events(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(text('''SELECT id,source,received_at,summary,reported_user,job_id,version_id,processed_at
        FROM ncm_change_events WHERE device_id=:id ORDER BY received_at DESC LIMIT 100'''), {'id': device_id})).mappings().all()
    return {'data': [{k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v) for k, v in r.items()} for r in rows]}


class ChangeEventIn(BaseModel):
    summary: str = Field(min_length=3, max_length=500)
    reported_user: Optional[str] = Field(default=None, max_length=120)
    reference: Optional[str] = Field(default=None, max_length=120)


@device_router.post('/{device_id}/ncm-change-events', status_code=202)
async def report_change_event(device_id: UUID, data: ChangeEventIn, db: AsyncSession = Depends(get_db),
                              user: User = Depends(require_permission('ncm.manage'))):
    """An approved change (e.g. from a change-management system) announces a
    configuration change; a debounced backup follows like a syslog event."""
    await _device_access(db, user, device_id)
    if not (await db.execute(text('SELECT 1 FROM device_ncm WHERE device_id=:d'), {'d': device_id})).first():
        raise HTTPException(409, 'Device is not enrolled for backup')
    from uuid import uuid4
    summary = data.summary + (f' [{data.reference}]' if data.reference else '')
    row = (await db.execute(text('''INSERT INTO ncm_change_events(device_id,source,source_ref,received_at,summary,reported_user)
        VALUES(:d,'api',:ref,now(),:s,:u) RETURNING id'''),
        {'d': device_id, 'ref': str(uuid4()), 's': summary, 'u': data.reported_user})).scalar()
    await ncm_events.audit(db, user, 'change_event.report', device_id=device_id, resource_id=row, metadata={'reference': data.reference})
    await db.commit()
    return {'id': str(row), 'message': 'Change recorded; a backup is queued after a short quiet period.'}


# --------------------------------------------------------------------------- #
# Baselines, pins and restore evidence
# --------------------------------------------------------------------------- #
class BaselineIn(BaseModel):
    version_id: UUID
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=1000)


@device_router.post('/{device_id}/ncm-baselines', status_code=201)
async def approve_baseline(device_id: UUID, data: BaselineIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.baseline'))):
    await _device_access(db, user, device_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:device,0))'), {'device': str(device_id)})
    version = (await db.execute(text('SELECT config_type,validated FROM device_configs WHERE id=:version AND device_id=:device FOR UPDATE'), {'version': data.version_id, 'device': device_id})).first()
    if not version:
        raise HTTPException(404, 'Configuration version not found')
    if not version.validated:
        raise HTTPException(422, 'A baseline requires a validated device capture')
    await db.execute(text('UPDATE ncm_baselines SET retired_at=now() WHERE device_id=:device AND config_type=:type AND retired_at IS NULL'), {'device': device_id, 'type': version.config_type})
    await db.execute(text('UPDATE device_configs SET pinned=true WHERE id=:version'), {'version': data.version_id})
    baseline = (await db.execute(text('''INSERT INTO ncm_baselines(device_id,version_id,config_type,name,reason,actor_id,actor_name)
        VALUES(:device,:version,:type,:name,:reason,:actor,:username) RETURNING id'''), {'device': device_id, 'version': data.version_id, 'type': version.config_type, 'name': data.name, 'reason': data.reason, 'actor': user.id, 'username': user.username})).scalar()
    await ncm_events.audit(db, user, 'baseline.approve', device_id=device_id, resource_id=baseline, metadata={'version': str(data.version_id)})
    await db.commit()
    return {'id': str(baseline)}


@device_router.get('/{device_id}/ncm-baselines')
async def baselines(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    """Active and retired baselines with drift of the latest capture against
    the active one (normalized comparison: runtime noise is ignored)."""
    await _device_access(db, user, device_id)
    rows = (await db.execute(text('''SELECT b.*,c.captured_at AS version_captured_at,c.norm_hash AS baseline_norm,
        (SELECT norm_hash FROM device_configs l WHERE l.device_id=b.device_id AND l.config_type=b.config_type
           ORDER BY l.captured_at DESC,l.id DESC LIMIT 1) AS latest_norm,
        (SELECT id FROM device_configs l WHERE l.device_id=b.device_id AND l.config_type=b.config_type
           ORDER BY l.captured_at DESC,l.id DESC LIMIT 1) AS latest_id
        FROM ncm_baselines b JOIN device_configs c ON c.id=b.version_id
        WHERE b.device_id=:id ORDER BY b.created_at DESC LIMIT 200'''), {'id': device_id})).mappings().all()
    out = []
    for r in rows:
        drift = None
        if r['retired_at'] is None:
            drift = 'unknown' if not (r['baseline_norm'] and r['latest_norm']) else 'in_sync' if r['baseline_norm'] == r['latest_norm'] else 'drifted'
        out.append({'id': str(r['id']), 'device_id': str(r['device_id']), 'version_id': str(r['version_id']),
                    'config_type': r['config_type'], 'name': r['name'], 'reason': r['reason'],
                    'actor_name': r['actor_name'], 'created_at': r['created_at'].isoformat(),
                    'retired_at': r['retired_at'].isoformat() if r['retired_at'] else None,
                    'version_captured_at': r['version_captured_at'].isoformat() if r['version_captured_at'] else None,
                    'drift': drift, 'latest_version_id': str(r['latest_id']) if r['latest_id'] else None})
    return {'data': out}


class RetireIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


@device_router.post('/{device_id}/ncm-baselines/{baseline_id}/retire')
async def retire_baseline(device_id: UUID, baseline_id: UUID, data: RetireIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission('ncm.baseline'))):
    """Retire the active baseline. The version stays protected by baseline history."""
    await _device_access(db, user, device_id)
    row = (await db.execute(text('''UPDATE ncm_baselines SET retired_at=now() WHERE id=:id AND device_id=:d AND retired_at IS NULL
        RETURNING id'''), {'id': baseline_id, 'd': device_id})).first()
    if not row:
        raise HTTPException(409, 'Baseline is absent or already retired')
    await ncm_events.audit(db, user, 'baseline.retire', device_id=device_id, resource_id=baseline_id, metadata={'reason': data.reason})
    await db.commit()
    return {'retired': True}


class PinIn(BaseModel):
    pinned: bool


@device_router.put('/{device_id}/configs/{version_id}/pin')
async def pin_config(device_id: UUID, version_id: UUID, data: PinIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.baseline'))):
    await _device_access(db, user, device_id)
    if not data.pinned and (await db.execute(text('SELECT 1 FROM ncm_baselines WHERE version_id=:id LIMIT 1'), {'id': version_id})).first():
        raise HTTPException(409, 'Baseline history protects this recovery version')
    row = (await db.execute(text('UPDATE device_configs SET pinned=:pinned WHERE id=:version AND device_id=:device RETURNING id'), {'pinned': data.pinned, 'version': version_id, 'device': device_id})).first()
    if not row:
        raise HTTPException(404, 'Configuration version not found')
    await ncm_events.audit(db, user, 'config.pin' if data.pinned else 'config.unpin', device_id=device_id, resource_id=version_id)
    await db.commit()
    return {'pinned': data.pinned}


class RestoreTestIn(BaseModel):
    version_id: UUID
    result: str = Field(pattern='^(passed|partial|failed)$')
    method: str = Field(pattern='^(lab_device|isolated_replica|production_rollback|vendor_tool)$')
    target: str = Field(min_length=2, max_length=200)
    scope_notes: Optional[str] = Field(default=None, max_length=2000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    tested_at: datetime

    @field_validator('tested_at')
    @classmethod
    def not_future(cls, value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value > datetime.now(timezone.utc):
            raise ValueError('Test date cannot be in the future')
        return value


@device_router.get('/{device_id}/ncm-restore-tests')
async def restore_tests(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(text('''SELECT t.*,c.captured_at AS version_captured_at,c.config_type FROM ncm_restore_tests t
        JOIN device_configs c ON c.id=t.version_id WHERE t.device_id=:id ORDER BY t.tested_at DESC'''), {'id': device_id})).mappings().all()
    return {'data': [{k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v) for k, v in r.items()} for r in rows]}


@device_router.post('/{device_id}/ncm-restore-tests', status_code=201)
async def record_restore_test(device_id: UUID, data: RestoreTestIn, db: AsyncSession = Depends(get_db),
                              user: User = Depends(require_permission('ncm.baseline'))):
    """Record the outcome of an operator-performed restore drill. Recording
    evidence never writes to a device."""
    await _device_access(db, user, device_id)
    version = (await db.execute(text('SELECT validated FROM device_configs WHERE id=:v AND device_id=:d'), {'v': data.version_id, 'd': device_id})).first()
    if not version:
        raise HTTPException(404, 'Configuration version not found')
    row = (await db.execute(text('''INSERT INTO ncm_restore_tests(device_id,version_id,result,method,target,scope_notes,notes,tested_at,actor_id,actor_name)
        VALUES(:d,:v,:result,:method,:target,:scope,:notes,:at,:actor,:name) RETURNING id'''),
        {'d': device_id, 'v': data.version_id, 'result': data.result, 'method': data.method, 'target': data.target,
         'scope': data.scope_notes, 'notes': data.notes, 'at': data.tested_at, 'actor': user.id, 'name': user.username})).scalar()
    await ncm_events.audit(db, user, 'restore_test.record', device_id=device_id, resource_id=row,
                           metadata={'version': str(data.version_id), 'result': data.result, 'method': data.method})
    await db.commit()
    return {'id': str(row)}


@router.get('/compare')
async def compare_versions(device_a: UUID, version_a: UUID, device_b: UUID, version_b: UUID, raw: bool = False, normalized: bool = True, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _bulk_access(db, user, [device_a, device_b])
    if raw and not await user_has_permission(db, user, 'ncm.export'):
        raise HTTPException(403, 'Raw configuration export permission required')
    contents, meta = [], []
    for device, version in [(device_a, version_a), (device_b, version_b)]:
        row = (await db.execute(text('SELECT content,content_enc,content_hash,config_type,platform FROM device_configs WHERE id=:v AND device_id=:d'), {'v': version, 'd': device})).first()
        if not row:
            raise HTTPException(404, 'Configuration version not found')
        contents.append(read_content(row))
        meta.append({'config_type': row.config_type, 'platform': row.platform})
    result = config_diff(*contents, raw=raw, normalized=normalized)
    warnings = []
    if meta[0]['platform'] and meta[1]['platform'] and meta[0]['platform'] != meta[1]['platform']:
        warnings.append('The versions come from different platform drivers; syntax differences are expected.')
    if meta[0]['config_type'] != meta[1]['config_type']:
        warnings.append('Comparing different configuration types (running vs startup).')
    for device, version in [(device_a, version_a), (device_b, version_b)]:
        await ncm_events.audit(db, user, 'config.compare', device_id=device, resource_id=version, metadata={'raw': raw})
    await db.commit()
    return {**result, 'warnings': warnings}


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@router.get('/notification-channels')
async def notification_channels(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage'))):
    from app.services.ncm_delivery import SUPPORTED_CHANNELS
    rows = (await db.execute(text('SELECT id,name,type,enabled FROM notification_channels ORDER BY name'))).mappings().all()
    return {'data': [{**dict(r), 'id': str(r['id'])} for r in rows if r['enabled'] and r['type'] in SUPPORTED_CHANNELS],
            'unsupported': [{'id': str(r['id']), 'name': r['name'], 'type': r['type']} for r in rows if r['enabled'] and r['type'] not in SUPPORTED_CHANNELS],
            'supported_types': list(SUPPORTED_CHANNELS)}


def _delivery_view(r):
    return {'id': str(r['id']), 'device_id': str(r['device_id']), 'hostname': r.get('hostname'), 'event_key': r['event_key'],
            'event_type': (r.get('payload') or {}).get('event_type'), 'subject': (r.get('payload') or {}).get('subject'),
            'channel_id': str(r['channel_id']), 'channel_name': r.get('channel_name') or 'Deleted channel',
            'channel_type': r.get('channel_type'), 'status': r['status'], 'attempts': r['attempts'],
            'created_at': r['created_at'].isoformat(), 'delivered_at': r['delivered_at'].isoformat() if r['delivered_at'] else None,
            'next_attempt_at': r['next_attempt_at'].isoformat() if r.get('next_attempt_at') else None,
            'last_error': r['last_error']}


_DELIVERY_SQL = '''SELECT o.*,c.name AS channel_name,c.type AS channel_type,d.hostname FROM ncm_notification_outbox o
    JOIN devices d ON d.id=o.device_id LEFT JOIN notification_channels c ON c.id=o.channel_id'''


@device_router.get('/{device_id}/ncm-deliveries')
async def delivery_history(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(text(_DELIVERY_SQL + ' WHERE o.device_id=:device ORDER BY o.created_at DESC LIMIT 100'), {'device': device_id})).mappings().all()
    return {'data': [_delivery_view(r) for r in rows]}


@router.get('/deliveries')
async def fleet_deliveries(status: Optional[str] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                           db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    scope = await visible_tags(db, user)
    clauses, params = ['TRUE'], {'vis_tags': scope, 'limit': limit, 'offset': offset}
    if scope:
        clauses.append(jsonb_tags_visible('d.tags'))
    if status:
        clauses.append('o.status = ANY(:statuses)'); params['statuses'] = status.split(',')
    where = ' WHERE ' + ' AND '.join(clauses)
    total = (await db.execute(text('SELECT count(*) FROM ncm_notification_outbox o JOIN devices d ON d.id=o.device_id' + where), params)).scalar()
    rows = (await db.execute(text(_DELIVERY_SQL + where + ' ORDER BY o.created_at DESC LIMIT :limit OFFSET :offset'), params)).mappings().all()
    return {'data': [_delivery_view(r) for r in rows], 'total': total}


@router.post('/deliveries/{delivery_id}/retry')
async def retry_delivery(delivery_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage'))):
    """Deliberately replay a failed or disabled delivery (receivers dedupe on event_id)."""
    row = (await db.execute(text('SELECT device_id,status FROM ncm_notification_outbox WHERE id=:id'), {'id': delivery_id})).first()
    if not row:
        raise HTTPException(404, 'Delivery not found')
    await _device_access(db, user, row.device_id)
    if row.status not in ('failed', 'disabled'):
        raise HTTPException(409, 'Only failed or disabled deliveries can be replayed')
    await db.execute(text("UPDATE ncm_notification_outbox SET status='pending',attempts=0,next_attempt_at=now() WHERE id=:id"), {'id': delivery_id})
    await ncm_events.audit(db, user, 'delivery.replay', device_id=row.device_id, resource_id=delivery_id)
    await db.commit()
    return {'status': 'pending'}


# --------------------------------------------------------------------------- #
# Audit and archive search
# --------------------------------------------------------------------------- #
@router.get('/audit')
async def audit_log(device_id: Optional[UUID] = None, action: Optional[str] = None, actor: Optional[str] = None,
                    since: Optional[datetime] = None, until: Optional[datetime] = None,
                    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                    db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.manage', 'audit.view'))):
    """Searchable NCCM audit trail. Scoped users see only records for devices
    in their scope (never fleet-wide credential records)."""
    scope = await visible_tags(db, user)
    clauses, params = ['TRUE'], {'vis_tags': scope, 'limit': limit, 'offset': offset}
    if scope:
        clauses.append('a.device_id IS NOT NULL AND ' + jsonb_tags_visible('d.tags'))
    if device_id:
        clauses.append('a.device_id=:device'); params['device'] = device_id
    if action:
        clauses.append('a.action LIKE :action'); params['action'] = action.replace('*', '%')
    if actor:
        clauses.append('a.actor_name ILIKE :actor'); params['actor'] = f'%{actor}%'
    if since:
        clauses.append('a.created_at>=:since'); params['since'] = since
    if until:
        clauses.append('a.created_at<=:until'); params['until'] = until
    where = ' WHERE ' + ' AND '.join(clauses)
    base = ' FROM ncm_audit a LEFT JOIN devices d ON d.id=a.device_id' + where
    total = (await db.execute(text('SELECT count(*)' + base), params)).scalar()
    rows = (await db.execute(text('SELECT a.*,d.hostname' + base + ' ORDER BY a.created_at DESC LIMIT :limit OFFSET :offset'), params)).mappings().all()
    actions = [r[0] for r in (await db.execute(text('SELECT DISTINCT action FROM ncm_audit ORDER BY 1'))).fetchall()]
    return {'data': [{'id': str(r['id']), 'created_at': r['created_at'].isoformat(), 'actor_name': r['actor_name'],
                      'action': r['action'], 'device_id': str(r['device_id']) if r['device_id'] else None,
                      'hostname': r['hostname'], 'resource_id': r['resource_id'], 'metadata': r['metadata']} for r in rows],
            'total': total, 'actions': actions}


@router.get('/search')
async def search_archive(q: str = Query(min_length=2, max_length=200), regex: bool = False,
                         config_type: str = Query(default='running', pattern='^(running|startup)$'),
                         limit: int = Query(default=50, ge=1, le=200),
                         db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    """Search the latest stored configuration of every visible device.
    Matching runs on the REDACTED text unless the caller may export raw
    configurations, so a search can never reveal a secret value."""
    raw = await user_has_permission(db, user, 'ncm.export')
    if regex and not await user_has_permission(db, user, 'ncm.manage'):
        raise HTTPException(403, 'Regular-expression search requires NCM manage permission')
    try:
        pattern = safe_pattern(q if regex else re.escape(q), max_length=200)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    scope = await visible_tags(db, user)
    predicate = ' AND ' + jsonb_tags_visible('d.tags') if scope else ''
    rows = (await db.execute(text(f'''SELECT DISTINCT ON (c.device_id) c.id,c.device_id,c.content,c.content_enc,c.content_hash,
        c.captured_at,c.validated,d.hostname,host(d.ip_address) AS ip
        FROM device_configs c JOIN devices d ON d.id=c.device_id
        WHERE c.config_type=:t{predicate} ORDER BY c.device_id,c.captured_at DESC,c.id DESC'''),
        {'t': config_type, 'vis_tags': scope})).fetchall()
    results, devices_matched, total_matches = [], 0, 0
    for r in rows:
        try:
            content = read_content(r)
        except Exception:
            continue
        text_ = content if raw else redact_config(content)
        matches = []
        for number, line in enumerate(text_.splitlines(), 1):
            if pattern.search(line):
                matches.append({'line': number, 'text': line[:400]})
        if matches:
            devices_matched += 1
            total_matches += len(matches)
            if len(results) < limit:
                results.append({'device_id': str(r.device_id), 'hostname': r.hostname, 'ip': r.ip, 'version_id': str(r.id),
                                'captured_at': r.captured_at.isoformat(), 'validated': r.validated,
                                'match_count': len(matches), 'matches': matches[:20]})
    await ncm_events.audit(db, user, 'archive.search', metadata={'query_length': len(q), 'regex': regex, 'raw': raw,
                                                                  'devices_matched': devices_matched})
    await db.commit()
    return {'data': results, 'devices_searched': len(rows), 'devices_matched': devices_matched,
            'total_matches': total_matches, 'redacted': not raw}
