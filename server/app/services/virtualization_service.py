"""Controller-side scheduled polling; row locks prevent duplicate Uvicorn workers."""
import asyncio
import logging
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.crypto import decrypt_config
from app.core.database import AsyncSessionLocal, get_ch_client
from app.models.virtualization import VirtualizationConnector
from app.services.virtualization.common import ConnectionConfig, ConnectorError, Snapshot, validate_target
from app.services.virtualization.nutanix import PrismCollector

logger = logging.getLogger(__name__)


async def collect_sdk(config):
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.services.virtualization.worker",
        cwd=str(Path(__file__).resolve().parents[2]), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await process.communicate(json.dumps(config.private_dict()).encode())
        if process.returncode:
            raise ConnectorError("SDK collector exited unexpectedly")
        result = json.loads(stdout)
        if "error" in result:
            raise ConnectorError(result["error"])
        return Snapshot(**result["snapshot"])
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def collect(config, timeout=300):
    async def run():
        await validate_target(config.endpoint)
        if config.provider.startswith("nutanix_"):
            return await PrismCollector(config).collect()
        return await collect_sdk(config)
    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except ConnectorError:
        raise
    except TimeoutError:
        raise ConnectorError("Collection exceeded its time budget; reduce the credential's entity scope or check endpoint latency") from None
    except Exception:
        # Never include SDK errors or provider bodies: they may contain credentials.
        raise ConnectorError("Collection failed; verify the endpoint API, permissions and collector dependencies") from None


def write_metrics(connector_id, snapshot, timestamp):
    rows = [[str(connector_id), r["id"], timestamp, key, value]
            for r in snapshot.resources for key, value in r["metrics"].items()]
    if rows:
        get_ch_client().insert("virtualization_metrics", rows,
            column_names=["connector_id", "resource_id", "timestamp", "metric", "value"])


async def poll_locked(db, row):
    row.last_attempt_at = datetime.now(timezone.utc)
    try:
        config = ConnectionConfig(**decrypt_config(row.config))
        result = await collect(config)
        sampled_at = datetime.now(timezone.utc)
        try:
            await asyncio.to_thread(write_metrics, row.id, result, sampled_at)
        except Exception:
            result.warnings.append("Metric history could not be written; current discovery is available")
        row.snapshot = dict(resources=result.resources, alerts=result.alerts)
        row.summary = result.summary()
        row.last_success_at = sampled_at
        row.last_error = None
        row.status = "partial" if result.warnings else "ok"
    except Exception as exc:
        row.status = "error"
        row.last_error = str(exc) if isinstance(exc, ConnectorError) else "Unable to load the encrypted monitoring configuration"
        # Preserve last successful snapshot and timestamp; clients mark it stale.
    row.next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=row.interval_seconds)


async def polling_loop():
    await asyncio.sleep(15)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                row = (await db.execute(select(VirtualizationConnector).where(
                    VirtualizationConnector.enabled.is_(True),
                    VirtualizationConnector.next_poll_at <= datetime.now(timezone.utc),
                ).order_by(VirtualizationConnector.next_poll_at).limit(1)
                    .with_for_update(skip_locked=True))).scalar_one_or_none()
                if row:
                    await poll_locked(db, row)
                    await db.commit()
            await asyncio.sleep(1 if row else 10)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Virtualization polling tick failed; retrying (details suppressed)")
            await asyncio.sleep(30)
