"""Target-scoped monitoring selection and observed availability per location."""
import asyncio
from datetime import datetime, timezone
import os
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db, get_clickhouse_client
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.sensor_service_checks import supports_service_auth

router = APIRouter(prefix='/monitoring-sites', tags=['Monitoring sites'])
TargetType = Literal['device', 'service_check']


class MonitoringSelection(BaseModel):
    controller_enabled: bool = True
    sensor_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def nonempty(self):
        self.sensor_ids = list(dict.fromkeys(self.sensor_ids))
        if not self.controller_enabled and not self.sensor_ids:
            raise ValueError('Select the controller or at least one sensor')
        return self


async def target_row(kind, target_id, db, user, *, lock=False):
    table = 'devices' if kind == 'device' else 'service_checks'
    row = (await db.execute(text(f'SELECT * FROM {table} WHERE id = :id' + (' FOR UPDATE' if lock else '')),
                            {'id': target_id})).mappings().first()
    scope = await scoping.visible_tags(db, user)
    if not row or not scoping.entity_visible(row['tags'], scope):
        raise HTTPException(404, 'Monitoring target not found')
    return row


async def selected_rows(kind, target_id, db):
    view, key = ('device_monitoring_vantages', 'device_id') if kind == 'device' else ('service_monitoring_vantages', 'service_check_id')
    return (await db.execute(text(f'''SELECT v.poller_id, v.sensor_id, s.name AS sensor_name,
        s.site_id, si.name AS site_name, s.location, s.status AS sensor_status,
        s.last_heartbeat_at, s.offline_after_s, s.bootstrap_config, s.api_key_hash IS NOT NULL AS enrolled
        FROM {view} v LEFT JOIN sensors s ON s.id = v.sensor_id LEFT JOIN sites si ON si.id = s.site_id
        WHERE v.{key} = :id ORDER BY (v.poller_id = 'central') DESC, si.name NULLS LAST, s.name'''),
        {'id': target_id})).mappings().all()


def measurements(kind, target_id):
    client = get_clickhouse_client()
    params = {'id': str(target_id), 'controller': os.getenv('POLLER_ID', 'poller-01')}
    # Only the configured controller ID is normalized; old/unassigned sensor IDs
    # are never relabeled as controller observations.
    poller = "if(poller_id = {controller:String}, 'central', poller_id)"
    output = {}
    table, key, latency = ('ping_metrics', 'device_id', 'rtt_ms') if kind == 'device' else ('service_metrics', 'service_check_id', 'response_ms')
    query = f'''SELECT {poller} AS source, max(timestamp) AS last_result_at,
        argMax(is_up, timestamp) AS last_is_up, argMax({latency}, timestamp) AS latency_ms,
        avg(toFloat64(is_up)) * 100 AS availability_pct, count() AS samples
        FROM {table} WHERE {key} = {{id:UUID}} AND timestamp >= now() - INTERVAL 24 HOUR
        GROUP BY source'''
    output['ping' if kind == 'device' else 'service'] = {r['source']: dict(r, is_up=r['last_is_up']) for r in client.query(query, parameters=params).named_results()}
    if kind == 'device':
        query = f'''SELECT {poller} AS source, max(timestamp) AS last_result_at, count() AS samples
            FROM snmp_metrics WHERE device_id = {{id:UUID}} AND timestamp >= now() - INTERVAL 24 HOUR
            GROUP BY source'''
        output['snmp'] = {r['source']: r for r in client.query(query, parameters=params).named_results()}
    return output


def observation(sample, interval, enabled, sensor_state, now):
    sample = sample or {}
    ts = sample.get('last_result_at')
    if ts and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    fresh = bool(ts and (now - ts).total_seconds() <= max(interval * 3, 180))
    if not enabled:
        state = 'disabled'
    elif sensor_state in ('offline', 'disabled', 'pending'):
        state = 'probe_' + sensor_state
    elif not fresh:
        state = 'no_data'
    else:
        state = 'up' if sample.get('is_up', True) else 'down'
    return {'state': state, 'last_result_at': ts, 'latency_ms': sample.get('latency_ms'),
            'availability_pct': sample.get('availability_pct'), 'samples': sample.get('samples', 0)}


@router.get('/{kind}/{target_id}')
async def get_monitoring_sites(kind: TargetType, target_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    target = await target_row(kind, target_id, db, user)
    selected = await selected_rows(kind, target_id, db)
    try:
        observed = await asyncio.to_thread(measurements, kind, target_id)
    except Exception as exc:
        logging.getLogger(__name__).exception('Could not query site measurements for %s %s', kind, target_id)
        raise HTTPException(503, 'Site measurements are temporarily unavailable') from exc
    scope = await scoping.visible_tags(db, user)
    sensors = (await db.execute(text('''SELECT s.id, s.name, s.tags, s.site_id, si.name AS site_name, s.location,
        s.status, s.version, s.last_heartbeat_at, s.offline_after_s, s.bootstrap_config,
        s.api_key_hash IS NOT NULL AS enrolled FROM sensors s LEFT JOIN sites si ON si.id = s.site_id
        ORDER BY si.name NULLS LAST, s.name'''))).mappings().all()
    now = datetime.now(timezone.utc)
    def sensor_state(row):
        if (row.get('bootstrap_config') or {}).get('authorization_pending') or not row.get('enrolled'):
            return 'pending'
        heartbeat = row.get('last_heartbeat_at')
        if row['status'] in ('online', 'degraded') and (not heartbeat or (now-heartbeat).total_seconds() > (row.get('offline_after_s') or 180)):
            return 'offline'
        return row['status']
    choices = []
    for s in sensors:
        if not scoping.entity_visible(s['tags'], scope):
            continue
        status = sensor_state(s)
        if kind == 'service_check' and (target.get('credential_id') or target.get('workflow_steps')) and not supports_service_auth(s.get('version')):
            status = 'update_required'
        choices.append({k: s[k] for k in ('id', 'name', 'site_id', 'site_name', 'location')} | {'status': status, 'available': status in ('online', 'degraded')})
    sites = []
    for s in selected:
        central = s['poller_id'] == 'central'
        status = 'online' if central else sensor_state(dict(s) | {'status': s['sensor_status']})
        checks = {}
        for check in (['ping', 'snmp'] if kind == 'device' else ['service']):
            interval = target.get('snmp_poll_interval' if check == 'snmp' else 'ping_interval' if check == 'ping' else 'check_interval') or 60
            enabled = target.get(check + '_enabled', True) if kind == 'device' else target['enabled']
            checks[check] = observation(observed.get(check, {}).get(s['poller_id']), interval, enabled, status, now)
        sites.append({'poller_id': s['poller_id'], 'sensor_id': s['sensor_id'],
                      'name': 'Controller' if central else s['site_name'] or s['location'] or 'Unassigned site',
                      'sensor_name': s['sensor_name'], 'location': s['location'], 'probe_status': status, 'checks': checks})
    return {'controller_enabled': any(s['poller_id'] == 'central' for s in selected),
            'sensor_ids': [s['sensor_id'] for s in selected if s['sensor_id']],
            'sites': sites, 'available_sensors': choices, 'window': '24h',
            'remote_supported': True}


@router.put('/{kind}/{target_id}')
async def set_monitoring_sites(kind: TargetType, target_id: UUID, data: MonitoringSelection,
                               db: AsyncSession = Depends(get_db), user: User = Depends(require_operator_user)):
    target = await target_row(kind, target_id, db, user)
    existing = {s['sensor_id'] for s in await selected_rows(kind, target_id, db)}
    scope = await scoping.visible_tags(db, user)
    # Ingestion locks the sensor before updating the target. Keep that order
    # here too, so an assignment save cannot deadlock an incoming result batch.
    for sid in sorted(data.sensor_ids, key=str):
        s = (await db.execute(text('''SELECT *, api_key_hash IS NOT NULL AS enrolled FROM sensors WHERE id=:id FOR SHARE'''), {'id': sid})).mappings().first()
        if not s or not scoping.entity_visible(s['tags'], scope):
            raise HTTPException(400, 'Unknown or inaccessible sensor')
        if kind == 'service_check' and (target.get('credential_id') or target.get('workflow_steps')) and not supports_service_auth(s.get('version')):
            raise HTTPException(400, 'Update this sensor to 1.23.5 or later for authenticated and workflow service checks')
        if sid not in existing and (s['status'] not in ('online', 'degraded') or not s['enrolled'] or (s['bootstrap_config'] or {}).get('authorization_pending') or not s['last_heartbeat_at'] or (datetime.now(timezone.utc)-s['last_heartbeat_at']).total_seconds() > (s['offline_after_s'] or 180)):
            raise HTTPException(400, 'New monitoring locations require an online, authorized sensor')
    table = 'devices' if kind == 'device' else 'service_checks'
    await db.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE'), {'id': target_id})
    await db.execute(text('''INSERT INTO monitoring_policies(target_type,target_id,controller_enabled)
        VALUES (:kind,:id,:central) ON CONFLICT(target_type,target_id) DO UPDATE
        SET controller_enabled=EXCLUDED.controller_enabled, updated_at=NOW()'''), {'kind': kind, 'id': target_id, 'central': data.controller_enabled})
    await db.execute(text('DELETE FROM sensor_assignments WHERE target_type=:kind AND target_id=:id'), {'kind': kind, 'id': target_id})
    for sid in data.sensor_ids:
        await db.execute(text('INSERT INTO sensor_assignments(sensor_id,target_type,target_id,priority) VALUES (:sid,:kind,:id,100)'), {'sid': sid, 'kind': kind, 'id': target_id})
    await db.execute(text(f'UPDATE {table} SET default_sensor_id=NULL WHERE id=:id'), {'id': target_id})
    await write_audit_log(db, actor=user, action='monitoring.sites.update', resource_type=kind, resource_id=str(target_id),
                          metadata={'controller_enabled': data.controller_enabled, 'sensor_ids': [str(s) for s in data.sensor_ids]})
    await db.commit()
    return {'saved': True}
