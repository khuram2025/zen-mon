"""Server-agent and APM control-plane diagnostics.

This collector answers the incident question that generic health probes cannot:
is the agent merely heartbeating, or are host metrics/APM data actually moving?
It reads only operational state and aggregate counts from PostgreSQL; no API-key
hashes, enrollment tokens, command parameters, or diagnostic payloads are read.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from . import CollectorContext, CollectorResult


QUERIES: dict[str, str] = {
    "telemetry/fleet.json": """
        SELECT a.id AS agent_id, a.server_id, s.display_name, s.hostname,
               s.primary_ip, s.status AS server_status, s.last_seen AS server_last_seen,
               a.status AS agent_status, a.version, a.current_version, a.desired_version,
               a.last_heartbeat_at, a.last_metric_at, a.queue_depth, a.spool_bytes,
               a.update_ring, a.config_apply_error
        FROM agents a
        LEFT JOIN servers s ON s.id = a.server_id
        ORDER BY a.hostname, a.id
    """,
    "telemetry/fleet-apm-status.json": """
        SELECT a.id AS agent_id, a.server_id, a.apm_status,
               (aac.agent_id IS NOT NULL) AS apm_credential_bound
        FROM agents a
        LEFT JOIN agent_apm_credentials aac ON aac.agent_id = a.id
        ORDER BY a.hostname, a.id
    """,
    "telemetry/host-upload-ledger.json": """
        SELECT agent_id, server_id,
               count(*) AS batches_24h,
               count(*) FILTER (WHERE completed_at IS NULL) AS incomplete_batches,
               coalesce(sum(accepted), 0) AS accepted,
               coalesce(sum(rejected), 0) AS rejected,
               count(*) FILTER (WHERE jsonb_array_length(errors) > 0) AS batches_with_errors,
               max(completed_at) AS last_completed_at,
               max(sequence_end) AS max_sequence_end
        FROM agent_host_result_batches
        WHERE created_at >= now() - interval '24 hours'
        GROUP BY agent_id, server_id
        ORDER BY agent_id
    """,
    "telemetry/recent-host-upload-errors.json": """
        SELECT agent_id, server_id, batch_id, sequence_start, sequence_end,
               accepted, rejected, errors, clock_skew_s, created_at, completed_at
        FROM agent_host_result_batches
        WHERE completed_at IS NULL OR rejected > 0 OR jsonb_array_length(errors) > 0
        ORDER BY created_at DESC LIMIT 50
    """,
    "telemetry/agent-commands.json": """
        SELECT c.command, c.status, count(*) AS count,
               max(c.created_at) AS latest_created_at,
               max(c.completed_at) AS latest_completed_at
        FROM agent_commands c
        WHERE c.created_at >= now() - interval '7 days'
        GROUP BY c.command, c.status
        ORDER BY c.command, c.status
    """,
    "telemetry/recent-command-failures.json": """
        SELECT c.id AS command_id, c.agent_id, c.command, c.status,
               c.created_at, c.sent_at, c.completed_at, r.error_message, r.received_at
        FROM agent_commands c
        LEFT JOIN agent_command_results r ON r.command_id = c.id
        WHERE c.status IN ('failed', 'expired')
        ORDER BY c.created_at DESC LIMIT 50
    """,
    "telemetry/agent-packages.json": """
        SELECT platform, arch, version, channel, file_name, file_size, sha256,
               (signature IS NOT NULL AND length(signature) > 0) AS signature_present,
               signed_by, is_latest, released_at, created_at
        FROM agent_packages
        ORDER BY platform, arch, channel, released_at DESC
    """,
    "telemetry/inventory-freshness/processes.json": """
        SELECT s.id AS server_id, s.hostname, max(i.updated_at) AS latest
        FROM servers s LEFT JOIN server_process_inventory i ON i.server_id = s.id
        GROUP BY s.id, s.hostname ORDER BY s.hostname, s.id
    """,
    "telemetry/inventory-freshness/services.json": """
        SELECT s.id AS server_id, s.hostname, max(i.updated_at) AS latest
        FROM servers s LEFT JOIN server_service_inventory i ON i.server_id = s.id
        GROUP BY s.id, s.hostname ORDER BY s.hostname, s.id
    """,
    "telemetry/inventory-freshness/filesystems.json": """
        SELECT s.id AS server_id, s.hostname, max(i.updated_at) AS latest
        FROM servers s LEFT JOIN server_filesystem_inventory i ON i.server_id = s.id
        GROUP BY s.id, s.hostname ORDER BY s.hostname, s.id
    """,
    "telemetry/inventory-freshness/network.json": """
        SELECT s.id AS server_id, s.hostname, max(i.updated_at) AS latest
        FROM servers s LEFT JOIN server_network_interface_inventory i ON i.server_id = s.id
        GROUP BY s.id, s.hostname ORDER BY s.hostname, s.id
    """,
    "telemetry/inventory-freshness/software.json": """
        SELECT s.id AS server_id, s.hostname, max(i.updated_at) AS latest
        FROM servers s LEFT JOIN server_software_inventory i ON i.server_id = s.id
        GROUP BY s.id, s.hostname ORDER BY s.hostname, s.id
    """,
    "telemetry/apm-services.json": """
        SELECT svc.id, svc.name, env.name AS environment, svc.health,
               svc.last_seen_at, svc.last_rps, svc.last_error_rate,
               svc.last_p95_ms, svc.last_apdex, svc.updated_at
        FROM apm_services svc
        LEFT JOIN apm_environments env ON env.id = svc.env_id
        ORDER BY svc.name, env.name
    """,
    "telemetry/apm-agent-instrumentation.json": """
        SELECT agent_id, server_id, runtime, instrumentation_state,
               count(*) AS process_count, max(last_seen_at) AS latest_seen_at
        FROM apm_agent_processes
        GROUP BY agent_id, server_id, runtime, instrumentation_state
        ORDER BY agent_id, runtime, instrumentation_state
    """,
    "telemetry/apm-ingest-keys.json": """
        SELECT kind, enabled, count(*) AS key_count,
               count(*) FILTER (WHERE revoked_at IS NULL) AS not_revoked,
               max(last_used_at) AS latest_used_at
        FROM apm_ingest_keys
        GROUP BY kind, enabled ORDER BY kind, enabled
    """,
}


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="telemetry")
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        result.fail("DATABASE_URL not set; cannot collect agent/APM state")
        return result

    try:
        snapshot = asyncio.run(_collect_async(db_url.replace("+asyncpg", "", 1)))
    except Exception as exc:  # noqa: BLE001
        result.fail(f"telemetry database connection failed: {exc.__class__.__name__}: {exc}")
        return result

    succeeded = 0
    for arcname, payload in snapshot.items():
        result.files[arcname] = _dump(payload)
        if "error" in payload:
            result.warn(f"{arcname} query failed")
        else:
            succeeded += 1
    if not succeeded:
        result.fail("all agent/APM telemetry queries failed")
    return result


async def _collect_async(url: str) -> dict[str, dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(url, timeout=10, command_timeout=10)
    try:
        out: dict[str, dict[str, Any]] = {}
        for arcname, sql in QUERIES.items():
            try:
                rows = await conn.fetch(sql)
                out[arcname] = {"rows": [dict(row) for row in rows]}
            except Exception as exc:  # noqa: BLE001
                out[arcname] = {"error": f"{exc.__class__.__name__}: {exc}"}
        return out
    finally:
        await conn.close()


def _dump(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
