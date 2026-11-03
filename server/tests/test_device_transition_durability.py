"""Exercise the durable central and sensor handoff against disposable PostgreSQL."""
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import device_map_webhook as map_work, sensor_health_service as sensors
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_alert_funnel import session, world  # noqa: F401
from test_service_transition_inbox import factory


@pytest.mark.parametrize("fail", [False, True])
def test_go_state_and_alert_commit_together(pg_db, fail):
    go = shutil.which("go")
    if not go:
        pytest.skip("Go unavailable")
    device = pg_db.scalar("""
        INSERT INTO devices(hostname,ip_address,status,ping_enabled)
        VALUES('fixture','192.0.2.97','up',true) RETURNING id
    """)
    assert pg_db.scalar("SELECT owner_kind FROM device_polling_owner WHERE device_id=$1", device) == "central"
    pg_db.psql("""
        GRANT SELECT,UPDATE ON devices TO zenplus;
        GRANT SELECT ON device_polling_owner TO zenplus;
        GRANT SELECT,INSERT,UPDATE ON device_status_state,device_flap_episodes TO zenplus;
    """)
    if fail:
        pg_db.psql("""
            CREATE FUNCTION reject_transition() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture rejection'; END $$;
            CREATE TRIGGER reject_transition BEFORE INSERT ON sensor_transition_outbox
            FOR EACH ROW EXECUTE FUNCTION reject_transition();
        """)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(ZENPLUS_DEVICE_TRANSITION_TEST_DSN=pg_db.url_for("zenplus").replace("postgresql+asyncpg://", "postgres://"),
               ZENPLUS_DEVICE_TRANSITION_TEST_ID=str(device),
               ZENPLUS_DEVICE_TRANSITION_EXPECT_FAILURE=str(int(fail)))
    result = subprocess.run(
        [go, "test", "./internal/store", "-run", "^TestDurableDeviceTransitionPrivatePostgres$", "-count=1"],
        cwd=Path(__file__).resolve().parents[2] / "poller", env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert pg_db.scalar("SELECT status FROM devices WHERE id=$1", device) == ("up" if fail else "down")
    assert pg_db.scalar("SELECT count(*) FROM sensor_transition_outbox") == (0 if fail else 1)


@pytest.mark.asyncio
async def test_failed_map_enqueue_rolls_back_and_recovery_cannot_overtake(world, monkeypatch):
    monkeypatch.setenv("DEVICE_TRANSITION_INBOX_ENABLED", "1")
    w = world
    mid = w.db.scalar("""INSERT INTO manual_maps(name,webhook_enabled,webhook_url)
                         VALUES('test',true,'https://map.example.invalid/hook') RETURNING id""")
    w.db.execute("INSERT INTO manual_map_nodes(map_id,device_id) VALUES($1,$2)", mid, w.device)
    observed = datetime.now(timezone.utc).isoformat()
    for ordinal, (old, new) in enumerate([("up", "down"), ("down", "up")]):
        w.db.execute("""
            INSERT INTO sensor_transition_outbox(endpoint,idempotency_key,transition_type,entity_id,payload)
            VALUES('central',$1,'device',$2,$3::jsonb)
        """, str(ordinal), w.device, {
            "device_id": w.device, "hostname": "edge", "ip_address": "10.88.0.1",
            "old_status": old, "new_status": new, "timestamp": observed,
        })
    original = map_work.enqueue

    async def fail(*_):
        raise RuntimeError("fixture")

    monkeypatch.setattr(map_work, "enqueue", fail)
    async with session(w.db) as db:
        assert await sensors.dispatch_sensor_transition_outbox(db) == {"processed": 0, "failed": 1}
    assert w.db.scalar("SELECT count(*) FROM alerts") == 0
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 0
    assert w.db.scalar("SELECT count(*) FROM sensor_transition_outbox WHERE processed_at IS NOT NULL") == 0

    monkeypatch.setattr(map_work, "enqueue", original)
    w.db.execute("UPDATE sensor_transition_outbox SET next_attempt_at=clock_timestamp()")
    async with session(w.db) as db:
        assert await sensors.dispatch_sensor_transition_outbox(db) == {"processed": 2, "failed": 0}
    assert w.db.scalar("SELECT count(*) FROM alerts WHERE status='active'") == 0
    assert w.db.scalar("SELECT count(*) FROM notification_outbox") == 2
    assert [r[0] for r in w.db.query("SELECT payload->>'new_status' FROM device_map_webhook_work ORDER BY id")] == ["down", "up"]
    assert not w.mails and not w.hooks

    posts = []

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, json, headers):
            posts.append((url, json["new_status"], headers["Idempotency-Key"]))
            if json["new_status"] == "down":
                raise ConnectionError("unknown provider outcome")
            return SimpleNamespace(status_code=202)

    monkeypatch.setattr(map_work, "NotificationHTTPClient", Client)
    async with factory(w) as sf:
        assert await map_work.deliver_one(sf, lambda: True)
        assert await map_work.deliver_one(sf, lambda: True)
        assert not await map_work.deliver_one(sf, lambda: True)
    assert [r[0] for r in w.db.query("SELECT status FROM device_map_webhook_work ORDER BY id")] == ["uncertain", "accepted"]
    assert [post[1] for post in posts] == ["down", "up"]
