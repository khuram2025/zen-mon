"""Ingestion backpressure while free space is critical.

The storage watchdog creates its ``ingest-paused`` marker when free space is
critically low and nothing more can be reclaimed within policy. Bulk,
non-essential telemetry endpoints (host agent metrics, APM traces) then answer
503 with Retry-After: agents keep the data in their spools and replay it once
space returns. Availability monitoring (ping/service/SNMP results) is never
paused, so alerting keeps working.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import HTTPException

from app.services import storage_watchdog

RETRY_AFTER_S = 300
_CACHE_S = 2.0
_cache: dict = {"at": 0.0, "value": None}


def ingest_pause() -> Optional[dict]:
    now = time.monotonic()
    if now - _cache["at"] >= _CACHE_S:
        _cache["value"] = storage_watchdog.ingest_paused()
        _cache["at"] = now
    return _cache["value"]


def raise_if_paused(what: str) -> None:
    pause = ingest_pause()
    if pause:
        raise HTTPException(
            503,
            f"{what} ingestion is paused: the appliance is critically low on disk space "
            f"({pause.get('reason', 'free space critically low')}); retry later",
            headers={"Retry-After": str(RETRY_AFTER_S)},
        )
