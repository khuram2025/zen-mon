"""Durable delivery keys (NOTIFY 03): one send per incident, lifecycle event,
channel and recipient — across workers, retries and restarts.

The field report had two identical interface-error mails seconds apart. Two
API workers evaluated the same incident, and nothing below the evaluator could
tell that the second send repeated the first. dispatch_to_channels now claims
``alert_delivery_keys`` (alert, event, channel, recipient) and commits the
claim before it sends: a delivery already claimed, accepted or left uncertain
is skipped, a definite failure may be retried, and a genuinely new event (a
recovery, another incident) has new keys and goes out.

Runs on a disposable PostgreSQL; only the transports are faked.
"""
from __future__ import annotations

import asyncio
import contextlib
import smtplib
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.services import alert_delivery_log as dl
from app.services import alert_notify_state as ns
from app.services import host_alert_service as has

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def world(pg_db, monkeypatch):
    dl.reset_for_tests()
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM notification_gateways")
    db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
               " VALUES ('mail', 'smtp', $1::jsonb, true, true)",
               {"host": "smtp.invalid", "from_email": "zenplus@example.invalid"})

    def channel(name, kind, config):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config)"
                             " VALUES ($1, $2, $3::jsonb) RETURNING id", name, kind, config))

    ch = NS(
        noc=channel("noc", "email", {"recipients": "ops@example.test, Ops@Example.test, noc@example.test"}),
        mgmt=channel("mgmt", "email", {"recipients": "OPS@example.test; mgr@example.test"}),
        hook=channel("hook", "webhook", {"url": "https://hooks.example.invalid/zenplus"}),
    )
    server = db.scalar("INSERT INTO servers (display_name, hostname) VALUES ('srv', 'srv') RETURNING id")

    def alert():
        return str(db.scalar(
            "INSERT INTO alerts (server_id, severity, message, status, metadata)"
            " VALUES ($1, 'warning', 'Memory high', 'active', '{}'::jsonb) RETURNING id", server))

    mails: list[str] = []
    hooks: list[dict] = []
    behaviour = NS(delay=0.0, error=None)

    async def send_email(gw, recipients, subject, body, html_body=None):
        await asyncio.sleep(behaviour.delay)     # a slow SMTP session widens the race
        if behaviour.error:
            raise behaviour.error
        mails.extend(r.strip().lower() for r in recipients.split(","))
        return True

    async def send_hook(kind, cfg, ctx):
        await asyncio.sleep(behaviour.delay)
        hooks.append(ctx)
        return True, None

    monkeypatch.setattr(alert_engine, "_send_email", send_email)
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    return NS(db=db, ch=ch, alert=alert, mails=mails, hooks=hooks, behaviour=behaviour)


def ctx(alert_id=None, event="trigger", **extra):
    return {"subject": "[WARNING] ALERT: Memory high — srv", "body": "Memory high.",
            "message": "ZenPlus WARNING — Memory high", "rule_name": "Memory high",
            "hostname": "srv", "severity": "warning", "status": "ALERT",
            "alert_id": alert_id, "delivery_event": event if alert_id else None, **extra}


async def dispatch(w, channels, c):
    async with session(w.db) as s:
        return await has.dispatch_to_channels(s, channels, c)


def keys(w, alert_id):
    return w.db.query("SELECT event, channel_id::text, recipient, state, error"
                      " FROM alert_delivery_keys WHERE alert_id = $1 ORDER BY event, recipient",
                      alert_id)


@pytest.mark.asyncio
async def test_two_concurrent_dispatches_of_one_event_send_once(world):
    w = world
    w.behaviour.delay = 0.3
    alert_id = w.alert()
    channels = [w.ch.noc, w.ch.mgmt, w.ch.hook]
    sent = await asyncio.wait_for(asyncio.gather(
        dispatch(w, channels, ctx(alert_id)), dispatch(w, channels, ctx(alert_id))), 20)
    # Every recipient got the trigger once, the webhook fired once.
    assert sorted(w.mails) == ["mgr@example.test", "noc@example.test", "ops@example.test"]
    assert len(w.hooks) == 1
    assert {state for *_, state, _ in keys(w, alert_id)} == {"accepted"}
    # Both callers learn the trigger went out (the loser counts the winner's
    # deliveries), so neither leaves it pending for a late re-send.
    assert all(n >= 1 for n in sent)
    # The skipped duplicates are auditable in the delivery log.
    skipped = w.db.query("SELECT error FROM alert_deliveries WHERE alert_id = $1 AND outcome = 'skipped'",
                         alert_id)
    assert skipped and all(e.startswith("duplicate:") for (e,) in skipped)


@pytest.mark.asyncio
async def test_a_retry_or_restart_does_not_resend_an_accepted_event(world):
    w = world
    alert_id = w.alert()
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1   # counted, not sent
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]


@pytest.mark.asyncio
async def test_channels_and_recipients_are_deduplicated_within_one_dispatch(world):
    w = world
    alert_id = w.alert()
    # The rule lists noc twice; ops@ is on both email channels (any case).
    assert await dispatch(w, [w.ch.noc, w.ch.noc, w.ch.mgmt], ctx(alert_id)) == 2
    assert sorted(w.mails) == ["mgr@example.test", "noc@example.test", "ops@example.test"]
    rows = keys(w, alert_id)
    assert [(r[1], r[2]) for r in rows] == [
        (w.ch.mgmt, "mgr@example.test"), (w.ch.noc, "noc@example.test"), (w.ch.noc, "ops@example.test")]
    # Unkeyed dispatches dedupe too.
    w.mails.clear()
    await dispatch(w, [w.ch.noc, w.ch.mgmt, w.ch.noc], ctx())
    assert sorted(w.mails) == ["mgr@example.test", "noc@example.test", "ops@example.test"]


@pytest.mark.asyncio
async def test_an_uncertain_send_is_never_resent(world):
    w = world
    alert_id = w.alert()
    # The server dropped the connection after DATA: it may have accepted it.
    w.behaviour.error = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 0
    assert {r[3] for r in keys(w, alert_id)} == {"uncertain"}
    w.behaviour.error = None
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1   # possibly delivered: counted
    assert w.mails == []
    [(outcome, error)] = w.db.query("SELECT outcome, error FROM alert_deliveries"
                                    " WHERE alert_id = $1 AND outcome <> 'skipped'", alert_id)
    assert outcome == "failed" and error.startswith("uncertain:")


@pytest.mark.asyncio
async def test_a_definite_failure_may_be_retried(world):
    w = world
    alert_id = w.alert()
    w.behaviour.error = ConnectionRefusedError("connection refused")
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 0
    assert {r[3] for r in keys(w, alert_id)} == {"failed"}
    w.behaviour.error = None
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]
    assert {r[3] for r in keys(w, alert_id)} == {"accepted"}


@pytest.mark.asyncio
async def test_a_genuine_new_event_is_sent(world):
    w = world
    first = w.alert()
    await dispatch(w, [w.ch.noc, w.ch.hook], ctx(first))
    await dispatch(w, [w.ch.noc, w.ch.hook], ctx(first, "recovery", is_recovery=True, resolved=True))
    await dispatch(w, [w.ch.noc, w.ch.hook], ctx(w.alert()))     # another incident
    assert len(w.mails) == 6 and len(w.hooks) == 3


@pytest.mark.asyncio
async def test_without_an_event_key_dispatch_behaves_as_before(world):
    w = world
    alert_id = w.alert()
    await dispatch(w, [w.ch.noc], {**ctx(alert_id), "delivery_event": None})
    await dispatch(w, [w.ch.noc], {**ctx(alert_id), "delivery_event": None})
    assert len(w.mails) == 4 and keys(w, alert_id) == []


@pytest.mark.asyncio
async def test_a_missing_incident_row_falls_back_to_an_unkeyed_send(world):
    w = world
    gone = "00000000-0000-0000-0000-0000000000aa"
    assert await dispatch(w, [w.ch.noc], ctx(gone)) == 1
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]


@pytest.mark.asyncio
async def test_only_one_worker_claims_a_pending_late_trigger(world):
    """The network late-trigger race: a trigger suppressed by quiet hours
    ("notified": false) is claimed false -> sending by exactly one pass."""
    w = world
    alert_id = w.alert()
    w.db.execute("UPDATE alerts SET metadata = '{\"notified\": false}'::jsonb WHERE id = $1", alert_id)

    async def claim():
        async with session(w.db) as s:
            got = await ns.claim_pending(s, alert_id)
            await asyncio.sleep(0.2)
            await s.commit()
            return got

    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]
    async with session(w.db) as s:
        assert await ns.is_pending(s, alert_id) is False      # in flight
        assert await ns.was_notified(s, alert_id) is False    # ... and not yet confirmed
        await ns.stamp(s, alert_id, True)
        await s.commit()
        assert await ns.was_notified(s, alert_id) is True


@pytest.mark.asyncio
async def test_concurrent_escalation_sweeps_page_a_level_once(world, monkeypatch):
    from app.services import alert_escalation_service as esc

    w = world
    rule = str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, notify_channels, escalation_levels)"
        " VALUES ('Memory high', 'host_memory_pct', 'gt', 90, '[]'::jsonb, $1::jsonb) RETURNING id",
        [{"after_minutes": 1, "notify_channels": [w.ch.mgmt]}]))
    alert_id = w.alert()
    w.db.execute("UPDATE alerts SET rule_id = $2, triggered_at = now() - interval '10 minutes',"
                 " metadata = '{\"notified\": true}'::jsonb WHERE id = $1", alert_id, rule)
    pages: list[dict] = []

    async def page(db, channels, c):
        await asyncio.sleep(0.2)
        pages.append(c)
        return 1

    monkeypatch.setattr(has, "dispatch_to_channels", page)

    async def sweep():
        async with session(w.db) as s:
            return await esc.sweep_escalations(s)

    results = await asyncio.wait_for(asyncio.gather(sweep(), sweep()), 20)
    assert sum(r["escalated"] for r in results) == 1 and len(pages) == 1
    assert pages[0]["delivery_event"] == "escalation:L1:r0"
    assert pages[0]["lifecycle"] == "escalated" and pages[0]["incident_ref"].startswith("INC-")
    events = w.db.scalar("SELECT count(*) FROM alert_events WHERE alert_id = $1 AND kind = 'escalated'",
                         alert_id)
    assert events == 1


def test_recipient_keys_and_transport_outcomes():
    assert has._recipients("Ops@X.test, ops@x.test; noc@x.test,,", "email") == [
        ("ops@x.test", "Ops@X.test"), ("noc@x.test", "noc@x.test")]
    assert has._recipients("+966 50 123 4567,+966-50-1234567", "sms") == [
        ("+966501234567", "+966 50 123 4567")]
    assert has._unique_channels(["a", "b", "a", None, ""]) == ["a", "b"]
    # Errors that prove nothing was handed over may be retried; the rest are
    # uncertain and never re-sent.
    for exc in (ConnectionRefusedError(), smtplib.SMTPAuthenticationError(535, b"no"),
                smtplib.SMTPRecipientsRefused({}), dl.DeliveryRejected("HTTP 500"),
                ValueError("no recipient")):
        assert dl.outcome_for_exception(exc) == "failed", exc
    for exc in (TimeoutError(), smtplib.SMTPServerDisconnected(), RuntimeError("?")):
        assert dl.outcome_for_exception(exc) == "uncertain", exc


@pytest.mark.asyncio
async def test_a_send_interrupted_before_any_outcome_is_retried_once_stale(world):
    """A worker that died between claiming and sending leaves keys 'claimed'
    with no outcome. A fresh claim is still in flight and is left alone; one
    older than ten minutes was interrupted, and the page is sent rather than lost."""
    w = world
    alert_id = w.alert()
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1
    w.db.execute("UPDATE alert_delivery_keys SET state = 'claimed', finished_at = NULL WHERE alert_id = $1",
                 alert_id)
    w.mails.clear()
    await dispatch(w, [w.ch.noc], ctx(alert_id))
    assert w.mails == []                                   # in flight: not sent twice
    w.db.execute("UPDATE alert_delivery_keys SET claimed_at = now() - interval '11 minutes'"
                 " WHERE alert_id = $1", alert_id)
    assert await dispatch(w, [w.ch.noc], ctx(alert_id)) == 1
    assert sorted(w.mails) == ["noc@example.test", "ops@example.test"]
    assert {state for *_, state, _ in keys(w, alert_id)} == {"accepted"}


@pytest.mark.asyncio
async def test_an_interrupted_trigger_claim_can_be_taken_over_when_stale(world):
    w = world
    alert_id = w.alert()
    w.db.execute("UPDATE alerts SET metadata = jsonb_build_object('notified', 'sending',"
                 " 'sending_since', (now() - interval '1 minute')::text) WHERE id = $1", alert_id)
    async with session(w.db) as s:
        assert await ns.is_pending(s, alert_id) is False
        assert await ns.claim_pending(s, alert_id) is False
    w.db.execute("UPDATE alerts SET metadata = jsonb_build_object('notified', 'sending',"
                 " 'sending_since', to_char(now() - interval '11 minutes', 'YYYY-MM-DD\"T\"HH24:MI:SSOF'))"
                 " WHERE id = $1", alert_id)
    async with session(w.db) as s:
        assert await ns.is_pending(s, alert_id) is True
        assert await ns.claim_pending(s, alert_id) is True
        await s.commit()
    async with session(w.db) as s:
        assert await ns.claim_pending(s, alert_id) is False      # the new claim is fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_state", ["claimed", "accepted", "failed", "uncertain"])
async def test_a_stale_attempt_cannot_overwrite_its_replacement(world, replacement_state):
    w = world
    alert_id = w.alert()
    async with session(w.db) as s:
        old, _ = await dl.claim_key(s, alert_id, "trigger", w.ch.hook)
        await s.commit()
    logical_id = w.db.scalar("SELECT id FROM alert_delivery_keys WHERE alert_id=$1", alert_id)
    w.db.execute("UPDATE alert_delivery_keys SET claimed_at=now()-interval '11 minutes'"
                 " WHERE alert_id=$1", alert_id)
    async with session(w.db) as s:
        current, _ = await dl.claim_key(s, alert_id, "trigger", w.ch.hook)
        assert old and current and old != current
        if replacement_state != "claimed":
            await dl.finish_keys(s, [current], replacement_state, "replacement outcome")
        await s.commit()
    before = w.db.query("SELECT id,claim_token,state,error,finished_at FROM alert_delivery_keys"
                        " WHERE alert_id=$1", alert_id)
    async with session(w.db) as s:
        await dl.finish_keys(s, [old], "failed", "late old worker")
        await s.commit()
    assert w.db.query("SELECT id,claim_token,state,error,finished_at FROM alert_delivery_keys"
                      " WHERE alert_id=$1", alert_id) == before
    assert before[0][0] == logical_id
    assert before[0][2] == replacement_state


@pytest.mark.asyncio
async def test_failed_retry_rotates_token_and_terminal_completion_is_immutable(world):
    w = world
    alert_id = w.alert()
    async with session(w.db) as s:
        first, _ = await dl.claim_key(s, alert_id, "trigger", w.ch.hook)
        await dl.finish_keys(s, [first], "failed")
        await s.commit()
        second, _ = await dl.claim_key(s, alert_id, "trigger", w.ch.hook)
        assert first != second
        await s.commit()
        await dl.finish_keys(s, [first], "uncertain")
        await dl.finish_keys(s, [second], "accepted")
        await s.commit()
        accepted = w.db.query("SELECT state,finished_at FROM alert_delivery_keys WHERE alert_id=$1", alert_id)
        await dl.finish_keys(s, [second], "failed", "duplicate completion")
        await s.commit()
        assert w.db.query("SELECT state,finished_at FROM alert_delivery_keys WHERE alert_id=$1", alert_id) == accepted
        assert accepted[0][0] == "accepted"
        with pytest.raises(ValueError, match="Invalid delivery completion state"):
            await dl.finish_keys(s, [second], "claimed")


@pytest.mark.asyncio
async def test_paused_dispatch_cannot_replace_a_newer_provider_receipt(world, monkeypatch):
    w = world
    alert_id = w.alert()
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provider(kind, cfg, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()
            return False, ConnectionRefusedError("old attempt failed")
        return True, None

    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", provider)
    old = asyncio.create_task(dispatch(w, [w.ch.hook], ctx(alert_id)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        w.db.execute("UPDATE alert_delivery_keys SET claimed_at=now()-interval '11 minutes'"
                     " WHERE alert_id=$1", alert_id)
        assert await dispatch(w, [w.ch.hook], ctx(alert_id)) == 1
        resume.set()
        assert await asyncio.wait_for(old, 5) == 0
        assert {r[3] for r in keys(w, alert_id)} == {"accepted"}
        # This deliberately permits the existing stale retry policy. It does
        # not claim to fence either provider request or deliver exactly once.
        assert calls == 2
    finally:
        resume.set()
        if not old.done():
            old.cancel()
        await asyncio.gather(old, return_exceptions=True)


def test_attempt_migration_preserves_legacy_identity_outcomes_and_is_repeatable(world):
    from pathlib import Path

    w = world
    alert_id = w.alert()
    # Recreate the N-1 table shape inside this disposable database only.
    w.db.execute("ALTER TABLE alert_delivery_keys DROP COLUMN claim_token")
    for state in ("claimed", "accepted", "failed", "uncertain"):
        w.db.execute("INSERT INTO alert_delivery_keys(alert_id,event,channel_id,state,error)"
                     " VALUES($1,$2,$3,$2,'preserve')", alert_id, state, w.ch.hook)
    before = w.db.query("SELECT id,event,state,error,claimed_at,finished_at FROM alert_delivery_keys ORDER BY event")
    migration = Path(__file__).resolve().parents[2] / "scripts/migrate-170-notification-attempt-ownership.sql"
    w.db.psql_file(migration)
    tokens = w.db.query("SELECT claim_token FROM alert_delivery_keys ORDER BY event")
    assert len({t[0] for t in tokens}) == 4 and all(t[0] for t in tokens)
    assert w.db.query("SELECT id,event,state,error,claimed_at,finished_at FROM alert_delivery_keys ORDER BY event") == before
    w.db.psql_file(migration)
    assert w.db.query("SELECT claim_token FROM alert_delivery_keys ORDER BY event") == tokens
