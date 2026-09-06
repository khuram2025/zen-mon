"""Sensor-scoped inventory and observations without exposing monitoring secrets."""
from datetime import datetime, timezone
from sqlalchemy import text
from app.core import scoping
from app.core.database import get_clickhouse_client

DEVICE_SQL = """SELECT d.id, d.hostname AS name, d.ip_address::text AS address,
    d.tags, d.ping_enabled, d.snmp_enabled, d.ping_interval, d.snmp_poll_interval,
    CASE WHEN EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id=:sid AND a.target_type='device' AND a.target_id=d.id)
      THEN 'Direct' WHEN d.default_sensor_id=:sid THEN 'Default sensor' ELSE 'Device group' END AS assignment_source
    FROM devices d WHERE EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id=:sid AND a.target_type='device' AND a.target_id=d.id)
    OR (NOT EXISTS (SELECT 1 FROM monitoring_policies p WHERE p.target_type='device' AND p.target_id=d.id)
        AND (d.default_sensor_id=:sid OR EXISTS (SELECT 1 FROM sensor_assignments a
          WHERE a.sensor_id=:sid AND a.target_type='group' AND a.target_id=d.group_id))) ORDER BY d.hostname,d.id"""
SERVICE_SQL = """SELECT sc.id, sc.name, sc.check_type, sc.tags, sc.enabled, sc.check_interval,
    sc.credential_id IS NOT NULL OR jsonb_array_length(COALESCE(sc.workflow_steps,'[]'::jsonb)) > 0 AS needs_auth_support,
    CASE WHEN EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id=:sid AND a.target_type='service_check' AND a.target_id=sc.id)
      THEN 'Direct' ELSE 'Default sensor' END AS assignment_source
    FROM service_checks sc WHERE EXISTS (SELECT 1 FROM sensor_assignments a
       WHERE a.sensor_id=:sid AND a.target_type='service_check' AND a.target_id=sc.id)
    OR (sc.default_sensor_id=:sid AND NOT EXISTS (SELECT 1 FROM monitoring_policies p
       WHERE p.target_type='service_check' AND p.target_id=sc.id)) ORDER BY sc.name,sc.id"""

async def assigned_targets(db, sensor_id, user):
    scope = await scoping.visible_tags(db, user)
    result=[]
    for query in (DEVICE_SQL, SERVICE_SQL):
        rows=(await db.execute(text(query), {'sid':sensor_id})).mappings().all()
        result.append([dict(row) for row in rows if scoping.entity_visible(row['tags'], scope)])
    return result


def sensor_connection(sensor, now):
    if sensor.get('status') == 'disabled': return 'disabled'
    if not sensor.get('api_key_hash') or (sensor.get('bootstrap_config') or {}).get('authorization_pending'): return 'pending'
    heartbeat=sensor.get('last_heartbeat_at')
    if not heartbeat: return 'offline'
    if heartbeat.tzinfo is None: heartbeat=heartbeat.replace(tzinfo=timezone.utc)
    age=(now-heartbeat).total_seconds()
    if age > (sensor.get('offline_after_s') or 180): return 'offline'
    if age > (sensor.get('degraded_after_s') or 90): return 'degraded'
    return sensor.get('status') or 'online'


def sensor_measurements(sensor_id, devices, services):
    client=get_clickhouse_client()
    result={}
    for kind,table,key,rows,latency in [
        ('ping','ping_metrics','device_id',devices,'rtt_ms'),
        ('snmp','snmp_metrics','device_id',devices,None),
        ('service','service_metrics','service_check_id',services,'response_ms')]:
        if not rows: result[kind]={};continue
        fields=', argMax(is_up,timestamp) AS last_is_up, argMax('+latency+',timestamp) AS latency_ms, avg(toFloat64(is_up))*100 AS availability_pct' if latency else ''
        query=f"""SELECT {key} AS target_id, max(timestamp) AS last_result_at, count() AS samples {fields}
            FROM {table} WHERE poller_id={{sensor:String}} AND {key} IN {{ids:Array(UUID)}}
            AND timestamp >= now() - INTERVAL 24 HOUR GROUP BY {key}"""
        result[kind]={str(row['target_id']):dict(row, is_up=row.get('last_is_up', True)) for row in client.query(query,parameters={
            'sensor':str(sensor_id),'ids':[str(row['id']) for row in rows]}).named_results()}
    return result


def target_results(devices, services, measurements, status, version, now):
    from app.api.v1.monitoring_sites import observation
    from app.services.sensor_service_checks import supports_service_auth
    result=[]
    for kind,rows in [('devices',devices),('services',services)]:
        items=[]
        for row in rows:
            checks={}
            for check in (['ping','snmp'] if kind=='devices' else ['service']):
                enabled=row.get(check+'_enabled',False) if kind=='devices' else row['enabled']
                interval=row.get('snmp_poll_interval' if check=='snmp' else 'ping_interval' if check=='ping' else 'check_interval') or 60
                checks[check]=observation(measurements.get(check,{}).get(str(row['id'])),interval,enabled,status,now)
                if check=='service' and enabled and row.get('needs_auth_support') and not supports_service_auth(version):
                    checks[check]['state']='update_required'
            items.append({'id':str(row['id']),'name':row['name'],
                'address':row.get('address'),'check_type':row.get('check_type'),
                'assignment_source':row['assignment_source'],'checks':checks})
        result.append(items)
    return result
