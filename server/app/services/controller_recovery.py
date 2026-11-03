"""Durable, bounded protection for missing observations during controller outages.

Only absence-based decisions use this gate. Fresh ICMP/service failures and
metric breaches retain their normal confirmation rules. A reporting agent
immediately resumes normal silence detection, even while its peers reconnect.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

KEY = "monitoring.controller_recovery"
# Agent communication backoff caps at 600s, plus two default heartbeat ticks.
RECONNECT_S = 660
HARD_LIMIT_S = 1800
LEASE_GAP_S = 90
TICK_S = 15
logger = logging.getLogger("zenplus.controller_recovery")


def invocation_id() -> str:
    token = os.getenv("INVOCATION_ID")
    if token:
        return token
    # Uvicorn workers share a parent. Boot identity prevents PID reuse on reboot.
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = "development"
    return f"{boot}:{os.getppid()}"


@dataclass(frozen=True)
class Recovery:
    started_at: float
    ready_at: float | None
    deadline: float
    last_tick: float
    invocation: str
    reason: str = "controller restart"
    settled: bool = False

    def hold_missing(self, observed_at: datetime | None, now: float, after_s: int) -> bool:
        if now >= self.deadline:
            return False
        if self.ready_at is None or now - self.last_tick > LEASE_GAP_S:
            return True
        if observed_at is not None and observed_at.timestamp() >= self.ready_at:
            return False
        return now < min(self.ready_at + RECONNECT_S + after_s, self.deadline)

    def sql_parameters(self) -> dict:
        """Same gate for sweep SQL, with a per-row silence threshold."""
        return {"cr_ready": self.ready_at, "cr_deadline": self.deadline,
                "cr_tick": self.last_tick, "cr_reconnect": RECONNECT_S,
                "cr_gap": LEASE_GAP_S}


def silence_is_observable(column: str, threshold: str) -> str:
    # Both arguments are developer-owned SQL expressions, never request values.
    return f"""(EXTRACT(EPOCH FROM NOW()) >= :cr_deadline OR
        (CAST(:cr_ready AS double precision) IS NOT NULL AND EXTRACT(EPOCH FROM NOW()) - :cr_tick <= :cr_gap
         AND ({column} >= to_timestamp(:cr_ready) OR
              EXTRACT(EPOCH FROM NOW()) >= :cr_ready + :cr_reconnect + {threshold})))"""


async def load(db) -> Recovery | None:
    value = (await db.execute(text("SELECT value FROM system_settings WHERE key=:key"),
                              {"key": KEY})).scalar()
    if not value:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return Recovery(**value)


async def save(db, state: Recovery) -> None:
    await db.execute(text("""INSERT INTO system_settings(key,value) VALUES (:key,CAST(:value AS jsonb))
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"""),
        {"key": KEY, "value": json.dumps(state.__dict__)})


async def transition(db, *, token: str, now: float, ready: bool = False,
                     planned: bool = False) -> Recovery:
    """One epoch across workers. Repeated restarts never renew an unfinished deadline."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": KEY})
    old = await load(db)
    changed = old is None or old.invocation != token or planned
    gap = old is not None and now - old.last_tick > LEASE_GAP_S
    if changed or gap:
        deadline = old.deadline if old and not old.settled else now + HARD_LIMIT_S
        state = Recovery(now, now if ready and not planned else None, deadline, now, token,
                         "planned update" if planned else "controller restart or monitoring interruption")
        logger.warning("Monitoring recovery started; missing-observation protection ends by %s",
                       datetime.fromtimestamp(deadline, timezone.utc).isoformat())
    else:
        state = Recovery(old.started_at, old.ready_at if old.ready_at is not None else (now if ready else None),
                         old.deadline, now, token, old.reason,
                         old.settled or (ready and now >= old.deadline))
    await save(db, state)
    await db.commit()
    return state


async def startup(*, ready: bool) -> None:
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await transition(db, token=invocation_id(), now=datetime.now(timezone.utc).timestamp(), ready=ready)


async def recovery_loop() -> None:
    from app.core.database import AsyncSessionLocal
    while True:
        await asyncio.sleep(TICK_S)
        try:
            async with AsyncSessionLocal() as db:
                state = await transition(db, token=invocation_id(),
                                         now=datetime.now(timezone.utc).timestamp(), ready=True)
                if state.ready_at and state.last_tick >= state.deadline and not state.settled:
                    logger.error("Controller recovery deadline reached; normal outage alerting resumes")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Controller monitoring readiness lease failed")
