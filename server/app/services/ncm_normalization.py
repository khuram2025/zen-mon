"""Bring stored comparison data up to the current normalization.

When ``ncm_content.normalize_config`` changes meaning (NORMALIZATION_VERSION),
two kinds of stored state are stale:

* ``device_configs.norm_hash`` — baseline drift compares these hashes, so a
  baseline captured before the change would read as "drifted" forever;
* open "configuration changed" alerts raised only because of what the new
  normalization ignores (FortiOS re-encrypts every secret on each print).

``reconcile`` runs in small batches on the NCM one-minute timer. It recomputes
hashes for every archived version, then closes an open change alert only when
BOTH versions it compared are still archived and are identical under the
current normalization. Alerts whose versions have been pruned cannot be
verified and are left for an operator. Nothing is sent for these closures.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from app.services.ncm_content import NORMALIZATION_VERSION, normalize_config, read_content

SETTINGS_KEY = 'ncm.normalization'
CLOSE_NOTE = ('Closed automatically after an upgrade: the two backups differ only in secrets the '
              'device re-encrypts on every backup, so the configuration did not change.')


async def _state(db) -> dict:
    value = (await db.execute(text('SELECT value FROM system_settings WHERE key=:k'), {'k': SETTINGS_KEY})).scalar()
    if isinstance(value, str):
        value = json.loads(value)
    return value or {}


async def _save(db, state: dict) -> None:
    await db.execute(text('''INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb))
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value'''), {'k': SETTINGS_KEY, 'v': json.dumps(state)})


async def _rehash(db, cursor, batch_size):
    rows = (await db.execute(text('''SELECT id,content,content_enc,content_hash,norm_hash FROM device_configs
        WHERE CAST(:cursor AS uuid) IS NULL OR id > CAST(:cursor AS uuid) ORDER BY id LIMIT :limit'''),
        {'cursor': cursor, 'limit': batch_size})).fetchall()
    updated = unreadable = 0
    for row in rows:
        try:
            content = read_content(row)
        except Exception:
            unreadable += 1
            continue
        digest = hashlib.sha256(normalize_config(content).encode()).hexdigest()
        if digest != row.norm_hash:
            await db.execute(text('UPDATE device_configs SET norm_hash=:h WHERE id=:id'), {'h': digest, 'id': row.id})
            updated += 1
    return rows, updated, unreadable


async def _content(db, device_id, version_id):
    row = (await db.execute(text('''SELECT content,content_enc,content_hash FROM device_configs
        WHERE id=CAST(:v AS uuid) AND device_id=:d'''), {'v': version_id, 'd': device_id})).first()
    return read_content(row) if row else None


async def close_false_changes(db) -> dict:
    alerts = (await db.execute(text('''SELECT id,device_id,status,metadata FROM alerts
        WHERE status IN ('pending','active','acknowledged') AND metadata->>'ncm'='true'
          AND metadata->>'ncm_event'='config.changed' ORDER BY triggered_at'''))).fetchall()
    closed = unverifiable = 0
    for alert in alerts:
        meta = alert.metadata or {}
        try:
            before = await _content(db, alert.device_id, meta.get('from_version'))
            after = await _content(db, alert.device_id, meta.get('to_version'))
        except Exception:
            before = after = None
        if before is None or after is None:
            unverifiable += 1
            continue
        if normalize_config(before) != normalize_config(after):
            continue
        await db.execute(text('''UPDATE alerts SET status='resolved',resolved_at=now(),
            metadata=COALESCE(metadata,'{}'::jsonb)||CAST(:m AS jsonb) WHERE id=:id'''),
            {'id': alert.id, 'm': json.dumps({'resolved_by': 'migration', 'resolution_note': CLOSE_NOTE})})
        await db.execute(text('UPDATE device_configs SET is_change=false WHERE id=CAST(:v AS uuid)'),
                         {'v': meta.get('to_version')})
        await db.execute(text('''INSERT INTO alert_events (alert_id,kind,source,actor_name,reason,old_status,new_status)
            VALUES (:id,'resolved','system','ZenPlus',:reason,:old,'resolved')'''),
            {'id': alert.id, 'reason': CLOSE_NOTE, 'old': alert.status})
        closed += 1
    return {'closed': closed, 'unverifiable': unverifiable}


async def reconcile(db, *, batch_size: int = 200):
    """One step of the post-upgrade pass; returns None once current."""
    state = await _state(db)
    if int(state.get('version') or 1) >= NORMALIZATION_VERSION:
        return None
    rows, updated, unreadable = await _rehash(db, state.get('cursor'), batch_size)
    progress = {'rehashed': int(state.get('rehashed') or 0) + updated,
                'unreadable': int(state.get('unreadable') or 0) + unreadable}
    if len(rows) == batch_size:
        await _save(db, {**state, **progress, 'cursor': str(rows[-1].id)})
        return {'phase': 'rehash', **progress}
    alerts = await close_false_changes(db)
    await _save(db, {'version': NORMALIZATION_VERSION, **progress, 'alerts': alerts})
    return {'phase': 'done', **progress, **alerts}
