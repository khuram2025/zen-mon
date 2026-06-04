"""Per-feature health summaries.

Tiny JSON files that let a support engineer scan ``features/*.json`` and see,
at a glance, whether each major feature has its tables, has any data, and
when something last happened. No PII, no secrets — just EXISTS / COUNT /
MAX-of-timestamp.

If a feature's table doesn't exist on the appliance, the JSON shows
``"table_exists": false`` — which is exactly the signal we want for missing
migrations and 502-after-feature-rollout type incidents.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from . import CollectorContext, CollectorResult


FEATURE_PROBES: dict[str, list[tuple[str, str]]] = {
    "features/snmp.json": [
        ("snmp_credentials", "updated_at"),
        ("devices_with_credential",
         "(SELECT count(*) FROM devices WHERE snmp_credential_id IS NOT NULL)"),
    ],
    "features/discovery.json": [
        ("discovery_profiles", "updated_at"),
        ("discovery_runs", "started_at"),
        ("discovery_results", "discovered_at"),
    ],
    "features/windows-credentials.json": [
        ("windows_credentials", "updated_at"),
    ],
    "features/server-monitoring.json": [
        ("servers", "updated_at"),
        ("agents", "last_heartbeat_at"),
        ("agent_policies", "updated_at"),
    ],
    "features/sensors.json": [
        ("sensors", "last_heartbeat_at"),
        ("sensor_assignments", "updated_at"),
        ("sites", "updated_at"),
    ],
    "features/notifications.json": [
        ("notification_channels", "updated_at"),
        ("notification_gateways", "updated_at"),
    ],
    "features/netflow.json": [
        ("flow_exporters", "updated_at"),
    ],
}


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="features")

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        result.fail("DATABASE_URL not set")
        return result
    asyncpg_url = db_url.replace("+asyncpg", "", 1)

    try:
        snapshot = asyncio.run(_collect_async(asyncpg_url))
    except Exception as exc:  # noqa: BLE001
        result.fail(f"feature probes failed: {exc.__class__.__name__}: {exc}")
        return result

    for arcname, data in snapshot.items():
        result.files[arcname] = (json.dumps(data, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    return result


async def _collect_async(url: str) -> dict[str, dict[str, Any]]:
    import asyncpg

    out: dict[str, dict[str, Any]] = {}
    conn = await asyncpg.connect(url, timeout=10)
    try:
        for arcname, probes in FEATURE_PROBES.items():
            section: dict[str, Any] = {}
            for label, target in probes:
                if "(" in target:
                    # subquery target — already a count expression
                    try:
                        value = await conn.fetchval("SELECT " + target)
                        section[label] = int(value or 0)
                    except Exception as exc:  # noqa: BLE001
                        section[label] = {"error": str(exc)}
                    continue

                # Otherwise ``target`` is a column name on table ``label``.
                table = label
                column = target
                section[table] = await _probe_table(conn, table, column)
            out[arcname] = section
    finally:
        await conn.close()
    return out


async def _probe_table(conn, table: str, recency_column: str) -> dict[str, Any]:
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table,
    )
    if not exists:
        return {"table_exists": False}
    try:
        row = await conn.fetchrow(
            f"SELECT count(*) AS n, max({recency_column}) AS latest FROM {table}"
        )
        return {
            "table_exists": True,
            "row_count": int(row["n"]) if row else 0,
            "latest": str(row["latest"]) if row and row["latest"] is not None else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"table_exists": True, "error": str(exc)}
