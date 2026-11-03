#!/usr/bin/python3
"""Record a bounded monitoring interruption before an OTA stops API/poller.

Runs from the verified payload, including when the installed updater is old.
Uses the installed interpreter/dependencies and local configuration privately.
"""
import asyncio
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
root = Path(os.environ.get("ZENPLUS_DIR", "/opt/zenplus"))
python = root / "venv/bin/python"
if sys.prefix != str(root / "venv"):
    os.execv(str(python), [str(python), "-B", __file__])
os.chdir(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


async def main():
    from datetime import datetime, timezone
    from app.core.database import AsyncSessionLocal, engine
    from app.services.controller_recovery import transition
    async with AsyncSessionLocal() as db:
        await transition(db, token="ota-maintenance", now=datetime.now(timezone.utc).timestamp(), planned=True)
    await engine.dispose()
    print("Bounded controller monitoring maintenance recorded")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Never expose a DSN or driver exception containing connection secrets.
        raise SystemExit("Unable to record monitoring maintenance; update stopped before service shutdown")
