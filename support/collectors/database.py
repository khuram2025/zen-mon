"""PostgreSQL diagnostics collector.

Never dumps data. Captures only the metadata a support engineer needs to
spot schema drift, missing migrations, hung locks, or runaway transactions
— each of which we've actually seen on customer appliances.

Uses asyncpg directly so the worker doesn't drag the SQLAlchemy import
graph into the root process. ``DATABASE_URL`` is loaded from the same .env
that the API service uses (the systemd template's ``EnvironmentFile=``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import CollectorContext, CollectorResult


KEY_TABLES = (
    "users",
    "devices",
    "device_groups",
    "snmp_credentials",
    "windows_credentials",
    "service_checks",
    "alert_rules",
    "alerts",
    "audit_logs",
    "subscriptions",
    "discovery_profiles",
    "discovery_runs",
    "discovery_results",
    "servers",
    "agents",
    "sensors",
    "sensor_assignments",
    "notification_channels",
    "notification_gateways",
    "schema_migrations",
)

# Columns we know recent features rely on. Surfacing their presence/absence
# here turns "the 502 nobody can explain" into "ah, that migration didn't
# apply" in one glance.
CRITICAL_SCHEMA_CHECKS: tuple[tuple[str, str], ...] = (
    ("devices", "snmp_credential_id"),
    ("device_groups", "snmp_credential_id"),
    ("snmp_credentials", "id"),
    ("windows_credentials", "id"),
    ("discovery_profiles", "snmp_credential_ids"),
    ("discovery_profiles", "windows_credential_ids"),
    ("discovery_results", "credential_used"),
    ("audit_logs", "id"),
    ("schema_migrations", "filename"),
    ("schema_migrations", "checksum"),
)


def collect(ctx: CollectorContext) -> CollectorResult:
    result = CollectorResult(section="database")
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        result.fail("DATABASE_URL not set; cannot connect to PostgreSQL")
        return result

    asyncpg_url = db_url.replace("+asyncpg", "", 1)

    try:
        snapshot = asyncio.run(_collect_async(ctx, asyncpg_url))
    except Exception as exc:  # noqa: BLE001
        result.fail(f"postgres connect failed: {exc.__class__.__name__}: {exc}")
        return result

    for arc_name, body in snapshot.items():
        result.files[arc_name] = body

    # Run scripts/run-migrations.py --status if available — its output is the
    # quickest evidence of checksum drift (today's first incident).
    migration_status = _run_migration_status(ctx)
    result.files["database/migration-status.txt"] = migration_status.encode("utf-8")

    return result


async def _collect_async(ctx: CollectorContext, url: str) -> dict[str, bytes]:
    import asyncpg

    out: dict[str, bytes] = {}
    conn = await asyncpg.connect(url, timeout=10)
    try:
        # 1. version + size
        version = await conn.fetchval("SELECT version()")
        size_row = await conn.fetchrow(
            "SELECT current_database() AS db, pg_database_size(current_database()) AS bytes"
        )
        out["database/postgres-version.txt"] = (version or "").encode("utf-8") + b"\n"
        out["database/postgres-size.json"] = _dump({
            "database": size_row["db"] if size_row else None,
            "bytes": int(size_row["bytes"]) if size_row else 0,
        })

        # 2. schema_migrations rows (this is the headline collector for the
        #    migration-drift incident).
        try:
            rows = await conn.fetch(
                "SELECT filename, checksum, applied_at, duration_ms "
                "FROM schema_migrations ORDER BY filename"
            )
            out["database/schema-migrations.json"] = _dump({
                "rows": [{
                    "filename": r["filename"],
                    "checksum": r["checksum"],
                    "applied_at": str(r["applied_at"]),
                    "duration_ms": r["duration_ms"],
                } for r in rows],
            })
        except Exception as exc:  # noqa: BLE001
            out["database/schema-migrations.json"] = _dump({"error": str(exc)})

        # 3. critical schema-presence checks
        schema_checks: list[dict[str, Any]] = []
        for table, column in CRITICAL_SCHEMA_CHECKS:
            present = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2)",
                table, column,
            )
            schema_checks.append({"table": table, "column": column, "present": bool(present)})
        out["database/critical-schema-checks.json"] = _dump({"checks": schema_checks})

        # 4. row counts for known tables (counts only — no PII)
        row_counts: dict[str, Any] = {}
        for table in KEY_TABLES:
            try:
                count = await conn.fetchval(f"SELECT count(*) FROM {table}")
                row_counts[table] = int(count)
            except Exception as exc:  # noqa: BLE001
                row_counts[table] = {"error": str(exc)}
        out["database/row-counts.json"] = _dump({"counts": row_counts})

        # 5. recent audit actions — metadata redacted (keys only).
        try:
            audit_rows = await conn.fetch(
                "SELECT created_at, actor_username, actor_role, action, resource_type, resource_id "
                "FROM audit_logs ORDER BY created_at DESC LIMIT 100"
            )
            out["database/recent-audit-actions.json"] = _dump({
                "rows": [{
                    "created_at": str(r["created_at"]),
                    "actor_username": r["actor_username"],
                    "actor_role": r["actor_role"],
                    "action": r["action"],
                    "resource_type": r["resource_type"],
                    "resource_id": r["resource_id"],
                } for r in audit_rows],
            })
        except Exception as exc:  # noqa: BLE001
            out["database/recent-audit-actions.json"] = _dump({"error": str(exc)})

        # 6. pg_stat_activity (no query bodies — those can carry user data)
        try:
            stat = await conn.fetch(
                "SELECT pid, datname, usename, application_name, state, "
                "wait_event_type, wait_event, "
                "EXTRACT(EPOCH FROM (now() - xact_start)) AS xact_age_s "
                "FROM pg_stat_activity ORDER BY xact_age_s DESC NULLS LAST LIMIT 50"
            )
            out["database/pg-stat-activity-redacted.json"] = _dump({
                "rows": [dict(r) for r in stat],
            })
        except Exception as exc:  # noqa: BLE001
            out["database/pg-stat-activity-redacted.json"] = _dump({"error": str(exc)})

        # 7. locks on hot tables (today's 502 incident triages here).
        try:
            locks = await conn.fetch(
                "SELECT l.pid, l.locktype, l.mode, l.granted, "
                "c.relname AS relation "
                "FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation "
                "WHERE c.relname IN "
                "('devices','snmp_credentials','windows_credentials','audit_logs','schema_migrations') "
                "ORDER BY granted, mode"
            )
            out["database/pg-locks.json"] = _dump({
                "rows": [dict(r) for r in locks],
            })
        except Exception as exc:  # noqa: BLE001
            out["database/pg-locks.json"] = _dump({"error": str(exc)})

        # 8. installed extensions
        try:
            ext = await conn.fetch(
                "SELECT extname, extversion FROM pg_extension ORDER BY extname"
            )
            out["database/extensions.json"] = _dump({
                "extensions": [{"name": r["extname"], "version": r["extversion"]} for r in ext],
            })
        except Exception as exc:  # noqa: BLE001
            out["database/extensions.json"] = _dump({"error": str(exc)})
    finally:
        await conn.close()
    return out


def _run_migration_status(ctx: CollectorContext) -> str:
    runner = ctx.zenplus_root / "scripts" / "run-migrations.py"
    if not runner.exists() or not shutil.which("python3"):
        return "[support-bundle: run-migrations.py not available]\n"
    try:
        proc = subprocess.run(
            ["sudo", "-u", "postgres", "python3", str(runner),
             "--scripts-dir", str(ctx.zenplus_root / "scripts"),
             "--database", "zenplus", "--status"],
            capture_output=True, text=True, timeout=30,
        )
        out = proc.stdout
        if proc.returncode != 0:
            out += f"\n[support-bundle: exited {proc.returncode}]\n{proc.stderr}"
        return out
    except subprocess.TimeoutExpired:
        return "[support-bundle: run-migrations.py --status timed out]\n"
    except Exception as exc:  # noqa: BLE001
        return f"[support-bundle: {exc}]\n"


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
