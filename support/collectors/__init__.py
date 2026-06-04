"""Collector registry for support bundles.

Each collector is a callable that produces a ``CollectorResult``. The
registry preserves declaration order so the bundle's manifest reflects the
order phases ran in. One failing collector must never abort the bundle —
each result records its own ``status`` and the worker keeps going.

To add a collector: write a new module under ``support/collectors/``, define
``collect(ctx) -> CollectorResult``, and append it to ``ALL_COLLECTORS``.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("zenplus.support.collector")


SUPPORT_ROOT = Path("/opt/zenplus/support")
ZENPLUS_ROOT = Path("/opt/zenplus")
UPDATER_ROOT = ZENPLUS_ROOT / "updater"


@dataclasses.dataclass
class CollectorContext:
    """Read-only context handed to every collector."""

    job_id: str
    time_range: str
    include_extended_logs: bool
    zenplus_root: Path = ZENPLUS_ROOT
    updater_root: Path = UPDATER_ROOT

    def since_arg(self) -> str:
        return {
            "1h": "1 hour ago",
            "6h": "6 hours ago",
            "24h": "24 hours ago",
            "7d": "7 days ago",
        }.get(self.time_range, "24 hours ago")


@dataclasses.dataclass
class CollectorResult:
    """One collector run.

    ``files`` maps in-bundle paths (e.g. ``logs/journal-nginx.log``) to bytes
    the worker will hand to the archiver after running through the redactor.
    ``status`` is set by the collector itself; ``notes`` is a human-readable
    list of warnings/errors the support engineer should see in the manifest.
    """

    section: str
    status: str = "ok"  # ok | warning | failed
    files: dict[str, bytes] = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)
    started_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    def warn(self, msg: str) -> None:
        if self.status == "ok":
            self.status = "warning"
        self.notes.append(msg)

    def fail(self, msg: str) -> None:
        self.status = "failed"
        self.notes.append(msg)

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "files": sorted(self.files.keys()),
            "notes": self.notes,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "completed_at": self.completed_at.isoformat(timespec="seconds") if self.completed_at else None,
        }


Collector = Callable[[CollectorContext], CollectorResult]


def run_collector(name: str, fn: Collector, ctx: CollectorContext) -> CollectorResult:
    """Run one collector and catch ANY exception so the bundle can finish."""
    try:
        result = fn(ctx)
    except Exception as exc:  # noqa: BLE001 — collectors must not crash the worker
        logger.exception("collector %s crashed", name)
        result = CollectorResult(section=name)
        result.fail(f"collector raised: {exc.__class__.__name__}: {exc}")
    result.completed_at = datetime.now(timezone.utc)
    return result


# Import collector modules lazily so this package can be imported in tests
# without dragging in subprocess- or asyncpg-heavy side effects from every
# collector. The order below is the order they run in for one bundle — it
# determines the ``phase`` value the API surfaces to the dashboard.
def all_collectors() -> list[tuple[str, Collector]]:
    """Return the full ordered collector list to run for one bundle."""
    from . import (
        inventory,
        health,
        logs,
        database,
        clickhouse,
        config_files,
        network,
        storage,
        updates,
        features,
    )
    return [
        ("inventory", inventory.collect),
        ("health", health.collect),
        ("logs", logs.collect),
        ("database", database.collect),
        ("clickhouse", clickhouse.collect),
        ("config_files", config_files.collect),
        ("network", network.collect),
        ("storage", storage.collect),
        ("updates", updates.collect),
        ("features", features.collect),
    ]
