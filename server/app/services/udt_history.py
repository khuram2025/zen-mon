"""Summarize addresses without discarding their recorded observation periods."""
from sqlalchemy import text


async def address_history(db, endpoint_id):
    result = await db.execute(text("""
        WITH addresses AS (
            SELECT ip, string_agg(DISTINCT source, ', ' ORDER BY source) AS source,
                   bool_or(active AND last_seen >= NOW() - INTERVAL '24 hours') AS active,
                   min(first_seen) AS first_seen, max(last_seen) AS last_seen,
                   count(*) AS period_count
            FROM udt_ip_history WHERE endpoint_id = :id GROUP BY ip
        )
        SELECT host(a.ip) AS ip, a.source, a.active, a.first_seen, a.last_seen, a.period_count,
               (SELECT count(DISTINCT h.endpoint_id) FROM udt_ip_history h
                WHERE h.ip = a.ip AND h.active
                  AND h.last_seen >= NOW() - INTERVAL '24 hours') AS active_endpoint_count
        FROM addresses a ORDER BY a.active DESC, a.last_seen DESC, a.ip
    """), {"id": endpoint_id})
    return [dict(row) for row in result.mappings()]


async def address_periods(db, endpoint_id, ip, skip, limit):
    params = {"id": endpoint_id, "ip": ip, "skip": skip, "limit": limit}
    total = (await db.execute(text("""
        SELECT count(*) FROM udt_ip_history
        WHERE endpoint_id = :id AND ip = CAST(:ip AS inet)
    """), params)).scalar_one()
    rows = (await db.execute(text("""
        SELECT h.id, host(h.ip) AS ip, h.source,
               h.active AND h.last_seen >= NOW() - INTERVAL '24 hours' AS active,
               h.first_seen, h.last_seen, COALESCE(d.hostname, host(d.ip_address)) AS reporting_device
        FROM udt_ip_history h LEFT JOIN devices d ON d.id = h.reporting_device_id
        WHERE h.endpoint_id = :id AND h.ip = CAST(:ip AS inet)
        ORDER BY h.first_seen DESC, h.id DESC LIMIT :limit OFFSET :skip
    """), params)).mappings()
    return {"data": [dict(row) for row in rows], "meta": {"total": total, "skip": skip, "limit": limit}}


async def address_evidence(db, endpoint_id, ip, skip, limit):
    """Recent reported bindings, with legacy provenance explicitly distinguished.

    No cross-router network scope is inferred from an interface index or an
    endpoint's current switch VLAN. Repeated reports do not prove ownership.
    """
    params = {"id": endpoint_id, "ip": ip, "skip": skip, "limit": limit}
    query = """
        WITH requested AS (
            SELECT 1 FROM udt_ip_history WHERE endpoint_id = :id AND ip = CAST(:ip AS inet) LIMIT 1
        ), reports AS (
            SELECT e.endpoint_id, e.reporting_device_id, e.if_index, e.source,
                   e.first_seen, e.last_seen, e.observation_count,
                   (SELECT count(*) FROM unnest(e.recent_sightings) t
                    WHERE t >= NOW() - INTERVAL '24 hours') >= 3 AS repeated,
                   false AS legacy
            FROM udt_ip_evidence e
            WHERE e.ip = CAST(:ip AS inet) AND e.last_seen >= NOW() - INTERVAL '24 hours'
              AND EXISTS (SELECT 1 FROM requested)
            UNION ALL
            SELECT h.endpoint_id, h.reporting_device_id, NULL::integer, h.source,
                   min(h.first_seen), max(h.last_seen), NULL::bigint, false, true
            FROM udt_ip_history h
            WHERE h.ip = CAST(:ip AS inet) AND h.active AND h.last_seen >= NOW() - INTERVAL '24 hours'
              AND EXISTS (SELECT 1 FROM requested)
              AND NOT EXISTS (SELECT 1 FROM udt_ip_evidence e
                  WHERE e.endpoint_id = h.endpoint_id AND e.ip = h.ip
                    AND e.reporting_device_id = h.reporting_device_id AND e.source = h.source
                    AND e.last_seen >= h.last_seen)
            GROUP BY h.endpoint_id, h.reporting_device_id, h.source
        )
    """
    total = (await db.execute(text(query + "SELECT count(*) FROM reports"), params)).scalar_one()
    rows = (await db.execute(text(query + """
        SELECT r.*, e.mac::text AS mac, e.hostname AS endpoint_name,
               COALESCE(d.hostname, host(d.ip_address)) AS reporter,
               host(d.ip_address) AS reporter_ip,
               (SELECT COALESCE(di.if_name, di.if_descr) FROM device_interfaces di
                WHERE di.device_id = r.reporting_device_id AND di.if_index = r.if_index LIMIT 1) AS interface_name
        FROM reports r JOIN udt_endpoints e ON e.id = r.endpoint_id
        LEFT JOIN devices d ON d.id = r.reporting_device_id
        ORDER BY r.last_seen DESC, r.endpoint_id, r.reporting_device_id, r.if_index, r.source
        LIMIT :limit OFFSET :skip
    """), params)).mappings()
    return {"data": [dict(row) for row in rows], "meta": {"total": total, "skip": skip, "limit": limit}}
