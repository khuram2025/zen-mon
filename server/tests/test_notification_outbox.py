"""Real PostgreSQL crash, ownership and transaction tests; providers are fake."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_alert_service as has, notification_outbox as queue
from app.services import alert_delivery_log as dl, notification_grouping as ng
from app.api.v1 import alert_engine
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_alert_delivery_keys import world, ctx  # noqa: F401

REAL_SEND_EMAIL = alert_engine._send_email


@pytest.mark.asyncio
async def test_queue_detail_and_retry_http_permissions(world):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.v1 import alerts as api
    from app.core.database import get_db
    from app.core.security import get_current_user
    w, aid = world, world.alert()
    user = SimpleNamespace(id=None, username="lab-reader", role="viewer", scope_tags=[])
    w.db.execute("INSERT INTO roles(name,display_name,permissions) VALUES('outbox-test-manager','Lab manager',$1::jsonb)",
                 ["alerts.view", "alerts.manage"])
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        wid = w.db.scalar("SELECT id FROM notification_outbox")
        app = FastAPI()
        app.include_router(api.router)

        async def database():
            async with sf() as db:
                db.answers_role_lookup = True  # opt out of the suite's fake-DB role shim
                yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: user
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lab.test") as client:
            detail = await client.get(f"/alerts/{aid}")
            assert detail.status_code == 200, detail.text
            work = detail.json()["notification_work"][0]
            assert work["status"] == "pending"
            assert set(work) == {"id", "event", "status", "attempts", "created_at", "due_at", "finished_at", "last_error"}
            path = f"/alerts/{aid}/notification-work/{wid}/retry"
            assert (await client.post(path)).status_code == 403
            user.role = "outbox-test-manager"
            user.scope_tags = ["unrelated-site"]
            assert (await client.get(f"/alerts/{aid}")).status_code == 404
            assert (await client.post(path)).status_code == 404
            user.scope_tags = []
            assert (await client.post(path)).status_code == 200


@asynccontextmanager
async def factory(w, role="zen"):
    engine = create_async_engine(w.db.url_for(role), poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def enqueue(w, sf, aid, event="trigger", channels=None, **extra):
    async with sf() as db:
        n = await has.dispatch_to_channels(db, channels if channels is not None else [w.ch.noc, w.ch.hook],
            ctx(aid, event, _enqueue_notification=True, **extra))
        await db.commit()
        return n


def due(w):
    w.db.execute("UPDATE notification_outbox SET due_at=clock_timestamp()-interval '1 second'")


@pytest.mark.asyncio
async def test_application_role_can_enqueue_and_deliver_with_migration_grants(world):
    w, aid = world, world.alert()
    # The fixture's legacy tables belong to its isolated migration owner.
    # Model their installed application grants, but do NOT grant the new
    # outbox or sequence here: migration 171 must provide those privileges.
    w.db.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT tablename FROM pg_tables
                 WHERE schemaname='public' AND tablename<>'notification_outbox' LOOP
            EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE %I TO zenplus',r.tablename);
        END LOOP;
    END $$""")
    async with factory(w, role="zenplus") as sf:
        await enqueue(w, sf, aid)
        await queue.deliver_due(sf)
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_enqueue_is_transactional_and_restart_delivers_once(world):
    w = world
    aid = w.alert()
    async with factory(w) as sf:
        async with sf() as db:
            await has.dispatch_to_channels(db, [w.ch.noc], ctx(aid, _enqueue_notification=True))
            assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 0
            assert w.mails == []
            await db.rollback()
        assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 0
        await enqueue(w, sf, aid)
    # A new engine/session factory represents a restarted worker.
    async with factory(w) as sf:
        assert await queue.deliver_due(sf) == 1
        assert await queue.deliver_due(sf) == 0
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]
    assert len(w.hooks) == 1
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_duplicate_enqueue_preserves_original_payload(world):
    w, aid = world, world.alert()
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        await enqueue(w, sf, aid, body="changed after first durable event")
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 1
    assert w.db.scalar("SELECT payload->>'body' FROM notification_outbox") == "Memory high."


@pytest.mark.asyncio
async def test_provider_failure_retries_and_recovery_waits_for_trigger(world):
    w, aid = world, world.alert()
    w.behaviour.error = ConnectionRefusedError("unreachable")
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc])
        await enqueue(w, sf, aid, "recovery", channels=[w.ch.noc], is_recovery=True)
        assert await queue.deliver_due(sf) == 1
        assert w.db.query("SELECT event,status,attempts FROM notification_outbox ORDER BY sequence") == [
            ("trigger", "pending", 1), ("recovery", "pending", 0)]
        w.behaviour.error = None
        due(w)
        assert await queue.deliver_due(sf) == 2
    assert w.db.query("SELECT status FROM notification_outbox ORDER BY sequence") == [("accepted",), ("accepted",)]
    assert len(w.mails) == 4


@pytest.mark.asyncio
async def test_uncertain_provider_outcome_is_visible_and_not_resent(world):
    w, aid = world, world.alert()
    w.behaviour.error = TimeoutError("may have been accepted")
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc])
        await queue.deliver_due(sf)
        assert w.db.scalar("SELECT status FROM notification_outbox") == "uncertain"
        w.behaviour.error = None
        due(w)
        assert await queue.deliver_due(sf) == 0
    assert w.mails == []


@pytest.mark.asyncio
async def test_recipient_restriction_is_part_of_the_durable_plan(world):
    w, aid = world, world.alert()
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc], only_recipients=["ops@example.test"])
        await queue.deliver_due(sf)
    assert w.mails == ["ops@example.test"]
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_filtered_sms_route_never_sends_the_full_configured_list(world, monkeypatch):
    from app.api.v1 import alert_engine
    w, aid = world, world.alert()
    w.db.execute("INSERT INTO notification_gateways(name,type,config,is_default,enabled)"
                 " VALUES('SMS','sms','{\"url\":\"https://sms.example.invalid\"}'::jsonb,true,true)")
    cid = str(w.db.scalar("INSERT INTO notification_channels(name,type,config)"
                          " VALUES('SMS','sms',$1::jsonb) RETURNING id",
                          {"phone_numbers": "+15550000001,+15550000002"}))
    received = []

    async def send(gw, recipients, message, **kwargs):
        received.append(recipients)
        return True

    monkeypatch.setattr(alert_engine, "_send_sms", send)
    async with factory(w) as sf:
        # Model a budget/grouping decision that holds the second recipient.
        await enqueue(w, sf, aid, channels=[cid])
        w.db.execute("UPDATE notification_outbox SET routes=jsonb_set(routes,'{0,recipients}',$1::jsonb)",
                     ["+15550000001"])
        await queue.deliver_due(sf)
    assert received == ["+15550000001"]
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_provider_acceptance_followed_by_receipt_error_is_uncertain(world, monkeypatch):
    w, aid = world, world.alert()
    finish = dl.finish_keys
    interrupted = False

    async def fail_once(db, tokens, state, error=None, **kwargs):
        nonlocal interrupted
        if state == "accepted" and not interrupted:
            interrupted = True
            raise RuntimeError("receipt write interrupted after provider acceptance")
        await finish(db, tokens, state, error, **kwargs)

    monkeypatch.setattr(dl, "finish_keys", fail_once)
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc])
        await queue.deliver_due(sf)
        due(w)
        assert await queue.deliver_due(sf) == 0
    assert len(w.mails) == 2
    assert w.db.scalar("SELECT status FROM notification_outbox") == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_network_incident_and_intent_commit_together(world, monkeypatch, fail):
    from app.services import network_alert_service as network
    w = world
    did = str(w.db.scalar("INSERT INTO devices(hostname,ip_address,status)"
                          " VALUES('switch','192.0.2.77','up') RETURNING id"))
    rid = str(w.db.scalar("INSERT INTO alert_rules(name,metric,operator,threshold,severity,notify_channels)"
                          " VALUES('CPU','cpu','>',90,'warning',$1::jsonb) RETURNING id", [w.ch.noc]))
    if fail:
        async def refused(*args):
            raise RuntimeError("intent store unavailable")
        monkeypatch.setattr(queue, "enqueue", refused)
    async with factory(w) as sf:
        async with sf() as db:
            rule = (await db.execute(text("SELECT * FROM alert_rules WHERE id=CAST(:id AS uuid)"), {"id": rid})).first()
            work = network._apply(db, rule, did, "switch", None, None, True, 95, "CPU high", {}, 0, 0)
            if fail:
                with pytest.raises(RuntimeError, match="intent store unavailable"):
                    await work
                await db.rollback()
            else:
                await work
    assert w.db.scalar("SELECT count(*) FROM alerts") == (0 if fail else 1)
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == (0 if fail else 1)
    assert w.mails == []


@pytest.mark.asyncio
async def test_retry_action_obeys_scope_state_and_required_audit(world, monkeypatch):
    from uuid import UUID
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.api.v1 import alerts as api
    from app.services import audit_service
    w, aid = world, world.alert()
    user = SimpleNamespace(id=None, username="outbox-test", role="admin", scope_tags=[])
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        wid = w.db.scalar("SELECT id FROM notification_outbox")
        async with sf() as db:
            result = await api.retry_notification_work(UUID(aid), wid, db, user)
            assert result["scheduled"] is True
        assert w.db.scalar("SELECT count(*) FROM audit_logs WHERE action='alert.notification_work.retry'") == 1
        w.db.execute("UPDATE notification_outbox SET due_at=clock_timestamp()+interval '1 hour'")
        before = w.db.scalar("SELECT due_at FROM notification_outbox")

        async def no_audit(*args, **kwargs):
            assert kwargs["required"] is True
            raise audit_service.AuditUnavailable()

        with monkeypatch.context() as patch:
            patch.setattr(audit_service, "write_audit_log", no_audit)
            async with sf() as db:
                with pytest.raises(audit_service.AuditUnavailable):
                    await api.retry_notification_work(UUID(aid), wid, db, user)
                await db.rollback()
        assert w.db.scalar("SELECT due_at FROM notification_outbox") == before
        limited = SimpleNamespace(id=None, username="limited", role="viewer", scope_tags=["different-site"])
        async with sf() as db:
            with pytest.raises(HTTPException) as denied:
                await api.retry_notification_work(UUID(aid), wid, db, limited)
            assert denied.value.status_code == 404
        w.db.execute("UPDATE notification_outbox SET status='uncertain'")
        async with sf() as db:
            with pytest.raises(HTTPException) as conflict:
                await api.retry_notification_work(UUID(aid), wid, db, user)
            assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_two_workers_claim_one_dispatch(world):
    w, aid = world, world.alert()
    w.behaviour.delay = .2
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        results = await asyncio.gather(queue.deliver_due(sf), queue.deliver_due(sf))
    assert sum(results) == 1
    assert len(w.mails) == 2 and len(w.hooks) == 1


@pytest.mark.asyncio
async def test_stale_owner_cannot_finish_or_send_after_takeover(world):
    w, aid = world, world.alert()
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        async with sf() as db:
            old = await queue.claim(db)
        w.db.execute("UPDATE notification_outbox SET lease_expires_at=clock_timestamp()-interval '1 second'")
        async with sf() as db:
            new = await queue.claim(db)
            assert old.claim_token != new.claim_token
            await queue.finish(db, old, "accepted")
            with pytest.raises(dl.DeliveryNotSent):
                await queue.owned(db, old, renew=True)
        with pytest.raises(dl.DeliveryNotSent):
            await queue.execute(old, sf)
        assert w.mails == [] and w.hooks == []
        await queue.execute(new, sf)
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_accepted_keys_survive_crash_before_queue_completion(world, monkeypatch):
    w, aid = world, world.alert()
    finish = queue.finish
    crashed = False

    async def crash_once(db, job, status, error=None):
        nonlocal crashed
        if status == "accepted" and not crashed:
            crashed = True
            raise RuntimeError("lost queue completion")
        await finish(db, job, status, error)

    monkeypatch.setattr(queue, "finish", crash_once)
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        await queue.deliver_due(sf)
        assert w.db.scalar("SELECT status FROM notification_outbox") == "pending"
        due(w)
        await queue.deliver_due(sf)
    assert crashed and len(w.mails) == 2 and len(w.hooks) == 1
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_changed_channel_cannot_redirect_stored_notification(world):
    w, aid = world, world.alert()
    original = w.db.scalar("SELECT config FROM notification_channels WHERE id=$1", w.ch.noc)
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc])
        w.db.execute("UPDATE notification_channels SET config=$2::jsonb WHERE id=$1",
                     w.ch.noc, {"recipients": "different@example.test"})
        await queue.deliver_due(sf)
        assert w.db.scalar("SELECT status FROM notification_outbox") == "blocked"
        assert w.mails == []
        w.db.execute("UPDATE notification_channels SET config=$2::jsonb WHERE id=$1", w.ch.noc, original)
        due(w)
        await queue.deliver_due(sf)
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]


@pytest.mark.asyncio
async def test_key_storage_failure_never_sends_unkeyed(world, monkeypatch):
    w, aid = world, world.alert()

    async def unavailable(*args, **kwargs):
        raise RuntimeError("key storage unavailable")

    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        monkeypatch.setattr(dl, "claim_key", unavailable)
        await queue.deliver_due(sf)
    assert w.mails == [] and w.hooks == []
    assert w.db.scalar("SELECT status FROM notification_outbox") == "pending"


@pytest.mark.asyncio
async def test_grouping_and_incident_updates_rollback_together(world):
    w, aid = world, world.alert()
    ng.reset_for_tests()
    async with factory(w) as sf:
        async with sf() as db:
            await ng.save_settings(db, {"enabled": True})
            await db.commit()
        async with sf() as db:
            await db.execute(text("UPDATE alerts SET message='transaction change' WHERE id=CAST(:id AS uuid)"), {"id": aid})
            await has.dispatch_to_channels(db, [w.ch.noc, w.ch.hook],
                ctx(aid, _enqueue_notification=True, groupable=True, priority="P3"))
            assert w.db.scalar("SELECT count(*) FROM notification_digest_items") == 0
            assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 0
            await db.rollback()
        assert w.db.scalar("SELECT message FROM alerts WHERE id=$1", aid) == "Memory high"
        await enqueue(w, sf, aid, groupable=True, priority="P3")
        assert w.db.scalar("SELECT status FROM notification_outbox") == "held"
        assert w.db.scalar("SELECT count(*) FROM notification_digests") == 2
        assert await queue.deliver_due(sf) == 0
    assert w.mails == [] and w.hooks == []


@pytest.mark.asyncio
async def test_direct_recipients_and_alternate_channels_keep_deduplication(world):
    w, aid = world, world.alert()
    async with factory(w) as sf:
        await enqueue(w, sf, aid, channels=[w.ch.noc, w.ch.mgmt], recipients=[
            {"kind": "email", "address": "ops@example.test"},
            {"kind": "email", "address": "person@example.test"}])
        await queue.deliver_due(sf)
    assert sorted(w.mails) == ["mgr@example.test", "noc@example.test", "ops@example.test", "person@example.test"]
    assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"


@pytest.mark.asyncio
async def test_deleted_incident_cancels_pending_work(world):
    w, aid = world, world.alert()
    async with factory(w) as sf:
        await enqueue(w, sf, aid)
        w.db.execute("DELETE FROM alerts WHERE id=$1", aid)
        await queue.deliver_due(sf)
    assert w.db.scalar("SELECT status FROM notification_outbox") == "cancelled"
    assert w.mails == [] and w.hooks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_host_incident_and_intent_are_one_transaction(world, monkeypatch, fail):
    w = world
    sid = str(w.db.scalar("SELECT id FROM servers LIMIT 1"))
    rid = str(w.db.scalar("""INSERT INTO alert_rules(name,metric,operator,threshold,notify_channels)
        VALUES('Memory high','host_memory_pct','gt',90,$1::jsonb) RETURNING id""", [w.ch.noc]))
    if fail:
        async def refused(*args):
            raise RuntimeError("intent store unavailable")
        monkeypatch.setattr(queue, "enqueue", refused)
    async with factory(w) as sf:
        async with sf() as db:
            rule = (await db.execute(text("SELECT * FROM alert_rules WHERE id=CAST(:id AS uuid)"), {"id": rid})).first()
            work = has._apply_state(db, rule, sid, "srv", None, True, 95, "Memory high",
                                   allowed=True, counts={"raised": 0, "resolved": 0}, opened=None)
            if fail:
                with pytest.raises(RuntimeError, match="intent store unavailable"):
                    await work
                await db.rollback()
            else:
                await work
    assert w.db.scalar("SELECT count(*) FROM alerts") == (0 if fail else 1)
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == (0 if fail else 1)
    assert w.mails == []


@pytest.mark.asyncio
async def test_real_smtp_partial_rejection_and_retry_keep_recipient_receipts(world, monkeypatch):
    """Loopback SMTP only: exercise the actual transport, including its thread."""
    import socketserver
    import threading
    import time
    w, aid = world, world.alert()
    received, renewals = [], []

    class SMTP(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            self.wfile.write(b"220 isolated test relay\r\n")
            recipients = []
            while line := self.rfile.readline():
                command = line.decode().strip()
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    answer = b"250 isolated.test\r\n"
                elif verb == "MAIL":
                    recipients = []
                    answer = b"250 OK\r\n"
                elif verb == "RCPT":
                    recipient = command.split("<", 1)[1].split(">", 1)[0].lower()
                    if recipient == self.server.reject:
                        answer = b"550 recipient refused\r\n"
                    else:
                        recipients.append(recipient)
                        answer = b"250 OK\r\n"
                elif verb == "DATA":
                    self.wfile.write(b"354 send message\r\n")
                    while self.rfile.readline() not in (b".\r\n", b""):
                        pass
                    # Ownership renewal must run while the real SMTP client waits.
                    time.sleep(.15)
                    received.extend(recipients)
                    answer = b"250 accepted\r\n"
                elif verb == "QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    break
                else:
                    answer = b"250 OK\r\n"
                self.wfile.write(answer)

    owned = queue.owned

    async def track_renewal(db, job, *, renew=False):
        if renew:
            renewals.append(job.id)
        await owned(db, job, renew=renew)

    monkeypatch.setattr(alert_engine, "_send_email", REAL_SEND_EMAIL)
    monkeypatch.setattr(queue, "HEARTBEAT_SECONDS", .02)
    monkeypatch.setattr(queue, "owned", track_renewal)
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTP) as relay:
        relay.daemon_threads = True
        relay.reject = "noc@example.test"
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        w.db.execute("UPDATE notification_gateways SET config=$1::jsonb WHERE type='smtp'",
                     {"host": "127.0.0.1", "port": relay.server_address[1], "encryption": "none",
                      "from_email": "lab@example.test"})
        try:
            async with factory(w) as sf:
                await enqueue(w, sf, aid, channels=[w.ch.noc])
                await queue.deliver_due(sf)
                assert w.db.query("SELECT recipient,state FROM alert_delivery_keys ORDER BY recipient") == [
                    ("noc@example.test", "failed"), ("ops@example.test", "accepted")]
                assert w.db.scalar("SELECT status FROM notification_outbox") == "pending"
                relay.reject = None
                due(w)
                await queue.deliver_due(sf)
                assert w.db.scalar("SELECT status FROM notification_outbox") == "accepted"
            assert sorted(received) == ["noc@example.test", "ops@example.test"]
            assert renewals
        finally:
            await asyncio.to_thread(relay.shutdown)
            thread.join(timeout=2)
