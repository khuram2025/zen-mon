"""Appliance clock diagnostics (review B01).

The UI calibrates "now" from the X-Server-Time header on every API response
and uses this endpoint to explain a disagreement (NTP not synchronized, zone).
Read-only: it never changes time configuration.
"""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/system", tags=["System"])


def _yes_no(value: str | None) -> bool | None:
    v = (value or "").strip().lower()
    if v in ("yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    return None


def read_timedatectl(timeout: float = 2.0) -> dict:
    """Parse `timedatectl show`; never raises."""
    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP", "-p", "Timezone"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"source": "unavailable"}
    if proc.returncode != 0:
        return {"source": "unavailable"}
    values = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    if not values:
        return {"source": "unavailable"}
    ntp = _yes_no(values.get("NTP"))
    return {
        "source": "timedatectl",
        "timezone": values.get("Timezone") or None,
        "ntp_synchronized": _yes_no(values.get("NTPSynchronized")),
        "ntp_service": None if ntp is None else ("active" if ntp else "inactive"),
    }


def _local_zone() -> str | None:
    try:
        return datetime.now().astimezone().tzname()
    except Exception:
        return None


@router.get("/time")
async def system_time(current_user: User = Depends(get_current_user)):
    info = await asyncio.to_thread(read_timedatectl)
    now = datetime.now(timezone.utc)
    return {
        "server_time": now.isoformat(),
        "server_time_ms": int(now.timestamp() * 1000),
        "timezone": info.get("timezone") or _local_zone(),
        "ntp_synchronized": info.get("ntp_synchronized"),
        "ntp_service": info.get("ntp_service"),
        "source": info.get("source", "unavailable"),
    }
