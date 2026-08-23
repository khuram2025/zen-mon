"""Post-update schema gate.

The updater used to declare an update successful once an HTTP health endpoint
returned 200. That check says the API process started; it says nothing about
whether the database has the tables that process needs. An appliance therefore
took a v1.6.0 version stamp while its ClickHouse SNMP tables and several
PostgreSQL columns were missing, and reported healthy for weeks.

This module closes that loop: it converges both databases with the migrations
on disk and returns a verdict. ``agent.run_update`` refuses to write the new
version marker unless the verdict is clean, and rolls the appliance back
instead — an appliance on the old version with a consistent schema is a working
appliance; a half-migrated one is not.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")
SYNC_SCRIPT = ZENPLUS_DIR / "scripts" / "sync-schema.py"


def _fallback(scripts_dir: Path) -> dict:
    """Converge ClickHouse only, for a payload without sync-schema.py.

    PostgreSQL still has its own tracked runner invoked by the run_migration
    step, so ClickHouse is the gap worth closing here.
    """
    from .clickhouse_sync import sync_clickhouse_migrations

    summary = sync_clickhouse_migrations(scripts_dir)
    problems: list[str] = []
    if summary.get("error"):
        problems.append(f"clickhouse: {summary['error']}")
    for item in summary.get("failed", []):
        problems.append(f"clickhouse: {item['filename']} failed")
    for item in summary.get("unresolved", []):
        problems.append(f"clickhouse: {item['filename']} {item.get('reason', 'unresolved')}")
    return {"ok": not problems, "problems": problems, "clickhouse": summary,
            "postgres": {}, "source": "fallback"}


def sync_and_verify(scripts_dir: Path | None = None, *, timeout: int = 1800) -> dict:
    """Apply every pending migration on disk, then report whether drift remains.

    Returns ``{ok: bool, problems: [str], postgres: {...}, clickhouse: {...}}``.
    ``ok`` is False when anything is pending, failed, checksum-drifted, or when
    a database could not be reached — "cannot tell" is never treated as healthy.
    """
    scripts_dir = scripts_dir or (ZENPLUS_DIR / "scripts")

    if not SYNC_SCRIPT.exists():
        logger.warning("Schema gate: %s missing, falling back to ClickHouse-only sync",
                       SYNC_SCRIPT)
        return _fallback(scripts_dir)

    try:
        result = subprocess.run(
            ["python3", str(SYNC_SCRIPT),
             "--scripts-dir", str(scripts_dir), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ZENPLUS_DIR),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "problems": [f"schema sync did not complete: {e}"],
                "postgres": {}, "clickhouse": {}, "source": "error"}

    status = None
    stdout = result.stdout.strip()
    if stdout:
        try:
            status = json.loads(stdout[stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            status = None

    if status is None:
        return {
            "ok": result.returncode == 0,
            "problems": [] if result.returncode == 0 else [
                f"schema sync exited {result.returncode}: "
                f"{(result.stderr or stdout).strip()[:500]}"
            ],
            "postgres": {}, "clickhouse": {}, "source": "exit-code",
        }

    status.setdefault("source", "sync-schema")
    for line in (result.stderr or "").splitlines():
        if line.startswith("DRIFT "):
            logger.error("Schema gate: %s", line[len("DRIFT "):])

    pg = status.get("postgres", {})
    ch = status.get("clickhouse", {})
    logger.info(
        "Schema gate: postgres applied=%d pending=%d | clickhouse applied=%d "
        "healed=%d baselined=%d pending=%d",
        len(pg.get("applied", [])), len(pg.get("pending", [])),
        len(ch.get("applied", [])), len(ch.get("healed", [])),
        len(ch.get("baselined", [])), len(ch.get("pending", [])),
    )
    return status
