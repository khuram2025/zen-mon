"""Transactional notification work for migrated incident producers.

Enqueue never commits or contacts providers. The incident owner commits it.
Dispatch reuses the shared recipient/gateway transports with strict delivery
keys. A queue claim is not provider acceptance. External delivery is not exactly
once: an interrupted provider request may be retried after the legacy key lease.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.crypto import decrypt_config
from app.services import alert_delivery_log as dl

logger = logging.getLogger("zenplus.notification_outbox")
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
MAX_PAYLOAD_BYTES = 256 * 1024
ACTIVE_STATES = ("pending", "processing", "blocked")


def route_fingerprint(channel) -> str:
    # Store only a digest. Re-encryption with a new nonce must not change the
    # identity of an otherwise identical destination configuration.
    raw = json.dumps([channel.type, decrypt_config(channel.config), str(channel.gateway_id or "")],
                     sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def enqueue(db, channel_ids: list, ctx: dict) -> int:
    """Return the number of queued/held routes, not accepted deliveries.

One immutable dispatch envelope per incident event. Locking the incident orders
trigger/recovery envelopes and makes duplicate evaluation return the first plan.
Grouping membership, the envelope and incident changes share the caller's commit.
"""
    from app.services import host_alert_service as has, notification_grouping as ng

    if not channel_ids and not any(has._direct_targets(ctx).values()):
        return 0
    aid = str(UUID(str(ctx.get("alert_id"))))
    event = str(ctx.get("delivery_event") or "").strip()
    if not event or len(event) > 64:
        raise ValueError("A durable notification needs an event of 1 to 64 characters")
    channels = has._unique_channels(channel_ids)
    if len(channels) > 128:
        raise ValueError("Too many notification channels")
    payload = {k: v for k, v in ctx.items() if not k.startswith("_")}
    if len(json.dumps(payload, default=str).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Notification payload exceeds 256 KiB")
    if not (await db.execute(text("SELECT id FROM alerts WHERE id=CAST(:id AS uuid) FOR UPDATE"),
                             {"id": aid})).first():
        raise ValueError("Notification incident does not exist")
    existing = (await db.execute(text(
        "SELECT routes,status FROM notification_outbox WHERE alert_id=CAST(:id AS uuid) AND event=:event"),
        {"id": aid, "event": event})).first()
    if existing:
        return max(1, len(existing.routes)) if existing.status != "cancelled" else 0

    prior = await dl.delivered_recipients(db, aid, event, strict=True)
    gate = await ng.gate_for(db, payload, (aid, event), strict=True)
    routes, direct, held_keys, queued_keys = [], [], set(), set()
    held_count = 0
    raw_only = payload.get("only_recipients")
    only = None if raw_only is None else {str(k) for k in raw_only}

    async def plan(cid, kind, name, targets, *, direct_kind=None, fingerprint=None):
        nonlocal held_count
        targets = [(k, a) for k, a in targets if k not in held_keys
                   and (only is None or not k or k in only)]
        # Preserve alternate channels for a queued recipient. Only its first
        # route decides grouping; an alternate must not also create a digest.
        fresh = [(k, a) for k, a in targets if k not in queued_keys and k not in prior]
        retained = [(k, a) for k, a in targets if k in queued_keys or k in prior]
        if gate and fresh:
            fresh, held = await gate.route(db, channel_id=cid, channel_name=name,
                channel_type=kind, targets=fresh, direct_kind=direct_kind,
                commit=False, strict=True)
            for item in held:
                held_keys.update(k for k in item.keys if k)
                held_count += 1
                await dl.record(db, alert_id=aid, channel_id=cid, channel_name=name,
                                channel_type=kind, kind=dl.kind_from_ctx(payload),
                                outcome=item.log_outcome, error=item.log_text)
        selected = retained + fresh
        if selected:
            routes.append({"channel_id": cid, "kind": kind, "fingerprint": fingerprint,
                           "recipients": [k for k, _ in selected]})
            # Empty webhook recipient keys are channel-local, not global.
            queued_keys.update(k for k, _ in selected if k)
        return selected

    for cid in channels:
        UUID(cid)
        channel = (await db.execute(text(
            "SELECT name,type,config,gateway_id,enabled FROM notification_channels WHERE id=CAST(:id AS uuid)"),
            {"id": cid})).first()
        if channel is None:
            routes.append({"channel_id": cid, "kind": "missing", "fingerprint": None, "recipients": [""]})
            continue
        cfg = decrypt_config(channel.config)
        targets = (has._recipients(cfg.get("recipients" if channel.type == "email" else "phone_numbers"), channel.type)
                   if channel.type in ("email", "sms") else [("", "")])
        if not targets:
            routes.append({"channel_id": cid, "kind": channel.type,
                           "fingerprint": route_fingerprint(channel), "recipients": [""]})
            continue
        await plan(cid, channel.type, channel.name, targets, fingerprint=route_fingerprint(channel))
    for kind, values in has._direct_targets(payload).items():
        for key, address, entry in values:
            selected = await plan(has.DIRECT_CHANNEL_ID, kind, ng.DIRECT_CHANNEL_NAME,
                                  [(key, address)], direct_kind=kind)
            if selected:
                direct.append(entry)
    payload["recipients"] = direct
    if sum(len(r["recipients"]) for r in routes) > 1024:
        raise ValueError("Too many notification destinations")
    if not routes and not held_count:
        return 0
    await db.execute(text("""INSERT INTO notification_outbox(alert_id,event,payload,routes,status,finished_at)
        VALUES(CAST(:aid AS uuid),:event,CAST(:payload AS jsonb),CAST(:routes AS jsonb),:status,
               CASE WHEN :status='held' THEN clock_timestamp() ELSE NULL END)"""),
        {"aid": aid, "event": event, "payload": json.dumps(payload, default=str),
         "routes": json.dumps(routes), "status": "pending" if routes else "held"})
    return len(routes) + held_count


async def claim(db):
    """Claim one due envelope, preserving unresolved order within an incident."""
    token = str(uuid4())
    row = (await db.execute(text("""UPDATE notification_outbox SET status='processing',
        claim_token=CAST(:token AS uuid),lease_expires_at=clock_timestamp()+make_interval(secs=>:lease),
        attempts=attempts+1 WHERE id=(
          SELECT q.id FROM notification_outbox q
          WHERE ((q.status IN ('pending','blocked') AND q.due_at<=clock_timestamp())
             OR (q.status='processing' AND q.lease_expires_at<=clock_timestamp()))
          AND NOT EXISTS (SELECT 1 FROM notification_outbox earlier
              WHERE earlier.alert_id=q.alert_id AND earlier.sequence<q.sequence
                AND earlier.status IN ('pending','processing','blocked'))
          ORDER BY q.due_at,q.sequence LIMIT 1 FOR UPDATE OF q SKIP LOCKED)
        RETURNING id,alert_id,event,payload,routes,attempts,claim_token"""),
        {"token": token, "lease": LEASE_SECONDS})).first()
    await db.commit()
    return row


async def owned(db, job, *, renew=False):
    if renew:
        sql = """UPDATE notification_outbox SET lease_expires_at=clock_timestamp()+make_interval(secs=>:lease)
            WHERE id=:id AND claim_token=:token AND status='processing'
              AND lease_expires_at>clock_timestamp() RETURNING id"""
    else:
        sql = """SELECT id FROM notification_outbox WHERE id=:id AND claim_token=:token
            AND status='processing' AND lease_expires_at>clock_timestamp()"""
    got = (await db.execute(text(sql), {"id": job.id, "token": job.claim_token, "lease": LEASE_SECONDS})).first()
    if not got:
        raise dl.DeliveryNotSent("Notification execution ownership lost")


async def finish(db, job, status, error=None):
    delay = min(900, 15 * 2 ** min(job.attempts, 6))
    # Generic categories only. Provider errors or endpoint/credential values
    # must never enter the queue's operational status.
    await db.execute(text("""UPDATE notification_outbox SET status=:status,last_error=:error,
        claim_token=NULL,lease_expires_at=NULL,due_at=clock_timestamp()+make_interval(secs=>:delay),
        finished_at=CASE WHEN :status IN ('accepted','uncertain','held','cancelled')
            THEN clock_timestamp() ELSE NULL END
        WHERE id=:id AND claim_token=:token AND status='processing' AND lease_expires_at>clock_timestamp()"""),
        {"id": job.id, "token": job.claim_token, "status": status, "error": error, "delay": delay})
    await db.commit()


async def receipt_status(db, job):
    rows = (await db.execute(text("""SELECT channel_id::text,recipient,state FROM alert_delivery_keys
        WHERE alert_id=:aid AND event=:event"""), {"aid": job.alert_id, "event": job.event})).all()
    groups = {}
    for route in job.routes:
        for recipient in route["recipients"]:
            key = recipient or "channel:" + route["channel_id"]
            groups.setdefault(key, set())
    for cid, recipient, state in rows:
        key = recipient or "channel:" + cid
        if key in groups:
            groups[key].add(state)
    outcomes = []
    for states in groups.values():
        outcomes.append("accepted" if "accepted" in states else "uncertain" if "uncertain" in states else "pending")
    if outcomes and all(s == "accepted" for s in outcomes):
        return "accepted"
    if outcomes and all(s in ("accepted", "uncertain") for s in outcomes):
        return "uncertain"
    return "pending"


async def execute(job, session_factory):
    from app.services import host_alert_service as has

    async with session_factory() as db:
        if job.alert_id is None:
            await finish(db, job, "cancelled", "Incident removed")
            return
        snapshot = {r["channel_id"]: r for r in job.routes if r["channel_id"] != has.DIRECT_CHANNEL_ID}
        ctx = dict(job.payload)
        ctx.update(_require_delivery_key=True, _skip_grouping=True,
                   _outbox_route_targets={k: v["recipients"] for k, v in snapshot.items()})
        blocked = False

        async def guard():
            # An independent short transaction does not interfere with the
            # dispatch session's durable key and receipt commits.
            async with session_factory() as check:
                await owned(check, job)

        async def guard_route(cid, channel):
            nonlocal blocked
            if snapshot[cid]["fingerprint"] != route_fingerprint(channel):
                blocked = True
                raise dl.DeliveryNotSent("Notification channel configuration changed")

        await guard()
        await has.dispatch_to_channels(db, list(snapshot), ctx, delivery_guard=guard, route_guard=guard_route)
        status = await receipt_status(db, job)
        if status == "pending" and blocked:
            status = "blocked"
        await finish(db, job, status, "Channel configuration changed" if blocked
                     else "Delivery not yet accepted" if status == "pending" else None)


async def heartbeat(job, session_factory):
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with session_factory() as db:
            await owned(db, job, renew=True)
            await db.commit()


async def deliver_due(session_factory=None, *, limit=20):
    if session_factory is None:
        from app.core.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    processed = 0
    for _ in range(limit):
        async with session_factory() as db:
            job = await claim(db)
        if job is None:
            break
        work = asyncio.create_task(execute(job, session_factory))
        pulse = asyncio.create_task(heartbeat(job, session_factory))
        try:
            done, _ = await asyncio.wait({work, pulse}, return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                await pulse
            await work
            processed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            logger.warning("Notification execution interrupted; durable work retained", exc_info=False)
            async with session_factory() as db:
                await finish(db, job, "pending", "Execution interrupted")
        finally:
            for task in (work, pulse):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, pulse, return_exceptions=True)
    return processed


async def notification_outbox_loop():
    while True:
        try:
            await deliver_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Notification queue unavailable; work retained", exc_info=False)
        await asyncio.sleep(5)
