"""Durable map webhook delivery for confirmed device transitions."""
import asyncio
import hashlib
import json
import logging
import os

from sqlalchemy import text

from app.services.notification_transport import NotificationHTTPClient

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("DEVICE_TRANSITION_INBOX_ENABLED") == "1"


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


async def enqueue(db, event, observed_at, is_recovery: bool):
    rows = (await db.execute(text("""
        SELECT DISTINCT m.id,m.name,m.webhook_url
        FROM manual_maps m JOIN manual_map_nodes n ON n.map_id=m.id
        WHERE n.device_id=:id AND m.webhook_enabled AND m.webhook_url<>''
        ORDER BY m.id
    """), {"id": event.device_id})).all()
    for row in rows:
        payload = {
            "event": "device_status_change", "map_id": str(row.id), "map_name": row.name,
            "device_id": event.device_id, "hostname": event.hostname, "ip_address": event.ip_address,
            "old_status": event.old_status, "new_status": event.new_status, "is_recovery": is_recovery,
            "is_flapping": event.is_flapping, "timestamp": observed_at.isoformat(),
        }
        await db.execute(text("""
            INSERT INTO device_map_webhook_work(map_id,payload,destination_sha256)
            VALUES(:map_id,CAST(:payload AS jsonb),:digest)
        """), {"map_id": row.id, "payload": json.dumps(payload), "digest": _fingerprint(row.webhook_url)})


async def deliver_one(session_factory, permitted):
    async with session_factory() as db:
        row = (await db.execute(text("""
            UPDATE device_map_webhook_work SET status='sending',claimed_at=clock_timestamp()
            WHERE id=(
                SELECT q.id FROM device_map_webhook_work q WHERE q.status='pending'
                  AND NOT EXISTS (SELECT 1 FROM device_map_webhook_work earlier
                      WHERE earlier.map_id=q.map_id AND earlier.id<q.id
                        AND earlier.status IN ('pending','sending'))
                ORDER BY q.id FOR UPDATE OF q SKIP LOCKED LIMIT 1
            )
            RETURNING id,map_id,delivery_id,payload,destination_sha256
        """))).mappings().first()
        await db.commit()
        if row is None:
            return False
        url = (await db.execute(text("""
            SELECT webhook_url FROM manual_maps WHERE id=:id AND webhook_enabled
        """), {"id": row["map_id"]})).scalar_one_or_none()
        await db.rollback()

    status, code = "cancelled", None
    if url and _fingerprint(url) == row["destination_sha256"]:
        if not permitted():
            # A later owner marks a stranded claim uncertain. Fencing must
            # stop this owner before another can touch its delivery state.
            return False
        try:
            async with asyncio.timeout(20):
                async with NotificationHTTPClient(timeout=10) as client:
                    reply = await client.post(url, json=row["payload"],
                                              headers={"Idempotency-Key": str(row["delivery_id"])})
                    code = reply.status_code
                    status = "accepted" if 200 <= code < 300 else ("uncertain" if code >= 500 else "failed")
        except Exception:
            # The peer may have processed the request before the connection
            # failed. Do not resend an unknown external outcome automatically.
            status = "uncertain"
    async with session_factory() as db:
        await db.execute(text("""
            UPDATE device_map_webhook_work SET status=:status,response_status=:code,finished_at=clock_timestamp()
            WHERE id=:id AND status='sending'
        """), {"id": row["id"], "status": status, "code": code})
        await db.commit()
    return True


async def device_map_webhook_loop():
    from app.core.controller_admission import permitted
    from app.core.database import AsyncSessionLocal

    while True:
        try:
            if permitted():
                async with AsyncSessionLocal() as db:
                    await db.execute(text("""
                        UPDATE device_map_webhook_work SET status='uncertain',finished_at=clock_timestamp()
                        WHERE status='sending' AND claimed_at<clock_timestamp()-interval '2 minutes'
                    """))
                    await db.execute(text("""
                        DELETE FROM device_map_webhook_work WHERE id IN (
                            SELECT id FROM device_map_webhook_work
                            WHERE status IN ('accepted','failed','cancelled')
                              AND finished_at<clock_timestamp()-interval '7 days'
                            ORDER BY finished_at LIMIT 1000)
                    """))
                    await db.commit()
                for _ in range(32):
                    if not permitted() or not await deliver_one(AsyncSessionLocal, permitted):
                        break
        except Exception as exc:
            logger.warning("Device map webhook work deferred (%s)", type(exc).__name__)
        await asyncio.sleep(2)
