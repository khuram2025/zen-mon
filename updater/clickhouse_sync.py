"""Apply pending ClickHouse migrations after an OTA update.

This module is now a thin adapter. The real convergence logic lives in
``scripts/ch_migrate.py`` so that a single implementation is shared by the
updater, the release-time hook (``scripts/sync-schema.py``) and anyone running
it by hand on an appliance.

Why the adapter still exists
----------------------------
``agent.run_update`` imports this lazily, *after* ``apply_code`` has landed the
new payload, so an appliance running the old updater still executes the new
code on the very first update that carries it. Keeping the
``sync_clickhouse_migrations()`` name and signature is what lets the fix heal a
fleet in one pass instead of two.

The behaviour change that matters: the previous version baselined a hardcoded
list of legacy migrations — recording them as applied without running them —
whenever the ledger was empty. On an appliance that had never actually received
those migrations that stamped a lie into the ledger, and
``zenplus.snmp_metrics`` was never created. ``ch_migrate`` decides by probing
what really exists instead. See its module docstring.
"""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

SCRIPTS_DIR = Path("/opt/zenplus/scripts")


def _load_ch_migrate(scripts_dir: Path):
    """Import scripts/ch_migrate.py by path.

    It lives in scripts/ rather than in this package because it must also run
    standalone, without the updater's dependencies, as an ordinary script.
    """
    path = Path(scripts_dir) / "ch_migrate.py"
    spec = importlib.util.spec_from_file_location("zenplus_ch_migrate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sync_clickhouse_migrations(scripts_dir: Path = SCRIPTS_DIR) -> dict:
    """Converge the ClickHouse schema with the migrations on disk.

    Best-effort at this layer: the authoritative gate is
    ``scripts/sync-schema.py``, which runs as a manifest step and fails the
    update on unresolved drift. Here we log and return a summary so a
    ClickHouse hiccup cannot abort an otherwise-good code update mid-flight.

    Returns ``{applied, baselined, healed, pending, failed, unresolved}``.
    """
    empty: dict[str, list] = {
        "applied": [], "baselined": [], "healed": [],
        "pending": [], "failed": [], "unresolved": [],
    }

    try:
        ch_migrate = _load_ch_migrate(scripts_dir)
    except (ImportError, OSError, SyntaxError) as e:
        logger.error("ClickHouse sync: cannot load ch_migrate.py, skipping: %s", e)
        return empty

    try:
        summary = ch_migrate.sync(Path(scripts_dir))
    except Exception as e:  # ClickHouse unreachable etc. — never abort the update here
        logger.error("ClickHouse sync: %s", e)
        empty["error"] = str(e)
        return empty

    if summary.get("healed"):
        logger.warning(
            "ClickHouse sync: healed %d migration(s) recorded as applied but "
            "missing from the schema: %s",
            len(summary["healed"]), summary["healed"],
        )
    if summary.get("applied") or summary.get("baselined"):
        logger.info("ClickHouse sync: applied=%s baselined=%s",
                    summary.get("applied"), summary.get("baselined"))
    if summary.get("failed") or summary.get("unresolved"):
        logger.error("ClickHouse sync: failed=%s unresolved=%s",
                     summary.get("failed"), summary.get("unresolved"))
    if not any(summary.get(k) for k in
               ("applied", "baselined", "healed", "failed", "unresolved")):
        logger.info("ClickHouse sync: schema already up to date")
    return summary
