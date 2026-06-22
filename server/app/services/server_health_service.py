"""Server health status, staleness sweeping, and server-scoped alerts.

Three jobs:

* Compute a server's health (``healthy``/``warning``/``critical``) plus
  human-readable ``status_reasons`` from the freshest telemetry, every time a
  metric batch is ingested.
* Periodically sweep agents/servers whose heartbeats stopped — agents roll
  ``online → stale → offline`` and their server rolls to ``stale``; an
  *agent offline* alert is raised on the offline transition and resolved on
  the next successful heartbeat.
* Small helpers to raise/resolve alerts scoped to a server (used here and by
  the baseline compliance engine).

Thresholds are deliberately conservative defaults; per-policy overrides can
be layered on later without changing the storage model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.filesystem_monitoring import pg_capacity_filter

logger = logging.getLogger("zenplus.server_health")

# ── Health thresholds (percent) ──────────────────────────────────────

CPU_WARN, CPU_CRIT = 90.0, 98.0
MEM_WARN, MEM_CRIT = 90.0, 97.0
DISK_WARN, DISK_CRIT = 85.0, 95.0

# ── Staleness windows (seconds) ──────────────────────────────────────
# Heartbeat default is 30s: stale after ~4 missed beats, offline after 10 min.

AGENT_STALE_AFTER_S = 120
AGENT_OFFLINE_AFTER_S = 600
SERVER_STALE_AFTER_S = 300
SWEEP_INTERVAL_S = 60


# ── Server-scoped alert helpers ──────────────────────────────────────

async def create_server_alert(
    db: AsyncSession,
    server_id: str,
    severity: str,
    message: str,
    source: str,
    dedupe: str,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Insert an active alert for a server unless an identical one is open.

    ``dedupe`` identifies the condition instance (e.g. ``baseline:<rule_id>``
    or ``agent_offline``) so repeat evaluations don't stack duplicates.
    Returns True when a new alert row was created.
    """
    # Respect an active silence (snooze until a time, or mute forever).
    silenced = (await db.execute(
        text("""SELECT 1 FROM alert_silences
                WHERE server_id = :sid AND dedupe = :dedupe
                  AND (until IS NULL OR until > NOW())
                LIMIT 1"""),
        {"sid": server_id, "dedupe": dedupe},
    )).first()
    if silenced:
        return False

    # Treat acknowledged like active so an acknowledged alert isn't re-created
    # every evaluation cycle while the condition persists.
    existing = (await db.execute(
        text("""SELECT id FROM alerts
                WHERE server_id = :sid AND status IN ('active', 'acknowledged')
                  AND metadata->>'dedupe' = :dedupe
                LIMIT 1"""),
        {"sid": server_id, "dedupe": dedupe},
    )).first()
    if existing:
        return False

    meta = dict(metadata or {})
    meta["source"] = source
    meta["dedupe"] = dedupe
    await db.execute(
        text("""INSERT INTO alerts (server_id, severity, message, status, metadata)
                VALUES (:sid, :sev, :msg, 'active', CAST(:meta AS jsonb))"""),
        {"sid": server_id, "sev": severity, "msg": message, "meta": json.dumps(meta)},
    )
    return True


async def resolve_server_alerts(db: AsyncSession, server_id: str, dedupe: str) -> int:
    """Resolve open alerts for a server matching a dedupe key (or prefix with %)."""
    res = await db.execute(
        text("""UPDATE alerts SET status = 'resolved', resolved_at = NOW()
                WHERE server_id = :sid AND status IN ('active', 'acknowledged')
                  AND metadata->>'dedupe' LIKE :dedupe"""),
        {"sid": server_id, "dedupe": dedupe},
    )
    return res.rowcount or 0


# ── Health computation (called from metric ingest) ───────────────────

def _level(value: float, warn: float, crit: float) -> Optional[str]:
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return None


async def compute_server_health(
    db: AsyncSession,
    server_id: str,
    by_kind: dict[str, list[dict]],
) -> tuple[str, list[str], list[dict[str, str]]]:
    """Derive (status, reasons, issues) from the just-ingested batch + inventory.

    CPU/memory use the batch average (the agent already averages over its
    collection interval); filesystem usage uses the last-known inventory
    rows that the same batch just upserted.

    Service stop/start state is inventory-only here — admins opt in to
    service/process alerts via ``host_service_down`` / ``host_process_down``
    rules (evaluated by host_alert_service).

    Each issue includes ``level``, ``reason``, and a stable ``dedupe`` key
    used to mirror the condition into the alerts table.
    """
    status = "healthy"
    reasons: list[str] = []
    issues: list[dict[str, str]] = []

    def _bump(level: str, reason: str, dedupe: str) -> None:
        nonlocal status
        reasons.append(reason)
        issues.append({"level": level, "reason": reason, "dedupe": dedupe})
        if level == "critical":
            status = "critical"
        elif level == "warning" and status != "critical":
            status = "warning"

    cpu_rows = by_kind.get("cpu") or []
    if cpu_rows:
        vals = [float(r.get("cpu_total_pct") or 0) for r in cpu_rows]
        avg = sum(vals) / len(vals)
        lvl = _level(avg, CPU_WARN, CPU_CRIT)
        if lvl:
            _bump(
                lvl,
                f"CPU at {avg:.1f}% (≥ {CPU_CRIT if lvl == 'critical' else CPU_WARN:.0f}%)",
                "health:cpu",
            )

    mem_rows = by_kind.get("memory") or []
    if mem_rows:
        vals = []
        for r in mem_rows:
            pct = float(r.get("used_pct") or 0)
            if not pct:
                total, used = float(r.get("total_bytes") or 0), float(r.get("used_bytes") or 0)
                pct = (used / total * 100.0) if total else 0.0
            vals.append(pct)
        avg = sum(vals) / len(vals)
        lvl = _level(avg, MEM_WARN, MEM_CRIT)
        if lvl:
            _bump(
                lvl,
                f"Memory at {avg:.1f}% (≥ {MEM_CRIT if lvl == 'critical' else MEM_WARN:.0f}%)",
                "health:memory",
            )

    fs_rows = (await db.execute(
        text(f"""SELECT mount, used_pct FROM server_filesystem_inventory
                WHERE server_id = :sid AND used_pct IS NOT NULL
                  AND {pg_capacity_filter()}"""),
        {"sid": server_id},
    )).all()
    for mount, used_pct in fs_rows:
        lvl = _level(float(used_pct or 0), DISK_WARN, DISK_CRIT)
        if lvl:
            _bump(
                lvl,
                f"Filesystem {mount} at {float(used_pct):.1f}% (≥ {DISK_CRIT if lvl == 'critical' else DISK_WARN:.0f}%)",
                f"health:filesystem:{mount}",
            )

    return status, reasons, issues


async def sync_health_alerts(
    db: AsyncSession,
    server_id: str,
    issues: list[dict[str, str]],
) -> None:
    """Mirror computed health issues into the alerts table (create + resolve)."""
    active_dedupes = {issue["dedupe"] for issue in issues}
    for issue in issues:
        await create_server_alert(
            db,
            server_id,
            severity=issue["level"],
            message=issue["reason"],
            source="health_threshold",
            dedupe=issue["dedupe"],
        )

    open_rows = (await db.execute(
        text("""SELECT metadata->>'dedupe' AS dedupe FROM alerts
                WHERE server_id = :sid AND status IN ('active', 'acknowledged')
                  AND metadata->>'source' = 'health_threshold'"""),
        {"sid": server_id},
    )).all()
    for row in open_rows:
        if row.dedupe not in active_dedupes:
            await resolve_server_alerts(db, server_id, row.dedupe)


async def store_server_health(
    db: AsyncSession,
    server_id: str,
    status: str,
    reasons: list[str],
    issues: Optional[list[dict[str, str]]] = None,
) -> None:
    """Persist computed health; never overrides disabled servers."""
    await db.execute(
        text("""UPDATE servers SET
                    status = CASE WHEN status = 'disabled' THEN status ELSE :st END,
                    status_reasons = CAST(:reasons AS jsonb),
                    updated_at = NOW()
                WHERE id = :sid"""),
        {"sid": server_id, "st": status, "reasons": json.dumps(reasons)},
    )
    if issues is not None:
        await sync_health_alerts(db, server_id, issues)


async def refresh_server_health(db: AsyncSession, server_id: str) -> tuple[str, list[str]]:
    """Recompute health from inventory + latest metrics (no new ingest required)."""
    status, reasons, issues = await compute_server_health(db, server_id, {})
    await store_server_health(db, server_id, status, reasons, issues)
    return status, reasons


# ── Staleness sweep ──────────────────────────────────────────────────

async def sweep_stale(db: AsyncSession) -> dict[str, int]:
    """Roll agents online→stale→offline and servers →stale on missing data.

    Raises a critical *agent offline* alert per server on the offline
    transition (resolved again by the next heartbeat in agents.py).
    """
    stale = await db.execute(
        text(f"""UPDATE agents SET status = 'stale', updated_at = NOW()
                 WHERE status = 'online'
                   AND last_heartbeat_at < NOW() - INTERVAL '{AGENT_STALE_AFTER_S} seconds'
                 RETURNING id"""),
    )
    stale_n = len(stale.fetchall())

    offline = await db.execute(
        text(f"""UPDATE agents SET status = 'offline', updated_at = NOW()
                 WHERE status IN ('online', 'stale')
                   AND last_heartbeat_at < NOW() - INTERVAL '{AGENT_OFFLINE_AFTER_S} seconds'
                 RETURNING id, server_id, hostname, last_heartbeat_at"""),
    )
    offline_rows = offline.fetchall()

    servers_stale = await db.execute(
        text(f"""UPDATE servers SET
                     status = 'stale',
                     status_reasons = CAST(:reason AS jsonb),
                     updated_at = NOW()
                 WHERE collection_mode = 'agent'
                   AND status NOT IN ('disabled', 'stale', 'unknown')
                   AND (last_seen IS NULL OR last_seen < NOW() - INTERVAL '{SERVER_STALE_AFTER_S} seconds')
                 RETURNING id"""),
        {"reason": json.dumps(["No data received from agent (heartbeats stopped)"])},
    )
    servers_n = len(servers_stale.fetchall())

    for row in offline_rows:
        agent_id, server_id, hostname, last_hb = row
        if not server_id:
            continue
        await create_server_alert(
            db,
            str(server_id),
            severity="critical",
            message=f"Agent on {hostname or 'server'} is offline — last heartbeat {last_hb:%Y-%m-%d %H:%M UTC}" if last_hb else f"Agent on {hostname or 'server'} is offline",
            source="agent_offline",
            dedupe="agent_offline",
            metadata={"agent_id": str(agent_id)},
        )

    await db.commit()
    if stale_n or offline_rows or servers_n:
        logger.info("sweep: %d agents stale, %d offline, %d servers stale",
                    stale_n, len(offline_rows), servers_n)
    return {"agents_stale": stale_n, "agents_offline": len(offline_rows), "servers_stale": servers_n}


async def health_sweeper_loop() -> None:
    """Background task: run the staleness sweep every SWEEP_INTERVAL_S."""
    from app.core.database import AsyncSessionLocal

    # Let the app finish booting before the first sweep.
    await asyncio.sleep(10)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep_stale(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("staleness sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
