"""Resolve only the SNMP credentials for devices assigned to this sensor."""
from sqlalchemy import text
from app.core.crypto import decrypt_secret
from app.schemas.sensor import ConfigSNMP


async def sensor_snmp_config(sensor_id, db) -> dict[str, ConfigSNMP]:
    rows = (await db.execute(text("""
        SELECT d.id, d.snmp_version, d.snmp_port, d.snmp_community,
               d.snmp_v3_username, d.snmp_v3_context, d.snmp_auth_protocol,
               d.snmp_auth_passphrase, d.snmp_priv_protocol, d.snmp_priv_passphrase,
               d.snmp_timeout_ms, d.snmp_retries, d.snmp_poll_interval,
               to_jsonb(c) AS credential
          FROM devices d
          JOIN device_monitoring_vantages owner ON owner.device_id = d.id
          LEFT JOIN snmp_credentials c ON c.id = d.snmp_credential_id
         WHERE owner.sensor_id = :sid
           AND d.snmp_enabled = TRUE
         ORDER BY d.id
    """), {"sid": sensor_id})).mappings().all()
    result = {}
    for row in rows:
        credential = row.get("credential") or {}
        def pick(key, fallback):
            value = credential.get(key)
            return value if value is not None else row.get(fallback)
        result[str(row["id"])] = ConfigSNMP(
            version=pick("snmp_version", "snmp_version") or "2c",
            port=pick("port", "snmp_port") or 161,
            community=decrypt_secret(pick("community", "snmp_community")),
            v3_username=pick("v3_username", "snmp_v3_username"),
            v3_context=pick("v3_context", "snmp_v3_context"),
            v3_auth_protocol=pick("v3_auth_protocol", "snmp_auth_protocol"),
            v3_auth_passphrase=decrypt_secret(pick("v3_auth_passphrase", "snmp_auth_passphrase")),
            v3_priv_protocol=pick("v3_priv_protocol", "snmp_priv_protocol"),
            v3_priv_passphrase=decrypt_secret(pick("v3_priv_passphrase", "snmp_priv_passphrase")),
            timeout_ms=pick("timeout_ms", "snmp_timeout_ms") or 2000,
            retries=row.get("snmp_retries") if row.get("snmp_retries") is not None else 1,
            interval=row.get("snmp_poll_interval") or 60,
        )
    return result
