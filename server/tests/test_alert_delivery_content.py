"""What was sent (alerting plan 2.5, T-01, migrate-197).

alert_deliveries recorded who was sent which outcome but not what the message
said, so a complaint about a wrong SMS or email could not be checked. Each
accepted or failed delivery now keeps the subject, the text exactly as handed
to the transport (capped at 8,000 characters) and the recipient count — never
the addresses or phone numbers. The text is cleared after
system_settings['alert_delivery_content'].days; the row stays. The incident
detail shows the subject with alerts.view and the text only with alerts.manage.

Runs on a disposable PostgreSQL; only the transports are faked.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.services import alert_delivery_log as dl
from app.services import host_alert_service as has

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

MIGRATION = Path(__file__).resolve().parents[2] / "scripts" / "migrate-197-alert-delivery-content.sql"


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
    db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
               " VALUES ('sms', 'sms', $1::jsonb, true, true)",
               {"provider": "custom_http", "api_url": "https://sms.fixture.invalid/send"})

    def channel(name, kind, config):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config)"
                             " VALUES ($1, $2, $3::jsonb) RETURNING id", name, kind, config))

    ch = NS(
        mail=channel("noc mail", "email", {"recipients": "ops@example.test, noc@example.test"}),
        sms=channel("on-call sms", "sms", {"phone_numbers": "+966500000001, +966500000002"}),
        slack=channel("noc slack", "slack", {"url": "https://hooks.example.invalid/T000/secret"}),
    )
    server = db.scalar("INSERT INTO servers (display_name, hostname) VALUES ('srv', 'srv') RETURNING id")

    def alert():
        return str(db.scalar(
            "INSERT INTO alerts (server_id, severity, message, status, metadata)"
            " VALUES ($1, 'warning', 'Memory high', 'active', '{}'::jsonb) RETURNING id", server))

    mails: list[tuple] = []
    texts: list[tuple] = []

    async def send_email(gw, recipients, subject, body, html_body=None):
        mails.append((recipients, subject, body))
        return True

    async def send_sms(gw, phone, message, values=None):
        texts.append((phone, message))
        return True

    async def send_hook(kind, cfg, ctx):
        return True, None

    monkeypatch.setattr(alert_engine, "_send_email", send_email)
    monkeypatch.setattr(alert_engine, "_send_sms", send_sms)
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    return NS(db=db, ch=ch, alert=alert, mails=mails, texts=texts)


def ctx(alert_id, **extra):
    return {"subject": "[WARNING] ALERT: Memory high — srv", "body": "Memory on srv is above 90 %.",
            "message": "ZenPlus WARNING - Memory high on srv", "rule_name": "Memory high",
            "hostname": "srv", "severity": "warning", "status": "ALERT",
            "triggered_at": "2026-11-19T08:00:00+00:00",
            "alert_id": alert_id, "delivery_event": "trigger", **extra}


async def dispatch(w, channels, c):
    async with session(w.db) as s:
        n = await has.dispatch_to_channels(s, channels, c)
        await s.commit()                         # as the evaluators do after a dispatch
        return n


def deliveries(w, alert_id):
    return w.db.query("SELECT channel_type, outcome, subject, content, recipients_count"
                      " FROM alert_deliveries WHERE alert_id = $1 ORDER BY channel_type, attempted_at",
                      alert_id)


# ── Migration ───────────────────────────────────────────────────────────────

def test_migration_adds_nullable_columns_and_is_idempotent(pg_db):
    cols = dict(pg_db.query(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'alert_deliveries' AND column_name IN ('subject', 'content', 'recipients_count')"))
    assert cols == {"subject": "YES", "content": "YES", "recipients_count": "YES"}
    pg_db.psql_file(MIGRATION)               # replaying changes nothing
    pg_db.psql_file(MIGRATION)
    assert pg_db.scalar("SELECT count(*) FROM pg_indexes"
                        " WHERE indexname = 'idx_alert_deliveries_content_time'") == 1


# ── Recording ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_each_channel_records_what_it_handed_to_the_transport(world):
    w = world
    alert_id = w.alert()
    assert await dispatch(w, [w.ch.mail, w.ch.sms, w.ch.slack], ctx(alert_id)) == 3
    rows = deliveries(w, alert_id)
    by = {}
    for ctype, outcome, subject, content, count in rows:
        assert outcome == "accepted"
        by.setdefault(ctype, []).append((subject, content, count))

    # Email: the subject and plain-text body exactly as _send_email got them.
    [(rcpt, subject, body)] = w.mails
    assert by["email"] == [(subject, body, 2)]
    assert "srv" in body and "ops@example.test" not in body
    # SMS: one row per phone with the text as sent (timestamp included).
    assert len(w.texts) == 2 and w.texts[0][1] == w.texts[1][1]
    assert by["sms"] == [(None, w.texts[0][1], 1), (None, w.texts[0][1], 1)]
    assert w.texts[0][1].startswith("ZenPlus WARNING - Memory high on srv")
    # Chat: the payload's title (the event sentence) and facts, never the hook URL.
    [(slack_subject, slack_text, slack_count)] = by["slack"]
    assert slack_subject == ":warning: Memory on srv is above 90 %." and slack_count is None
    assert slack_text.startswith("Asset: srv\n") and "Rule: Memory high" in slack_text
    assert "Time: 2026-11-19 08:00" in slack_text

    # No address, phone number or channel secret is stored anywhere in the row.
    stored = " ".join(str(v) for r in w.db.query("SELECT * FROM alert_deliveries WHERE alert_id = $1",
                                                 alert_id) for v in r)
    for secret in ("ops@example.test", "noc@example.test", "966500000001", "T000/secret"):
        assert secret not in stored


@pytest.mark.asyncio
async def test_content_is_capped_and_skipped_sends_store_none(world):
    w = world
    alert_id = w.alert()
    huge = "x" * 20_000
    await dispatch(w, [w.ch.mail], ctx(alert_id, body=huge, html_body="<p>big</p>"))
    [(_, _, subject, content, _)] = deliveries(w, alert_id)
    assert len(content) == dl.CONTENT_MAX and content.endswith("…")
    assert w.mails[0][2] == huge                 # the transport still got everything

    # A disabled channel sends nothing, so nothing is recorded as sent.
    w.db.execute("UPDATE notification_channels SET enabled = false WHERE id = $1", w.ch.mail)
    other = w.alert()
    await dispatch(w, [w.ch.mail], ctx(other))
    assert deliveries(w, other) == [("email", "skipped", None, None, None)]


@pytest.mark.asyncio
async def test_failed_send_keeps_the_text_it_tried(world, monkeypatch):
    w = world
    alert_id = w.alert()

    async def broken(gw, phone, message, values=None):
        raise dl.DeliveryRejected("HTTP 500")

    monkeypatch.setattr(alert_engine, "_send_sms", broken)
    await dispatch(w, [w.ch.sms], ctx(alert_id))
    rows = deliveries(w, alert_id)
    assert {r[1] for r in rows} == {"failed"}
    assert all(r[3].startswith("ZenPlus WARNING - Memory high on srv") for r in rows)


@pytest.mark.asyncio
async def test_record_still_logs_the_outcome_before_the_migration(pg_db):
    dl.reset_for_tests()
    pg_db.execute("ALTER TABLE alert_deliveries DROP COLUMN subject, DROP COLUMN content,"
                  " DROP COLUMN recipients_count")
    async with session(pg_db) as s:
        await dl.record(s, channel_name="old", channel_type="email", outcome="accepted",
                        subject="s", content="c", recipients_count=1)
        await dl.record(s, channel_name="old2", channel_type="email", outcome="accepted", content="c")
        await s.commit()
    assert pg_db.scalar("SELECT count(*) FROM alert_deliveries WHERE channel_name LIKE 'old%'") == 2
    pg_db.psql_file(MIGRATION)
    dl.reset_for_tests()


class _FakeClient:
    posts: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        _FakeClient.posts.append(json)
        return NS(status_code=202)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["slack", "teams", "discord", "pagerduty", "webhook"])
@pytest.mark.parametrize("recovery", [False, True])
async def test_webhook_record_matches_the_payload_the_sender_builds(monkeypatch, kind, recovery):
    """webhook_sent mirrors alert_engine's payload builders; this pins them together."""
    _FakeClient.posts = []
    monkeypatch.setattr(alert_engine, "NotificationHTTPClient", _FakeClient)
    c = {"hostname": "core-sw1", "ip_address": "10.0.0.1",
         "status": "RESOLVED" if recovery else "DOWN", "severity": "critical",
         "event_sentence": ("Switch core-sw1 (10.0.0.1) came back up at 11:06: it answers ping again."
                            if recovery else
                            "Switch core-sw1 (10.0.0.1) went down at 11:00: it does not answer ping."),
         "message": "ZenPlus CRITICAL - Core down: core-sw1 stopped answering ping",
         "subject": "[CRITICAL] core-sw1 is down", "is_recovery": recovery,
         "triggered_at": "2026-11-19T08:00:00Z", "local_time": "2026-11-19 11:00 GMT+3",
         "rule_id": "r1", "rule_name": "Core down", "site": "Riyadh-HQ",
         "alert_id": "3f2a9c1d-0000-4000-8000-000000000001", "incident_ref": "INC-3f2a9c1d",
         "action_url": "https://zp.example/alerts", "active_for": "6 minutes",
         "recipients": [{"kind": "email", "address": "boss@example.test"}],
         "html_body": "<p>secret html</p>"}
    ok, err = await alert_engine._dispatch_channel_result(
        kind, {"url": "https://h.invalid/x", "routing_key": "rk"}, c)
    assert ok and err is None
    [payload] = _FakeClient.posts
    rec = dl.webhook_sent(kind, c)
    sentence = c["event_sentence"]
    link = "https://zp.example/alerts/3f2a9c1d-0000-4000-8000-000000000001"

    def lines(rows):
        return "\n".join(f"{a}: {b}" for a, b in rows) + f"\nLink: {link}"

    if kind == "slack":
        att = payload["attachments"][0]
        assert payload["text"].endswith(sentence) and att["title"] == sentence
        assert att["title_link"] == link
        rows = [(f["title"], f["value"]) for f in att["fields"]]
        assert (rec["subject"], rec["content"]) == (payload["text"], lines(rows))
    elif kind == "teams":
        content = payload["attachments"][0]["content"]
        body = content["body"]
        assert body[0]["text"] == sentence and content["actions"][0]["url"] == link
        rows = [(f["title"], f["value"]) for f in body[1]["facts"]]
        assert (rec["subject"], rec["content"]) == (body[0]["text"], lines(rows))
    elif kind == "discord":
        emb = payload["embeds"][0]
        assert emb["title"] == sentence and emb["url"] == link
        rows = [(f["name"], f["value"]) for f in emb["fields"]]
        assert (rec["subject"], rec["content"]) == (emb["title"], lines(rows))
    elif kind == "pagerduty":
        assert rec["subject"] is None and rec["content"] == payload["payload"]["summary"] == sentence
        assert payload["links"] == [{"href": link, "text": "View incident"}]
        assert "rk" not in str(rec)
    else:
        assert (rec["subject"], rec["content"]) == (payload["subject"], payload["message"])
    if kind != "pagerduty":
        facts = dict(rows) if kind != "webhook" else {}
        if facts:
            # Asset, site, local time, rule and incident; the duration only on a recovery.
            assert facts["Asset"] == "core-sw1 (10.0.0.1)" and facts["Site"] == "Riyadh-HQ"
            assert facts["Time"] == "2026-11-19 11:00 GMT+3" and facts["Incident"] == "INC-3f2a9c1d"
            assert facts.get("Duration") == ("6 minutes" if recovery else None)
    # Nothing that carries recipient addresses or the HTML mail leaves the appliance.
    posted = str(payload)
    assert "boss@example.test" not in posted and "secret html" not in posted


# ── Retention ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_clears_old_text_and_keeps_the_rows(world):
    w = world
    alert_id = w.alert()
    now = datetime.now(timezone.utc)
    for days_ago in (40, 10, 0):
        w.db.execute("INSERT INTO alert_deliveries (alert_id, channel_type, outcome, subject, content,"
                     " recipients_count, attempted_at) VALUES ($1, 'email', 'accepted', 's', 'c', 2, $2)",
                     alert_id, now - timedelta(days=days_ago, minutes=1))
    async with session(w.db) as s:
        assert await dl.content_settings(s) == {"days": 30}
        assert await dl.purge_content(s, now) == 1
    kept = w.db.query("SELECT subject IS NULL, content IS NULL, recipients_count FROM alert_deliveries"
                      " WHERE alert_id = $1 ORDER BY attempted_at", alert_id)
    assert kept == [(True, True, 2), (False, False, 2), (False, False, 2)]

    async with session(w.db) as s:
        assert await dl.save_content_settings(s, 7) == {"days": 7}
        await s.commit()
        assert await dl.purge_content(s, now) == 1
        assert await dl.purge_content(s, now) == 0
        # Out-of-range stored values fall back into 1..365.
        await dl.save_content_settings(s, 9999)
        await s.commit()
        assert await dl.content_settings(s) == {"days": 365}
    assert w.db.scalar("SELECT count(*) FROM alert_deliveries WHERE alert_id = $1", alert_id) == 3


@pytest.mark.asyncio
async def test_retention_tick_runs_at_most_hourly(world):
    w = world

    def factory():
        return session(w.db)

    assert await dl.content_retention_tick(factory) == 0
    assert await dl.content_retention_tick(factory) is None     # not due again yet
    dl._last_content_sweep -= dl.CONTENT_SWEEP_INTERVAL_S + 1
    assert await dl.content_retention_tick(factory) == 0


# ── API ─────────────────────────────────────────────────────────────────────

def manager(db):
    return api_client(db, role="t_dc_manager", permissions=["alerts.view", "alerts.manage"],
                      username="dc-manager", persist_user=True)


def viewer(db):
    return api_client(db, role="t_dc_viewer", permissions=["alerts.view"], username="dc-viewer",
                      persist_user=True)


@pytest.mark.asyncio
async def test_detail_shows_subject_to_viewers_and_text_only_to_managers(world):
    w = world
    alert_id = w.alert()
    await dispatch(w, [w.ch.mail, w.ch.sms], ctx(alert_id))
    with manager(w.db) as c:
        r = c.get(f"/api/v1/alerts/{alert_id}")
        assert r.status_code == 200, r.text
        ds = r.json()["deliveries"]
        mail = next(d for d in ds if d["channel_type"] == "email")
        assert mail["subject"] == w.mails[0][1] and mail["content"] == w.mails[0][2]
        assert mail["recipients_count"] == 2 and mail["content_withheld"] is False
        assert all(d["content"] == w.texts[0][1] for d in ds if d["channel_type"] == "sms")
    with viewer(w.db) as c:
        ds = c.get(f"/api/v1/alerts/{alert_id}").json()["deliveries"]
        mail = next(d for d in ds if d["channel_type"] == "email")
        assert mail["subject"] == w.mails[0][1] and mail["recipients_count"] == 2
        assert all(d["content"] is None and d["content_withheld"] is True for d in ds)


def test_delivery_content_settings_api(world):
    db = world.db
    with viewer(db) as c:
        assert c.get("/api/v1/alerts/delivery-content").json() == {"days": 30}
        assert c.put("/api/v1/alerts/delivery-content", json={"days": 10}).status_code == 403
    with manager(db) as c:
        assert c.put("/api/v1/alerts/delivery-content", json={"days": 0}).status_code == 422
        assert c.put("/api/v1/alerts/delivery-content", json={"days": 366}).status_code == 422
        r = c.put("/api/v1/alerts/delivery-content", json={"days": 14})
        assert r.status_code == 200 and r.json() == {"days": 14}
        assert c.get("/api/v1/alerts/delivery-content").json() == {"days": 14}
    [(meta,)] = db.query("SELECT metadata FROM audit_logs WHERE action = 'alert.delivery_content.update'")
    assert meta["before"] == {"days": 30} and meta["after"] == {"days": 14}
