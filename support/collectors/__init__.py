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
import signal
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("zenplus.support.collector")


SUPPORT_ROOT = Path("/opt/zenplus/support")
ZENPLUS_ROOT = Path("/opt/zenplus")
UPDATER_ROOT = ZENPLUS_ROOT / "updater"
DEFAULT_COLLECTOR_TIMEOUT_S = 120


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
    archived_files: list[str] = dataclasses.field(default_factory=list)
    truncated_files: list[str] = dataclasses.field(default_factory=list)
    skipped_files: list[str] = dataclasses.field(default_factory=list)
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
        completed = self.completed_at
        duration_ms = (
            max(0, int((completed - self.started_at).total_seconds() * 1000))
            if completed else None
        )
        return {
            "status": self.status,
            # ``files`` remains the compatibility field and now means files
            # that actually made it into the archive. Before packaging it
            # falls back to generated files, as unit-level collector callers
            # do not have an archive stage.
            "files": sorted(self.archived_files or self.files.keys()),
            "generated_files": sorted(self.files.keys()),
            "truncated_files": sorted(self.truncated_files),
            "skipped_files": sorted(self.skipped_files),
            "generated_bytes": sum(len(body) for body in self.files.values()),
            "notes": self.notes,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "completed_at": completed.isoformat(timespec="seconds") if completed else None,
            "duration_ms": duration_ms,
        }


Collector = Callable[[CollectorContext], CollectorResult]


class CollectorTimeoutError(TimeoutError):
    """Raised inside a collector when its wall-clock budget is exhausted."""


def run_collector(
    name: str,
    fn: Collector,
    ctx: CollectorContext,
    *,
    timeout_s: int = DEFAULT_COLLECTOR_TIMEOUT_S,
) -> CollectorResult:
    """Run one collector with failure isolation and a Linux wall-clock limit.

    The worker is single-threaded under systemd, so SIGALRM safely interrupts
    blocking Python/socket/subprocess waits on the appliance. Platforms that
    do not expose SIGALRM (notably Windows unit-test hosts) retain exception
    isolation while each collector's own command timeouts remain in force.
    """
    previous_handler = None
    use_alarm = (
        timeout_s > 0
        and hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )

    def _on_timeout(_signum, _frame):
        raise CollectorTimeoutError(f"collector exceeded {timeout_s}s wall-clock limit")

    try:
        if use_alarm:
            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _on_timeout)
            signal.setitimer(signal.ITIMER_REAL, timeout_s)
        result = fn(ctx)
    except CollectorTimeoutError as exc:
        logger.error("collector %s timed out after %ss", name, timeout_s)
        result = CollectorResult(section=name)
        result.fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — collectors must not crash the worker
        logger.exception("collector %s crashed", name)
        result = CollectorResult(section=name)
        result.fail(f"collector raised: {exc.__class__.__name__}: {exc}")
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
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
        database,
        clickhouse,
        config_files,
        network,
        storage,
        updates,
        features,
        telemetry,
        reachability,
        logs,
    )
    return [
        ("inventory", inventory.collect),
        ("health", health.collect),
        ("database", database.collect),
        ("clickhouse", clickhouse.collect),
        ("config_files", config_files.collect),
        ("network", network.collect),
        ("storage", storage.collect),
        ("updates", updates.collect),
        ("features", features.collect),
        ("telemetry", telemetry.collect),
        ("reachability", reachability.collect),
        # Logs are intentionally last: bounded structured diagnostics must not
        # be starved if several noisy journals consume the optional data cap.
        ("logs", logs.collect),
    ]
