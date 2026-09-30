"""NCCM compliance workspace: policy packs, results, exceptions and trends."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.scoping import visible_tags, jsonb_tags_visible
from app.core.security import require_permission
from app.models.user import User
from app.services import ncm_compliance, ncm_events
from app.services.ncm_collection import DRIVERS
from app.services.ncm_content import read_content, safe_pattern
from app.api.v1.ncm import _device_access, _global_credentials_access

router = APIRouter(prefix="/ncm/compliance", tags=["NCM Compliance"])
device_router = APIRouter(prefix="/devices", tags=["NCM Compliance"])


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v


async def _policy_admin(db, user):
    # Policies and fleet-wide exceptions affect every device; they require
    # unrestricted device scope, like shared connection profiles.
    await _global_credentials_access(db, user)


class RuleIn(BaseModel):
    rule_key: Optional[str] = Field(default=None, max_length=80)
    title: str = Field(min_length=3, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    severity: str = Field(pattern='^(critical|high|medium|low|info)$')
    check_type: str = Field(pattern='^(must_match|must_not_match|section_must_match|section_must_not_match|section_any_must_match)$')
    pattern: str = Field(min_length=1, max_length=300)
    section_pattern: Optional[str] = Field(default=None, max_length=300)
    platforms: Optional[list[str]] = None
    remediation: Optional[str] = Field(default=None, max_length=2000)
    enabled: bool = True

    @field_validator('pattern', 'section_pattern')
    @classmethod
    def valid_pattern(cls, value):
        if value is not None:
            try:
                safe_pattern(value)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
        return value

    @field_validator('platforms')
    @classmethod
    def valid_platforms(cls, value):
        if value and any(p not in DRIVERS for p in value):
            raise ValueError('Unknown platform driver')
        return value or None


class PolicyIn(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    platforms: Optional[list[str]] = None
    enabled: bool = True
    rules: list[RuleIn] = Field(default_factory=list, max_length=200)

    @field_validator('platforms')
    @classmethod
    def valid_platforms(cls, value):
        if value and any(p not in DRIVERS for p in value):
            raise ValueError('Unknown platform driver')
        return value or None


def _slug(title, used):
    import re
    base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60] or 'rule'
    key, n = base, 2
    while key in used:
        key, n = f'{base}-{n}', n + 1
    used.add(key)
    return key


async def _scope_clause(db, user, alias='d'):
    scope = await visible_tags(db, user)
    return (' AND ' + jsonb_tags_visible(f'{alias}.tags')) if scope else '', scope


@router.get('/summary')
async def summary(days: int = Query(30, ge=7, le=365), db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await ncm_compliance.ensure_builtin(db)
    await db.commit()
    clause, scope = await _scope_clause(db, user)
    params = {'vis_tags': scope}
    per_device = (await db.execute(text(f'''SELECT r.device_id,
        count(*) FILTER (WHERE r.status='fail') AS fail, count(*) FILTER (WHERE r.status='unknown') AS unknown
        FROM ncm_compliance_results r JOIN devices d ON d.id=r.device_id WHERE TRUE{clause} GROUP BY r.device_id'''), params)).fetchall()
    severity = (await db.execute(text(f'''SELECT ru.severity,count(*) AS n FROM ncm_compliance_results r
        JOIN ncm_policy_rules ru ON ru.id=r.rule_id JOIN devices d ON d.id=r.device_id
        WHERE r.status='fail'{clause} GROUP BY ru.severity'''), params)).fetchall()
    policies = (await db.execute(text(f'''SELECT p.id,p.name,p.enabled,p.builtin_key,p.version,p.platforms,
        (SELECT count(*) FROM ncm_policy_rules x WHERE x.policy_id=p.id AND x.enabled) AS rules,
        count(DISTINCT r.device_id) AS devices,
        count(*) FILTER (WHERE r.status='pass') AS passed, count(*) FILTER (WHERE r.status='fail') AS failed,
        count(*) FILTER (WHERE r.status='unknown') AS unknown, count(*) FILTER (WHERE r.status='excepted') AS excepted,
        max(r.assessed_at) AS assessed_at
        FROM ncm_policies p LEFT JOIN (ncm_compliance_results r JOIN devices d ON d.id=r.device_id{clause})
          ON r.policy_id=p.id
        GROUP BY p.id ORDER BY p.enabled DESC,p.name'''), params)).fetchall()
    trend = (await db.execute(text('''SELECT day,sum(devices) AS devices,sum(passed) AS passed,sum(failed) AS failed,
        sum(unknown) AS unknown,sum(excepted) AS excepted FROM ncm_compliance_snapshots
        WHERE day>=current_date-CAST(:days AS integer) GROUP BY day ORDER BY day'''), {'days': days})).fetchall()
    expiring = (await db.execute(text('''SELECT count(*) FROM ncm_compliance_exceptions WHERE revoked_at IS NULL
        AND expires_at>now() AND expires_at<now()+interval '14 days' '''))).scalar()
    enrolled = (await db.execute(text(f'SELECT count(*) FROM device_ncm n JOIN devices d ON d.id=n.device_id WHERE TRUE{clause}'), params)).scalar()
    return {
        'devices_assessed': len(per_device),
        'devices_enrolled': enrolled,
        'devices_compliant': sum(1 for r in per_device if not r.fail and not r.unknown),
        'devices_failing': sum(1 for r in per_device if r.fail),
        'devices_unknown': sum(1 for r in per_device if r.unknown and not r.fail),
        'failures_by_severity': {s: next((r.n for r in severity if r.severity == s), 0) for s in ncm_compliance.SEVERITIES},
        'policies': [{'id': str(p.id), 'name': p.name, 'enabled': p.enabled, 'builtin': bool(p.builtin_key), 'version': p.version,
                      'platforms': p.platforms, 'rules': p.rules, 'devices': p.devices, 'passed': p.passed, 'failed': p.failed,
                      'unknown': p.unknown, 'excepted': p.excepted, 'assessed_at': _iso(p.assessed_at)} for p in policies],
        'trend': [{'day': r.day.isoformat(), 'devices': r.devices, 'passed': r.passed, 'failed': r.failed,
                   'unknown': r.unknown, 'excepted': r.excepted} for r in trend],
        'trend_note': 'Trend points are recorded once per day by the scheduler; days before the first evaluation have no data.',
        'exceptions_expiring_14d': expiring,
    }


@router.get('/policies')
async def list_policies(db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await ncm_compliance.ensure_builtin(db)
    await db.commit()
    policies = (await db.execute(text('SELECT * FROM ncm_policies ORDER BY builtin_key IS NULL, name'))).mappings().all()
    rules = (await db.execute(text('SELECT * FROM ncm_policy_rules ORDER BY position,title'))).mappings().all()
    by_policy = {}
    for r in rules:
        by_policy.setdefault(r['policy_id'], []).append({k: _iso(v) for k, v in r.items()})
    return {'data': [{**{k: _iso(v) for k, v in p.items()}, 'builtin': bool(p['builtin_key']),
                      'rules': by_policy.get(p['id'], [])} for p in policies]}


async def _write_rules(db, policy_id, rules):
    existing = {r.rule_key for r in (await db.execute(text('SELECT rule_key FROM ncm_policy_rules WHERE policy_id=:p'), {'p': policy_id})).fetchall()}
    used, keys = set(), []
    for position, rule in enumerate(rules):
        key = rule.rule_key if rule.rule_key else _slug(rule.title, used | existing)
        used.add(key)
        keys.append(key)
        if rule.check_type.startswith('section') and not rule.section_pattern:
            raise HTTPException(422, f'Rule "{rule.title}" needs a section pattern')
        await db.execute(text('''INSERT INTO ncm_policy_rules(policy_id,rule_key,title,description,severity,check_type,pattern,section_pattern,platforms,remediation,enabled,position)
            VALUES(:p,:k,:t,:d,:s,:c,:pat,:sec,:plat,:rem,:en,:pos)
            ON CONFLICT(policy_id,rule_key) DO UPDATE SET title=:t,description=:d,severity=:s,check_type=:c,pattern=:pat,
              section_pattern=:sec,platforms=:plat,remediation=:rem,enabled=:en,position=:pos'''),
            {'p': policy_id, 'k': key, 't': rule.title, 'd': rule.description, 's': rule.severity, 'c': rule.check_type,
             'pat': rule.pattern, 'sec': rule.section_pattern, 'plat': rule.platforms, 'rem': rule.remediation,
             'en': rule.enabled, 'pos': position})
    await db.execute(text('DELETE FROM ncm_policy_rules WHERE policy_id=:p AND NOT (rule_key = ANY(:keys))'), {'p': policy_id, 'keys': keys})


@router.post('/policies', status_code=201)
async def create_policy(data: PolicyIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    await _policy_admin(db, user)
    policy_id = (await db.execute(text('''INSERT INTO ncm_policies(name,description,platforms,enabled,created_by)
        VALUES(:n,:d,:p,:e,:u) RETURNING id'''), {'n': data.name, 'd': data.description, 'p': data.platforms,
                                                  'e': data.enabled, 'u': user.username})).scalar()
    await _write_rules(db, policy_id, data.rules)
    await ncm_events.audit(db, user, 'compliance.policy_create', resource_id=policy_id, metadata={'rules': len(data.rules)})
    await db.commit()
    return {'id': str(policy_id)}


@router.put('/policies/{policy_id}')
async def update_policy(policy_id: UUID, data: PolicyIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    await _policy_admin(db, user)
    row = (await db.execute(text('SELECT builtin_key FROM ncm_policies WHERE id=:id FOR UPDATE'), {'id': policy_id})).first()
    if not row:
        raise HTTPException(404, 'Policy not found')
    if row.builtin_key:
        # Built-in packs are versioned with the product: only enable/disable.
        await db.execute(text('UPDATE ncm_policies SET enabled=:e,updated_at=now() WHERE id=:id'), {'id': policy_id, 'e': data.enabled})
    else:
        await db.execute(text('''UPDATE ncm_policies SET name=:n,description=:d,platforms=:p,enabled=:e,version=version+1,updated_at=now()
            WHERE id=:id'''), {'id': policy_id, 'n': data.name, 'd': data.description, 'p': data.platforms, 'e': data.enabled})
        await _write_rules(db, policy_id, data.rules)
    await ncm_events.audit(db, user, 'compliance.policy_update', resource_id=policy_id, metadata={'enabled': data.enabled})
    await db.commit()
    return {'id': str(policy_id)}


class EnabledIn(BaseModel):
    enabled: bool


@router.put('/policies/{policy_id}/enabled')
async def set_policy_enabled(policy_id: UUID, data: EnabledIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    await _policy_admin(db, user)
    row = (await db.execute(text('UPDATE ncm_policies SET enabled=:e,updated_at=now() WHERE id=:id RETURNING id'), {'id': policy_id, 'e': data.enabled})).first()
    if not row:
        raise HTTPException(404, 'Policy not found')
    if not data.enabled:
        await db.execute(text('DELETE FROM ncm_compliance_results WHERE policy_id=:p'), {'p': policy_id})
    await ncm_events.audit(db, user, 'compliance.policy_enable' if data.enabled else 'compliance.policy_disable', resource_id=policy_id)
    await db.commit()
    return {'enabled': data.enabled}


@router.post('/policies/{policy_id}/clone', status_code=201)
async def clone_policy(policy_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    await _policy_admin(db, user)
    src = (await db.execute(text('SELECT * FROM ncm_policies WHERE id=:id'), {'id': policy_id})).mappings().first()
    if not src:
        raise HTTPException(404, 'Policy not found')
    new_id = (await db.execute(text('''INSERT INTO ncm_policies(name,description,platforms,enabled,created_by)
        VALUES(:n,:d,:p,false,:u) RETURNING id'''), {'n': f"{src['name']} (custom)", 'd': src['description'],
                                                    'p': src['platforms'], 'u': user.username})).scalar()
    await db.execute(text('''INSERT INTO ncm_policy_rules(policy_id,rule_key,title,description,severity,check_type,pattern,section_pattern,platforms,remediation,enabled,position)
        SELECT :new,rule_key,title,description,severity,check_type,pattern,section_pattern,platforms,remediation,enabled,position
        FROM ncm_policy_rules WHERE policy_id=:src'''), {'new': new_id, 'src': policy_id})
    await ncm_events.audit(db, user, 'compliance.policy_clone', resource_id=new_id, metadata={'source': str(policy_id)})
    await db.commit()
    return {'id': str(new_id)}


@router.delete('/policies/{policy_id}')
async def delete_policy(policy_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    await _policy_admin(db, user)
    row = (await db.execute(text('SELECT builtin_key FROM ncm_policies WHERE id=:id'), {'id': policy_id})).first()
    if not row:
        raise HTTPException(404, 'Policy not found')
    if row.builtin_key:
        raise HTTPException(409, 'Built-in packs cannot be deleted; disable them instead')
    await db.execute(text('DELETE FROM ncm_policies WHERE id=:id'), {'id': policy_id})
    await ncm_events.audit(db, user, 'compliance.policy_delete', resource_id=policy_id)
    await db.commit()
    return {'deleted': True}


class RuleTestIn(BaseModel):
    device_id: UUID
    rule: RuleIn


@router.post('/rules/test')
async def test_rule(data: RuleTestIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    """Dry-run a rule against a device's latest validated running configuration."""
    await _device_access(db, user, data.device_id)
    row = (await db.execute(text('''SELECT content,content_enc,content_hash,captured_at FROM device_configs WHERE device_id=:d
        AND config_type='running' AND validated ORDER BY captured_at DESC,id DESC LIMIT 1'''), {'d': data.device_id})).first()
    if not row:
        return {'status': 'unknown', 'detail': 'This device has no validated running configuration.', 'evidence': []}
    if data.rule.check_type.startswith('section') and not data.rule.section_pattern:
        raise HTTPException(422, 'Section checks need a section pattern')
    status, detail, evidence = ncm_compliance.check(data.rule.model_dump(), read_content(row))
    return {'status': status, 'detail': detail, 'evidence': evidence, 'captured_at': row.captured_at.isoformat()}


@router.get('/results')
async def results(policy_id: Optional[UUID] = None, status: Optional[str] = None, severity: Optional[str] = None,
                  device_id: Optional[UUID] = None, q: Optional[str] = None,
                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                  db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    clause, scope = await _scope_clause(db, user)
    clauses, params = ['TRUE' + clause], {'vis_tags': scope, 'limit': limit, 'offset': offset}
    if policy_id:
        clauses.append('r.policy_id=:policy'); params['policy'] = policy_id
    if status:
        clauses.append('r.status = ANY(:statuses)'); params['statuses'] = status.split(',')
    if severity:
        clauses.append('ru.severity = ANY(:severities)'); params['severities'] = severity.split(',')
    if device_id:
        clauses.append('r.device_id=:device'); params['device'] = device_id
    if q:
        clauses.append('(d.hostname ILIKE :q OR ru.title ILIKE :q)'); params['q'] = f'%{q}%'
    base = f'''FROM ncm_compliance_results r JOIN ncm_policy_rules ru ON ru.id=r.rule_id JOIN ncm_policies p ON p.id=r.policy_id
        JOIN devices d ON d.id=r.device_id WHERE {' AND '.join(clauses)}'''
    total = (await db.execute(text('SELECT count(*) ' + base), params)).scalar()
    rows = (await db.execute(text(f'''SELECT r.*,ru.title,ru.severity,ru.remediation,ru.check_type,ru.rule_key,p.name AS policy_name,
        d.hostname,host(d.ip_address) AS ip {base}
        ORDER BY CASE r.status WHEN 'fail' THEN 0 WHEN 'unknown' THEN 1 WHEN 'excepted' THEN 2 ELSE 3 END,
          array_position(ARRAY['critical','high','medium','low','info'],ru.severity), d.hostname
        LIMIT :limit OFFSET :offset'''), params)).mappings().all()
    return {'data': [{k: _iso(v) for k, v in r.items()} for r in rows], 'total': total}


@device_router.get('/{device_id}/ncm-compliance')
async def device_compliance(device_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    await _device_access(db, user, device_id)
    rows = (await db.execute(text('''SELECT r.*,ru.title,ru.severity,ru.remediation,ru.check_type,ru.description,p.name AS policy_name,
        c.captured_at AS version_captured_at
        FROM ncm_compliance_results r JOIN ncm_policy_rules ru ON ru.id=r.rule_id JOIN ncm_policies p ON p.id=r.policy_id
        LEFT JOIN device_configs c ON c.id=r.version_id WHERE r.device_id=:d
        ORDER BY p.name,CASE r.status WHEN 'fail' THEN 0 WHEN 'unknown' THEN 1 WHEN 'excepted' THEN 2 ELSE 3 END,ru.position'''),
        {'d': device_id})).mappings().all()
    platform = (await db.execute(text('SELECT platform FROM device_ncm WHERE device_id=:d'), {'d': device_id})).scalar()
    return {'data': [{k: _iso(v) for k, v in r.items()} for r in rows],
            'platform_resolved': bool(platform and platform != 'autodetect'),
            'note': None if rows else ('Select or detect the platform driver to assess this device.' if not platform or platform == 'autodetect'
                                       else 'No enabled policy applies to this platform yet, or it has not been evaluated.')}


@router.post('/evaluate')
async def evaluate(device_id: Optional[UUID] = None, db: AsyncSession = Depends(get_db),
                   user: User = Depends(require_permission('ncm.manage', 'ncm.compliance'))):
    clause, scope = await _scope_clause(db, user)
    if device_id:
        await _device_access(db, user, device_id)
        clause += ' AND n.device_id=:device'
    ids = [r.device_id for r in (await db.execute(text(f'SELECT n.device_id FROM device_ncm n JOIN devices d ON d.id=n.device_id WHERE TRUE{clause}'),
                                                  {'vis_tags': scope, 'device': device_id})).fetchall()]
    totals = await ncm_compliance.evaluate_all(db, ids)
    await ncm_events.audit(db, user, 'compliance.evaluate', metadata=totals)
    await db.commit()
    return totals


class ExceptionIn(BaseModel):
    rule_id: UUID
    device_id: Optional[UUID] = None
    owner: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=5, max_length=2000)
    expires_at: datetime

    @field_validator('expires_at')
    @classmethod
    def bounded(cls, value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if value <= now:
            raise ValueError('Expiry must be in the future')
        if value > now + timedelta(days=366):
            raise ValueError('Exceptions may last at most one year; renew them deliberately')
        return value


@router.get('/exceptions')
async def list_exceptions(include_inactive: bool = False, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.view'))):
    clause, scope = await _scope_clause(db, user)
    visibility = f' AND (e.device_id IS NULL OR EXISTS(SELECT 1 FROM devices d WHERE d.id=e.device_id{clause}))'
    active = '' if include_inactive else ' AND e.revoked_at IS NULL AND e.expires_at>now()'
    rows = (await db.execute(text(f'''SELECT e.*,ru.title,ru.severity,p.name AS policy_name,d.hostname FROM ncm_compliance_exceptions e
        JOIN ncm_policy_rules ru ON ru.id=e.rule_id JOIN ncm_policies p ON p.id=ru.policy_id LEFT JOIN devices d ON d.id=e.device_id
        WHERE TRUE{visibility}{active} ORDER BY e.expires_at'''), {'vis_tags': scope})).mappings().all()
    now = datetime.now(timezone.utc)
    return {'data': [{**{k: _iso(v) for k, v in r.items()},
                      'state': 'revoked' if r['revoked_at'] else 'expired' if r['expires_at'] <= now else
                               'expiring' if r['expires_at'] < now + timedelta(days=14) else 'active'} for r in rows]}


@router.post('/exceptions', status_code=201)
async def create_exception(data: ExceptionIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    if data.device_id:
        await _device_access(db, user, data.device_id)
    else:
        await _policy_admin(db, user)
    if not (await db.execute(text('SELECT 1 FROM ncm_policy_rules WHERE id=:id'), {'id': data.rule_id})).first():
        raise HTTPException(404, 'Rule not found')
    row = (await db.execute(text('''INSERT INTO ncm_compliance_exceptions(rule_id,device_id,owner,reason,expires_at,created_by)
        VALUES(:r,:d,:o,:reason,:x,:u) RETURNING id'''), {'r': data.rule_id, 'd': data.device_id, 'o': data.owner,
                                                           'reason': data.reason, 'x': data.expires_at, 'u': user.username})).scalar()
    await ncm_events.audit(db, user, 'compliance.exception_create', device_id=data.device_id, resource_id=row,
                           metadata={'rule': str(data.rule_id), 'owner': data.owner, 'expires_at': data.expires_at.isoformat()})
    targets = [data.device_id] if data.device_id else None
    await ncm_compliance.evaluate_all(db, targets)
    await db.commit()
    return {'id': str(row)}


@router.post('/exceptions/{exception_id}/revoke')
async def revoke_exception(exception_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_permission('ncm.compliance'))):
    row = (await db.execute(text('SELECT device_id FROM ncm_compliance_exceptions WHERE id=:id AND revoked_at IS NULL'), {'id': exception_id})).first()
    if not row:
        raise HTTPException(409, 'Exception is absent or already revoked')
    if row.device_id:
        await _device_access(db, user, row.device_id)
    else:
        await _policy_admin(db, user)
    await db.execute(text('UPDATE ncm_compliance_exceptions SET revoked_at=now(),revoked_by=:u WHERE id=:id'), {'id': exception_id, 'u': user.username})
    await ncm_events.audit(db, user, 'compliance.exception_revoke', device_id=row.device_id, resource_id=exception_id)
    await ncm_compliance.evaluate_all(db, [row.device_id] if row.device_id else None)
    await db.commit()
    return {'revoked': True}
