"""Private PostgreSQL checks for the native trap and incident commit boundaries."""
import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import notification_outbox, trap_incident_inbox
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_alert_funnel import world  # noqa: F401
from test_service_transition_inbox import factory


def enqueue(db, source, oid, stamp):
    event = uuid4()
    db.execute("""
        WITH staged AS (
            INSERT INTO snmp_trap_incident_inbox(event_id,source_key,observed_at,payload)
            VALUES($1,$2,$3,$4::jsonb) RETURNING payload_bytes
        )
        UPDATE host_ingest_capacity SET trap_bytes=trap_bytes+staged.payload_bytes
        FROM staged WHERE singleton
    """, event, source, stamp, {"source_ip": source, "trap_oid": oid})
    return event


@pytest.mark.asyncio
async def test_rollback_preserves_source_order_and_cooldown(world, monkeypatch):
    w = world
    oid = "1.3.6.1.6.3.1.1.5.3"
    w.db.execute("""
        UPDATE alert_rules SET device_id=NULL,metric='trap',trap_oid=$1 WHERE id=$2
    """, oid, w.rule)
    stamp = datetime.now(timezone.utc)
    first = enqueue(w.db, "192.0.2.77", oid, stamp)
    enqueue(w.db, "192.0.2.77", oid, stamp)
    admitted = w.db.scalar("SELECT trap_bytes FROM host_ingest_capacity")
    original = notification_outbox.enqueue

    async def fail(*_):
        raise RuntimeError("injected notification enqueue failure")

    monkeypatch.setattr(notification_outbox, "enqueue", fail)
    async with factory(w) as sf:
        assert await trap_incident_inbox.consume_one(sf)
        assert w.db.scalar("SELECT count(*) FROM alerts") == 0
        assert w.db.scalar("SELECT trap_bytes FROM host_ingest_capacity") == admitted
        assert not await trap_incident_inbox.consume_one(sf)
        monkeypatch.setattr(notification_outbox, "enqueue", original)
        w.db.execute("UPDATE snmp_trap_incident_inbox SET due_at=clock_timestamp()")
        results = await asyncio.gather(*(trap_incident_inbox.consume_one(sf) for _ in range(2)))
        if results.count(True) == 1:
            assert await trap_incident_inbox.consume_one(sf)
        assert not await trap_incident_inbox.consume_one(sf)

    assert w.db.scalar("SELECT count(*) FROM alerts") == 2
    assert w.db.scalar("SELECT trap_bytes FROM host_ingest_capacity") == 0
    assert w.db.scalar("""
        SELECT a.metadata->>'trap_event_id' FROM alerts a
        JOIN notification_outbox n ON n.alert_id=a.id
    """) == str(first)
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 1
    assert not w.mails and not w.hooks


def test_native_producer_capacity_rollback_and_exact_retry(pg_db):
    go = shutil.which("go")
    if not go:
        pytest.skip("Go executable unavailable for cross-language fixture")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["ZENPLUS_TRAP_JOURNAL_TEST_DSN"] = pg_db.url_for("zenplus").replace("postgresql+asyncpg://", "postgres://")
    result = subprocess.run(
        [go, "test", "./internal/store", "-run", "^TestTrapJournalPrivatePostgres$", "-count=1"],
        cwd=Path(__file__).resolve().parents[2] / "poller",
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
