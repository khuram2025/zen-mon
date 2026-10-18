"""One evaluator pass at a time across API workers.

Production runs two uvicorn workers and every background loop is started in
both. The periodic alert evaluators (network, host, APM, APM SLO burn) and the
escalation sweeper were written as if that were harmless: they look up the
open incident, decide, write and then notify. Two workers running the same
pass at the same moment both decide, and both notify — the identical
interface-error mails a few seconds apart in the October 2026 field report.

Each pass now first takes a transaction-scoped advisory try-lock on a
dedicated session, keyed by a hashed name ("zenplus:network_alert_pass"), and
runs on its own session that commits freely. A worker that does not get the
lock skips that tick: the other worker is doing the same work right now.
Hashed string keys keep these locks out of the literal integer keys other
loops use (several of which collide with each other).

The lock ends with the lock session's transaction, so a crashed worker never
holds it past its connection.
"""

from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator, Callable

from sqlalchemy import text

logger = logging.getLogger("zenplus.alert_worker_lock")

NETWORK_ALERT_PASS = "zenplus:network_alert_pass"
HOST_ALERT_PASS = "zenplus:host_alert_pass"
APM_ALERT_PASS = "zenplus:apm_alert_pass"
APM_SLO_PASS = "zenplus:apm_slo_pass"
ESCALATION_PASS = "zenplus:escalation_pass"
NOTIFICATION_DIGEST_PASS = "zenplus:notification_digest_pass"


@contextlib.asynccontextmanager
async def pass_lock(name: str, session_factory: Callable | None = None) -> AsyncIterator[bool]:
    """Yield True when this worker owns the pass ``name``, False to skip it.

    ``session_factory`` defaults to the application's AsyncSessionLocal; tests
    pass one bound to a disposable database.
    """
    if session_factory is None:
        from app.core.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    async with session_factory() as lock_db:
        got = bool((await lock_db.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": name},
        )).scalar())
        try:
            if not got:
                logger.debug("%s: another worker is running this pass; skipping", name)
            yield got
        finally:
            # Ending the transaction releases the lock.
            await lock_db.rollback()


async def run_pass(name: str, work: Callable, session_factory: Callable | None = None):
    """Run ``await work(db)`` under :func:`pass_lock`; None when skipped."""
    if session_factory is None:
        from app.core.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    async with pass_lock(name, session_factory) as held:
        if not held:
            return None
        async with session_factory() as db:
            return await work(db)
