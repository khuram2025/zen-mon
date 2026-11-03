"""Opt-in device collection fallback, serialized with device status writers."""

from sqlalchemy import text


async def reconcile_controller_fallback(db):
    await db.execute(text("""
        UPDATE sensors SET fallback_healthy_since = CASE
            WHEN status = 'online' THEN COALESCE(fallback_healthy_since, NOW())
            ELSE NULL END
        WHERE controller_fallback_enabled OR controller_fallback_active
    """))
    changes = (await db.execute(text("""
        SELECT id, name, status, controller_fallback_active, CASE
            WHEN NOT controller_fallback_enabled THEN FALSE
            WHEN status IN ('offline', 'disabled') AND last_heartbeat_at IS NOT NULL THEN TRUE
            WHEN status = 'online' AND fallback_healthy_since <=
                 NOW() - make_interval(secs => fallback_recovery_seconds) THEN FALSE
            ELSE controller_fallback_active END AS next_active
        FROM sensors
        WHERE controller_fallback_enabled OR controller_fallback_active
        ORDER BY id FOR UPDATE
    """))).mappings().all()
    changed = []
    for change in changes:
        if change['controller_fallback_active'] == change['next_active']:
            continue
        # Status ingestion takes this same device lock before checking ownership.
        # Complete the former writer before publishing the new ownership view.
        await db.execute(text("""
            SELECT d.id FROM devices d WHERE EXISTS (
                SELECT 1 FROM device_monitoring_vantages v
                WHERE v.device_id = d.id AND v.sensor_id = :id
            ) ORDER BY d.id FOR NO KEY UPDATE OF d
        """), {"id": change['id']})
        await db.execute(text("""
            UPDATE sensors SET controller_fallback_active = :active,
                fallback_changed_at = clock_timestamp(), updated_at = NOW() WHERE id = :id
        """), {"id": change['id'], "active": change['next_active']})
        await db.execute(text("""
            INSERT INTO sensor_events(sensor_id, kind, detail)
            VALUES (:id, 'controller_fallback', jsonb_build_object(
                'active', CAST(:active AS boolean), 'reason', CAST(:reason AS text)))
        """), {"id": change['id'], "active": change['next_active'],
               "reason": 'Controller device polling activated' if change['next_active']
               else 'Device polling returned to sensor policy'})
        changed.append(dict(change))
    return changed
