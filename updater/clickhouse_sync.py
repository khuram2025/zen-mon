"""Apply pending ClickHouse migrations after an OTA update.

Background
----------
PostgreSQL migrations are auto-applied with a ledger (``scripts/run-migrations.py``
+ the ``schema_migrations`` table). ClickHouse had no equivalent: its migrations
only ran via ``bin/first-boot-init.sh`` (exactly once, gated by a sentinel) or via
an explicit, opt-in ``run_migration`` step that a release builder had to remember
to package. When a feature shipped its code but its ClickHouse schema was not
packaged, the controller kept rejecting metrics with "Table ... does not exist"
(e.g. the host_* metric tables).

This module closes that gap: it runs on *every* update, discovers the
``migrate-*-clickhouse.sql`` files already shipped onto disk (``scripts/`` is part
of every release payload), and applies the ones not yet recorded in a ClickHouse
``schema_migrations`` ledger.

Safety
------
Some legacy migrations are NOT safe to replay — they backfill rollups with blind
``INSERT``s and create materialized views without ``IF NOT EXISTS`` (011, 012).
Replaying them on an already-migrated appliance would duplicate rows / hard-error.
So on a fresh (empty) ledger we *baseline* those legacy migrations as
already-applied without running them. Everything else is idempotent
(``CREATE ... IF NOT EXISTS``) and safe to (re)apply. All NEW ClickHouse
migrations must stay idempotent.
"""

import hashlib
import logging
from pathlib import Path

from .steps.run_migration import apply_clickhouse_sql

logger = logging.getLogger("zenplus.updater")

SCRIPTS_DIR = Path("/opt/zenplus/scripts")

# ClickHouse migrations that predate this sync and are unsafe to replay on an
# appliance where they already ran (blind backfill INSERTs / non-IF-NOT-EXISTS
# materialized views). On a fresh ledger these are recorded as applied WITHOUT
# being executed. Do not add idempotent migrations here — let them run.
_LEGACY_BASELINE = {
    "migrate-004-snmp-clickhouse.sql",
    "migrate-006-services-v2-clickhouse.sql",
    "migrate-011-activity-rollups-clickhouse.sql",
    "migrate-012-ping-rollups-clickhouse.sql",
    "migrate-20260506-netflow-clickhouse.sql",
}

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS zenplus.schema_migrations
(
    filename String,
    checksum String,
    applied_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(applied_at)
ORDER BY filename
"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _record(filename: str, checksum: str) -> None:
    apply_clickhouse_sql(
        "INSERT INTO zenplus.schema_migrations (filename, checksum) "
        f"VALUES ('{_esc(filename)}', '{_esc(checksum)}')"
    )


def _applied_filenames() -> set[str]:
    out = apply_clickhouse_sql(
        "SELECT DISTINCT filename FROM zenplus.schema_migrations FORMAT TabSeparated"
    )
    return {line.strip() for line in out.splitlines() if line.strip()}


def sync_clickhouse_migrations(scripts_dir: Path = SCRIPTS_DIR) -> dict:
    """Apply pending ClickHouse migrations. Best-effort, never raises.

    Returns a summary dict: {applied: [...], baselined: [...], failed: [...]}.
    A migration that fails is logged and left unrecorded so the next update
    retries it; it does not abort the rest of the sync or the update.
    """
    summary: dict[str, list[str]] = {"applied": [], "baselined": [], "failed": []}

    migrations = sorted(scripts_dir.glob("migrate-*-clickhouse.sql"))
    if not migrations:
        logger.info("ClickHouse sync: no migration files on disk, nothing to do")
        return summary

    try:
        apply_clickhouse_sql(_LEDGER_DDL)
        applied = _applied_filenames()
    except Exception as e:  # ClickHouse unreachable etc. — don't fail the update
        logger.error("ClickHouse sync: cannot reach ledger, skipping: %s", e)
        return summary

    # First run on this appliance: baseline the unsafe-to-replay legacy
    # migrations so we never re-execute their backfills, while still letting
    # idempotent ones (host metrics, future) apply below.
    if not applied:
        for path in migrations:
            if path.name in _LEGACY_BASELINE:
                try:
                    _record(path.name, _sha256(path))
                    applied.add(path.name)
                    summary["baselined"].append(path.name)
                    logger.info("ClickHouse sync: baselined %s (not run)", path.name)
                except Exception as e:
                    logger.error("ClickHouse sync: baseline failed for %s: %s",
                                 path.name, e)

    for path in migrations:
        if path.name in applied:
            continue
        try:
            apply_clickhouse_sql(path.read_text())
            _record(path.name, _sha256(path))
            summary["applied"].append(path.name)
            logger.info("ClickHouse sync: applied %s", path.name)
        except Exception as e:
            summary["failed"].append(path.name)
            logger.error("ClickHouse sync: %s failed (will retry next update): %s",
                         path.name, e)

    if summary["applied"] or summary["failed"]:
        logger.info("ClickHouse sync done: applied=%s baselined=%s failed=%s",
                    summary["applied"], summary["baselined"], summary["failed"])
    else:
        logger.info("ClickHouse sync: schema already up to date")
    return summary
