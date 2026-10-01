"""NetFlow background loops, started once from the API lifespan (contract §9.4).

Each NetFlow service that needs a periodic job exposes one coroutine function;
``start_netflow_tasks(app)`` starts every one that exists in this build:

    netflow_rollup.netflow_rollup_loop               rollup backfill / healer
    netflow_detection.netflow_detection_loop         detection engine
    netflow_ti.netflow_ti_loop                       threat-intel feed refresh
    netflow_health.netflow_health_loop               exporter / collector health
    netflow_alert_service.netflow_alert_loop         flow alert rules
    netflow_forensics_export.netflow_export_loop     queued forensic exports

A module or function that is not present yet is skipped, so the registry can
list loops that land in later changes. Each loop takes its own PostgreSQL
advisory lock (the API runs two uvicorn workers), so starting it in every
worker is safe.

Loops are supervised: one that returns or raises is logged and restarted after
a back-off instead of silently stopping for the life of the process. Shutdown
cancels them all.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("zenplus.netflow")

LOOPS: tuple[tuple[str, str], ...] = (
    ("app.services.netflow_rollup", "netflow_rollup_loop"),
    ("app.services.netflow_detection", "netflow_detection_loop"),
    ("app.services.netflow_ti", "netflow_ti_loop"),
    ("app.services.netflow_health", "netflow_health_loop"),
    ("app.services.netflow_alert_service", "netflow_alert_loop"),
    ("app.services.netflow_forensics_export", "netflow_export_loop"),
)

RESTART_BACKOFF_S = (5.0, 30.0, 120.0, 600.0)
# A loop that ran this long before failing starts the back-off from the top.
HEALTHY_RUN_S = 600.0


def _resolve(module_name: str, func_name: str) -> Callable[[], Awaitable[Any]] | None:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None          # not part of this build yet
        logger.exception("netflow: %s failed to import", module_name)
        return None
    except Exception:
        logger.exception("netflow: %s failed to import", module_name)
        return None
    func = getattr(module, func_name, None)
    if func is None:
        return None
    if not inspect.iscoroutinefunction(func):
        logger.error("netflow: %s.%s is not a coroutine function; not started", module_name, func_name)
        return None
    return func


async def _supervise(name: str, func: Callable[[], Awaitable[Any]]) -> None:
    failures = 0
    while True:
        started = time.monotonic()
        try:
            await func()
            logger.warning("netflow: background loop %s returned; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow: background loop %s crashed; restarting", name)
        if time.monotonic() - started >= HEALTHY_RUN_S:
            failures = 0
        delay = RESTART_BACKOFF_S[min(failures, len(RESTART_BACKOFF_S) - 1)]
        failures += 1
        await asyncio.sleep(delay)


def start_netflow_tasks(app: Any, loops: tuple[tuple[str, str], ...] = LOOPS) -> list[asyncio.Task]:
    """Start every available NetFlow loop; keeps them on ``app.state.netflow_tasks``
    and (once per app) registers their cancellation for application shutdown."""
    tasks: list[asyncio.Task] = []
    for module_name, func_name in loops:
        func = _resolve(module_name, func_name)
        if func is None:
            continue
        name = f"{module_name.rsplit('.', 1)[-1]}.{func_name}"
        tasks.append(asyncio.create_task(_supervise(name, func), name=f"netflow:{name}"))
    app.state.netflow_tasks = tasks
    if tasks:
        logger.info("netflow: started %s", ", ".join(t.get_name() for t in tasks))
    # The app object outlives one startup/shutdown cycle (tests start it
    # repeatedly), so the handler is registered once and reads the tasks of
    # the current cycle from app.state.
    if not getattr(app.state, "netflow_tasks_stop_registered", False):
        async def _stop() -> None:
            await stop_netflow_tasks(app)

        app.router.add_event_handler("shutdown", _stop)
        app.state.netflow_tasks_stop_registered = True
    return tasks


async def stop_netflow_tasks(app: Any) -> None:
    """Cancel the loops started by start_netflow_tasks and wait for them."""
    tasks = list(getattr(app.state, "netflow_tasks", None) or [])
    app.state.netflow_tasks = []
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
